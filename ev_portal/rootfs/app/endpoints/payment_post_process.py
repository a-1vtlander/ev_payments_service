import asyncio
import html
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import state

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/payment_post_process", response_class=HTMLResponse)
async def payment_post_process(
    uid: str = "",
    order_id: str = "",
):
    """
    Square redirects the customer here after payment is completed.

    Flow:
      1. Validate ``uid`` against _pending_sessions (anti-spoof check).
      2. Publish an authorize_session MQTT request using booking_id as
         idempotency key.
      3. Wait up to RESPONSE_TIMEOUT seconds for a response on
         authorize_session/response.
      4. If ``{success: true}`` → render "charger enabled" page.
         Otherwise → render error page with the broker response.
    """
    log.info("payment_post_process: uid=%r order_id=%r", uid, order_id)

    # ── 1. Validate session UID ────────────────────────────────────────────
    if not uid:
        log.warning("payment_post_process: missing uid")
        return HTMLResponse(
            content="<h2>Invalid request</h2><p>Missing session token.</p>",
            status_code=400,
        )

    session = state._pending_sessions.pop(uid, None)
    if session is None:
        log.warning("payment_post_process: unknown or already-used uid=%r", uid)
        return HTMLResponse(
            content=(
                "<h2>Session not found</h2>"
                "<p>This payment link has already been processed or has expired.</p>"
            ),
            status_code=400,
        )

    booking_id    = session["booking_id"]
    payment_token = session["payment_token"]
    log.info(
        "payment_post_process validated: uid=%s booking_id=%s payment_token=%s order_id=%s",
        uid, booking_id, payment_token, order_id,
    )

    # ── 2. Guard: MQTT must be connected ───────────────────────────────────
    if not (state.mqtt_client and state.mqtt_client.is_connected()):
        log.error("MQTT not connected – cannot authorize session")
        return HTMLResponse(
            content="Service temporarily unavailable: MQTT not connected",
            status_code=503,
        )

    if state._session_lock is None:
        return HTMLResponse(content="Server not fully initialised", status_code=503)

    home_id    = state._app_config.get("home_id",    "base_lander")
    charger_id = state._app_config.get("charger_id", "chargepoint:home:charger:1")

    async with state._session_lock:
        # Drain any stale messages from a previous timed-out authorize.
        auth_q = state._topic_queues.get(state._authorize_response_topic)
        if auth_q:
            while not auth_q.empty():
                auth_q.get_nowait()

        # ── 3. Publish authorize_session request ───────────────────────────
        payload = json.dumps({
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "home_id":    home_id,
            "charger_id": charger_id,
            "booking_id": booking_id,      # idempotency key
            "order_id":   order_id,        # Square order ID for reconciliation
            "payment_token": payment_token,
        }, indent=2)

        result = state.mqtt_client.publish(
            state._authorize_request_topic, payload, qos=1
        )
        if result.rc != 0:
            log.error(
                "MQTT publish failed for %s: rc=%s",
                state._authorize_request_topic, result.rc,
            )
            return HTMLResponse(
                content="Failed to send authorize request to charger", status_code=503
            )

        log.info("Publishing authorize_session to %s:\n%s", state._authorize_request_topic, payload)
        log.info(
            "Waiting up to %.0fs for response on %s",
            state.RESPONSE_TIMEOUT, state._authorize_response_topic,
        )

        # ── 4. Wait for authorize response ─────────────────────────────────
        try:
            response_raw = await asyncio.wait_for(
                auth_q.get(), timeout=state.RESPONSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            safe_topic = html.escape(state._authorize_response_topic)
            return HTMLResponse(
                content=(
                    f"<h1>Charger did not respond</h1>"
                    f"<p>Timed out after {int(state.RESPONSE_TIMEOUT)}s waiting on "
                    f"<code>{safe_topic}</code></p>"
                ),
                status_code=504,
            )

    # ── Parse authorize response ───────────────────────────────────────────
    try:
        auth_parsed = json.loads(response_raw)
    except (json.JSONDecodeError, ValueError):
        auth_parsed = {}
        log.warning("Authorize response was not valid JSON: %s", response_raw)

    log.info("Authorize response: %s", auth_parsed)

    # ── 5a. Success ────────────────────────────────────────────────────────
    if auth_parsed.get("success") is True:
        safe_booking = html.escape(booking_id)
        safe_order   = html.escape(order_id) if order_id else "<em>not provided</em>"
        safe_token   = html.escape(payment_token)
        return HTMLResponse(content=f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Charger Ready</title>
  <style>
    body {{ font-family: sans-serif; max-width: 520px; margin: 60px auto;
            padding: 0 1rem; color: #222; text-align: center; }}
    .icon {{ font-size: 4rem; margin-bottom: .5rem; }}
    h1 {{ font-size: 1.6rem; color: #1a7f3c; margin-bottom: .4rem; }}
    .sub {{ color: #555; margin-bottom: 2rem; font-size: 1.05rem; }}
    .card {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px;
             padding: 1rem 1.2rem; margin-bottom: 1rem; text-align: left; }}
    .label {{ font-size: .75rem; text-transform: uppercase; letter-spacing: .05em;
              color: #777; margin-bottom: .2rem; }}
    .value {{ font-family: monospace; font-size: .95rem; word-break: break-all; }}
    .note  {{ font-size: .82rem; color: #777; margin-top: 1.5rem; }}
  </style>
</head>
<body>
  <div class="icon">&#9889;</div>
  <h1>EV Charger Enabled</h1>
  <p class="sub">Your payment has been authorised.<br>
  You can now plug in your car.</p>

  <div class="card">
    <div class="label">Booking ID</div>
    <div class="value">{safe_booking}</div>
  </div>
  <div class="card">
    <div class="label">Square order ID</div>
    <div class="value">{safe_order}</div>
  </div>
  <div class="card">
    <div class="label">Payment token</div>
    <div class="value">{safe_token}</div>
  </div>

  <p class="note">This is a pre-authorisation hold. Your card will be charged
  for the actual energy consumed after your session ends; any difference will
  be adjusted or refunded automatically.</p>
</body>
</html>
""")

    # ── 5b. Failure ────────────────────────────────────────────────────────
    safe_response = html.escape(response_raw)
    log.error("Authorize session failed: %s", response_raw)
    return HTMLResponse(
        content=(
            "<html><body style='font-family:sans-serif;max-width:600px;margin:40px auto'>"
            "<h2>&#10060; Charger authorisation failed</h2>"
            "<p>The charger responded but did not grant authorisation:</p>"
            f"<pre style='background:#f6f8fa;padding:1rem;border-radius:6px'>{safe_response}</pre>"
            "<p><a href='/'>&#8592; Home</a></p>"
            "</body></html>"
        ),
        status_code=502,
    )
