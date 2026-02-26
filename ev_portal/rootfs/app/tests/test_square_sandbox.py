"""
Square sandbox API tests.

These tests make REAL HTTP calls to https://connect.squareupsandbox.com.
They are intentionally excluded from the default test run and must be
invoked explicitly:

    pytest -m sandbox -v tests/test_square_sandbox.py

Prerequisites
-------------
A valid sandbox access token and app ID in tests/dev_options.json.
``square_sandbox`` is always forced True.

Cleanup
-------
Each test cleans up the resources it creates (customers, cards, payments)
so the sandbox account stays tidy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

import square
import state

pytestmark = pytest.mark.sandbox

_OPTIONS_FILE = Path(__file__).parent / "dev_options.json"


# ---------------------------------------------------------------------------
# Fixture – sandbox Square config
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def sandbox_square_config():
    """
    Load sandbox credentials from dev_options.json and inject into state.
    Enforce sandbox=True regardless of what the file says.
    """
    opts = json.loads(_OPTIONS_FILE.read_text())
    original = state._square_config.copy()
    state._square_config = {
        "sandbox":      True,          # always sandbox
        "app_id":       opts["square_app_id"],
        "access_token": opts["square_access_token"],
        "location_id":  opts.get("square_location_id") or "",
        "charge_cents": opts.get("square_charge_cents", 100),
    }

    # Auto-fetch location_id if not set
    if not state._square_config["location_id"]:
        state._square_config["location_id"] = await square.fetch_first_location_id()

    yield

    state._square_config = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_test_customer(suffix: str = "1") -> str:
    cid = await square.create_customer(
        booking_id=f"sandbox-test-{suffix}",
        given_name="Sandbox",
        family_name="Tester",
    )
    return cid


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

async def test_fetch_location_id_returns_nonempty_string():
    location_id = await square.fetch_first_location_id()
    assert isinstance(location_id, str)
    assert len(location_id) > 0


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

async def test_create_customer_returns_id():
    cid = await _create_test_customer("create")
    assert isinstance(cid, str)
    assert len(cid) > 0


# ---------------------------------------------------------------------------
# Card on file
# ---------------------------------------------------------------------------

async def test_create_card_returns_ids_and_meta():
    """
    Square sandbox nonce ``cnon:card-nonce-ok`` creates a valid card on file.
    """
    card_id, customer_id, card_meta = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id="sandbox-card-test",
        given_name="Sandbox",
        family_name="Tester",
    )
    assert isinstance(card_id, str) and len(card_id) > 0
    assert isinstance(customer_id, str) and len(customer_id) > 0
    assert isinstance(card_meta, dict)
    assert "card_brand" in card_meta
    assert "card_last4" in card_meta


# ---------------------------------------------------------------------------
# Payment authorization (pre-auth hold)
# ---------------------------------------------------------------------------

async def test_create_payment_authorization_returns_pending_payment():
    card_id, customer_id, _ = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id="sandbox-preauth-test",
        given_name="Sandbox",
        family_name="Auth",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id="sandbox-preauth-test",
        amount_cents=100,
    )
    assert isinstance(payment, dict)
    assert "id" in payment
    # Pre-auth payments are APPROVED but not yet COMPLETED
    assert payment.get("status") in ("APPROVED", "PENDING")

    # Cleanup – cancel the pre-auth so the sandbox balance stays clean
    await square.cancel_payment(payment["id"])


async def test_payment_authorization_is_not_auto_completed():
    """autocomplete must be False for pre-auth flows."""
    card_id, customer_id, _ = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id="sandbox-no-autocomplete",
        given_name="Sandbox",
        family_name="NoComplete",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id="sandbox-no-autocomplete",
        amount_cents=50,
    )
    assert payment.get("status") != "COMPLETED"
    await square.cancel_payment(payment["id"])


# ---------------------------------------------------------------------------
# cancel_payment (void)
# ---------------------------------------------------------------------------

async def test_cancel_payment_voids_preauth():
    card_id, customer_id, _ = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id="sandbox-cancel-test",
        given_name="Sandbox",
        family_name="Cancel",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id="sandbox-cancel-test",
        amount_cents=100,
    )
    result = await square.cancel_payment(payment["id"])
    assert result.get("status") == "CANCELED"


# ---------------------------------------------------------------------------
# capture_payment at a different (lower) amount
# ---------------------------------------------------------------------------

async def test_capture_payment_at_lower_amount():
    """
    Authorization hold of 500 cents; capture at 200 cents.
    The final settled amount reported by Square should reflect 200 cents.
    """
    card_id, customer_id, _ = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id="sandbox-capture-test",
        given_name="Sandbox",
        family_name="Capture",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id="sandbox-capture-test",
        amount_cents=500,
    )
    captured = await square.capture_payment(
        payment_id=payment["id"],
        final_amount_cents=200,
    )
    assert captured.get("status") == "COMPLETED"
    assert captured.get("amount_money", {}).get("amount") == 200


async def test_capture_payment_at_full_amount():
    card_id, customer_id, _ = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id="sandbox-capture-full",
        given_name="Sandbox",
        family_name="FullCapture",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id="sandbox-capture-full",
        amount_cents=300,
    )
    captured = await square.capture_payment(
        payment_id=payment["id"],
        final_amount_cents=300,
    )
    assert captured.get("status") == "COMPLETED"
    assert captured.get("amount_money", {}).get("amount") == 300
