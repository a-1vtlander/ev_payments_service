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

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import db
import state


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
# Fail-open: no keys issued yet
# ---------------------------------------------------------------------------

async def test_no_keys_issued_allows_all_requests(key_client: AsyncClient):
    """When no keys have been issued the middleware must not block any request."""
    resp = await key_client.get("/health")
    assert resp.status_code == 200


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


async def test_invalid_key_param_returns_403(key_client: AsyncClient):
    await _issue_key()  # ensure at least one key exists so fail-open doesn't apply
    resp = await key_client.get("/start?key=not-a-real-key", follow_redirects=False)
    assert resp.status_code == 403


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


async def test_invalid_access_key_param_returns_403(key_client: AsyncClient):
    await _issue_key()
    resp = await key_client.get("/start?access_key=garbage", follow_redirects=False)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------

async def test_valid_cookie_grants_access(key_client: AsyncClient):
    key = await _issue_key()
    key_client.cookies.set("ev_access_key", key)
    resp = await key_client.get("/start", follow_redirects=False)
    # Should reach the endpoint (200/302/503) but not be blocked (403)
    assert resp.status_code != 403


async def test_expired_cookie_returns_403(key_client: AsyncClient):
    await _issue_key()  # ensure fail-open doesn't apply
    expired = await _issue_expired_key()
    key_client.cookies.set("ev_access_key", expired)
    resp = await key_client.get("/start", follow_redirects=False)
    assert resp.status_code == 403


async def test_missing_cookie_no_param_returns_403(key_client: AsyncClient):
    await _issue_key()
    resp = await key_client.get("/start", follow_redirects=False)
    assert resp.status_code == 403
