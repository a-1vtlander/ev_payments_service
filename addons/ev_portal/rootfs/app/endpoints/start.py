import asyncio
import html
import json
import logging
import uuid

import db
from endpoints.session import render_session_page
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import state
import square
from portal_templates import templates

log = logging.getLogger(__name__)


def render_card_form(
    request: Request,
    session_uid: str,
    booking_id: str,
    amount_cents: int,
    *,
    error_message: str = "",
    submit_url: str = "/submit_payment",
    given_name_hint: str = "",
    family_name_hint: str = "",
    booking_is_active: bool = False,
    booking_start_time: str = "",
    booking_end_time: str = "",
    booking_end_display: str = "",
    rate_display: str = "",
):
    """Render the Square Web Payments SDK card form via Jinja2 template.

    Called from GET /start (and /enable-ev-session) and, if needed, from
    POST /submit_payment to redisplay the form with an inline error banner.
    """
    return templates.TemplateResponse(
        request,
        "start.html",
        {
            "booking_id":         booking_id,
            "amount_display":     f"${amount_cents / 100:.2f} USD",
            "amount_cents":       amount_cents,
            "js_url":             square.sdk_js_url(),
            "app_id":             state._square_config.get("app_id", ""),
            "location_id":        state._square_config.get("location_id", ""),
            "session_uid":        session_uid,
            "submit_url":         submit_url,
            "error_message":      error_message,
            "given_name_hint":    given_name_hint,
            "family_name_hint":   family_name_hint,
            "booking_is_active":    booking_is_active,
            "booking_start_time":  booking_start_time,
            "booking_end_time":    booking_end_time,
            "booking_end_display": booking_end_display,
            "rate_display":        rate_display,
        },
    )


router = APIRouter()


