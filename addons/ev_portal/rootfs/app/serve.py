"""
serve.py – Launch guest (HTTP:8090) and admin (HTTPS:8091) uvicorn servers.

This is the production and local-dev entry point:
  python serve.py

The guest FastAPI app (main:app) runs on plain HTTP port 8090.  In
production, Cloudflare Tunnel sits in front and presents valid HTTPS to
browsers — the browser sees HTTPS so Square's Web Payments SDK gets the
secure context it requires.  The admin FastAPI app (admin.app:admin_app)
runs on HTTPS port 8091, protected by session-cookie / Basic Auth.

Both servers share runtime state via state.py; the guest app's lifespan
initialises MQTT, Square, and the DB.
"""

import asyncio
import logging
import os
import sys

import uvicorn

import state
from config import load_config
from tls import ensure_cert, ensure_guest_cert
import acme_tls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

GUEST_PORT  = 8090
KEYMGR_PORT = 8092


async def _serve_all() -> None:
    cfg = load_config()

    # Populate admin config in state BEFORE any server starts so auth works
    # from the very first request even if the lifespan hasn't run yet.
    state._admin_config = cfg["admin"]

    admin_cfg = cfg["admin"]

    # ── Guest server ───────────────────────────────────────────────────────────
    # In production: plain HTTP – Cloudflare terminates TLS at the edge.
    # In dev (EV_GUEST_HTTPS=1): self-signed HTTPS bound to 127.0.0.1 only so
    # Square's Web Payments SDK gets the secure context it requires locally.
    dev_https = os.environ.get("EV_GUEST_HTTPS", "").strip() not in ("", "0", "false", "no")
    if dev_https:
        try:
            guest_cert, guest_key = ensure_guest_cert()
        except Exception as exc:
            log.critical("TLS setup for guest server failed: %s", exc)
            guest_cert = guest_key = None

        if guest_cert:
            guest_config = uvicorn.Config(
                "main:app",
                host="127.0.0.1",
                port=GUEST_PORT,
                ssl_certfile=guest_cert,
                ssl_keyfile=guest_key,
                log_level="info",
                access_log=True,
            )
            log.info(
                "Guest server starting on https://127.0.0.1:%s  (localhost-only TLS)",
                GUEST_PORT,
            )
        else:
            dev_https = False  # fall through to plain HTTP below

    if not dev_https:
        guest_config = uvicorn.Config(
            "main:app",
            host="0.0.0.0",
            port=GUEST_PORT,
            log_level="info",
            access_log=True,
        )
    servers = [uvicorn.Server(guest_config)]

    # ── Admin server (HTTPS) ───────────────────────────────────────────────
    cert_path = key_path = None  # also used by keymgr below
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

    log.info("Guest server starting on http://0.0.0.0:%s  (plain HTTP; Cloudflare provides HTTPS at edge)", GUEST_PORT)

    # ── Key manager server — ACME cert preferred, falls back to admin/guest cert ─
    keymgr_cfg    = cfg.get("keymgr", {})
    keymgr_domain = keymgr_cfg.get("domain", "").strip()
    cf_token      = keymgr_cfg.get("cloudflare_token", "").strip()
    cf_zone_id    = keymgr_cfg.get("cloudflare_zone_id", "").strip()

    # Wire the guest-portal hostname into the keymgr router so it can build
    # the redirect URL after issuing a key.
    import keymgr.router as _km_router
    _km_router.PORTAL_HOST = keymgr_cfg.get("ev_portal_domain", "").strip()
    keymgr_kwargs: dict = {}

    if keymgr_domain and cf_token:
        try:
            from tls import TLS_DIR
            km_cert, km_key = await acme_tls.ensure_acme_cert(
                keymgr_domain, cf_token, TLS_DIR, cf_zone_id=cf_zone_id
            )
            keymgr_kwargs = {"ssl_certfile": km_cert, "ssl_keyfile": km_key}
            log.info("Key manager using ACME cert for %s", keymgr_domain)
        except Exception as exc:
            log.error(
                "ACME cert provisioning failed for %s: %s — keymgr will try admin cert",
                keymgr_domain, exc,
            )

    if not keymgr_kwargs:
        if admin_cfg["enabled"] and cert_path:
            keymgr_kwargs = {"ssl_certfile": cert_path, "ssl_keyfile": key_path}
            log.info("Key manager falling back to admin TLS cert")
        elif dev_https and guest_cert:
            keymgr_kwargs = {"ssl_certfile": guest_cert, "ssl_keyfile": guest_key}
            log.info("Key manager using guest dev TLS cert")
        else:
            log.warning(
                "Key manager starting WITHOUT TLS — browsers will reject HTTPS connections to port %s",
                KEYMGR_PORT,
            )
    keymgr_config = uvicorn.Config(
        "keymgr.app:keymgr_app",
        host="0.0.0.0",
        port=KEYMGR_PORT,
        log_level="info",
        access_log=True,
        **keymgr_kwargs,
    )
    servers.append(uvicorn.Server(keymgr_config))
    scheme = "https" if keymgr_kwargs else "http"
    log.info("Key manager starting on %s://0.0.0.0:%s", scheme, KEYMGR_PORT)

    await asyncio.gather(*[s.serve() for s in servers])


if __name__ == "__main__":
    try:
        asyncio.run(_serve_all())
    except KeyboardInterrupt:
        sys.exit(0)
