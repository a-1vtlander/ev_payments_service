"""
Unit tests for finalize._handle_finalize.

Square API calls and DB writes are mocked.  Tests exercise:
  - JSON parse errors
  - Missing fields
  - Session not found
  - Already CAPTURED / VOIDED guards
  - Missing square_payment_id
  - Happy-path capture (amount > 0)
  - Happy-path void (amount == 0)
  - Retry logic (fail N-1 times then succeed)
  - Exhausted retries → mark_failed
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import db
import state
from finalize import _handle_finalize, _MAX_RETRIES
from tests.conftest import (
    TEST_BOOKING_ID, TEST_CHARGER_ID, TEST_SESSION_ID,
)

IK = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"

_BASE_ROW = {
    "idempotency_key":         IK,
    "charger_id":              TEST_CHARGER_ID,
    "booking_id":              TEST_BOOKING_ID,
    "session_id":              TEST_SESSION_ID,
    "state":                   "AUTHORIZED",
    "authorized_amount_cents": 500,
    "square_environment":      "sandbox",
    "square_payment_id":       "pay_preauth",
}


def _good_payload(amount: int = 500) -> str:
    return json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": amount})


# ---------------------------------------------------------------------------
# JSON / field validation
# ---------------------------------------------------------------------------

async def test_invalid_json_is_ignored(tmp_db) -> None:
    """Invalid JSON payload must log an error and return without crashing."""
    await _handle_finalize("{not valid json")
    # No exception raised; DB should be empty
    assert await db.get_session(IK) is None


async def test_missing_booking_id_is_ignored(tmp_db) -> None:
    await _handle_finalize(json.dumps({"final_amount_cents": 100}))
    assert await db.get_session(IK) is None


async def test_missing_amount_is_ignored(tmp_db) -> None:
    await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID}))
    assert await db.get_session(IK) is None


async def test_zero_amount_is_valid(tmp_db) -> None:
    """final_amount_cents=0 is valid (void path); must not be treated as missing."""
    row_result = {**_BASE_ROW}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row_result)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})),
        patch("db.mark_voided", new=AsyncMock()) as mock_voided,
    ):
        await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": 0}))
        mock_voided.assert_called_once()


# ---------------------------------------------------------------------------
# Session lookup guards
# ---------------------------------------------------------------------------

async def test_session_not_found_is_ignored(tmp_db) -> None:
    with patch("db.get_session_by_booking_id", new=AsyncMock(return_value=None)):
        await _handle_finalize(_good_payload())
    # nothing crashed, nothing written
    assert await db.get_session(IK) is None


async def test_already_captured_is_skipped(tmp_db) -> None:
    row = {**_BASE_ROW, "state": "CAPTURED"}
    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock()) as mock_capture,
    ):
        await _handle_finalize(_good_payload())
        mock_capture.assert_not_called()


async def test_already_voided_is_skipped(tmp_db) -> None:
    row = {**_BASE_ROW, "state": "VOIDED"}
    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock()) as mock_cancel,
    ):
        await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": 0}))
        mock_cancel.assert_not_called()


async def test_missing_square_payment_id_calls_mark_failed(tmp_db) -> None:
    row = {**_BASE_ROW, "square_payment_id": None}
    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("db.mark_failed", new=AsyncMock()) as mock_fail,
    ):
        await _handle_finalize(_good_payload())
        mock_fail.assert_called_once()
        assert "missing square_payment_id" in mock_fail.call_args.args[1]


# ---------------------------------------------------------------------------
# Capture – happy path (amount > 0)
# ---------------------------------------------------------------------------

async def test_capture_success_calls_mark_captured(tmp_db) -> None:
    row = {**_BASE_ROW}
    payment_result = {"id": "pay_done", "amount_money": {"amount": 500, "currency": "USD"}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)),
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
    ):
        await _handle_finalize(_good_payload(500))
        mock_captured.assert_called_once_with(
            idempotency_key=IK,
            square_capture_payment_id="pay_done",
            captured_amount_cents=500,
        )


async def test_capture_uses_payment_id_from_result(tmp_db) -> None:
    row = {**_BASE_ROW, "square_payment_id": "pay_original"}
    payment_result = {"id": "pay_updated", "amount_money": {"amount": 300}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)),
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
    ):
        await _handle_finalize(_good_payload(300))
        assert mock_captured.call_args.kwargs["square_capture_payment_id"] == "pay_updated"
        assert mock_captured.call_args.kwargs["captured_amount_cents"] == 300


async def test_capture_falls_back_to_original_payment_id_if_result_missing(tmp_db) -> None:
    row = {**_BASE_ROW, "square_payment_id": "pay_fallback"}
    payment_result = {}  # no "id" key

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)),
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
    ):
        await _handle_finalize(_good_payload(100))
        assert mock_captured.call_args.kwargs["square_capture_payment_id"] == "pay_fallback"


# ---------------------------------------------------------------------------
# Void – happy path (amount == 0)
# ---------------------------------------------------------------------------

async def test_void_calls_cancel_payment(tmp_db) -> None:
    row = {**_BASE_ROW}
    cancel_result = {"id": "pay_preauth"}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value=cancel_result)) as mock_cancel,
        patch("db.mark_voided", new=AsyncMock()) as mock_voided,
    ):
        await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": 0}))
        mock_cancel.assert_called_once_with(payment_id="pay_preauth")
        mock_voided.assert_called_once_with(idempotency_key=IK, square_payment_id="pay_preauth")


async def test_void_does_not_call_capture(tmp_db) -> None:
    row = {**_BASE_ROW}
    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})),
        patch("square.capture_payment", new=AsyncMock()) as mock_capture,
        patch("db.mark_voided", new=AsyncMock()),
    ):
        await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": 0}))
        mock_capture.assert_not_called()


# ---------------------------------------------------------------------------
# Retry logic – capture
# ---------------------------------------------------------------------------

async def test_capture_retries_on_failure_then_succeeds(tmp_db) -> None:
    row = {**_BASE_ROW}
    payment_result = {"id": "pay_ok", "amount_money": {"amount": 200}}
    call_count = {"n": 0}

    async def flaky_capture(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < _MAX_RETRIES:
            raise RuntimeError("transient error")
        return payment_result

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(side_effect=flaky_capture)),
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("asyncio.sleep", new=AsyncMock()),   # skip real delays
    ):
        await _handle_finalize(_good_payload(200))
        assert call_count["n"] == _MAX_RETRIES
        mock_captured.assert_called_once()


async def test_capture_all_retries_exhausted_calls_mark_failed(tmp_db) -> None:
    row = {**_BASE_ROW}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(side_effect=RuntimeError("always fails"))),
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(200))
        mock_failed.assert_called_once()
        mock_captured.assert_not_called()
        assert "capture failed" in mock_failed.call_args.args[1]


# ---------------------------------------------------------------------------
# Retry logic – void
# ---------------------------------------------------------------------------

async def test_void_retries_on_failure_then_succeeds(tmp_db) -> None:
    row = {**_BASE_ROW}
    cancel_result = {"id": "pay_preauth"}
    call_count = {"n": 0}

    async def flaky_cancel(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < _MAX_RETRIES:
            raise RuntimeError("transient void error")
        return cancel_result

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(side_effect=flaky_cancel)),
        patch("db.mark_voided", new=AsyncMock()) as mock_voided,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": 0}))
        assert call_count["n"] == _MAX_RETRIES
        mock_voided.assert_called_once()


async def test_void_all_retries_exhausted_calls_mark_failed(tmp_db) -> None:
    row = {**_BASE_ROW}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(side_effect=RuntimeError("always fails"))),
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
        patch("db.mark_voided", new=AsyncMock()) as mock_voided,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _handle_finalize(json.dumps({"booking_id": TEST_BOOKING_ID, "final_amount_cents": 0}))
        mock_failed.assert_called_once()
        mock_voided.assert_not_called()
        assert "void failed" in mock_failed.call_args.args[1]