@router.get("/start", response_class=HTMLResponse)
async def start_session(request: Request):
    # -- Guard: MQTT must be connected -------------------------------------
    if not (state.mqtt_client and state.mqtt_client.is_connected()):
        log.warning("MQTT not connected - rejecting /start")
        return HTMLResponse(
            content="Service temporarily unavailable: MQTT not connected",
            status_code=503,
        )

    if state._session_lock is None:
        return HTMLResponse(content="Server not fully initialised", status_code=503)

    if state._session_lock.locked():
        return HTMLResponse(
            content="A session request is already in progress - please wait and try again",
            status_code=429,
        )

    async with state._session_lock:
        # Drain any stale messages from a previous timed-out request.
        q = state._topic_queues.get(state._booking_response_topic)
        if q:
            while not q.empty():
                q.get_nowait()

        home_id    = state._app_config.get("home_id",    "base_lander")
        charger_id = state._app_config.get("charger_id", "chargepoint:home:charger:1")
        request_topic = f"ev/charger/{home_id}/{charger_id}/booking/request_session"

        payload = json.dumps({
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "home_id":    home_id,
            "charger_id": charger_id,
        }, indent=2)

        result = state.mqtt_client.publish(request_topic, payload, qos=1)
        if result.rc != 0:
            log.error("MQTT publish failed for %s: rc=%s", request_topic, result.rc)
            return HTMLResponse(content="Failed to send booking request", status_code=503)

        log.info("Publishing booking request to %s:\n%s", request_topic, payload)
        log.info(
            "Waiting up to %.0fs for response on %s",
            state.RESPONSE_TIMEOUT, state._booking_response_topic,
        )

        try:
            response_raw = await asyncio.wait_for(
                q.get(), timeout=state.RESPONSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            safe_topic = html.escape(state._booking_response_topic)
            return HTMLResponse(
                content=(
                    f"<h1>No response received</h1>"
                    f"<p>Timed out after {int(state.RESPONSE_TIMEOUT)}s waiting on "
                    f"<code>{safe_topic}</code></p>"
                ),
                status_code=504,
            )

    # -- Parse booking response ---------------------------------------------
    try:
        parsed = json.loads(response_raw)
    except (json.JSONDecodeError, ValueError):
        parsed = {}
        log.warning("Booking response was not valid JSON: %s", response_raw)

    booking_id = str(parsed.get("booking_id") or "unknown")

    # initial_authorization_amount arrives in dollars (e.g. 1.50 = $1.50).
    # Convert to cents for Square; fall back to configured default (already cents).
    _raw_dollars = parsed.get("initial_authorization_amount")
    if _raw_dollars is not None:
        amount_cents = round(float(_raw_dollars) * 100)
    else:
        amount_cents = int(state._square_config.get("charge_cents", 100))

    # -- Guest / booking display fields ------------------------------------
    booking_is_active = str(parsed.get("booking_is_active", "")).strip().lower() == "on"
    raw_guest_name    = str(parsed.get("guest_name") or "").strip()
    # Strip the reservation code parenthetical, e.g. "Taylor Busch (HMT99KRHA5)" → "Taylor Busch"
    import re as _re
    clean_name = _re.sub(r'\s*\([^)]*\)\s*$', '', raw_guest_name).strip() or raw_guest_name

    if booking_is_active and clean_name:
        name_parts   = clean_name.split(None, 1)
        given_name_hint  = name_parts[0]
        family_name_hint = name_parts[1] if len(name_parts) > 1 else ""
    else:
        given_name_hint  = ""
        family_name_hint = ""

    booking_start_time = str(parsed.get("booking_start_time") or "").strip()
    booking_end_time   = str(parsed.get("booking_end_time")   or "").strip()
    try:
        _end_dt = datetime.strptime(booking_end_time, "%Y-%m-%d %H:%M:%S")
        booking_end_display = _end_dt.strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        booking_end_display = booking_end_time

    rate_per_kwh = parsed.get("rate_per_kwh")
    if rate_per_kwh is not None:
        rate_display = f"${float(rate_per_kwh):.2f}/kWh"
    else:
        rate_display = ""

    # -- Validate Square config ---------------------------------------------
    if not state._square_config.get("access_token"):
        return HTMLResponse(content="Square access token not configured", status_code=503)
    if not state._square_config.get("location_id"):
        return HTMLResponse(
            content="square_location_id is not set in options - update your config and restart",
            status_code=503,
        )

    # -- Check DB: if this booking is already authorized, skip the card form
    idempotency_key = f"ev:{charger_id}:{booking_id}"
    existing        = await db.get_session(idempotency_key)
    if existing and existing["state"] in ("AUTHORIZED", "CAPTURED"):
        log.info("Session already authorized for %s, rendering success page", idempotency_key)
        return render_session_page(request, existing)

    # -- Generate one-time session UID for CSRF/spoof protection -----------
    session_uid = str(uuid.uuid4())
    state._pending_sessions[session_uid] = {
        "booking_id":   booking_id,
        "amount_cents": amount_cents,
    }
    log.info("Session UID %s created for booking_id=%s amount_cents=%s",
             session_uid, booking_id, amount_cents)

    # -- Persist to DB (only if not already authorized) --------------------
    await db.upsert_session({
        "idempotency_key":         idempotency_key,
        "charger_id":              charger_id,
        "booking_id":              booking_id,
        "session_id":              session_uid,
        "state":                   "AWAITING_PAYMENT_INFO",
        "authorized_amount_cents": amount_cents,
        "square_environment":      "sandbox" if state._square_config.get("sandbox") else "production",
        "guest_name":              raw_guest_name,
        "booking_end_time":        booking_end_time,
    })

    # -- Render card form ---------------------------------------------------
    return render_card_form(
        request, session_uid, booking_id, amount_cents,
        submit_url="/submit_payment",
        given_name_hint=given_name_hint,
        family_name_hint=family_name_hint,
        booking_is_active=booking_is_active,
        booking_start_time=booking_start_time,
        booking_end_time=booking_end_time,
        booking_end_display=booking_end_display,
        rate_display=rate_display,
    )

