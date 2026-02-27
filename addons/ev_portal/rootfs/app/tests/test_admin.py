"""
tests/test_admin.py – Unit tests for the admin HTTPS interface.

Covers:
  - Login page (GET /admin/login)
  - Login form submission (POST /admin/login)
  - Logout (GET /admin/logout)
  - Auth: session cookie, Basic Auth, unauthenticated browser, unauthenticated API
  - Admin index (GET /admin/)
  - Admin health (GET /admin/health)
  - Retry charge (POST /admin/sessions/{ik}/retry)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

import db
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
    assert "Admin Dashboard" in resp.text
    assert "All Sessions" in resp.text


# ---------------------------------------------------------------------------
# Retry charge (POST /admin/sessions/{ik}/retry)
# ---------------------------------------------------------------------------

from tests.conftest import TEST_BOOKING_ID, TEST_CHARGER_ID, TEST_SESSION_ID

_RETRY_IK = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"

_FAILED_SESSION = {
    "idempotency_key":         _RETRY_IK,
    "charger_id":              TEST_CHARGER_ID,
    "booking_id":              TEST_BOOKING_ID,
    "session_id":              TEST_SESSION_ID,
    "state":                   "FAILED",
    "square_environment":      "sandbox",
    "square_payment_id":       "pay_preauth",
    "square_card_id":          "card_test",
    "square_customer_id":      "cust_test",
    "authorized_amount_cents": 5000,
}


def _auth_cookie(admin_client: AsyncClient) -> None:
    from admin.auth import SESSION_COOKIE, make_session_token
    admin_client.cookies.set(SESSION_COOKIE, make_session_token(TEST_ADMIN_USER))


async def test_retry_success_marks_captured(admin_client: AsyncClient) -> None:
    """Successful retry: direct charge fires and DB state moves to CAPTURED."""
    _auth_cookie(admin_client)
    await db.upsert_session(_FAILED_SESSION)
    await db.mark_failed(_RETRY_IK, "previous error")

    charge_result = {"id": "pay_retry", "amount_money": {"amount": 4500, "currency": "USD"}}
    with patch("square.charge_card_payment", new=AsyncMock(return_value=charge_result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_RETRY_IK}/retry",
            json={"amount_cents": 4500},
            follow_redirects=False,
        )

    assert resp.status_code in (200, 303)
    row = await db.get_session(_RETRY_IK)
    assert row["state"] == "CAPTURED"
    assert row["captured_amount_cents"] == 4500


async def test_retry_wrong_state_returns_409(admin_client: AsyncClient) -> None:
    """Only FAILED sessions can be retried."""
    _auth_cookie(admin_client)
    session = {**_FAILED_SESSION, "state": "AUTHORIZED"}
    await db.upsert_session(session)

    with patch("square.charge_card_payment", new=AsyncMock()) as mock_charge:
        resp = await admin_client.post(
            f"/admin/sessions/{_RETRY_IK}/retry",
            json={"amount_cents": 4500},
        )

    assert resp.status_code == 409
    mock_charge.assert_not_called()


async def test_retry_missing_card_returns_422(admin_client: AsyncClient) -> None:
    """If no stored card/customer, 422 is returned."""
    _auth_cookie(admin_client)
    session = {**_FAILED_SESSION, "square_card_id": None, "square_customer_id": None}
    await db.upsert_session(session)
    await db.mark_failed(_RETRY_IK, "previous error")

    with patch("square.charge_card_payment", new=AsyncMock()) as mock_charge:
        resp = await admin_client.post(
            f"/admin/sessions/{_RETRY_IK}/retry",
            json={"amount_cents": 4500},
        )

    assert resp.status_code == 422
    mock_charge.assert_not_called()


async def test_retry_square_error_returns_502(admin_client: AsyncClient) -> None:
    """If Square raises, 502 is returned and DB state stays FAILED."""
    _auth_cookie(admin_client)
    await db.upsert_session(_FAILED_SESSION)
    await db.mark_failed(_RETRY_IK, "previous error")

    with patch("square.charge_card_payment", new=AsyncMock(side_effect=RuntimeError("sq down"))):
        resp = await admin_client.post(
            f"/admin/sessions/{_RETRY_IK}/retry",
            json={"amount_cents": 4500},
        )

    assert resp.status_code == 502
    row = await db.get_session(_RETRY_IK)
    assert row["state"] == "FAILED"


async def test_retry_html_form_redirects_to_detail(admin_client: AsyncClient) -> None:
    """HTML form POST (amount_dollars) redirects to the session detail page."""
    _auth_cookie(admin_client)
    await db.upsert_session(_FAILED_SESSION)
    await db.mark_failed(_RETRY_IK, "previous error")

    charge_result = {"id": "pay_retry2", "amount_money": {"amount": 5000}}
    with patch("square.charge_card_payment", new=AsyncMock(return_value=charge_result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_RETRY_IK}/retry",
            data={"amount_dollars": "50.00"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "/admin/sessions/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Static files – admin app
# ---------------------------------------------------------------------------

_ADMIN_STATIC_FILES = [
    "/static/css/portal.css",
    "/static/js/payment.js",
    "/static/images/Extravio%20EV%20Management.webp",
    "/static/images/Square_Logo_2025_White.svg",
]


@pytest.mark.parametrize("path", _ADMIN_STATIC_FILES)
async def test_admin_static_file_returns_200(admin_client: AsyncClient, path: str) -> None:
    """Every known static asset must be served without error on the admin app."""
    resp = await admin_client.get(path)
    assert resp.status_code == 200, f"Admin static file {path} returned {resp.status_code}"


async def test_admin_dashboard_static_refs_all_load(admin_client: AsyncClient) -> None:
    """Parse the rendered admin dashboard and verify every /static/ reference loads."""
    import re
    _auth_cookie(admin_client)
    page = await admin_client.get("/admin/")
    assert page.status_code == 200

    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
    assert refs, "No /static/ references found on admin dashboard"

    for ref in set(refs):
        r = await admin_client.get(ref)
        assert r.status_code == 200, f"Admin dashboard references {ref!r} but got {r.status_code}"
