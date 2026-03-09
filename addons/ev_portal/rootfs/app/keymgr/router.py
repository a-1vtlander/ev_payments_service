"""
keymgr/router.py — Routes for the key-management server (port 8092).

GET  /              — Fetch (or generate) a valid key and redirect to
                      baselander-ev.extravio.co?access_key=<key>
GET  /keygen        — Key management UI; shows newly issued key when ?issued=<key>
POST /keygen/issue  — Generate a new UUID key, persist to DB, redirect to /keygen?issued=<key>
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

import db

log    = logging.getLogger(__name__)
router = APIRouter()

KEY_TTL_DAYS = 30

# ---------------------------------------------------------------------------
# Minimal self-contained HTML page (no external CSS dependency)
# ---------------------------------------------------------------------------

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EV Portal — Access Key Manager</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 2.5rem 2rem;
      width: 100%;
      max-width: 520px;
      text-align: center;
    }}
    h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 0.4rem; color: #f1f5f9; }}
    .sub {{ color: #94a3b8; font-size: 0.85rem; margin: 0 0 2rem; }}
    .issue-btn {{
      background: #3b82f6;
      border: none;
      border-radius: 8px;
      color: #fff;
      cursor: pointer;
      font-size: 1rem;
      font-weight: 600;
      padding: 0.75rem 2rem;
      transition: background 0.15s;
    }}
    .issue-btn:hover {{ background: #2563eb; }}
    .key-box {{
      background: #0f172a;
      border: 1px solid #22c55e;
      border-radius: 8px;
      margin-top: 2rem;
      padding: 1.25rem;
      text-align: left;
    }}
    .key-label {{
      color: #22c55e;
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 0.5rem;
    }}
    .key-value {{
      color: #f1f5f9;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 0.88rem;
      word-break: break-all;
      margin-bottom: 0.5rem;
    }}
    .key-meta {{ color: #64748b; font-size: 0.78rem; margin-bottom: 0.9rem; }}
    .url-label {{
      color: #64748b;
      font-size: 0.7rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 0.35rem;
    }}
    .key-url {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 6px;
      color: #93c5fd;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 0.78rem;
      overflow-wrap: break-word;
      padding: 0.55rem 0.75rem;
      word-break: break-all;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>EV Portal &mdash; Access Key Manager</h1>
    <p class="sub">Issue a 30-day browser access key for the charging portal.</p>
    <form method="post" action="/keygen/issue">
      <button class="issue-btn" type="submit">Issue New Key</button>
    </form>
    {key_block}
  </div>
</body>
</html>"""


def _build_key_block(key: str, expires_at: str) -> str:
    try:
        exp_dt      = datetime.fromisoformat(expires_at)
        exp_display = exp_dt.strftime("%B %-d, %Y at %H:%M UTC")
    except (ValueError, TypeError):
        exp_display = expires_at

    return f"""\
    <div class="key-box">
      <div class="key-label">Access Key &mdash; copy this now</div>
      <div class="key-value">{key}</div>
      <div class="key-meta">Valid until: {exp_display}</div>
      <div class="url-label">Portal URL with this key embedded</div>
      <div class="key-url">http://&lt;host&gt;:8090/start?key={key}</div>
    </div>"""


PORTAL_HOST = "baselander-ev.extravio.co"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_or_create_key() -> str:
    """Return a valid key from the DB, creating one if none exists."""
    row = await db.get_valid_access_key()
    if row:
        log.info("Using existing valid access key %s", row["key"])
        return row["key"]
    key        = str(uuid.uuid4())
    now        = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = (now + timedelta(days=KEY_TTL_DAYS)).isoformat()
    await db.create_access_key(key, created_at, expires_at)
    log.info("Auto-generated new access key %s  expires %s", key, expires_at)
    return key


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/")
async def index() -> RedirectResponse:
    """Fetch (or generate) a valid key and redirect to the portal with it."""
    key = await _get_or_create_key()
    target = f"https://{PORTAL_HOST}?access_key={key}"
    log.info("Redirecting to %s", target)
    return RedirectResponse(target, status_code=302)


@router.get("/keygen", response_class=HTMLResponse)
async def keygen(issued: str = "") -> HTMLResponse:
    """Key management UI. If ?issued=<key> present, display the newly issued key."""
    key_block = ""
    if issued:
        row = await db.get_access_key(issued)
        if row:
            key_block = _build_key_block(row["key"], row["expires_at"])
    return HTMLResponse(_PAGE.format(key_block=key_block))


@router.post("/keygen/issue")
async def issue_key() -> RedirectResponse:
    """Generate a new 30-day access key, persist it, and redirect to display it."""
    key        = str(uuid.uuid4())
    now        = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = (now + timedelta(days=KEY_TTL_DAYS)).isoformat()
    await db.create_access_key(key, created_at, expires_at)
    log.info("Issued new access key %s  expires %s", key, expires_at)
    return RedirectResponse(f"/keygen?issued={key}", status_code=303)
