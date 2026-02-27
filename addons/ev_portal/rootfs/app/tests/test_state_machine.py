"""
tests/test_state_machine.py
---------------------------
Explicit coverage of every state transition in the session state machine.

States (from db.py):
    CREATED / AWAITING_PAYMENT_INFO  ← initial, set via upsert_session
    AUTHORIZED                        ← mark_authorized
    FAILED                            ← mark_failed
    CAPTURED                          ← mark_captured
    VOIDED                            ← mark_voided
    CANCELED                          ← mark_canceled
    REFUNDED                          ← mark_refunded

Valid transitions exercised here:
    upsert         → AWAITING_PAYMENT_INFO
    upsert         → AUTHORIZED          (direct payment)
    AUTHORIZED     → CAPTURED            (normal finalize)
    AUTHORIZED     → VOIDED              (zero-amount finalize)
    AUTHORIZED     → CANCELED            (admin void)
    AUTHORIZED     → FAILED              (mark_failed on error)
    FAILED         → CAPTURED            (retry / overcharge direct charge)
    CAPTURED       → REFUNDED            (full or partial refund)
    CAPTURED       → AUTHORIZED          (reauthorize)

Guard / no-downgrade rules:
    AUTHORIZED must not be overwritten by a lower-rank upsert
    CAPTURED   must not be overwritten by a lower-rank upsert
"""

from __future__ import annotations

import pytest
from tests.conftest import TEST_BOOKING_ID, TEST_CHARGER_ID, TEST_SESSION_ID

import db

IK = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"


def _base(state: str = "AWAITING_PAYMENT_INFO", **kwargs) -> dict:
    row = {
        "idempotency_key":         IK,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              TEST_SESSION_ID,
        "state":                   state,
        "square_environment":      "sandbox",
        "square_payment_id":       "pay_test",
        "square_card_id":          "card_test",
        "square_customer_id":      "cust_test",
        "authorized_amount_cents": 5000,
    }
    row.update(kwargs)
    return row


# ---------------------------------------------------------------------------
# Initial state via upsert_session
# ---------------------------------------------------------------------------

async def test_upsert_creates_awaiting_payment_info(tmp_db) -> None:
    await db.upsert_session(_base("AWAITING_PAYMENT_INFO"))
    row = await db.get_session(IK)
    assert row["state"] == "AWAITING_PAYMENT_INFO"


async def test_upsert_creates_authorized_directly(tmp_db) -> None:
    """upsert with AUTHORIZED sets state immediately."""
    await db.upsert_session(_base("AUTHORIZED"))
    row = await db.get_session(IK)
    assert row["state"] == "AUTHORIZED"


# ---------------------------------------------------------------------------
# AWAITING_PAYMENT_INFO → AUTHORIZED
# ---------------------------------------------------------------------------

async def test_awaiting_to_authorized(tmp_db) -> None:
    await db.upsert_session(_base("AWAITING_PAYMENT_INFO"))
    await db.mark_authorized(IK, "pay_auth", 5000)
    row = await db.get_session(IK)
    assert row["state"] == "AUTHORIZED"
    assert row["square_payment_id"] == "pay_auth"
    assert row["authorized_amount_cents"] == 5000


# ---------------------------------------------------------------------------
# AUTHORIZED → CAPTURED
# ---------------------------------------------------------------------------

