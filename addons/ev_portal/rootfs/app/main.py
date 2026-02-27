"""
EV Charger Portal - application entry point.

Guest server (HTTPS, port 8090) – LAN / Tailscale only (access middleware).
Admin server (HTTPS, port 8091) – started by serve.py, requires Basic Auth.
                                   NOT mounted on this app.

All logic lives in:
  state.py                 - shared globals
  mqtt.py                  - MQTT client factory
  square.py                - Square API helpers
  lifespan.py              - startup / shutdown
  access.py                - IP access-control middleware (public app only)
  endpoints/health.py      - GET /health
  endpoints/debug.py       - GET /debug
  endpoints/start.py       - GET /start
  endpoints/submit_payment.py - POST /submit_payment
  endpoints/session.py     - GET /session/{uid}
  admin/                   - /admin/* routes (admin HTTPS server, port 8091 only)
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from access import AccessControlMiddleware
from lifespan import lifespan
import state
from endpoints import debug, health, session, start, submit_payment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = FastAPI(title="EV Charger Portal", lifespan=lifespan)

# ── Access control: LAN / Tailscale only; Cloudflare-tunnel-aware ─────────
app.add_middleware(AccessControlMiddleware)

# ── Static assets ──────────────────────────────────────────────────────────
_APP_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")

app.include_router(health.router)
app.include_router(debug.router)
app.include_router(start.router)
app.include_router(submit_payment.router)
app.include_router(session.router)


# ── GET / → redirect to /start ─────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root_redirect(request: Request):
    """Redirect root to /start with optional default_charger_id pre-filled."""
    default_charger = state._access_config.get("default_charger_id", "").strip()
    target = f"/start?charger_id={default_charger}" if default_charger else "/start"
    return HTMLResponse(
        content=(
            f'<meta http-equiv="refresh" content="0; url={target}">'
            f'<script>window.location.replace("{target}");</script>'
        ),
        status_code=200,
    )


# ── Apple Pay domain verification ──────────────────────────────────────────
@app.get(
    "/.well-known/apple-developer-merchantid-domain-association",
    include_in_schema=False,
)
async def apple_pay_domain_verification():
    """
    Serve Apple Pay domain association file verbatim.
    Returns 404 if applepay_domain_association is not configured.
    Uses plain Response (not HTMLResponse) to avoid any HTML-encoding of content.
    """
    content = state._access_config.get("applepay_domain_association", "")
    if not content:
        return Response(content="Not configured", status_code=404, media_type="text/plain")
    # Encode to bytes explicitly – avoids any str/encoding ambiguity in Starlette
    return Response(
        content=content.encode("utf-8"),
        status_code=200,
        media_type="text/plain; charset=utf-8",
    )


# ── /enable-ev-session alias ───────────────────────────────────────────────
@app.get("/enable-ev-session", include_in_schema=False)
async def enable_ev_session_alias(request: Request):
    """Friendly alias for /start – same handler, prettier URL for QR codes."""
    return await start.start_session(request)

