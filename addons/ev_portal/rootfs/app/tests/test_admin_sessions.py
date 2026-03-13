"""
tests/test_admin_sessions.py

Functional coverage for the admin session-management routes that are not
exercised by test_admin.py:

  GET  /admin/sessions              – list (JSON + HTML)
  GET  /admin/sessions/{ik}         – detail (JSON + HTML, 404)
  GET  /admin/db                    – DB introspection page
  GET  /admin/login                 – already-authenticated redirect
  GET  /admin/openapi.json          – protected schema endpoint
  GET  /admin/docs                  – protected Swagger UI
  POST /admin/sessions/{ik}/capture
  POST /admin/sessions/{ik}/void
  POST /admin/sessions/{ik}/refund
  POST /admin/sessions/{ik}/reauthorize
  POST /admin/sessions/{ik}/note
  POST /admin/sessions/{ik}/soft_delete
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import db
import state
from tests.conftest import TEST_BOOKING_ID, TEST_CHARGER_ID, TEST_HOME_ID

TEST_ADMIN_USER = "testadmin"
TEST_ADMIN_PASS = "testpassword"

# ---------------------------------------------------------------------------
# Shared session templates
# ---------------------------------------------------------------------------

_IK_AUTH  = f"ev:{TEST_CHARGER_ID}:booking-auth"
_IK_CAP   = f"ev:{TEST_CHARGER_ID}:booking-cap"
_IK_PLAIN = f"ev:{TEST_CHARGER_ID}:booking-plain"

_BASE = {
    "charger_id":              TEST_CHARGER_ID,
    "square_environment":      "sandbox",
    "authorized_amount_cents": 4000,
}

_AUTHORIZED = {
    **_BASE,
    "idempotency_key":    _IK_AUTH,
    "booking_id":         "booking-auth",
    "session_id":         str(uuid.uuid4()),
    "state":              "AUTHORIZED",
    "square_payment_id":  "pay_auth_01",
    "square_card_id":     "card_01",
    "square_customer_id": "cust_01",
    "card_brand":         "VISA",
    "card_last4":         "4242",
    "card_exp_month":     12,
    "card_exp_year":      2030,
}

_CAPTURED = {
    **_BASE,
    "idempotency_key":      _IK_CAP,
    "booking_id":           "booking-cap",
    "session_id":           str(uuid.uuid4()),
    "state":                "CAPTURED",
    "square_payment_id":    "pay_cap_01",
    "square_card_id":       "card_02",
    "square_customer_id":   "cust_02",
    "card_brand":           "MASTERCARD",
    "card_last4":           "1234",
    "card_exp_month":       6,
    "card_exp_year":        2028,
    "captured_amount_cents": 3800,
}

_PLAIN = {
    **_BASE,
    "idempotency_key": _IK_PLAIN,
    "booking_id":      "booking-plain",
    "session_id":      str(uuid.uuid4()),
    "state":           "AUTHORIZED",
    "square_payment_id": "pay_plain_01",
}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def admin_client(patched_state) -> AsyncClient:
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


def _auth(client: AsyncClient) -> None:
    from admin.auth import SESSION_COOKIE, make_session_token
    client.cookies.set(SESSION_COOKIE, make_session_token(TEST_ADMIN_USER))


# ---------------------------------------------------------------------------
# Login page – already-authenticated redirect
# ---------------------------------------------------------------------------

async def test_login_page_already_authed_redirects_to_admin(admin_client: AsyncClient):
    """GET /admin/login while holding a valid session cookie → 302 to /admin/."""
    _auth(admin_client)
    resp = await admin_client.get("/admin/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/admin/"


# ---------------------------------------------------------------------------
# Protected schema / docs endpoints
# ---------------------------------------------------------------------------

async def test_openapi_json_returns_schema_for_authed_admin(admin_client: AsyncClient):
    _auth(admin_client)
    resp = await admin_client.get("/admin/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "openapi" in body


async def test_docs_returns_html_for_authed_admin(admin_client: AsyncClient):
    _auth(admin_client)
    resp = await admin_client.get("/admin/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()


# ---------------------------------------------------------------------------
# GET /admin/db
# ---------------------------------------------------------------------------

async def test_db_view_returns_html_table_structure(admin_client: AsyncClient):
    _auth(admin_client)
    resp = await admin_client.get("/admin/db")
    assert resp.status_code == 200
    assert "sessions" in resp.text.lower()
    assert "access_keys" in resp.text.lower()


async def test_db_view_shows_access_key_rows(admin_client: AsyncClient):
    """When keys exist, the db page lists them."""
    from datetime import datetime, timedelta, timezone
    key = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    await db.create_access_key(key, now.isoformat(), (now + timedelta(days=30)).isoformat())
    _auth(admin_client)
    resp = await admin_client.get("/admin/db")
    assert resp.status_code == 200
    assert key in resp.text


# ---------------------------------------------------------------------------
# GET /admin/sessions  –  list
# ---------------------------------------------------------------------------

async def test_list_sessions_returns_json_for_api_client(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.get("/admin/sessions", headers={"accept": "application/json"})
    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body
    assert body["count"] >= 1
    keys = [s["idempotency_key"] for s in body["sessions"]]
    assert _IK_AUTH in keys


async def test_list_sessions_returns_html_for_browser(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.get("/admin/sessions", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "<table" in resp.text
    assert _IK_AUTH in resp.text


async def test_list_sessions_state_filter(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    resp = await admin_client.get(
        "/admin/sessions?state=CAPTURED",
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    for s in body["sessions"]:
        assert s["state"] == "CAPTURED"


async def test_list_sessions_include_deleted_flag(admin_client: AsyncClient):
    """Soft-deleted sessions are hidden by default but visible with include_deleted."""
    await db.upsert_session(_PLAIN)
    await db.soft_delete(_IK_PLAIN)
    _auth(admin_client)

    # Without flag: session absent
    resp = await admin_client.get("/admin/sessions", headers={"accept": "application/json"})
    body = resp.json()
    live_keys = [s["idempotency_key"] for s in body["sessions"]]
    assert _IK_PLAIN not in live_keys

    # With flag: session present
    resp = await admin_client.get("/admin/sessions?include_deleted=true", headers={"accept": "application/json"})
    body = resp.json()
    all_keys = [s["idempotency_key"] for s in body["sessions"]]
    assert _IK_PLAIN in all_keys


# ---------------------------------------------------------------------------
# GET /admin/sessions/{ik}  –  detail
# ---------------------------------------------------------------------------

async def test_get_session_detail_json(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.get(
        f"/admin/sessions/{_IK_AUTH}",
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["idempotency_key"] == _IK_AUTH
    assert row["state"] == "AUTHORIZED"


async def test_get_session_detail_html(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.get(
        f"/admin/sessions/{_IK_AUTH}",
        headers={"accept": "text/html"},
    )
    assert resp.status_code == 200
    assert _IK_AUTH in resp.text
    assert "AUTHORIZED" in resp.text


async def test_get_session_detail_shows_capture_button_for_authorized(admin_client: AsyncClient):
    """The detail page for an AUTHORIZED session includes a 'Capture' action button."""
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.get(
        f"/admin/sessions/{_IK_AUTH}",
        headers={"accept": "text/html"},
    )
    assert resp.status_code == 200
    assert "capture" in resp.text.lower()


async def test_get_session_detail_shows_retry_button_for_failed(admin_client: AsyncClient):
    """The detail page for a FAILED session includes a 'Retry' action button."""
    failed = {**_AUTHORIZED, "idempotency_key": _IK_AUTH, "state": "FAILED"}
    await db.upsert_session(failed)
    await db.mark_failed(_IK_AUTH, "some error")
    _auth(admin_client)
    resp = await admin_client.get(
        f"/admin/sessions/{_IK_AUTH}",
        headers={"accept": "text/html"},
    )
    assert resp.status_code == 200
    assert "retry" in resp.text.lower()


async def test_get_session_detail_404_for_unknown_key(admin_client: AsyncClient):
    _auth(admin_client)
    resp = await admin_client.get(
        "/admin/sessions/nonexistent-key",
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/sessions/{ik}/capture
# ---------------------------------------------------------------------------

async def test_capture_success_marks_session_captured(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    result = {"id": "pay_cap_new", "amount_money": {"amount": 3500}}
    with patch("square.capture_payment", new=AsyncMock(return_value=result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/capture",
            json={"amount_cents": 3500},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["captured_amount_cents"] == 3500
    row = await db.get_session(_IK_AUTH)
    assert row["state"] == "CAPTURED"
    assert row["captured_amount_cents"] == 3500


async def test_capture_html_form_redirects_to_session_detail(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    result = {"id": "pay_cap_form", "amount_money": {"amount": 4000}}
    with patch("square.capture_payment", new=AsyncMock(return_value=result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/capture",
            data={"amount_dollars": "40.00"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert f"/admin/sessions/" in resp.headers["location"]


async def test_capture_wrong_state_returns_409(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    with patch("square.capture_payment", new=AsyncMock()) as mock_cap:
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/capture",
            json={"amount_cents": 3800},
        )
    assert resp.status_code == 409
    mock_cap.assert_not_called()


async def test_capture_no_payment_id_returns_422(admin_client: AsyncClient):
    no_pay_id = {**_AUTHORIZED, "idempotency_key": _IK_AUTH, "square_payment_id": None}
    await db.upsert_session(no_pay_id)
    _auth(admin_client)
    with patch("square.capture_payment", new=AsyncMock()) as mock_cap:
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/capture",
            json={"amount_cents": 3500},
        )
    assert resp.status_code == 422
    mock_cap.assert_not_called()


async def test_capture_square_error_returns_502(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    with patch("square.capture_payment", new=AsyncMock(side_effect=RuntimeError("sq err"))):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/capture",
            json={"amount_cents": 3500},
        )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /admin/sessions/{ik}/void
# ---------------------------------------------------------------------------

async def test_void_success_marks_session_canceled(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    result = {"id": "pay_auth_01", "status": "CANCELED"}
    with patch("square.cancel_payment", new=AsyncMock(return_value=result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/void",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    row = await db.get_session(_IK_AUTH)
    assert row["state"] == "CANCELED"


async def test_void_wrong_state_returns_409(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    with patch("square.cancel_payment", new=AsyncMock()) as mock_void:
        resp = await admin_client.post(f"/admin/sessions/{_IK_CAP}/void")
    assert resp.status_code == 409
    mock_void.assert_not_called()


async def test_void_no_payment_id_returns_422(admin_client: AsyncClient):
    no_pay = {**_AUTHORIZED, "idempotency_key": _IK_AUTH, "square_payment_id": None}
    await db.upsert_session(no_pay)
    _auth(admin_client)
    resp = await admin_client.post(f"/admin/sessions/{_IK_AUTH}/void")
    assert resp.status_code == 422


async def test_void_square_error_returns_502(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    with patch("square.cancel_payment", new=AsyncMock(side_effect=RuntimeError("sq err"))):
        resp = await admin_client.post(f"/admin/sessions/{_IK_AUTH}/void")
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /admin/sessions/{ik}/refund
# ---------------------------------------------------------------------------

async def test_refund_success_marks_session_refunded(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    refund_result = {
        "id": "ref_01",
        "amount_money": {"amount": 3800},
    }
    with patch("square.refund_payment", new=AsyncMock(return_value=refund_result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/refund",
            json={"amount_cents": 3800, "reason": "customer request"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["refund_id"] == "ref_01"
    row = await db.get_session(_IK_CAP)
    assert row["state"] == "REFUNDED"


async def test_refund_html_form_redirects(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    refund_result = {"id": "ref_form", "amount_money": {"amount": 3800}}
    with patch("square.refund_payment", new=AsyncMock(return_value=refund_result)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/refund",
            data={"amount_dollars": "38.00", "reason": "test"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
    assert resp.status_code == 303


async def test_refund_wrong_state_returns_409(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    with patch("square.refund_payment", new=AsyncMock()) as mock_ref:
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/refund",
            json={"amount_cents": 1000},
        )
    assert resp.status_code == 409
    mock_ref.assert_not_called()


async def test_refund_no_payment_id_returns_422(admin_client: AsyncClient):
    no_pay = {**_CAPTURED, "idempotency_key": _IK_CAP, "square_payment_id": None}
    await db.upsert_session(no_pay)
    _auth(admin_client)
    resp = await admin_client.post(
        f"/admin/sessions/{_IK_CAP}/refund",
        json={"amount_cents": 1000},
    )
    assert resp.status_code == 422


async def test_refund_square_error_returns_502(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    with patch("square.refund_payment", new=AsyncMock(side_effect=RuntimeError("sq err"))):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/refund",
            json={"amount_cents": 1000},
        )
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /admin/sessions/{ik}/reauthorize
# ---------------------------------------------------------------------------

async def test_reauthorize_success_marks_authorized(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    new_payment = {"id": "pay_reauth_01"}
    with patch("square.create_payment_authorization", new=AsyncMock(return_value=new_payment)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/reauthorize",
            json={"amount_cents": 5000},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["new_payment_id"] == "pay_reauth_01"
    row = await db.get_session(_IK_CAP)
    assert row["state"] == "AUTHORIZED"


async def test_reauthorize_wrong_state_returns_409(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    with patch("square.create_payment_authorization", new=AsyncMock()) as mock_auth:
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_AUTH}/reauthorize",
            json={"amount_cents": 5000},
        )
    assert resp.status_code == 409
    mock_auth.assert_not_called()


async def test_reauthorize_no_card_returns_422(admin_client: AsyncClient):
    no_card = {**_CAPTURED, "square_card_id": None, "square_customer_id": None}
    await db.upsert_session(no_card)
    _auth(admin_client)
    resp = await admin_client.post(
        f"/admin/sessions/{_IK_CAP}/reauthorize",
        json={"amount_cents": 5000},
    )
    assert resp.status_code == 422


async def test_reauthorize_square_error_returns_502(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    with patch("square.create_payment_authorization", new=AsyncMock(side_effect=RuntimeError("sq err"))):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/reauthorize",
            json={"amount_cents": 5000},
        )
    assert resp.status_code == 502


async def test_reauthorize_html_form_redirects(admin_client: AsyncClient):
    await db.upsert_session(_CAPTURED)
    _auth(admin_client)
    new_payment = {"id": "pay_reauth_02"}
    with patch("square.create_payment_authorization", new=AsyncMock(return_value=new_payment)):
        resp = await admin_client.post(
            f"/admin/sessions/{_IK_CAP}/reauthorize",
            data={"amount_dollars": "50.00"},
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
    assert resp.status_code == 303


# ---------------------------------------------------------------------------
# POST /admin/sessions/{ik}/note
# ---------------------------------------------------------------------------

async def test_add_note_json_persists_note(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.post(
        f"/admin/sessions/{_IK_AUTH}/note",
        json={"note": "operator note text"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    row = await db.get_session(_IK_AUTH)
    assert row["note"] == "operator note text"


async def test_add_note_html_form_redirects(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.post(
        f"/admin/sessions/{_IK_AUTH}/note",
        data={"note": "form note"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/admin/sessions/" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /admin/sessions/{ik}/soft_delete
# ---------------------------------------------------------------------------

async def test_soft_delete_marks_session_deleted(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.post(f"/admin/sessions/{_IK_AUTH}/soft_delete")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Session should no longer appear in default (non-deleted) listing
    rows = await db.list_sessions(include_deleted=False)
    keys = [r["idempotency_key"] for r in rows]
    assert _IK_AUTH not in keys


async def test_soft_delete_html_form_redirects(admin_client: AsyncClient):
    await db.upsert_session(_AUTHORIZED)
    _auth(admin_client)
    resp = await admin_client.post(
        f"/admin/sessions/{_IK_AUTH}/soft_delete",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
