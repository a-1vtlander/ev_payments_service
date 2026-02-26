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

log = logging.getLogger(__name__)


def render_card_form(
    session_uid: str,
    booking_id: str,
    amount_cents: int,
    *,
    error_message: str = "",
    submit_url: str = "/submit_payment",
) -> HTMLResponse:
    """Render the Square Web Payments SDK card form.

    Can be called from the initial GET /start and also from POST /submit_payment
    to redisplay the form with an inline error banner when card processing fails.
    """
    safe_booking   = html.escape(booking_id)
    amount_dollars = amount_cents / 100
    js_url         = square.sdk_js_url()
    app_id         = html.escape(state._square_config.get("app_id", ""))
    location_id    = html.escape(state._square_config.get("location_id", ""))
    safe_uid       = html.escape(session_uid)
    error_banner   = (
        f'<div class="error-banner">&#9888;&nbsp;{html.escape(error_message)}</div>'
        if error_message else ""
    )

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EV Charging Payment</title>
  <script type="text/javascript" src="{js_url}"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: sans-serif; max-width: 480px; margin: 50px auto;
            padding: 0 1rem; color: #222; }}
    h1   {{ font-size: 1.4rem; margin-bottom: .25rem; }}
    .sub {{ color: #555; margin-bottom: 1.5rem; font-size: .95rem; }}
    .info-card {{ background: #f6f8fa; border: 1px solid #d0d7de;
                  border-radius: 8px; padding: .75rem 1rem; margin-bottom: 1.5rem; }}
    .label {{ font-size: .72rem; text-transform: uppercase; letter-spacing: .05em;
              color: #777; margin-bottom: .15rem; }}
    .value {{ font-family: monospace; font-size: .9rem; }}
    .name-row {{ display: flex; gap: .75rem; margin-bottom: 1rem; }}
    .name-row .field {{ flex: 1; display: flex; flex-direction: column; }}
    .name-row label {{ font-size: .72rem; text-transform: uppercase;
                       letter-spacing: .05em; color: #777; margin-bottom: .3rem; }}
    .name-row input {{ padding: .55rem .7rem; border: 1px solid #ccc;
                       border-radius: 6px; font-size: .95rem; }}
    .name-row input:focus {{ outline: none; border-color: #006aff; }}
    #card-container {{ min-height: 90px; margin-bottom: 1.25rem; }}
    #card-button {{
      width: 100%; padding: .75rem; background: #006aff; color: #fff;
      border: none; border-radius: 6px; font-size: 1rem; font-weight: 600;
      cursor: pointer;
    }}
    #card-button:disabled {{ background: #aaa; cursor: not-allowed; }}
    #card-button:hover:not(:disabled) {{ background: #0055cc; }}
    #payment-status {{ margin-top: .6rem; font-size: .9rem; color: #555; }}
    .error-banner {{ background: #fde8e8; border: 1px solid #f5c2c2;
                     color: #b91c1c; border-radius: 6px; padding: .65rem 1rem;
                     font-size: .9rem; margin-top: .75rem; line-height: 1.4; }}
    .note {{ font-size: .8rem; color: #777; margin-top: 1.25rem; }}
  </style>
</head>
<body>
  <h1>&#9889; EV Charging Authorization</h1>
  <p class="sub">Enter your card details to place an authorization hold and
  start your session. You will only be charged for the energy you actually use.</p>

  <div class="info-card">
    <div class="label">Booking ID</div>
    <div class="value">{safe_booking}</div>
  </div>
  <div class="info-card">
    <div class="label">Authorization hold amount</div>
    <div class="value">${amount_dollars:.2f} USD &mdash; adjusted after session</div>
  </div>

  <div class="name-row">
    <div class="field">
      <label for="given-name">First Name</label>
      <input id="given-name" type="text" placeholder="Jane" autocomplete="given-name">
    </div>
    <div class="field">
      <label for="family-name">Last Name</label>
      <input id="family-name" type="text" placeholder="Smith" autocomplete="family-name">
    </div>
  </div>

  <div id="card-container"></div>
  <button id="card-button" type="button">Authorize &amp; Start Charging</button>
  <div id="payment-status"></div>
  {error_banner}

  <p class="note">
    This places a temporary hold on your card. Your final charge reflects
    actual energy consumed; any unused amount is refunded automatically.
  </p>

  <script>
    (async () => {{
      if (!window.Square) {{
        document.getElementById('payment-status').textContent =
          'Square Payments SDK failed to load. Please refresh.';
        return;
      }}

      const payments = window.Square.payments('{app_id}', '{location_id}');
      const card = await payments.card();
      await card.attach('#card-container');

      const escHtml = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

      const infoCard = (label, val) =>
        '<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;' +
        'padding:1rem 1.2rem;margin-bottom:1rem;text-align:left">' +
        '<div style="font-size:.75rem;text-transform:uppercase;color:#777;margin-bottom:.2rem">' +
        label + '</div>' +
        '<div style="font-family:monospace;font-size:.9rem;word-break:break-all">' +
        val + '</div></div>';

      const showBanner = msg => {{
        let el = document.querySelector('.error-banner');
        if (!el) {{
          el = document.createElement('div');
          el.className = 'error-banner';
          btn.insertAdjacentElement('afterend', el);
        }}
        el.textContent = '\u26a0  ' + msg;
        el.scrollIntoView({{behavior: 'smooth', block: 'nearest'}});
      }};

      const btn = document.getElementById('card-button');
      const status = document.getElementById('payment-status');

      btn.addEventListener('click', async () => {{
        btn.disabled = true;
        status.textContent = 'Verifying card...';

        const givenName  = document.getElementById('given-name').value.trim();
        const familyName = document.getElementById('family-name').value.trim();
        if (!givenName || !familyName) {{
          status.textContent = 'Please enter your first and last name.';
          btn.disabled = false;
          return;
        }}

        let tokenResult;
        try {{
          tokenResult = await card.tokenize();
        }} catch (err) {{
          showBanner('Card tokenization error: ' + err.message);
          status.textContent = '';
          btn.disabled = false;
          return;
        }}

        if (tokenResult.status !== 'OK') {{
          const errs = (tokenResult.errors || []).map(e => e.message).join(', ');
          showBanner('Card error: ' + errs);
          status.textContent = '';
          btn.disabled = false;
          return;
        }}

        status.textContent = 'Processing...';

        const fd = new FormData();
        fd.append('source_id', tokenResult.token);
        fd.append('uid', '{safe_uid}');
        fd.append('given_name', givenName);
        fd.append('family_name', familyName);

        let resp, result;
        try {{
          resp = await fetch('{submit_url}', {{method: 'POST', body: fd}});
        }} catch (err) {{
          showBanner('Network error: could not reach server. ' + err.message);
          status.textContent = '';
          btn.disabled = false;
          return;
        }}

        try {{
          result = await resp.json();
        }} catch (_) {{
          const text = await resp.text().catch(() => 'no response body');
          showBanner('Server error (HTTP ' + resp.status + '): ' + text.slice(0, 200));
          status.textContent = '';
          btn.disabled = false;
          return;
        }}

        status.textContent = '';

        if (result.status === 'card_error') {{
          showBanner(result.message || 'Card processing failed. Please try a different card.');
          btn.disabled = false;
          return;
        }}

        if (result.status === 'error') {{
          document.body.innerHTML =
            '<div style="font-family:sans-serif;max-width:520px;margin:60px auto;padding:0 1rem">' +
            '<h2>\u274c Error</h2><p>' + escHtml(result.message) + '</p>' +
            '<p><a href="/">\u2190 Home</a></p></div>';
          return;
        }}

        if (result.status === 'success') {{
          const d = result;
          const amtStr = '$' + (d.amount_cents / 100).toFixed(2) + ' USD';
          document.body.innerHTML =
            '<div style="font-family:sans-serif;max-width:520px;margin:60px auto;' +
            'padding:0 1rem;color:#222;text-align:center">' +
            '<div style="font-size:4rem">&#9889;</div>' +
            '<h1 style="font-size:1.6rem;color:#1a7f3c">EV Charger Enabled</h1>' +
            '<p style="color:#555;margin-bottom:2rem">Authorization hold placed.<br>' +
            'You can now plug in your car.</p>' +
            infoCard('Booking ID', escHtml(d.booking_id)) +
            infoCard('Authorization hold', escHtml(amtStr)) +
            infoCard('Square payment ID', escHtml(d.payment_id)) +
            infoCard('Square card ID', escHtml(d.card_id)) +
            '<p style="font-size:.82rem;color:#777;margin-top:1.5rem">Pre-auth hold only. ' +
            'Final charge reflects actual energy used.</p></div>';
          return;
        }}

        showBanner('Unexpected response from server. Please try again.');
        btn.disabled = false;
      }});
    }})();
  </script>
</body>
</html>
""")


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
        return render_session_page(existing)

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
    })

    # -- Render card form ---------------------------------------------------
    return render_card_form(
        session_uid, booking_id, amount_cents,
        submit_url=str(request.base_url) + "submit_payment",
    )

