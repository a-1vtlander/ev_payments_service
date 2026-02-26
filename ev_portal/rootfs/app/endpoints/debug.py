import html
import json
import sqlite3

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import db
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

<h2>Stored Cards</h2>
<p>Session data is now persisted to SQLite. <a href="/db">View session database &rarr;</a></p>

<p><a href="/">&#8592; Back</a></p>
</body>
</html>"""


@router.get("/db", response_class=HTMLResponse)
async def db_view():
    """Render all sessions from the SQLite DB as an HTML table."""
    import asyncio

    def _fetch():
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()]

    try:
        rows = await asyncio.to_thread(_fetch)
    except Exception as exc:
        return HTMLResponse(f"<pre>DB error: {html.escape(str(exc))}</pre>", status_code=500)

    if not rows:
        body = "<p>No sessions yet.</p>"
    else:
        cols = list(rows[0].keys())
        header = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        row_html = ""
        for r in rows:
            cells = "".join(
                f"<td data-full='{html.escape(str(r[c]) if r[c] is not None else '')}'>"
                f"{html.escape(str(r[c]) if r[c] is not None else '')}</td>"
                for c in cols
            )
            row_html += f"<tr>{cells}</tr>\n"
        body = (
            f"<table><thead><tr>{header}</tr></thead><tbody>{row_html}</tbody></table>"
        )

    return f"""<!DOCTYPE html>
<html>
<head><title>EV Portal \u2013 DB</title>
<style>
body{{font-family:monospace;font-size:.8rem;margin:20px;padding:0 20px}}
table{{border-collapse:collapse;table-layout:fixed;width:max-content;min-width:100%}}
th{{background:#e8edf2;padding:4px 8px;border:1px solid #ccc;white-space:nowrap;
   position:relative;overflow:hidden;min-width:60px;width:140px;
   resize:horizontal;}}
td{{padding:4px 8px;border:1px solid #ddd;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis;cursor:pointer;max-width:inherit}}
td:hover{{background:#fffbcc}}
#modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;align-items:center;justify-content:center}}
#modal-overlay.show{{display:flex}}
#modal-box{{background:#fff;border-radius:8px;padding:1.5rem;max-width:80vw;max-height:80vh;
           overflow:auto;box-shadow:0 4px 24px rgba(0,0,0,.3);position:relative}}
#modal-box pre{{margin:0;white-space:pre-wrap;word-break:break-all;font-size:.85rem}}
#modal-close{{position:absolute;top:.5rem;right:.75rem;font-size:1.2rem;cursor:pointer;
             border:none;background:none;color:#555}}
</style>
</head>
<body>
<h2>EV Portal \u2013 Sessions ({len(rows)})</h2>
<p>DB: <code>{html.escape(db.DB_PATH)}</code> &mdash; <em>drag column edges to resize &middot; click cell to expand</em></p>
<div style="overflow-x:auto">
{body}
</div>
<p><a href="/debug">&#8592; Debug</a> &nbsp;|&nbsp; <a href="/">&#8592; Home</a></p>

<div id="modal-overlay">
  <div id="modal-box">
    <button id="modal-close" onclick="closeModal()">&times;</button>
    <pre id="modal-content"></pre>
  </div>
</div>
<script>
  document.querySelectorAll('td').forEach(td => {{
    td.addEventListener('click', () => {{
      document.getElementById('modal-content').textContent = td.dataset.full;
      document.getElementById('modal-overlay').classList.add('show');
    }});
  }});
  function closeModal() {{
    document.getElementById('modal-overlay').classList.remove('show');
  }}
  document.getElementById('modal-overlay').addEventListener('click', e => {{
    if (e.target === document.getElementById('modal-overlay')) closeModal();
  }});
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});
</script>
</body>
</html>"""
