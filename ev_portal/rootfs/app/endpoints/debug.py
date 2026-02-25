import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import state
from lifespan import load_options

router = APIRouter()


@router.get("/debug", response_class=HTMLResponse)
async def debug():
    """Shows exactly what config the running server loaded."""
    opts = load_options()
    mqtt_connected = state.mqtt_client is not None and state.mqtt_client.is_connected()

    # Mask sensitive values.
    display_opts = dict(opts)
    for key in ("mqtt_password", "square_access_token"):
        if display_opts.get(key):
            display_opts[key] = "***"

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
<p><a href="/">&#8592; Back</a></p>
</body>
</html>"""
