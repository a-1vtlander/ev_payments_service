"""
tests/test_session_endpoint.py

Additional functional coverage for endpoints/session.py.

Covers uncovered lines (78% → higher):
  - GET /session/{uid}         → 404 on unknown uid
  - GET /session/{uid}/json    → 200 JSON + 404 on unknown uid
  - render_session_page        → card_line, guest_display variants,
                                  booking_end_display, accrued_bill_display
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fastapi import Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import db
import state
from tests.conftest import TEST_CHARGER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_session(
    *,
    session_id: str | None = None,
    guest_name: str = "Test Guest",
    card_brand: str = "VISA",
    card_last4: str = "4242",
    card_exp_month: int = 12,
    card_exp_year: int = 2030,
    authorized_amount_cents: int = 5000,
    booking_end_time: str = "",
) -> str:
    uid = session_id or str(uuid.uuid4())
    ik = f"ev:{TEST_CHARGER_ID}:ses-{uuid.uuid4().hex[:8]}"
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              f"bk-{uid[:8]}",
        "session_id":              uid,
        "state":                   "AUTHORIZED",
        "square_environment":      "sandbox",
        "square_payment_id":       "pay_ses_01",
        "square_card_id":          "card_ses_01",
        "authorized_amount_cents": authorized_amount_cents,
        "guest_name":              guest_name,
        "card_brand":              card_brand,
        "card_last4":              card_last4,
        "card_exp_month":          card_exp_month,
        "card_exp_year":           card_exp_year,
        "booking_end_time":        booking_end_time,
    })
    return uid


# ---------------------------------------------------------------------------
# GET /session/{uid}
# ---------------------------------------------------------------------------

async def test_session_page_404_for_unknown_uid(unit_client: AsyncClient):
    resp = await unit_client.get(f"/session/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_session_page_200_with_valid_uid(unit_client: AsyncClient):
    uid = await _insert_session(guest_name="Alice Smith")
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_session_page_shows_card_info(unit_client: AsyncClient):
    uid = await _insert_session(card_brand="VISA", card_last4="7500")
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "VISA" in resp.text
    assert "7500" in resp.text


async def test_session_page_shows_card_line_with_expiry(unit_client: AsyncClient):
    uid = await _insert_session(
        card_brand="MASTERCARD", card_last4="9999",
        card_exp_month=6, card_exp_year=2028,
    )
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "MASTERCARD" in resp.text
    assert "9999" in resp.text
    assert "06/2028" in resp.text


async def test_session_page_renders_without_card_info(unit_client: AsyncClient):
    """If card_brand and card_last4 are empty, the page still renders."""
    uid = await _insert_session(card_brand="", card_last4="", card_exp_month=0, card_exp_year=0)
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /session/{uid}/json
# ---------------------------------------------------------------------------

async def test_session_json_404_for_unknown_uid(unit_client: AsyncClient):
    resp = await unit_client.get(f"/session/{uuid.uuid4()}/json")
    assert resp.status_code == 404


async def test_session_json_returns_expected_fields(unit_client: AsyncClient):
    uid = await _insert_session(
        guest_name="Bob Jones",
        authorized_amount_cents=3300,
        card_brand="AMEX",
        card_last4="0001",
    )
    resp = await unit_client.get(f"/session/{uid}/json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized_amount_cents"] == 3300
    assert body["card_brand"] == "AMEX"
    assert body["card_last4"] == "0001"
    assert "state" in body
    assert "booking_id" in body


async def test_session_json_authorized_flag_set(unit_client: AsyncClient):
    """authorized=1 is set by mark_authorized(); verify the JSON field reflects it."""
    uid = str(uuid.uuid4())
    ik = f"ev:{TEST_CHARGER_ID}:auth-{uid[:8]}"
    await db.upsert_session({
        "idempotency_key": ik, "charger_id": TEST_CHARGER_ID,
        "booking_id": "bk-auth", "session_id": uid,
        "state": "AUTHORIZED", "square_environment": "sandbox",
        "authorized_amount_cents": 1000,
    })
    await db.mark_authorized(
        ik, square_payment_id="pay_x", authorized_amount_cents=1000,
        square_customer_id="cust_x", square_card_id="card_x",
        card_brand="VISA", card_last4="4242",
        card_exp_month=12, card_exp_year=2030,
    )
    resp = await unit_client.get(f"/session/{uid}/json")
    body = resp.json()
    assert body["authorized"] is True


# ---------------------------------------------------------------------------
# render_session_page – guest_display variants
# ---------------------------------------------------------------------------

async def test_session_page_guest_name_with_reservation_code(unit_client: AsyncClient):
    """Names like 'Alice Smith (RES-123)' split into name + reservation code."""
    uid = await _insert_session(guest_name="Alice Smith (RES-123)")
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "Alice Smith" in resp.text
    # The em-dash separator should be present when both name + code exist
    assert "\u2014" in resp.text or "RES-123" in resp.text


async def test_session_page_only_reservation_code_in_parens(unit_client: AsyncClient):
    """If the name is only a code in parens, it's displayed as the reservation code."""
    uid = await _insert_session(guest_name="(RES-ONLY)")
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "RES-ONLY" in resp.text


async def test_session_page_plain_name_no_parens(unit_client: AsyncClient):
    uid = await _insert_session(guest_name="Charlie Brown")
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "Charlie Brown" in resp.text


# ---------------------------------------------------------------------------
# render_session_page – booking_end_display
# ---------------------------------------------------------------------------

async def test_session_page_formats_booking_end_time(unit_client: AsyncClient):
    """booking_end_time in '%Y-%m-%d %H:%M:%S' format is rendered as 'Month Day, Year'."""
    uid = await _insert_session(booking_end_time="2026-07-04 14:30:00")
    resp = await unit_client.get(f"/session/{uid}")
    assert resp.status_code == 200
    assert "July" in resp.text
    assert "2026" in resp.text
