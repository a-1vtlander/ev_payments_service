import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import state

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index():
    mqtt_status  = "connected" if (state.mqtt_client and state.mqtt_client.is_connected()) else "disconnected"
    status_color = "green" if mqtt_status == "connected" else "red"
    home_id    = state._app_config.get("home_id",    "–")
    charger_id = state._app_config.get("charger_id", "–")
    base_topic = f"ev/charger/{home_id}/{charger_id}/booking"

    return f"""<!DOCTYPE html>
<html>
<head><title>EV Charger Portal</title>
<style>
body{{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px}}
code{{background:#f4f4f4;padding:2px 6px;border-radius:3px;word-break:break-all}}
table{{border-collapse:collapse;width:100%}}
td{{padding:6px 8px;border:1px solid #ddd}}
td:first-child{{font-weight:bold;width:160px;white-space:nowrap}}
</style>
</head>
<body>
<h1>EV Charger Portal</h1>
<table>
  <tr><td>MQTT</td><td><strong style="color:{status_color}">{mqtt_status}</strong></td></tr>
  <tr><td>Home ID</td><td><code>{html.escape(home_id)}</code></td></tr>
  <tr><td>Charger ID</td><td><code>{html.escape(charger_id)}</code></td></tr>
  <tr><td>Request topic</td><td><code>{html.escape(base_topic)}/request_session</code></td></tr>
  <tr><td>Response topic</td><td><code>{html.escape(state._booking_response_topic)}</code></td></tr>
  <tr><td>Authorize topic</td><td><code>{html.escape(state._authorize_request_topic)}</code></td></tr>
  <tr><td>Auth response</td><td><code>{html.escape(state._authorize_response_topic)}</code></td></tr>
</table>
<h2>Actions</h2>
<ul>
  <li><a href="/health"><code>GET /health</code></a> – liveness check</li>
  <li><a href="/debug"><code>GET /debug</code></a> – runtime config</li>
  <li><a href="/start"><code>GET /start</code></a> – request a booking session
    <em>(waits up to {int(state.RESPONSE_TIMEOUT)}s for broker response)</em></li>
  <li><a href="/db"><code>GET /db</code></a> – view session database</li>
</ul>
</body>
</html>"""
