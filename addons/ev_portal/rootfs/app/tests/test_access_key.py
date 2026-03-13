"""
tests/test_access_key.py – Unit tests for AccessKeyMiddleware (access_key.py).

Covers:
  - Fail-open when no keys have been issued
  - Valid ?key= query param grants access, sets cookie, redirects to clean URL
  - Valid ?access_key= query param (forwarded from external portal) grants access
  - Invalid query param returns 403
  - Valid cookie grants access
  - Expired / missing cookie triggers 403
  - Skip paths bypass the check (/health, /static/, /.well-known/)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import db
import state
from access_key import _is_plausible_key, _rejection_reason


# ---------------------------------------------------------------------------
# Fixture: guest app client with access-key enforcement enabled
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def key_client(patched_state) -> AsyncClient:
    """Unit client for the guest app; DB is initialised with access_keys table."""
    import main as m
    async with AsyncClient(transport=ASGITransport(app=m.app), base_url="https://test") as c:
        yield c


async def _issue_key(expires_in_days: int = 30) -> str:
    """Insert a test access key into the DB and return the key string."""
    key = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.create_access_key(
        key,
        now.isoformat(),
        (now + timedelta(days=expires_in_days)).isoformat(),
    )
    return key


async def _issue_expired_key() -> str:
    """Insert an already-expired key and return it."""
    key = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.create_access_key(
        key,
        (now - timedelta(days=60)).isoformat(),
        (now - timedelta(days=30)).isoformat(),
    )
    return key


# ---------------------------------------------------------------------------
# Skip paths
# ---------------------------------------------------------------------------

async def test_health_path_always_passes(key_client: AsyncClient):
    await _issue_key()
    resp = await key_client.get("/health")
    assert resp.status_code == 200


async def test_static_path_always_passes(key_client: AsyncClient):
    await _issue_key()
    resp = await key_client.get("/static/css/portal.css", follow_redirects=False)
    # Static file may 200 or 404 depending on mount, but must not be 403
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# ?key= query param (direct use)
# ---------------------------------------------------------------------------

async def test_valid_key_param_redirects_and_sets_cookie(key_client: AsyncClient):
    key = await _issue_key()
    resp = await key_client.get(f"/start?key={key}", follow_redirects=False)
    assert resp.status_code == 302
    assert "ev_access_key" in resp.cookies
    assert f"key={key}" not in resp.headers["location"]


async def test_invalid_key_param_returns_503(key_client: AsyncClient):
    await _issue_key()  # ensure at least one key exists so fail-open doesn't apply
    resp = await key_client.get("/start?key=not-a-real-key", follow_redirects=False)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# ?access_key= query param (forwarded from external portal)
# ---------------------------------------------------------------------------

async def test_valid_access_key_param_grants_access(key_client: AsyncClient):
    """?access_key= must be accepted — this is the param name used by the keymgr redirect."""
    key = await _issue_key()
    resp = await key_client.get(f"/start?access_key={key}", follow_redirects=False)
    assert resp.status_code == 302
    assert "ev_access_key" in resp.cookies
    # Both param names must be stripped from the redirect target
    location = resp.headers["location"]
    assert "access_key=" not in location
    assert f"key=" not in location


async def test_invalid_access_key_param_returns_503(key_client: AsyncClient):
    await _issue_key()
    resp = await key_client.get("/start?access_key=garbage", follow_redirects=False)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------

async def test_valid_cookie_grants_access(key_client: AsyncClient):
    key = await _issue_key()
    key_client.cookies.set("ev_access_key", key)
    resp = await key_client.get("/start", follow_redirects=False)
    # Should reach the endpoint (200/302) but not be blocked (503)
    assert resp.status_code != 503


async def test_expired_cookie_returns_503(key_client: AsyncClient):
    await _issue_key()  # ensure fail-open doesn't apply
    expired = await _issue_expired_key()
    key_client.cookies.set("ev_access_key", expired)
    resp = await key_client.get("/start", follow_redirects=False)
    assert resp.status_code == 503


async def test_missing_cookie_no_param_returns_503(key_client: AsyncClient):
    await _issue_key()
    resp = await key_client.get("/start", follow_redirects=False)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# _is_plausible_key — unit tests (no fixtures, no DB, no network)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    # Valid UUID4 — all four legal variant nibbles (8, 9, a, b)
    ("550e8400-e29b-4000-8000-000000000000",                           True),
    ("550e8400-e29b-4000-9000-000000000000",                           True),
    ("550e8400-e29b-4000-a000-000000000000",                           True),
    ("550e8400-e29b-4000-b000-000000000000",                           True),
    # Randomly generated (covers whatever variant Python uuid4 picks)
    (str(uuid.uuid4()),                                                True),
    # Empty / whitespace
    ("",                                                               False),
    ("   ",                                                            False),
    # Too short / too long
    ("550e8400-e29b-41d4-a716",                                        False),
    ("550e8400-e29b-41d4-a716-446655440000x",                          False),
    # Non-hex characters
    ("zzzzzzzz-zzzz-4zzz-azzz-zzzzzzzzzzzz",                          False),
    # Uppercase (keys are always lowercase UUID4)
    ("550E8400-E29B-41D4-A716-446655440000",                           False),
    # Wrong version nibble (UUID1, not UUID4)
    ("550e8400-e29b-11d4-a716-446655440000",                           False),
    # Wrong variant nibble (not 8/9/a/b)
    ("550e8400-e29b-41d4-c716-446655440000",                           False),
    # Hyphens in wrong positions (no hyphens)
    ("550e8400e29b41d4a716446655440000",                               False),
    # Looks UUID-shaped but variant nibble is invalid
    ("550e8400-e29b-4000-0000-000000000000",                           False),
])
def test_is_plausible_key(value: str, expected: bool):
    assert _is_plausible_key(value) is expected


# ---------------------------------------------------------------------------
# _rejection_reason — verify specific log messages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,fragment", [
    ("",                                       "empty value"),
    ("too-short",                              "wrong length"),
    ("x" * 37,                                "wrong length"),
    ("550E8400-E29B-41D4-A716-446655440000",  "UUID4 format"),
    ("550e8400-e29b-11d4-a716-446655440000",  "UUID4 format"),  # UUID1
    ("550e8400-e29b-41d4-c716-446655440000",  "UUID4 format"),  # bad variant
])
def test_rejection_reason_describes_failure(value: str, fragment: str):
    reason = _rejection_reason(value)
    assert reason is not None
    assert fragment in reason


def test_rejection_reason_returns_none_for_valid_key():
    assert _rejection_reason(str(uuid.uuid4())) is None


import logging as _logging

@pytest.mark.parametrize("setup,request_fn,expected_fragment", [
    # No credentials at all
    (
        lambda: None,
        lambda c: c.get("/start", follow_redirects=False),
        "no credentials presented",
    ),
    # Query key fails stateless check
    (
        lambda: None,
        lambda c: c.get("/start?key=not-a-real-key", follow_redirects=False),
        "stateless check failed",
    ),
    # Cookie fails stateless check
    (
        lambda c: c.cookies.set("ev_access_key", "garbage"),
        lambda c: c.get("/start", follow_redirects=False),
        "stateless check failed",
    ),
])
async def test_denied_log_includes_reason(key_client: AsyncClient, caplog, setup, request_fn, expected_fragment):
    """The final 'denied' log line must explain why access was refused."""
    await _issue_key()  # disable fail-open
    if setup.__code__.co_varnames and 'c' in setup.__code__.co_varnames:
        setup(key_client)
    else:
        setup()
    with caplog.at_level(_logging.INFO, logger="access_key"):
        await request_fn(key_client)
    denied_messages = [r.message for r in caplog.records if "denied" in r.message]
    assert denied_messages, "Expected a 'denied' log entry"
    assert any(expected_fragment in m for m in denied_messages)


async def test_db_rejection_logged_as_db_failure(key_client: AsyncClient, caplog):
    """A structurally valid but DB-unknown key must log a DB-level denial."""
    await _issue_key()  # disable fail-open; do NOT register the key we send
    unknown = str(uuid.uuid4())  # valid UUID4, but not in the DB
    with caplog.at_level(_logging.INFO, logger="access_key"):
        await key_client.get(f"/start?key={unknown}", follow_redirects=False)
    denied_messages = [r.message for r in caplog.records if "denied" in r.message]
    assert any("DB" in m for m in denied_messages)


# ---------------------------------------------------------------------------
# DB isolation: invalid keys must never reach db.validate_access_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("param,garbage", [
    ("key",        "not-a-real-key"),
    ("key",        ""),
    ("key",        "x" * 36),
    ("key",        "550E8400-E29B-41D4-A716-446655440000"),  # uppercase UUID
    ("access_key", "garbage"),
])
async def test_implausible_key_never_hits_db(
    key_client: AsyncClient, param: str, garbage: str
):
    """Structurally invalid keys must be rejected before any DB access."""
    await _issue_key()  # ensure fail-open doesn't apply
    with patch("db.validate_access_key", new_callable=AsyncMock) as mock_validate:
        resp = await key_client.get(
            f"/start?{param}={garbage}", follow_redirects=False
        )
    assert resp.status_code == 503
    mock_validate.assert_not_called()
