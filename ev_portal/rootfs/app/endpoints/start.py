import asyncio
import html
import httpx
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import state
import square

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/start", response_class=HTMLResponse)
async def start_session(request: Request):
    # ── Guard: MQTT must be connected ─────────────────────────────────────
    if not (state.mqtt_client and state.mqtt_client.is_connected()):
        log.warning("MQTT not connected – rejecting /start")
        return HTMLResponse(
            content="Service temporarily unavailable: MQTT not connected",
            status_code=503,
        )

    if state._session_lock is None:
        return HTMLResponse(content="Server not fully initialised", status_code=503)

    if state._session_lock.locked():
        return HTMLResponse(
            content="A session request is already in progress – please wait and try again",
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

    # ── Parse booking response ─────────────────────────────────────────────
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

    # ── Validate Square config ─────────────────────────────────────────────
    if not state._square_config.get("access_token"):
        return HTMLResponse(content="Square access token not configured", status_code=503)
    if not state._square_config.get("location_id"):
        return HTMLResponse(
            content="square_location_id is not set in options – update your config and restart",
            status_code=503,
        )

    # ── Generate one-time session UID for CSRF/spoof protection ───────────
    session_uid  = str(uuid.uuid4())
    redirect_url = str(request.base_url) + f"payment_post_process?uid={session_uid}"
    log.info(
        "Session UID %s → booking_id=%s  redirect_url=%s",
        session_uid, booking_id, redirect_url,
    )

    # ── Call Square ────────────────────────────────────────────────────────
    try:
        payment_url, payment_token = await square.create_payment_link(
            booking_id, amount_cents, redirect_url
        )
    except httpx.HTTPStatusError as exc:
        http_status = exc.response.status_code
        log.error("Square API error %s: %s", http_status, exc.response.text)
        try:
            errors = exc.response.json().get("errors", [])
        except Exception:
            errors = []
        if errors:
            rows = "".join(
                f"<tr><td>{html.escape(str(e.get('category', '')))}</td>"
                f"<td><strong>{html.escape(str(e.get('code', '')))}</strong></td>"
                f"<td>{html.escape(str(e.get('detail', '')))}</td></tr>"
                for e in errors
            )
            error_body = (
                "<table><thead><tr>"
                "<th>Category</th><th>Code</th><th>Detail</th>"
                "</tr></thead>"
                f"<tbody>{rows}</tbody></table>"
            )
        else:
            error_body = f"<pre>{html.escape(exc.response.text)}</pre>"
        return HTMLResponse(
            content=(
                "<html><body style='font-family:sans-serif;max-width:600px;margin:40px auto'>"
                f"<h2>Payment setup failed</h2>"
                f"<p>Square returned HTTP <strong>{http_status}</strong>:</p>"
                "<style>table{border-collapse:collapse;width:100%}"
                "th,td{border:1px solid #ccc;padding:6px 10px;text-align:left}"
                "th{background:#f0f0f0}</style>"
                f"{error_body}"
                "</body></html>"
            ),
            status_code=502,
        )
    except Exception as exc:
        log.error("Unexpected error calling Square: %s", exc)
        return HTMLResponse(
            content=f"Payment setup failed: {html.escape(str(exc))}", status_code=502
        )

    # Store session for post-process validation (consumed once on return).
    state._pending_sessions[session_uid] = {
        "booking_id":    booking_id,
        "payment_token": payment_token,
    }

    safe_url     = html.escape(payment_url)
    safe_token   = html.escape(payment_token)
    safe_booking = html.escape(booking_id)
    amount_dollars = amount_cents / 100

    return HTMLResponse(content=f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="3;url={safe_url}">
  <title>Processing EV Charging Payment\u2026</title>
  <style>
    body {{ font-family: sans-serif; max-width: 520px; margin: 60px auto;
            padding: 0 1rem; color: #222; }}
    h1   {{ font-size: 1.4rem; margin-bottom: .4rem; }}
    .sub {{ color: #555; margin-bottom: 1.5rem; }}
    .card {{ background: #f6f8fa; border: 1px solid #d0d7de;
             border-radius: 8px; padding: 1rem 1.2rem; margin-bottom: 1.5rem; }}
    .label {{ font-size: .75rem; text-transform: uppercase;
              letter-spacing: .05em; color: #777; margin-bottom: .2rem; }}
    .value {{ font-family: monospace; font-size: .95rem; word-break: break-all; }}
    .btn {{ display: inline-block; padding: .6rem 1.2rem; background: #006aff;
            color: #fff; border-radius: 6px; text-decoration: none;
            font-weight: 600; }}
    .btn:hover {{ background: #0055cc; }}
    .note {{ font-size: .82rem; color: #777; margin-top: 1.5rem; }}
    progress {{ width: 100%; height: 6px; margin-bottom: 1.5rem; }}
  </style>
</head>
<body>
  <h1>Redirecting to payment\u2026</h1>
  <p class="sub">Please complete the authorization hold to start your EV charging session.</p>
  <progress id="bar" max="3" value="0"></progress>

  <div class="card">
    <div class="label">Booking ID</div>
    <div class="value">{safe_booking}</div>
  </div>
  <div class="card">
    <div class="label">Authorization amount</div>
    <div class="value">${amount_dollars:.2f} USD (hold only \u2014 final charge adjusted after session)</div>
  </div>
  <div class="card">
    <div class="label">Payment token</div>
    <div class="value">{safe_token}</div>
  </div>

  <a class="btn" href="{safe_url}">Continue to payment &rarr;</a>

  <p class="note">You will be redirected automatically in 3 seconds.<br>
  This is an authorization hold.  Your card will not be fully charged until
  after your session ends; any unused amount will be refunded.</p>

  <script>
    var elapsed = 0;
    var bar = document.getElementById('bar');
    var iv = setInterval(function() {{
      elapsed += 1;
      bar.value = elapsed;
      if (elapsed >= 3) {{
        clearInterval(iv);
        window.location.href = "{safe_url}";
      }}
    }}, 1000);
  </script>
</body>
</html>
""")
