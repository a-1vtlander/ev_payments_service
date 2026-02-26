"""
EV Charger Portal - application entry point.

Guest server (HTTP, port 8090) – anonymous, LAN-only.
Admin server (HTTPS, port 8091) – started by serve.py, requires Basic Auth.

All logic lives in:
  state.py                 - shared globals
  mqtt.py                  - MQTT client factory
  square.py                - Square API helpers
  lifespan.py              - startup / shutdown
  endpoints/index.py       - GET /
  endpoints/health.py      - GET /health
  endpoints/debug.py       - GET /debug
  endpoints/start.py       - GET /start
  endpoints/payment_post_process.py - GET /payment_post_process
  admin/                   - /admin/* routes (admin HTTPS server only)
"""

import logging

from fastapi import Request
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from lifespan import lifespan
from endpoints import debug, health, index, session, start, submit_payment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = FastAPI(title="EV Charger Portal", lifespan=lifespan)

app.include_router(index.router)
app.include_router(health.router)
app.include_router(debug.router)
app.include_router(start.router)
app.include_router(submit_payment.router)
app.include_router(session.router)


# ---------------------------------------------------------------------------
# Admin redirect – convenience, not a proxy
# ---------------------------------------------------------------------------

def _admin_redirect_url(request: Request, path: str = "") -> str:
    """Build https://<host>:8091/admin/<path> from the incoming request Host."""
    import state
    try:
        host = request.headers.get("host", "") or ""
        # Strip any existing port from the host header.
        bare_host = host.split(":")[0] if ":" in host else host
        if not bare_host:
            bare_host = request.client.host if request.client else "localhost"
    except Exception:
        bare_host = "localhost"

    admin_port = state._admin_config.get("port_https", 8091)
    target = f"https://{bare_host}:{admin_port}/admin/"
    if path:
        target += path.lstrip("/")
    return target


@app.get("/admin", include_in_schema=False)
@app.get("/admin/", include_in_schema=False)
async def redirect_to_admin(request: Request):
    return RedirectResponse(url=_admin_redirect_url(request), status_code=302)


@app.get("/admin/{path:path}", include_in_schema=False)
async def redirect_admin_path(path: str, request: Request):
    return RedirectResponse(url=_admin_redirect_url(request, path), status_code=302)

