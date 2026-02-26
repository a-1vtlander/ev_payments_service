import html
import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import state
from config import load_config

router = APIRouter()


@router.get("/debug", response_class=HTMLResponse)
async def debug():
    """Shows exactly what config the running server loaded."""
    try:
        cfg = load_config()
        mqtt_cfg   = cfg["mqtt"]
        square_cfg = cfg["square"]
        app_cfg    = cfg["app"]

        # Build a flat display dict; mask sensitive values.
        display_opts = {
            "mqtt_host":            mqtt_cfg["host"],
            "mqtt_port":            mqtt_cfg["port"],
            "mqtt_username":        mqtt_cfg["username"],
            "mqtt_password":        "***" if mqtt_cfg["password"] else "(not set)",
            "home_id":              app_cfg["home_id"],
            "charger_id":           app_cfg["charger_id"],
            "square_sandbox":       square_cfg["sandbox"],
            "square_app_id":        square_cfg["app_id"],
            "square_access_token":  "***",
            "square_location_id":   square_cfg["location_id"],
            "square_charge_cents":  square_cfg["charge_cents"],
        }
    except Exception as exc:
        display_opts = {"error": str(exc)}

    mqtt_connected = state.mqtt_client is not None and state.mqtt_client.is_connected()

    opts_html = "\n".join(
        f"  <tr><td>{html.escape(str(k))}</td>"
        f"<td><code>{html.escape(str(v))}</code></td></tr>"
        for k, v in display_opts.items()
    )

    return f"""<!DOCTYPE html>
<html>
<head><title>EV Portal \u2013 Debug</title>
<style>
body{{font-family:monospace;max-width:700px;margin:40px auto;padding:0 20px}}
table{{border-collapse:collapse;width:100%}}
td{{padding:6px 10px;border:1px solid #ccc}}
td:first-child{{font-weight:bold;background:#f7f7f7;width:200px}}
</style>
</head>
<body>
<h2>EV Charger Portal \u2013 Runtime Config</h2>
<table>
  <tr><td>EV_OPTIONS_PATH</td><td><code>{html.escape(state.OPTIONS_PATH)}</code></td></tr>
  <tr><td>MQTT connected</td>
      <td><strong style="color:{'green' if mqtt_connected else 'red'}">
        {'yes' if mqtt_connected else 'no'}</strong></td></tr>
  {opts_html}
  <tr><td>booking response topic</td>
      <td><code>{html.escape(state._booking_response_topic)}</code></td></tr>
  <tr><td>authorize request topic</td>
      <td><code>{html.escape(state._authorize_request_topic)}</code></td></tr>
  <tr><td>authorize response topic</td>
      <td><code>{html.escape(state._authorize_response_topic)}</code></td></tr>
</table>

<h2>Session Database</h2>
<p>Session data is persisted in SQLite.
   View and manage sessions via the
   <a href="/admin">admin interface</a> (requires HTTPS + Basic Auth).</p>

<p><a href="/">&#8592; Back</a></p>
</body>
</html>"""
