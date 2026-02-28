import asyncio
import db
import httpx
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

import state
import square

log = logging.getLogger(__name__)
router = APIRouter()


def _parse_square_error(exc):
    """Extract a human-readable message from a Square API error."""
    try:
        body = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        errors = json.loads(body[body.index("{"):]).get("errors", [])
        msg = "; ".join(e.get("detail") or e.get("code") or "Unknown error" for e in errors)
        return msg or body
    except Exception:
        return str(exc)


# Digital wallet methods that arrive as one-time tokens – must NOT go via
# POST /v2/cards.  The token is passed directly as source_id to /v2/payments.
_DIGITAL_WALLET_METHODS = {"APPLE_PAY", "GOOGLE_PAY"}


@router.post("/submit_payment")
async def submit_payment(
    source_id: str = Form(...),
    uid: str = Form(...),
    given_name: str = Form(...),
    family_name: str = Form(...),
    payment_method: str = Form(""),
):
    try:
        return await _submit_payment_impl(
            source_id, uid, given_name, family_name,
            payment_method=payment_method.upper().strip(),
        )
    except Exception as exc:
        log.exception("Unhandled error in submit_payment")
        return JSONResponse(
            {"status": "error", "message": f"Unexpected server error: {exc}"},
            status_code=500,
        )


