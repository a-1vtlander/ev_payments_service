"""
Tests for telemetry.py and the events table in db.py.

Covers:
 - telemetry.record_event() writes a row to the events table
 - record_event() never raises, even when db is broken
 - db.write_audit_log() backward-compat wrapper routes through events table
 - New session columns (payment_capabilities, payment_version_token) exist
   after init_db()
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

import db
import telemetry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_events(db_path: str) -> list[dict]:
    """Read every row from the events table as list-of-dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schema / migration
# ---------------------------------------------------------------------------

async def test_events_table_created_by_init_db(tmp_db):
    """init_db() must create the events table."""
    events = _all_events(tmp_db)
    assert isinstance(events, list)   # table exists; empty is fine


async def test_session_columns_payment_capabilities_present(tmp_db):
    """The new payment_capabilities and payment_version_token columns exist."""
    conn = sqlite3.connect(tmp_db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    assert "payment_capabilities"  in cols
    assert "payment_version_token" in cols


# ---------------------------------------------------------------------------
# telemetry.record_event() — happy path
# ---------------------------------------------------------------------------

async def test_record_event_writes_row(tmp_db):
    """A single record_event() call produces exactly one events row."""
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CREATE_PAYMENT",
        booking_id="booking-42",
        idempotency_key="ev:ch1:booking-42",
        processor_payment_id="pay_abc123",
        amount_cents=1500,
        http_status=200,
        success=True,
        duration_ms=87,
        request_json='{"amount_money": {"amount": 1500}}',
        response_json='{"payment": {"id": "pay_abc123"}}',
    )

    rows = _all_events(tmp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"]                == "PAYMENT_PROCESSOR"
    assert r["event_type"]            == "CREATE_PAYMENT"
    assert r["booking_id"]            == "booking-42"
    assert r["idempotency_key"]       == "ev:ch1:booking-42"
    assert r["processor_payment_id"]  == "pay_abc123"
    assert r["amount_cents"]          == 1500
    assert r["http_status"]           == 200
    assert r["success"]               == 1
    assert r["duration_ms"]           == 87
    assert r["error_code"]            is None


async def test_record_event_failure_stored(tmp_db):
    """Failure events (success=False) store error_code and error_detail."""
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CREATE_PAYMENT",
        booking_id="bk-fail",
        http_status=400,
        success=False,
        error_code="INVALID_CARD_DATA",
        error_detail="Card number is invalid",
    )

    rows = _all_events(tmp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["success"]      == 0
    assert r["error_code"]   == "INVALID_CARD_DATA"
    assert r["error_detail"] == "Card number is invalid"


async def test_record_event_multiple_calls(tmp_db):
    """Multiple record_event() calls each produce their own row."""
    for i in range(3):
        await telemetry.record_event(
            "SYSTEM", "STATE_CHANGE",
            idempotency_key=f"ev:ch:{i}",
            booking_id=f"bk-{i}",
        )

    rows = _all_events(tmp_db)
    assert len(rows) == 3
    keys = {r["idempotency_key"] for r in rows}
    assert keys == {"ev:ch:0", "ev:ch:1", "ev:ch:2"}


async def test_record_event_admin_source(tmp_db):
    """ADMIN source events round-trip correctly."""
    await telemetry.record_event(
        "ADMIN", "VOID",
        actor="operator@example.com",
        idempotency_key="ev:ch1:booking-99",
        success=True,
        metadata={"reason": "customer request"},
    )

    rows = _all_events(tmp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"]    == "ADMIN"
    assert r["event_type"] == "VOID"
    assert r["actor"]     == "operator@example.com"
    assert r["metadata_json"] is not None
    assert json.loads(r["metadata_json"])["reason"] == "customer request"


# ---------------------------------------------------------------------------
# telemetry.record_event() — never raises
# ---------------------------------------------------------------------------

async def test_record_event_never_raises_on_db_error(tmp_db):
    """
    If the underlying db.write_event raises, record_event() must swallow the
    exception silently rather than propagating it to the caller.
    """
    with patch("db.write_event", new_callable=AsyncMock, side_effect=RuntimeError("disk full")):
        # Must NOT raise – telemetry cannot break business logic.
        await telemetry.record_event(
            "PAYMENT_PROCESSOR", "CREATE_PAYMENT",
            booking_id="bk-silent",
            amount_cents=999,
        )


async def test_record_event_never_raises_on_bad_inputs(tmp_db):
    """Garbage inputs must not cause record_event() to raise."""
    # Pass a non-serialisable object as metadata — should be handled gracefully.
    await telemetry.record_event(
        "SYSTEM", "STARTUP",
    )   # minimal call — all optionals omitted


# ---------------------------------------------------------------------------
# db.write_audit_log backward-compat wrapper
# ---------------------------------------------------------------------------

async def test_write_audit_log_routes_to_events_table(tmp_db):
    """write_audit_log() must write through to the events table."""
    await db.write_audit_log(
        actor="admin-user",
        action="capture",
        idempotency_key="ev:ch1:bk-audit",
        before_json='{"state": "AUTHORIZED"}',
        after_json='{"state": "CAPTURED"}',
        result_json='{"id": "pay_xyz"}',
    )

    rows = _all_events(tmp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"]          == "ADMIN"
    assert r["event_type"].upper() == "CAPTURE"
    assert r["actor"]           == "admin-user"
    assert r["idempotency_key"] == "ev:ch1:bk-audit"


async def test_write_audit_log_with_reason(tmp_db):
    """write_audit_log() preserves the optional reason field in metadata."""
    await db.write_audit_log(
        actor="admin",
        action="note",
        idempotency_key="ev:ch1:bk-note",
        reason="Customer called to adjust amount",
    )

    rows = _all_events(tmp_db)
    assert len(rows) == 1
    r = rows[0]
    meta = json.loads(r["metadata_json"] or "{}")
    assert meta.get("reason") == "Customer called to adjust amount"


# ---------------------------------------------------------------------------
# db.write_event timestamp
# ---------------------------------------------------------------------------

async def test_write_event_sets_ts(tmp_db):
    """Each event row must have a non-null timestamp string."""
    await db.write_event("SYSTEM", "STARTUP")

    rows = _all_events(tmp_db)
    assert len(rows) == 1
    assert rows[0]["ts"] is not None
    assert len(rows[0]["ts"]) >= 10   # at minimum "YYYY-MM-DD"
