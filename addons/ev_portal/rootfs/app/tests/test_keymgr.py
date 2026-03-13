"""
tests/test_keymgr.py

Functional coverage for the key-management server (keymgr/app.py + keymgr/router.py).

Routes tested:
  GET  /        – get-or-create a key, redirect to portal URL
  GET  /keygen  – key management UI (empty and with ?issued=<key>)
  POST /keygen/issue  – issue a new key and redirect to /keygen?issued=...

Also covers:
  _get_or_create_key  – reuses an existing valid key rather than creating a new one
  _build_key_block    – key display block rendered in /keygen?issued
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import db
import keymgr.router as kmr


# ---------------------------------------------------------------------------
# Fixture: keymgr test client
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def keymgr_client(tmp_db: str) -> AsyncClient:
    """
    AsyncClient for the keymgr FastAPI app.

    Uses ASGITransport (lifespan NOT triggered); the DB has already been
    initialised by the ``tmp_db`` fixture so there's nothing else to set up.
    PORTAL_HOST is set to a test domain so redirect targets can be checked.
    """
    kmr.PORTAL_HOST = "portal.example.com"
    from keymgr.app import keymgr_app
    async with AsyncClient(
        transport=ASGITransport(app=keymgr_app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /  –  get-or-create redirect
# ---------------------------------------------------------------------------

async def test_index_redirects_to_portal_url_with_key(keymgr_client: AsyncClient):
    """GET / should redirect to https://<PORTAL_HOST>?access_key=<uuid>."""
    resp = await keymgr_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://portal.example.com")
    assert "access_key=" in loc


async def test_index_reuses_existing_valid_key(keymgr_client: AsyncClient):
    """When a valid key already exists in the DB, GET / reuses it instead of creating one."""
    now = datetime.now(timezone.utc)
    existing_key = str(uuid.uuid4())
    await db.create_access_key(
        existing_key,
        now.isoformat(),
        (now + timedelta(days=30)).isoformat(),
    )
    resp = await keymgr_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert existing_key in resp.headers["location"]


async def test_index_creates_new_key_when_none_exists(keymgr_client: AsyncClient):
    """GET / with no existing keys creates one in the DB."""
    # Sanity: no keys in DB yet
    row = await db.get_valid_access_key()
    assert row is None

    resp = await keymgr_client.get("/", follow_redirects=False)
    assert resp.status_code == 302

    # A key must now exist in the DB
    row = await db.get_valid_access_key()
    assert row is not None
    assert row["key"] in resp.headers["location"]


# ---------------------------------------------------------------------------
# GET /keygen  –  management UI
# ---------------------------------------------------------------------------

async def test_keygen_returns_issue_form_html(keymgr_client: AsyncClient):
    """GET /keygen shows the key management page with an Issue button."""
    resp = await keymgr_client.get("/keygen")
    assert resp.status_code == 200
    assert "Issue New Key" in resp.text
    assert "<form" in resp.text


async def test_keygen_with_issued_param_shows_key_block(keymgr_client: AsyncClient):
    """GET /keygen?issued=<valid-key> displays the key and its expiry."""
    now = datetime.now(timezone.utc)
    key = str(uuid.uuid4())
    await db.create_access_key(
        key,
        now.isoformat(),
        (now + timedelta(days=30)).isoformat(),
    )
    resp = await keymgr_client.get(f"/keygen?issued={key}")
    assert resp.status_code == 200
    assert key in resp.text
    # The portal URL with the key embedded should appear
    assert f"?key={key}" in resp.text


async def test_keygen_with_unknown_issued_param_shows_form_only(keymgr_client: AsyncClient):
    """GET /keygen?issued=<unknown> silently shows no key block (unknown key)."""
    unknown_key = str(uuid.uuid4())
    resp = await keymgr_client.get(f"/keygen?issued={unknown_key}")
    assert resp.status_code == 200
    # The key VALUE itself should not appear (no key block was rendered)
    assert unknown_key not in resp.text


# ---------------------------------------------------------------------------
# POST /keygen/issue  –  issue a new key
# ---------------------------------------------------------------------------

async def test_issue_creates_key_in_db_and_redirects(keymgr_client: AsyncClient):
    """POST /keygen/issue creates a 30-day key and redirects to /keygen?issued=<key>."""
    resp = await keymgr_client.post("/keygen/issue", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/keygen?issued=")

    issued_key = loc.split("issued=", 1)[1]
    # Key must be in the DB
    row = await db.get_access_key(issued_key)
    assert row is not None
    assert row["key"] == issued_key


async def test_issue_key_is_valid_uuid4(keymgr_client: AsyncClient):
    """The issued key must be a canonical UUID4 string."""
    import re
    UUID4_RE = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    )
    resp = await keymgr_client.post("/keygen/issue", follow_redirects=False)
    issued_key = resp.headers["location"].split("issued=", 1)[1]
    assert UUID4_RE.match(issued_key), f"Issued key is not a valid UUID4: {issued_key!r}"


async def test_issue_key_expires_in_30_days(keymgr_client: AsyncClient):
    """The issued key's expiry should be approximately 30 days from now."""
    resp = await keymgr_client.post("/keygen/issue", follow_redirects=False)
    issued_key = resp.headers["location"].split("issued=", 1)[1]
    row = await db.get_access_key(issued_key)
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    delta = expires_at - datetime.now(timezone.utc)
    # Should be between 29 and 31 days
    assert timedelta(days=29) < delta < timedelta(days=31)


async def test_issue_then_redirect_shows_key_block(keymgr_client: AsyncClient):
    """Full flow: POST /keygen/issue → follow redirect → page shows the key."""
    resp = await keymgr_client.post("/keygen/issue", follow_redirects=True)
    assert resp.status_code == 200
    assert "Access Key" in resp.text


# ---------------------------------------------------------------------------
# _build_key_block helper
# ---------------------------------------------------------------------------

def test_build_key_block_contains_key_and_expiry():
    """_build_key_block renders the key value and formats the expiry date."""
    key = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=30)).isoformat()
    block = kmr._build_key_block(key, expires_at)
    assert key in block
    assert "Valid until" in block


def test_build_key_block_with_invalid_expiry_falls_back_gracefully():
    """If expires_at is not a valid ISO string, it's shown as-is."""
    key = str(uuid.uuid4())
    block = kmr._build_key_block(key, "NOT_A_DATE")
    assert key in block
    assert "NOT_A_DATE" in block
