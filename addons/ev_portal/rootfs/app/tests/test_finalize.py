"""
Unit tests for finalize._handle_finalize.

Square API calls and DB writes are mocked.  Tests exercise:
  - JSON parse errors
  - Missing fields
  - Session not found
  - Already CAPTURED / VOIDED guards
  - Missing square_payment_id
  - Happy-path capture (amount > 0, amount <= authorized)
  - Happy-path void (amount == 0)
  - Overcharge path (final > authorized): void pre-auth then direct charge
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


# ---------------------------------------------------------------------------
# Overcharge path – final_amount_cents > authorized_amount_cents
# ---------------------------------------------------------------------------

_BASE_ROW_WITH_CARD = {
    **_BASE_ROW,
    "authorized_amount_cents": 500,
    "square_card_id":          "card_abc",
    "square_customer_id":      "cust_xyz",
}


async def test_overcharge_voids_then_direct_charges(tmp_db) -> None:
    """When final > authorized: cancel pre-auth then issue a direct charge."""
    row = {**_BASE_ROW_WITH_CARD}
    charge_result = {"id": "pay_direct", "amount_money": {"amount": 750, "currency": "USD"}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})) as mock_cancel,
        patch("square.charge_card_payment", new=AsyncMock(return_value=charge_result)) as mock_charge,
        patch("square.capture_payment", new=AsyncMock()) as mock_capture,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
    ):
        await _handle_finalize(_good_payload(750))
        mock_cancel.assert_called_once_with(payment_id="pay_preauth")
        mock_charge.assert_called_once()
        charge_kwargs = mock_charge.call_args.kwargs
        assert charge_kwargs["amount_cents"] == 750
        assert charge_kwargs["card_id"] == "card_abc"
        assert charge_kwargs["customer_id"] == "cust_xyz"
        mock_capture.assert_not_called()
        mock_captured.assert_called_once_with(
            idempotency_key=IK,
            square_capture_payment_id="pay_direct",
            captured_amount_cents=750,
        )


async def test_overcharge_does_not_trigger_when_equal_to_authorized(tmp_db) -> None:
    """final == authorized uses the normal capture path, not void+recharge."""
    row = {**_BASE_ROW_WITH_CARD}  # authorized_amount_cents=500
    payment_result = {"id": "pay_ok", "amount_money": {"amount": 500}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)) as mock_capture,
        patch("square.cancel_payment", new=AsyncMock()) as mock_cancel,
        patch("square.charge_card_payment", new=AsyncMock()) as mock_charge,
        patch("db.mark_captured", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(500))
        mock_capture.assert_called_once()
        mock_cancel.assert_not_called()
        mock_charge.assert_not_called()


async def test_overcharge_missing_card_calls_mark_failed(tmp_db) -> None:
    """If square_card_id is missing, overcharge must mark_failed without charging."""
    row = {**_BASE_ROW_WITH_CARD, "square_card_id": None}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})),
        patch("square.charge_card_payment", new=AsyncMock()) as mock_charge,
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
    ):
        await _handle_finalize(_good_payload(750))
        mock_charge.assert_not_called()
        mock_failed.assert_called_once()
        assert "overcharge" in mock_failed.call_args.args[1]


async def test_overcharge_void_fails_still_charges(tmp_db) -> None:
    """If the void step fails, the direct charge still proceeds (hold expires on its own)."""
    row = {**_BASE_ROW_WITH_CARD}
    charge_result = {"id": "pay_direct", "amount_money": {"amount": 750}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(side_effect=RuntimeError("void error"))),
        patch("square.charge_card_payment", new=AsyncMock(return_value=charge_result)) as mock_charge,
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(750))
        mock_charge.assert_called_once()
        mock_captured.assert_called_once()
        mock_failed.assert_not_called()


async def test_overcharge_charge_fails_all_retries_calls_mark_failed(tmp_db) -> None:
    """If the direct charge fails all retries after a successful void, mark_failed."""
    row = {**_BASE_ROW_WITH_CARD}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})),
        patch("square.charge_card_payment", new=AsyncMock(side_effect=RuntimeError("charge error"))),
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(750))
        mock_failed.assert_called_once()
        mock_captured.assert_not_called()
        assert "direct charge failed" in mock_failed.call_args.args[1]


async def test_overcharge_charge_retries_on_failure_then_succeeds(tmp_db) -> None:
    """Direct charge retries N-1 times before succeeding on the last attempt."""
    row = {**_BASE_ROW_WITH_CARD}
    charge_result = {"id": "pay_direct", "amount_money": {"amount": 750}}
    call_count = {"n": 0}

    async def flaky_charge(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < _MAX_RETRIES:
            raise RuntimeError("transient charge error")
        return charge_result

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})),
        patch("square.charge_card_payment", new=AsyncMock(side_effect=flaky_charge)),
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(750))
        assert call_count["n"] == _MAX_RETRIES
        mock_captured.assert_called_once()


async def test_overcharge_idempotency_key_is_stable(tmp_db) -> None:
    """The same idempotency key is used for the direct charge on every retry attempt."""
    row = {**_BASE_ROW_WITH_CARD}
    charge_result = {"id": "pay_direct", "amount_money": {"amount": 750}}
    keys_used: list[str] = []

    async def capture_idem_key(**kwargs):
        keys_used.append(kwargs["idempotency_key"])
        return charge_result

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock(return_value={"id": "pay_preauth"})),
        patch("square.charge_card_payment", new=AsyncMock(side_effect=capture_idem_key)),
        patch("db.mark_captured", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(750))
        assert len(keys_used) == 1
        # Key is a 64-char SHA-256 hex digest (stable across retries, unique per amount)
        assert len(keys_used[0]) == 64
        assert keys_used[0] == keys_used[0].lower()
        # Two calls with same booking+amount must produce the same key
        import hashlib
        expected = hashlib.sha256(
            f"fin:{row['idempotency_key']}:750".encode()
        ).hexdigest()
        assert keys_used[0] == expected


# ---------------------------------------------------------------------------
# Apple Pay overcharge path
#
# Apple Pay (and other digital wallets) produce a one-time token that cannot
# be stored as a card-on-file.  The DB row therefore has square_card_id=""
# and square_customer_id="".
#
# Square returns EDIT_AMOUNT_UP in payment_capabilities for wallet payments,
# meaning the pre-auth can be updated in-place above the original authorized
# amount.  The correct path is:
#
#   1. Read payment_capabilities from the DB row.
#   2. If EDIT_AMOUNT_UP is present → PUT /v2/payments/{id} (new amount) +
#      POST /v2/payments/{id}/complete to capture.  Do NOT void; do NOT
#      attempt a direct recharge.
#   3. If EDIT_AMOUNT_UP is absent and no stored card → mark_failed.
#
# These tests document the required behaviour.  Tests that depend on the
# EDIT_AMOUNT_UP branch will FAIL until finalize.py is updated to check
# payment_capabilities.
# ---------------------------------------------------------------------------

# Wallet row: no stored card/customer but EDIT_AMOUNT_UP advertised by Square.
_BASE_ROW_WALLET = {
    **_BASE_ROW,
    "authorized_amount_cents": 2000,
    "square_card_id":          "",
    "square_customer_id":      "",
    "payment_capabilities":    '["EDIT_AMOUNT_UP"]',
    "payment_version_token":   "tok_abc",
}


async def test_apple_pay_overcharge_uses_capture_not_direct_charge(tmp_db) -> None:
    """
    Apple Pay overcharge with EDIT_AMOUNT_UP must use PUT+capture,
    NOT void+recharge.  charge_card_payment must never be called.
    """
    row = {**_BASE_ROW_WALLET}
    payment_result = {"id": "pay_preauth", "amount_money": {"amount": 2941}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)) as mock_capture,
        patch("square.cancel_payment", new=AsyncMock()) as mock_cancel,
        patch("square.charge_card_payment", new=AsyncMock()) as mock_charge,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
    ):
        await _handle_finalize(_good_payload(2941))
        mock_capture.assert_called_once_with(
            payment_id="pay_preauth",
            final_amount_cents=2941,
        )
        mock_cancel.assert_not_called()
        mock_charge.assert_not_called()
        mock_failed.assert_not_called()
        mock_captured.assert_called_once()


async def test_apple_pay_overcharge_captures_at_final_amount(tmp_db) -> None:
    """
    The amount passed to capture_payment must be the final billed amount,
    not the original authorized amount.
    """
    row = {**_BASE_ROW_WALLET}
    payment_result = {"id": "pay_preauth", "amount_money": {"amount": 2941}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)) as mock_capture,
        patch("square.cancel_payment", new=AsyncMock()),
        patch("square.charge_card_payment", new=AsyncMock()),
        patch("db.mark_captured", new=AsyncMock()),
    ):
        await _handle_finalize(_good_payload(2941))
        assert mock_capture.call_args.kwargs["final_amount_cents"] == 2941


async def test_apple_pay_overcharge_mark_captured_with_correct_values(tmp_db) -> None:
    """
    After a successful EDIT_AMOUNT_UP capture, mark_captured must be called
    with the payment_id and the final captured amount.
    """
    row = {**_BASE_ROW_WALLET}
    payment_result = {"id": "pay_preauth", "amount_money": {"amount": 2941}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)),
        patch("square.cancel_payment", new=AsyncMock()),
        patch("square.charge_card_payment", new=AsyncMock()),
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
    ):
        await _handle_finalize(_good_payload(2941))
        mock_captured.assert_called_once_with(
            idempotency_key=IK,
            square_capture_payment_id="pay_preauth",
            captured_amount_cents=2941,
        )


async def test_apple_pay_overcharge_without_edit_amount_up_captures_authorized_amount(tmp_db) -> None:
    """
    If EDIT_AMOUNT_UP is NOT in capabilities and there is no stored card,
    the authorized amount must be captured (partial capture) and a
    session_errors MQTT message published.  mark_failed must NOT be called.
    charge_card_payment must not be called.
    """
    row = {
        **_BASE_ROW_WALLET,
        "authorized_amount_cents": 2000,
        "payment_capabilities": "[]",  # Square did not grant EDIT_AMOUNT_UP
    }
    capture_result = {"id": "pay_preauth", "amount_money": {"amount": 2000}}
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = True

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock()),
        patch("square.capture_payment", new=AsyncMock(return_value=capture_result)) as mock_capture,
        patch("square.charge_card_payment", new=AsyncMock()) as mock_charge,
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("state.mqtt_client", mock_mqtt),
        patch("state._session_errors_topic", "ev/charger/home/charger/booking/session_errors"),
    ):
        await _handle_finalize(_good_payload(2941))
        # Captured at the authorized amount (not the final amount)
        mock_capture.assert_called_once_with(
            payment_id="pay_preauth",
            final_amount_cents=2000,
        )
        mock_captured.assert_called_once()
        assert mock_captured.call_args.kwargs["captured_amount_cents"] == 2000
        # session_errors published with shortfall info
        mock_mqtt.publish.assert_called_once()
        published_topic, published_payload = mock_mqtt.publish.call_args.args[:2]
        assert published_topic == "ev/charger/home/charger/booking/session_errors"
        payload = json.loads(published_payload)
        assert payload["shortfall_cents"] == 941
        assert payload["authorized_cents"] == 2000
        assert payload["booking_id"] == TEST_BOOKING_ID
        # No failure
        mock_failed.assert_not_called()
        mock_charge.assert_not_called()


async def test_apple_pay_overcharge_without_edit_amount_up_mqtt_disconnected_still_captures(tmp_db) -> None:
    """
    If MQTT is not connected when publishing session_errors, the partial
    capture must still have happened and be marked captured.
    """
    row = {
        **_BASE_ROW_WALLET,
        "authorized_amount_cents": 2000,
        "payment_capabilities": "[]",
    }
    capture_result = {"id": "pay_preauth", "amount_money": {"amount": 2000}}
    mock_mqtt = MagicMock()
    mock_mqtt.is_connected.return_value = False

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.cancel_payment", new=AsyncMock()),
        patch("square.capture_payment", new=AsyncMock(return_value=capture_result)),
        patch("square.charge_card_payment", new=AsyncMock()),
        patch("db.mark_failed", new=AsyncMock()) as mock_failed,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
        patch("state.mqtt_client", mock_mqtt),
        patch("state._session_errors_topic", "ev/charger/home/charger/booking/session_errors"),
    ):
        await _handle_finalize(_good_payload(2941))
        mock_captured.assert_called_once()
        mock_mqtt.publish.assert_not_called()
        mock_failed.assert_not_called()


async def test_apple_pay_normal_capture_not_affected(tmp_db) -> None:
    """
    When final_amount_cents <= authorized_amount_cents for an Apple Pay session,
    the normal capture path (PUT+complete) must be used regardless of capabilities.
    No void, no direct charge.
    """
    row = {**_BASE_ROW_WALLET, "authorized_amount_cents": 2000}
    payment_result = {"id": "pay_preauth", "amount_money": {"amount": 1800}}

    with (
        patch("db.get_session_by_booking_id", new=AsyncMock(return_value=row)),
        patch("square.capture_payment", new=AsyncMock(return_value=payment_result)) as mock_capture,
        patch("square.cancel_payment", new=AsyncMock()) as mock_cancel,
        patch("square.charge_card_payment", new=AsyncMock()) as mock_charge,
        patch("db.mark_captured", new=AsyncMock()) as mock_captured,
    ):
        await _handle_finalize(_good_payload(1800))
        mock_capture.assert_called_once_with(
            payment_id="pay_preauth",
            final_amount_cents=1800,
        )
        mock_cancel.assert_not_called()
        mock_charge.assert_not_called()
        mock_captured.assert_called_once()

