"""
config.py — Load and validate add-on options for EV Charger Portal.

In a real HA Supervisor environment the options file is written to
``/data/options.json`` automatically.  For local development set the
environment variable ``EV_OPTIONS_PATH`` to point at a different file
(see README – Local Development section).
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import db  # DB_PATH lives here
import state  # OPTIONS_PATH lives here

log = logging.getLogger(__name__)

# Fields that MUST be present and non-empty for the service to start.
# mqtt_password is intentionally excluded: anonymous brokers leave it empty.
_REQUIRED_FIELDS: list = []
# mqtt_host is optional: if absent, MQTT features are disabled until configured

_VALID_TLS_MODES = {"self_signed", "provided"}


def load_config() -> Dict[str, Any]:
    """Read *OPTIONS_PATH*, validate required keys, and return a structured config dict.

    Logs the options file path, DB path, and Square environment (sandbox vs
    production) at INFO level.  Credentials are **never** logged.

    Raises:
        RuntimeError: if the options file is missing or any required field is absent.
    """
    path = state.OPTIONS_PATH
    log.info("Options file : %s", path)

    # db_path is resolved after opts are loaded below.

    if not os.path.exists(path):
        raise RuntimeError(
            f"Options file not found: {path!r}.  "
            "In HA Supervisor this file is written automatically.  "
            "For local dev set EV_OPTIONS_PATH to your dev_options.json."
        )

    with open(path) as fh:
        opts: Dict[str, Any] = json.load(fh)

    # ── Apply db_path from options (overrides EV_DB_PATH / default) ──────
    raw_db_path = (opts.get("db_path") or "").strip()
    if raw_db_path:
        # Relative paths are resolved next to the options file.
        if not os.path.isabs(raw_db_path):
            raw_db_path = os.path.join(os.path.dirname(os.path.abspath(path)), raw_db_path)
        db.DB_PATH = raw_db_path
    log.info("DB path      : %s", db.DB_PATH)

    # ── Validate required fields (mqtt optional) ──────────────────────────
    if not opts.get("mqtt_host"):
        log.warning(
            "mqtt_host not set in options; MQTT functionality will be disabled until configured."
        )

    # ── Coerce types with safe defaults ───────────────────────────────────
    try:
        mqtt_port = int(opts.get("mqtt_port", 1883))
        if mqtt_port <= 0:
            raise ValueError("port must be > 0")
    except (TypeError, ValueError) as exc:
        log.warning("Invalid mqtt_port (%r): %s – falling back to 1883", opts.get("mqtt_port"), exc)
        mqtt_port = 1883

    sandbox: bool = bool(opts.get("square_sandbox", True))
    charge_cents: int = max(0, int(opts.get("square_charge_cents") or 100))

    # ── Select Square credentials based on environment ────────────────────
    if sandbox:
        app_id       = (opts.get("square_sandbox_app_id")       or "").strip()
        access_token = (opts.get("square_sandbox_access_token") or "").strip()
        cred_field   = "square_sandbox_app_id / square_sandbox_access_token"
    else:
        app_id       = (opts.get("square_production_app_id")       or "").strip()
        access_token = (opts.get("square_production_access_token") or "").strip()
        cred_field   = "square_production_app_id / square_production_access_token"

    if not app_id or not access_token:
        raise RuntimeError(
            f"square_sandbox={sandbox}: {cred_field} must both be set.  "
            "Update your add-on configuration."
        )

    home_id    = (opts.get("home_id")    or "").strip()
    charger_id = (opts.get("charger_id") or "").strip()
    default_charger_id = (opts.get("default_charger_id") or "").strip()

    # Apple Pay domain association file (not logged – may contain sensitive data)
    applepay_domain_association: str = opts.get("applepay_domain_association") or ""

    # Access control: list of allowed CIDRs (LAN + Tailscale)
    raw_cidrs = opts.get("access_allow_cidrs") or []
    if isinstance(raw_cidrs, str):
        raw_cidrs = [c.strip() for c in raw_cidrs.split(",") if c.strip()]
    access_allow_cidrs: list = [str(c).strip() for c in raw_cidrs if str(c).strip()]

    # ── Startup log (no secrets) ────────────────────────────────────────────
    log.info(
        "Square environment : %s",
        "sandbox" if sandbox else "production",
    )
    log.info(
        "MQTT broker        : %s:%s",
        opts.get("mqtt_host") or "(not set)",
        mqtt_port,
    )
    log.info(
        "Home / charger     : %s / %s",
        home_id or "(not set)",
        charger_id or "(not set)",
    )

    # ── Admin config ──────────────────────────────────────────────────────
    admin_enabled: bool = bool(opts.get("admin_enabled", True))
    admin_username: str = (opts.get("admin_username") or "admin").strip()
    admin_password: str = (opts.get("admin_password") or "")

    if admin_enabled and not admin_password:
        raise RuntimeError(
            "admin_enabled is true but admin_password is not set.  "
            "Set admin_password in the add-on config, or set admin_enabled: false to disable."
        )

    try:
        admin_port_https = int(opts.get("admin_port_https") or 8091)
        if admin_port_https <= 0:
            raise ValueError("port must be > 0")
    except (TypeError, ValueError):
        admin_port_https = 8091

    tls_mode: str = (opts.get("admin_tls_mode") or "self_signed").strip().lower()
    if tls_mode not in _VALID_TLS_MODES:
        log.warning("Invalid admin_tls_mode %r – defaulting to self_signed", tls_mode)
        tls_mode = "self_signed"

    log.info("Admin interface  : %s (port %s, tls=%s)",
             "enabled" if admin_enabled else "disabled", admin_port_https, tls_mode)

    return {
        "mqtt": {
            "host":     (opts.get("mqtt_host") or "").strip(),
            "port":     mqtt_port,
            "username": (opts.get("mqtt_username") or "").strip(),
            "password": opts.get("mqtt_password") or "",
        },
        "square": {
            "sandbox":      sandbox,
            "app_id":       app_id,
            "access_token": access_token,
            "location_id":  (opts.get("square_location_id") or "").strip(),
            "charge_cents": charge_cents,
        },
        "app": {
            "home_id":            home_id,
            "charger_id":         charger_id,
            "default_charger_id": default_charger_id,
        },
        "access": {
            "allow_cidrs":                  access_allow_cidrs,
            "default_charger_id":           default_charger_id,
            "applepay_domain_association":  applepay_domain_association,
        },
        "admin": {
            "enabled":        admin_enabled,
            "username":       admin_username,
            "password":       admin_password,
            "port_https":     admin_port_https,
            "tls_mode":       tls_mode,
            "tls_cert_path":  (opts.get("admin_tls_cert_path") or "").strip(),
            "tls_key_path":   (opts.get("admin_tls_key_path") or "").strip(),
        },
    }

