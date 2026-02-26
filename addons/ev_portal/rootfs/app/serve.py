"""
serve.py – Launch guest (HTTP:8090) and admin (HTTPS:8091) uvicorn servers.

This is the production and local-dev entry point:
  python serve.py

The guest FastAPI app (main:app) runs on HTTPS port 8090 and handles the
anonymous charger portal.  The admin FastAPI app (admin.app:admin_app) runs
on HTTPS port 8091, protected by HTTP Basic Auth.

Both servers share runtime state via state.py; the guest app's lifespan
initialises MQTT, Square, and the DB.
"""

import asyncio
import logging
import sys

import uvicorn

import state
from config import load_config
from tls import ensure_cert, ensure_guest_cert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

GUEST_PORT = 8090


async def _serve_all() -> None:
    cfg = load_config()

    # Populate admin config in state BEFORE any server starts so auth works
    # from the very first request even if the lifespan hasn't run yet.
    state._admin_config = cfg["admin"]

    admin_cfg = cfg["admin"]

    # ── Guest server (HTTPS) ──────────────────────────────────────────────
    # HTTPS is required so browsers grant a secure context, which Square's
    # Web Payments SDK mandates.  Authentication is NOT required on this server.
    try:
        guest_cert_path, guest_key_path = ensure_guest_cert()
    except Exception as exc:
        log.critical("TLS setup failed for guest server – cannot start: %s", exc)
        raise

    guest_config = uvicorn.Config(
        "main:app",
        host="0.0.0.0",
        port=GUEST_PORT,
        ssl_certfile=guest_cert_path,
        ssl_keyfile=guest_key_path,
        log_level="info",
        access_log=True,
    )
    servers = [uvicorn.Server(guest_config)]

    # ── Admin server (HTTPS) ───────────────────────────────────────────────
    if admin_cfg["enabled"]:
        try:
            cert_path, key_path = ensure_cert(admin_cfg)
        except Exception as exc:
            log.critical("TLS setup failed – admin server will NOT start: %s", exc)
            cert_path = key_path = None

        if cert_path:
            admin_config = uvicorn.Config(
                "admin.app:admin_app",
                host="0.0.0.0",
                port=admin_cfg["port_https"],
                ssl_certfile=cert_path,
                ssl_keyfile=key_path,
                log_level="info",
                access_log=True,
            )
            servers.append(uvicorn.Server(admin_config))
            log.info(
                "Admin server starting on https://0.0.0.0:%s  (tls=%s)",
                admin_cfg["port_https"], admin_cfg["tls_mode"],
            )
    else:
        log.info("Admin interface disabled (admin_enabled=false)")

    log.info("Guest server starting on https://0.0.0.0:%s", GUEST_PORT)

    await asyncio.gather(*[s.serve() for s in servers])


if __name__ == "__main__":
    try:
        asyncio.run(_serve_all())
    except KeyboardInterrupt:
        sys.exit(0)
