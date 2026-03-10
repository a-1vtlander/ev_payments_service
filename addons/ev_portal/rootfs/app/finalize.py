"""
finalize.py
-----------
Background asyncio task that drains the finalize_session MQTT topic queue.

Expected MQTT payload (JSON):
    {
        "booking_id": "<booking-id>",
        "final_amount_cents": 2500
    }

Flow:
  1. Deserialise payload.
  2. Look up the session row by booking_id.
  3. Guard: skip if already CAPTURED or VOIDED.
  4a. final_amount_cents == 0               → void pre-auth, mark VOIDED.
  4b. final_amount_cents <= authorized_amt  → PUT amount + POST /complete (capture).
  4c. final_amount_cents >  authorized_amt  → void pre-auth, then POST /v2/payments
                                               as a direct charge (autocomplete=True).
  5. On success  → db.mark_captured() / db.mark_voided()
     On exhausted retries → db.mark_failed()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Optional

import db
import square
import state

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY_S = 5.0

# HTTP 4xx errors from Square are client-side mistakes (bad idempotency key,
# invalid card, etc.) and will never succeed on a retry.  Only 5xx / network
# errors are worth retrying.
_CLIENT_ERR_RE = re.compile(r"error (4\d\d):")


def _is_client_error(exc: Exception) -> bool:
    """Return True if the exception represents a Square 4xx response."""
    return bool(_CLIENT_ERR_RE.search(str(exc)))


async def _handle_finalize(payload_str: str) -> None:
    """Process a single finalize_session message."""
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError as exc:
        log.error("finalize_session: invalid JSON payload: %s — %s", payload_str, exc)
        return

    booking_id: Optional[str] = payload.get("booking_id")
    final_amount_cents: Optional[int] = payload.get("final_amount_cents")

    if not booking_id or final_amount_cents is None:
        log.error(
            "finalize_session: missing booking_id or final_amount_cents in payload: %s",
            payload,
        )
        return

    log.info(
        "finalize_session: booking_id=%r  final_amount_cents=%d",
        booking_id,
        final_amount_cents,
    )

    try:
        row = await db.get_session_by_booking_id(booking_id)
    except Exception:
        log.exception("finalize_session: DB lookup failed for booking_id=%r", booking_id)
        return

    if row is None:
        log.warning("finalize_session: no session found for booking_id=%r — ignoring", booking_id)
        return

    idempotency_key: str = row["idempotency_key"]
    current_state: str = row.get("state", "")

    log.info(
        "finalize_session: row found — key=%r  state=%r  payment_id=%r  "
        "authorized_cents=%d  card_id=%r  customer_id=%r",
        idempotency_key,
        current_state,
        row.get("square_payment_id"),
        row.get("authorized_amount_cents") or 0,
        row.get("square_card_id"),
        row.get("square_customer_id"),
    )

    if current_state in ("CAPTURED", "VOIDED"):
        log.info(
            "finalize_session: session %r already %s — skipping",
            idempotency_key, current_state,
        )
        return

    if current_state not in ("AUTHORIZED",):
        log.warning(
            "finalize_session: session %r is in state %r (expected AUTHORIZED) — proceeding anyway",
            idempotency_key,
            current_state,
        )

    square_payment_id: Optional[str] = row.get("square_payment_id")
    if not square_payment_id:
        log.error(
            "finalize_session: session %r has no square_payment_id — cannot capture; "
            "full row state=%r  card_id=%r  booking_id=%r",
            idempotency_key,
            current_state,
            row.get("square_card_id"),
            booking_id,
        )
        await db.mark_failed(idempotency_key, "missing square_payment_id for capture")
        return

    # ── Zero-amount: void the pre-auth hold, no charge ───────────────────
    if final_amount_cents == 0:
        log.info(
            "finalize_session: final_amount_cents=0, voiding pre-auth  payment_id=%r",
            square_payment_id,
        )
        last_error = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                void_result = await square.cancel_payment(payment_id=square_payment_id)
                voided_id: str = void_result.get("id", square_payment_id)
                await db.mark_voided(
                    idempotency_key=idempotency_key,
                    square_payment_id=voided_id,
                )
                log.info(
                    "finalize_session: VOIDED  idempotency_key=%r  payment_id=%r",
                    idempotency_key, voided_id,
                )
                return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                log.warning("finalize_session: void attempt %d failed: %s", attempt, exc)
                if _is_client_error(exc):
                    log.error(
                        "finalize_session: void failed with non-retryable client error — aborting retries"
                    )
                    break
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_S)
        log.error(
            "finalize_session: all %d void attempts failed for %r — last error: %s",
            _MAX_RETRIES, idempotency_key, last_error,
        )
        await db.mark_failed(idempotency_key, f"void failed after {_MAX_RETRIES} attempts: {last_error}")
        return

    # ── Non-zero: capture (or void+recharge if final exceeds pre-auth) ────
    authorized_amount_cents: int = row.get("authorized_amount_cents") or 0
    exceeds_preauth = final_amount_cents > authorized_amount_cents

    if exceeds_preauth:
        # ── Check whether the payment supports in-place amount updates ────
        # Square returns EDIT_AMOUNT_UP in payment_capabilities for wallet
        # payments (Apple Pay, Google Pay).  When present we can capture above
        # the authorized amount by calling capture_payment with the higher
        # amount directly — no void needed, no stored card required.
        try:
            capabilities: list = json.loads(row.get("payment_capabilities") or "[]")
        except (ValueError, TypeError):
            capabilities = []

        can_edit_amount_up = "EDIT_AMOUNT_UP" in capabilities

        if can_edit_amount_up:
            log.warning(
                "finalize_session: final_amount_cents=%d exceeds authorized=%d for %r "
                "— EDIT_AMOUNT_UP present, capturing at final amount directly",
                final_amount_cents,
                authorized_amount_cents,
                idempotency_key,
            )
            # Fall through to the normal capture path below (which already
            # calls capture_payment with final_amount_cents).
            pass

        else:
            log.warning(
                "finalize_session: final_amount_cents=%d exceeds authorized=%d for %r "
                "— voiding pre-auth and issuing direct charge",
                final_amount_cents,
                authorized_amount_cents,
                idempotency_key,
            )

            # ── Step A: best-effort void of the pre-auth ──────────────────
            # Failure here is non-fatal — the hold will expire on its own.
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    await square.cancel_payment(payment_id=square_payment_id)
                    log.info(
                        "finalize_session: pre-auth voided for overcharge  payment_id=%r",
                        square_payment_id,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "finalize_session: void attempt %d/%d failed (non-fatal, hold will expire): %s",
                        attempt, _MAX_RETRIES, exc,
                    )
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_DELAY_S)

            # ── Step B: direct charge for final amount ─────────────────────
            square_card_id     = row.get("square_card_id")
            square_customer_id = row.get("square_customer_id")
            log.info(
                "finalize_session: direct-charge prerequisites — card_id=%r  customer_id=%r  "
                "booking_id=%r  final_amount_cents=%d",
                square_card_id, square_customer_id, booking_id, final_amount_cents,
            )
            if not square_card_id or not square_customer_id:
                # No stored card and no EDIT_AMOUNT_UP — cannot recover the
                # full amount.  Capture what was authorized, mark as captured,
                # and publish a session_errors notification so HA/owner knows
                # the shortfall requires manual follow-up.
                shortfall = final_amount_cents - authorized_amount_cents
                log.error(
                    "finalize_session: wallet overcharge with no recovery path — "
                    "capturing authorized amount %d cents (shortfall %d cents unpaid)  "
                    "key=%r  booking_id=%r",
                    authorized_amount_cents, shortfall, idempotency_key, booking_id,
                )
                try:
                    payment = await square.capture_payment(
                        payment_id=square_payment_id,
                        final_amount_cents=authorized_amount_cents,
                    )
                    captured_id: str = payment.get("id", square_payment_id)
                    await db.mark_captured(
                        idempotency_key=idempotency_key,
                        square_capture_payment_id=captured_id,
                        captured_amount_cents=authorized_amount_cents,
                    )
                    log.error(
                        "finalize_session: PARTIAL CAPTURE — charged %d cents, "
                        "%d cents shortfall requires manual collection  booking_id=%r",
                        authorized_amount_cents, shortfall, booking_id,
                    )
                except Exception as capture_exc:  # noqa: BLE001
                    log.exception(
                        "finalize_session: partial capture also failed for %r: %s",
                        idempotency_key, capture_exc,
                    )
                    await db.mark_failed(
                        idempotency_key,
                        f"wallet overcharge: partial capture failed: {capture_exc}",
                    )
                # Publish session_errors regardless of capture success so HA
                # always receives a notification about the shortfall.
                if state.mqtt_client and state.mqtt_client.is_connected():
                    error_payload = json.dumps({
                        "booking_id":            booking_id,
                        "error":                 "wallet_overcharge_partial_capture",
                        "authorized_cents":      authorized_amount_cents,
                        "final_cents":           final_amount_cents,
                        "shortfall_cents":       shortfall,
                        "message": (
                            f"Apple Pay/wallet overcharge: charged {authorized_amount_cents} cents "
                            f"(authorized), {shortfall} cents shortfall requires manual collection."
                        ),
                    })
                    state.mqtt_client.publish(
                        state._session_errors_topic, error_payload, qos=1
                    )
                    log.error(
                        "finalize_session: published session_errors for shortfall  "
                        "topic=%r  shortfall_cents=%d  booking_id=%r",
                        state._session_errors_topic, shortfall, booking_id,
                    )
                else:
                    log.error(
                        "finalize_session: MQTT not connected — could not publish session_errors "
                        "for booking_id=%r  shortfall_cents=%d",
                        booking_id, shortfall,
                    )
                return

            # Idempotency key includes the final amount so that a re-finalize at a
            # different amount never collides with a prior attempt at this booking.
            # SHA-256 hex is 64 chars; Square allows up to 128 — no truncation needed.
            charge_idem = hashlib.sha256(
                f"fin:{idempotency_key}:{final_amount_cents}".encode()
            ).hexdigest()
            log.info(
                "finalize_session: direct-charge idempotency_key=%r  (derived from key+amount)",
                charge_idem,
            )

            last_error: Optional[str] = None
            for attempt in range(1, _MAX_RETRIES + 1):
                log.info(
                    "finalize_session: direct-charge attempt %d/%d  amount=%d cents",
                    attempt, _MAX_RETRIES, final_amount_cents,
                )
                try:
                    payment = await square.charge_card_payment(
                        card_id=square_card_id,
                        customer_id=square_customer_id,
                        booking_id=booking_id,
                        amount_cents=final_amount_cents,
                        idempotency_key=charge_idem,
                    )
                    charged_id: str    = payment.get("id", "")
                    charged_cents: int = payment.get("amount_money", {}).get(
                        "amount", final_amount_cents
                    )
                    await db.mark_captured(
                        idempotency_key=idempotency_key,
                        square_capture_payment_id=charged_id,
                        captured_amount_cents=charged_cents,
                    )
                    log.info(
                        "finalize_session: OVERCHARGE CAPTURED  idempotency_key=%r  "
                        "charged_id=%r  cents=%d",
                        idempotency_key, charged_id, charged_cents,
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    log.warning(
                        "finalize_session: direct-charge attempt %d failed: %s",
                        attempt, exc,
                    )
                    if _is_client_error(exc):
                        log.error(
                            "finalize_session: direct-charge failed with non-retryable client error "
                            "— aborting retries (idempotency_key=%r)",
                            charge_idem,
                        )
                        break
                    if attempt < _MAX_RETRIES:
                        await asyncio.sleep(_RETRY_DELAY_S)

            log.error(
                "finalize_session: all %d direct-charge attempts failed for %r — %s",
                _MAX_RETRIES, idempotency_key, last_error,
            )
            await db.mark_failed(
                idempotency_key,
                f"direct charge failed after {_MAX_RETRIES} attempts: {last_error}",
            )
            return

    # ── Normal path: capture pre-auth at final amount ──────────────────────
    last_error: Optional[str] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        log.info(
            "finalize_session: capture attempt %d/%d  payment_id=%r  amount=%d cents",
            attempt,
            _MAX_RETRIES,
            square_payment_id,
            final_amount_cents,
        )
        try:
            # capture_payment returns the payment dict directly
            payment = await square.capture_payment(
                payment_id=square_payment_id,
                final_amount_cents=final_amount_cents,
            )
            captured_id: str = payment.get("id", square_payment_id)
            captured_cents: int = (
                payment.get("amount_money", {}).get("amount", final_amount_cents)
            )
            await db.mark_captured(
                idempotency_key=idempotency_key,
                square_capture_payment_id=captured_id,
                captured_amount_cents=captured_cents,
            )
            log.info(
                "finalize_session: CAPTURED  idempotency_key=%r  captured_id=%r  cents=%d",
                idempotency_key,
                captured_id,
                captured_cents,
            )
            return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.warning(
                "finalize_session: capture attempt %d failed: %s", attempt, exc
            )
            if _is_client_error(exc):
                log.error(
                    "finalize_session: capture failed with non-retryable client error "
                    "— aborting retries (payment_id=%r)",
                    square_payment_id,
                )
                break
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAY_S)

    # All retries exhausted
    log.error(
        "finalize_session: all %d capture attempts failed for %r — last error: %s",
        _MAX_RETRIES,
        idempotency_key,
        last_error,
    )
    await db.mark_failed(idempotency_key, f"capture failed after {_MAX_RETRIES} attempts: {last_error}")


async def finalize_session_consumer() -> None:
    """Long-running task: drain state._topic_queues[state._finalize_session_topic]."""
    log.info("finalize_session_consumer: starting")
    try:
        while True:
            # Wait until the topic is known (lifespan sets it before starting this task,
            # but guard against edge cases during testing).
            topic = state._finalize_session_topic
            if not topic:
                await asyncio.sleep(1)
                continue

            queue: asyncio.Queue = state._topic_queues.get(topic)
            if queue is None:
                await asyncio.sleep(1)
                continue

            payload_str: str = await queue.get()
            try:
                await _handle_finalize(payload_str)
            except Exception as exc:  # noqa: BLE001
                log.exception("finalize_session_consumer: unhandled error: %s", exc)
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        log.info("finalize_session_consumer: cancelled — shutting down")
        raise
