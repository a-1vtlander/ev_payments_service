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
import json
import logging
from typing import Optional

import db
import square
import state

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY_S = 5.0


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

    row = await db.get_session_by_booking_id(booking_id)
    if row is None:
        log.warning("finalize_session: no session found for booking_id=%r — ignoring", booking_id)
        return

    idempotency_key: str = row["idempotency_key"]
    current_state: str = row.get("state", "")

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
            "finalize_session: session %r has no square_payment_id — cannot capture",
            idempotency_key,
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
        log.warning(
            "finalize_session: final_amount_cents=%d exceeds authorized=%d for %r "
            "— voiding pre-auth and issuing direct charge",
            final_amount_cents,
            authorized_amount_cents,
            idempotency_key,
        )

        # ── Step A: best-effort void of the pre-auth ──────────────────────
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

        # ── Step B: direct charge for final amount ─────────────────────────
        square_card_id     = row.get("square_card_id")
        square_customer_id = row.get("square_customer_id")
        if not square_card_id or not square_customer_id:
            log.error(
                "finalize_session: missing card/customer for direct charge: "
                "card=%r customer=%r",
                square_card_id, square_customer_id,
            )
            await db.mark_failed(
                idempotency_key,
                f"overcharge: missing card_id={square_card_id!r} or "
                f"customer_id={square_customer_id!r}",
            )
            return

        # Stable idempotency key so Square deduplicates retries.
        charge_idem = f"fin:{idempotency_key}"[:45]

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