async def _submit_payment_impl(source_id, uid, given_name, family_name, payment_method="CARD"):
    is_wallet = payment_method in _DIGITAL_WALLET_METHODS
    log.info(
        "submit_payment: uid=%r given_name=%r family_name=%r payment_method=%r",
        uid, given_name, family_name, payment_method,
    )

    # 1. Validate session UID; fall back to DB to recover sessions after restart
    session = state._pending_sessions.pop(uid, None)
    if session is None:
        db_row = await db.get_session_by_uid(uid)
        if db_row is not None and db_row["state"] not in ("AUTHORIZED", "CAPTURED"):
            session = {
                "booking_id":   db_row["booking_id"],
                "amount_cents": db_row["authorized_amount_cents"],
            }
            log.info("submit_payment: recovered session from DB for uid=%r", uid)
        else:
            log.warning("submit_payment: unknown or already-used uid=%r", uid)
            return JSONResponse(
                {"status": "error", "message": "Session not found or already used. Please start again."},
                status_code=400,
            )

    booking_id      = session["booking_id"]
    amount_cents    = session["amount_cents"]
    charger_id      = state._app_config.get("charger_id", "")
    idempotency_key = f"ev:{charger_id}:{booking_id}"
    log.info("booking_id=%s amount_cents=%s idempotency_key=%s", booking_id, amount_cents, idempotency_key)

    # Idempotency: return existing result without re-calling Square
    existing = await db.get_session(idempotency_key)
    if existing and existing["state"] in ("AUTHORIZED", "CAPTURED"):
        log.info(
            "submit_payment: idempotent return for %s (state=%s payment_id=%s)",
            idempotency_key, existing["state"], existing["square_payment_id"],
        )
        return JSONResponse({
            "status":       "success",
            "booking_id":   booking_id,
            "payment_id":   existing["square_payment_id"],
            "card_id":      existing["square_card_id"],
            "amount_cents": existing["authorized_amount_cents"],
        })

    # Advance to AUTH_REQUESTED (creates row if start.py hasn't yet)
    await db.upsert_session({
        "idempotency_key":         idempotency_key,
        "charger_id":              charger_id,
        "booking_id":              booking_id,
        "session_id":              uid,
        "state":                   "AUTH_REQUESTED",
        "authorized_amount_cents": amount_cents,
        "square_environment":      "sandbox" if state._square_config.get("sandbox") else "production",
    })

    # 2. Tokenise card / charge -- two paths depending on payment method.
    if is_wallet:
        # ── Digital wallet (Apple Pay, Google Pay) ─────────────────────────
        # Wallet tokens are one-time-use and cannot be stored via POST /v2/cards.
        # Pass the token directly as source_id to POST /v2/payments.
        log.info("Digital wallet payment (%s): skipping card-on-file step", payment_method)
        try:
            payment = await square.create_payment_authorization(
                source_id, None, booking_id, amount_cents
            )
        except Exception as exc:
            err_msg = _parse_square_error(exc)
            log.error("Square wallet payment error: %s", exc)
            await db.mark_failed(idempotency_key, err_msg)
            return JSONResponse({"status": "card_error", "message": err_msg})

        # Extract card metadata from the payment response (no stored card/customer).
        card_info = payment.get("card_details", {}).get("card", {})
        card_meta = {
            "square_customer_id": "",
            "square_card_id":     "",
            "card_brand":         card_info.get("card_brand", payment_method),
            "card_last4":         card_info.get("last_4", ""),
            "card_exp_month":     card_info.get("exp_month"),
            "card_exp_year":      card_info.get("exp_year"),
        }
        card_id = ""
    else:
        # ── Card on file (standard card form) ──────────────────────────────
        try:
            card_id, customer_id, card_meta = await square.create_card(
                source_id, booking_id, given_name, family_name
            )
        except Exception as exc:
            err_msg = _parse_square_error(exc)
            log.error("Square create_card error: %s", exc)
            await db.mark_failed(idempotency_key, err_msg)
            return JSONResponse({"status": "card_error", "message": err_msg})

        log.info("Card created: booking_id=%s card_id=%s", booking_id, card_id)

        try:
            payment = await square.create_payment_authorization(
                card_id, customer_id, booking_id, amount_cents
            )
        except Exception as exc:
            err_msg = _parse_square_error(exc)
            log.error("Square create_payment_authorization error: %s", exc)
            await db.mark_failed(idempotency_key, err_msg)
            return JSONResponse({"status": "card_error", "message": err_msg})

    payment_id     = payment["id"]
    payment_status = payment.get("status", "UNKNOWN")
    log.info("Payment created: booking_id=%s payment_id=%s status=%s method=%s",
             booking_id, payment_id, payment_status, payment_method)

    await db.mark_authorized(
        idempotency_key,
        square_payment_id=payment_id,
        authorized_amount_cents=amount_cents,
        **card_meta,
    )

    # 4. MQTT authorize_session
    if not (state.mqtt_client and state.mqtt_client.is_connected()):
        return JSONResponse(
            {"status": "error",
             "message": f"Payment authorised but MQTT not connected. Contact support (booking: {booking_id})."},
            status_code=503,
        )
    if state._session_lock is None:
        return JSONResponse(
            {"status": "error", "message": "Server not fully initialised."},
            status_code=503,
        )

    home_id    = state._app_config.get("home_id", "")
    charger_id = state._app_config.get("charger_id", "")

    async with state._session_lock:
        auth_q = state._topic_queues.get(state._authorize_response_topic)
        if auth_q:
            while not auth_q.empty():
                auth_q.get_nowait()

        mqtt_payload = json.dumps({
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "home_id":      home_id,
            "charger_id":   charger_id,
            "booking_id":   booking_id,
            "payment_id":   payment_id,
            "amount_cents": amount_cents,
        }, indent=2)

        result = state.mqtt_client.publish(state._authorize_request_topic, mqtt_payload, qos=1)
        if result.rc != 0:
            return JSONResponse(
                {"status": "error",
                 "message": f"MQTT publish failed (rc={result.rc}). Payment authorised but charger not notified."},
                status_code=503,
            )

        log.info("Published authorize_session to %s:\n%s", state._authorize_request_topic, mqtt_payload)

        # 5. Wait for authorize_session/response
        try:
            response_raw = await asyncio.wait_for(auth_q.get(), timeout=state.RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"status": "error",
                 "message": (f"Charger did not respond within {int(state.RESPONSE_TIMEOUT)}s. "
                             f"Payment authorised (ID: {payment_id}). Contact support.")},
                status_code=504,
            )

    # Parse MQTT response
    try:
        auth_parsed = json.loads(response_raw)
    except (json.JSONDecodeError, ValueError):
        auth_parsed = {}
        log.warning("Authorize response was not valid JSON: %s", response_raw)

    log.info("Authorize response: %s", auth_parsed)

    # 6. Success
    if auth_parsed.get("success") is True:
        return JSONResponse({
            "status":       "success",
            "booking_id":   booking_id,
            "payment_id":   payment_id,
            "card_id":      card_id,
            "amount_cents": amount_cents,
            "session_url":  f"/session/{uid}",
        })

    # 7. Charger refused
    log.error("Authorize session refused: %s", response_raw)
    return JSONResponse(
        {"status": "error",
         "message": (f"Charger refused the session. "
                     f"Payment authorised (ID: {payment_id}). "
                     f"Response: {response_raw}")},
        status_code=502,
    )
