"""
tests/test_admin.py – Unit tests for the admin HTTPS interface.

Covers:
  - Login page (GET /admin/login)
  - Login form submission (POST /admin/login)
  - Logout (GET /admin/logout)
  - Auth: session cookie, Basic Auth, unauthenticated browser, unauthenticated API
  - Admin index (GET /admin/)
  - Admin health (GET /admin/health)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import state

# ---------------------------------------------------------------------------
# Test credentials (must match what patched_state sets, overridden below)
# ---------------------------------------------------------------------------

TEST_ADMIN_USER = "testadmin"
TEST_ADMIN_PASS = "testpassword"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_client(patched_state) -> AsyncClient:
    """AsyncClient for the admin FastAPI app with auth enabled.

    patched_state already initialises the DB and core state.
    We override _admin_config to turn admin on and use known test credentials.
    """
    state._admin_config = {
        "enabled":    True,
        "username":   TEST_ADMIN_USER,
        "password":   TEST_ADMIN_PASS,
        "port_https": 8091,
        "tls_mode":   "self_signed",
    }
    from admin.app import admin_app
    async with AsyncClient(
        transport=ASGITransport(app=admin_app),
        base_url="https://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


async def test_login_page_returns_html_form(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/login")
    assert resp.status_code == 200
    assert "<form" in resp.text
    assert 'name="username"' in resp.text
    assert 'name="password"' in resp.text


async def test_login_page_no_error_block_by_default(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/login")
    assert "Invalid username" not in resp.text


async def test_login_page_shows_error_when_flag_set(admin_client: AsyncClient):
    resp = await admin_client.get("/admin/login?error=1")
    assert resp.status_code == 200
    assert "Invalid username" in resp.text


# ---------------------------------------------------------------------------
# Login form submission
# ---------------------------------------------------------------------------


async def test_login_correct_creds_sets_cookie_and_redirects(admin_client: AsyncClient):
    resp = await admin_client.post(
        "/admin/login",
        data={"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASS},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/admin/sessions" in resp.headers["location"]
    assert "ev_admin_session" in resp.cookies


async def test_login_wrong_creds_redirects_with_error(admin_client: AsyncClient):
    resp = await admin_client.post(
        "/admin/login",
        data={"username": "wrong", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=1" in resp.headers["location"]
    assert "ev_admin_session" not in resp.cookies


async def test_login_correct_creds_cookie_grants_access(admin_client: AsyncClient):
    """After POST /admin/login, httpx stores the cookie and subsequent requests succeed."""
    await admin_client.post(
        "/admin/login",
        data={"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASS},
        follow_redirects=False,
    )
    resp = await admin_client.get("/admin/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


async def test_logout_clears_cookie_and_redirects(admin_client: AsyncClient):
    # First log in
    await admin_client.post(
        "/admin/login",
        data={"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASS},
        follow_redirects=False,
    )
    assert "ev_admin_session" in admin_client.cookies

    # Log out
    resp = await admin_client.get("/admin/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["location"]
    # Cookie should be cleared
    assert "ev_admin_session" not in admin_client.cookies


# ---------------------------------------------------------------------------
# Session token auth
# ---------------------------------------------------------------------------


async def test_health_with_valid_session_token_returns_200(admin_client: AsyncClient):
    from admin.auth import SESSION_COOKIE, make_session_token
    token = make_session_token(TEST_ADMIN_USER)
    admin_client.cookies.set(SESSION_COOKIE, token)
    resp = await admin_client.get("/admin/health")
    assert resp.status_code == 200


async def test_health_with_invalid_session_token_redirects_browser(admin_client: AsyncClient):
    from admin.auth import SESSION_COOKIE
    admin_client.cookies.set(SESSION_COOKIE, "bad.token.value")
    resp = await admin_client.get(
        "/admin/health",
        headers={"accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Basic Auth fallback
# ---------------------------------------------------------------------------


async def test_health_with_basic_auth_returns_200(admin_client: AsyncClient):
    resp = await admin_client.get(
        "/admin/health",
        auth=(TEST_ADMIN_USER, TEST_ADMIN_PASS),
    )
    assert resp.status_code == 200


async def test_health_with_wrong_basic_auth_returns_401(admin_client: AsyncClient):
    resp = await admin_client.get(
        "/admin/health",
        auth=("wrong", "credentials"),
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Unauthenticated requests
# ---------------------------------------------------------------------------


async def test_health_no_auth_browser_redirects_to_login(admin_client: AsyncClient):
    """Browser clients (Accept: text/html) should be redirected to /admin/login."""
    resp = await admin_client.get(
        "/admin/health",
        headers={"accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["location"]


async def test_health_no_auth_api_client_returns_401(admin_client: AsyncClient):
    """API clients (no text/html Accept) should receive 401."""
    resp = await admin_client.get(
        "/admin/health",
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin index dashboard
# ---------------------------------------------------------------------------


async def test_admin_index_with_valid_session_returns_html(admin_client: AsyncClient):
    from admin.auth import SESSION_COOKIE, make_session_token
    token = make_session_token(TEST_ADMIN_USER)
    admin_client.cookies.set(SESSION_COOKIE, token)
    resp = await admin_client.get("/admin/")
    assert resp.status_code == 200
    assert "Sign out" in resp.text
