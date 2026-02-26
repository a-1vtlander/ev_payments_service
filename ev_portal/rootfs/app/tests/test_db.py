"""
Unit tests for db.py – pure SQLite DAL, no HTTP or MQTT.

All tests use the ``tmp_db`` fixture which spins up a fresh DB in a temp
directory and restores ``db.DB_PATH`` afterwards.
"""

from __future__ import annotations

import pytest
import db
from tests.conftest import TEST_HOME_ID, TEST_CHARGER_ID, TEST_BOOKING_ID, TEST_SESSION_ID

IK = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"  # idempotency key


def _base_session(**overrides) -> dict:
    defaults = {
        "idempotency_key":         IK,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              TEST_SESSION_ID,
        "state":                   "READY_TO_PAY",
        "authorized_amount_cents": 100,
        "square_environment":      "sandbox",
    }
    return {**defaults, **overrides}


# ---------------------------------------------------------------------------
# init_db / schema
# ---------------------------------------------------------------------------

async def test_init_db_is_idempotent(tmp_db):
    """Calling init_db() twice must not raise."""
    await db.init_db()  # second call
    row = await db.get_session(IK)
    assert row is None  # table is empty


# ---------------------------------------------------------------------------
# upsert_session / get_session
# ---------------------------------------------------------------------------

async def test_upsert_and_get_session(tmp_db):
    await db.upsert_session(_base_session())
    row = await db.get_session(IK)
    assert row is not None
    assert row["booking_id"]   == TEST_BOOKING_ID
    assert row["charger_id"]   == TEST_CHARGER_ID
    assert row["session_id"]   == TEST_SESSION_ID
    assert row["state"]        == "READY_TO_PAY"


async def test_get_session_returns_none_for_missing_key(tmp_db):
    row = await db.get_session("ev:nobody:nothing")
    assert row is None


async def test_upsert_updates_existing_row(tmp_db):
    await db.upsert_session(_base_session())
    await db.upsert_session(_base_session(state="AUTH_REQUESTED"))
    row = await db.get_session(IK)
    assert row["state"] == "AUTH_REQUESTED"


async def test_upsert_does_not_downgrade_authorized(tmp_db):
    """Once AUTHORIZED, upsert with READY_TO_PAY must not change state."""
    await db.upsert_session(_base_session(state="AUTHORIZED"))
    await db.upsert_session(_base_session(state="READY_TO_PAY"))
    row = await db.get_session(IK)
    assert row["state"] == "AUTHORIZED"


async def test_upsert_does_not_downgrade_captured(tmp_db):
    """Once CAPTURED, upsert with READY_TO_PAY must not change state."""
    await db.upsert_session(_base_session(state="CAPTURED"))
    await db.upsert_session(_base_session(state="READY_TO_PAY"))
    row = await db.get_session(IK)
    assert row["state"] == "CAPTURED"


async def test_upsert_sets_created_at_once(tmp_db):
    await db.upsert_session(_base_session())
    row1 = await db.get_session(IK)
    created_at_1 = row1["created_at"]

    import asyncio; await asyncio.sleep(0.01)
    await db.upsert_session(_base_session(state="AUTH_REQUESTED"))
    row2 = await db.get_session(IK)

    # created_at must not change on update
    assert row2["created_at"] == created_at_1
    # updated_at should have changed
    assert row2["updated_at"] >= row2["created_at"]


# ---------------------------------------------------------------------------
# get_session_by_uid
# ---------------------------------------------------------------------------

async def test_get_session_by_uid(tmp_db):
    await db.upsert_session(_base_session())
    row = await db.get_session_by_uid(TEST_SESSION_ID)
    assert row is not None
    assert row["idempotency_key"] == IK


async def test_get_session_by_uid_returns_none_for_missing(tmp_db):
    row = await db.get_session_by_uid("does-not-exist")
    assert row is None


# ---------------------------------------------------------------------------
# get_session_by_booking_id
# ---------------------------------------------------------------------------

async def test_get_session_by_booking_id(tmp_db):
    await db.upsert_session(_base_session())
    row = await db.get_session_by_booking_id(TEST_BOOKING_ID)
    assert row is not None
    assert row["idempotency_key"] == IK


async def test_get_session_by_booking_id_returns_most_recent(tmp_db):
    """If multiple rows share a booking_id, the most recently updated one is returned."""
    import asyncio
    ik_old = f"ev:charger-old:{TEST_BOOKING_ID}"
    ik_new = f"ev:charger-new:{TEST_BOOKING_ID}"
    await db.upsert_session(_base_session(idempotency_key=ik_old, session_id="uid-old", charger_id="charger-old"))
    await asyncio.sleep(0.01)
    await db.upsert_session(_base_session(idempotency_key=ik_new, session_id="uid-new", charger_id="charger-new"))
    row = await db.get_session_by_booking_id(TEST_BOOKING_ID)
    assert row["idempotency_key"] == ik_new


async def test_get_session_by_booking_id_returns_none_for_missing(tmp_db):
    row = await db.get_session_by_booking_id("no-such-booking")
    assert row is None


# ---------------------------------------------------------------------------
# mark_authorized
# ---------------------------------------------------------------------------

async def test_mark_authorized(tmp_db):
    await db.upsert_session(_base_session())
    await db.mark_authorized(
        IK,
        square_payment_id="pay_abc123",
        authorized_amount_cents=100,
        square_customer_id="cust_1",
        square_card_id="card_1",
        card_brand="VISA",
        card_last4="4242",
        card_exp_month=12,
        card_exp_year=2029,
    )
    row = await db.get_session(IK)
    assert row["state"]                == "AUTHORIZED"
    assert row["authorized"]           == 1
    assert row["square_payment_id"]    == "pay_abc123"
    assert row["card_brand"]           == "VISA"
    assert row["card_last4"]           == "4242"
    assert row["last_error"]           is None


async def test_mark_authorized_no_row_does_nothing(tmp_db):
    """mark_authorized on a non-existent key must not crash."""
    await db.mark_authorized("ev:ghost:ghost", "pay_x", 0)
    # no row exists; get_session returns None
    assert await db.get_session("ev:ghost:ghost") is None


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

async def test_mark_failed(tmp_db):
    await db.upsert_session(_base_session())
    await db.mark_failed(IK, "card declined")
    row = await db.get_session(IK)
    assert row["state"]      == "FAILED"
    assert row["last_error"] == "card declined"


# ---------------------------------------------------------------------------
# mark_captured
# ---------------------------------------------------------------------------

async def test_mark_captured(tmp_db):
    await db.upsert_session(_base_session(state="AUTHORIZED"))
    await db.mark_captured(IK, "pay_captured_999", 750)
    row = await db.get_session(IK)
    assert row["state"]                     == "CAPTURED"
    assert row["square_capture_payment_id"] == "pay_captured_999"
    assert row["captured_amount_cents"]     == 750


# ---------------------------------------------------------------------------
# mark_voided
# ---------------------------------------------------------------------------

async def test_mark_voided(tmp_db):
    await db.upsert_session(_base_session(state="AUTHORIZED", square_payment_id="pay_voided"))
    await db.mark_voided(IK, "pay_voided")
    row = await db.get_session(IK)
    assert row["state"]                     == "VOIDED"
    assert row["captured_amount_cents"]     == 0
    assert row["square_capture_payment_id"] == "pay_voided"