async def test_authorized_to_captured(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_captured(IK, "pay_captured", 4800)
    row = await db.get_session(IK)
    assert row["state"] == "CAPTURED"
    assert row["captured_amount_cents"] == 4800
    assert row["square_capture_payment_id"] == "pay_captured"


# ---------------------------------------------------------------------------
# AUTHORIZED → VOIDED
# ---------------------------------------------------------------------------

async def test_authorized_to_voided(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED", square_payment_id="pay_void"))
    await db.mark_voided(IK, "pay_void")
    row = await db.get_session(IK)
    assert row["state"] == "VOIDED"


# ---------------------------------------------------------------------------
# AUTHORIZED → CANCELED (admin void)
# ---------------------------------------------------------------------------

async def test_authorized_to_canceled(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED", square_payment_id="pay_cancel"))
    await db.mark_canceled(IK, "pay_cancel")
    row = await db.get_session(IK)
    assert row["state"] == "CANCELED"


# ---------------------------------------------------------------------------
# Any live state → FAILED
# ---------------------------------------------------------------------------

async def test_authorized_to_failed(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_failed(IK, "card declined")
    row = await db.get_session(IK)
    assert row["state"] == "FAILED"
    assert "card declined" in row["last_error"]


async def test_captured_to_failed(tmp_db) -> None:
    """mark_failed can also be called on a CAPTURED session on unexpected error."""
    await db.upsert_session(_base("CAPTURED"))
    await db.mark_failed(IK, "unexpected error")
    row = await db.get_session(IK)
    assert row["state"] == "FAILED"


# ---------------------------------------------------------------------------
# FAILED → CAPTURED  (retry / overcharge direct-charge path)
# ---------------------------------------------------------------------------

async def test_failed_to_captured_retry(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_failed(IK, "original error")
    assert (await db.get_session(IK))["state"] == "FAILED"

    await db.mark_captured(IK, "pay_retry", 5000)
    row = await db.get_session(IK)
    assert row["state"] == "CAPTURED"
    assert row["captured_amount_cents"] == 5000


# ---------------------------------------------------------------------------
# CAPTURED → REFUNDED
# ---------------------------------------------------------------------------

async def test_captured_to_refunded_full(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_captured(IK, "pay_cap", 5000)
    await db.mark_refunded(IK, "refund_1", 5000)
    row = await db.get_session(IK)
    assert row["state"] == "REFUNDED"
    assert row["square_capture_payment_id"] == "refund_1"
    assert row["captured_amount_cents"] == 5000


async def test_captured_to_refunded_partial(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_captured(IK, "pay_cap", 5000)
    await db.mark_refunded(IK, "refund_partial", 2500)
    row = await db.get_session(IK)
    assert row["state"] == "REFUNDED"
    assert row["captured_amount_cents"] == 2500


# ---------------------------------------------------------------------------
# CAPTURED → AUTHORIZED  (reauthorize – mark_authorized resets the payment)
# ---------------------------------------------------------------------------

async def test_captured_to_reauthorized(tmp_db) -> None:
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_captured(IK, "pay_cap", 5000)
    assert (await db.get_session(IK))["state"] == "CAPTURED"

    await db.mark_authorized(IK, "pay_reauth", 7500)
    row = await db.get_session(IK)
    assert row["state"] == "AUTHORIZED"
    assert row["square_payment_id"] == "pay_reauth"
    assert row["authorized_amount_cents"] == 7500


# ---------------------------------------------------------------------------
# Guard: no-downgrade rules
# ---------------------------------------------------------------------------

async def test_authorized_not_downgraded_by_upsert(tmp_db) -> None:
    """upsert with AWAITING_PAYMENT_INFO must not overwrite AUTHORIZED."""
    await db.upsert_session(_base("AUTHORIZED"))
    await db.upsert_session(_base("AWAITING_PAYMENT_INFO"))
    row = await db.get_session(IK)
    assert row["state"] == "AUTHORIZED"


async def test_captured_not_downgraded_by_upsert(tmp_db) -> None:
    """upsert with AWAITING_PAYMENT_INFO must not overwrite CAPTURED."""
    await db.upsert_session(_base("AUTHORIZED"))
    await db.mark_captured(IK, "pay_cap", 5000)
    await db.upsert_session(_base("AWAITING_PAYMENT_INFO"))
    row = await db.get_session(IK)
    assert row["state"] == "CAPTURED"


# ---------------------------------------------------------------------------
# mark_authorized on missing key does not crash
# ---------------------------------------------------------------------------

async def test_mark_authorized_missing_key_is_noop(tmp_db) -> None:
    await db.mark_authorized("ev:ghost:ghost", "pay_ghost", 100)
    row = await db.get_session("ev:ghost:ghost")
    assert row is None
