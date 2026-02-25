"""
FastAPI lifespan – connects MQTT on startup, disconnects on shutdown.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

import state
from mqtt import build_mqtt_client
from square import fetch_first_location_id

log = logging.getLogger(__name__)


def load_options() -> dict:
    """Load add-on options from options.json (injected by HA Supervisor)."""
    if not os.path.exists(state.OPTIONS_PATH):
        log.warning("%s not found – using defaults", state.OPTIONS_PATH)
        return {}
    with open(state.OPTIONS_PATH) as f:
        return json.load(f)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Load config ────────────────────────────────────────────────────────
    opts = load_options()

    host = (opts.get("mqtt_host") or "localhost").strip()
    try:
        port = int(opts.get("mqtt_port", 1883))
    except (TypeError, ValueError):
        log.warning("Invalid mqtt_port in options, falling back to 1883")
        port = 1883

    home_id    = (opts.get("home_id")    or "base_lander").strip()
    charger_id = (opts.get("charger_id") or "chargepoint:home:charger:1").strip()

    state._app_config = {"home_id": home_id, "charger_id": charger_id}

    # Derive all MQTT topics from charger identity.
    base_topic = f"ev/charger/{home_id}/{charger_id}/booking"
    state._booking_response_topic   = f"{base_topic}/response"
    state._authorize_request_topic  = f"{base_topic}/authorize_session"
    state._authorize_response_topic = f"{base_topic}/authorize_session/response"

    log.info("Booking request topic  : %s/request_session", base_topic)
    log.info("Booking response topic : %s", state._booking_response_topic)
    log.info("Authorize request topic: %s", state._authorize_request_topic)
    log.info("Authorize response topic: %s", state._authorize_response_topic)

    # ── Square config ──────────────────────────────────────────────────────
    state._square_config = {
        "sandbox":      bool(opts.get("square_sandbox", True)),
        "app_id":       (opts.get("square_app_id")       or "").strip(),
        "access_token": (opts.get("square_access_token") or "").strip(),
        "location_id":  (opts.get("square_location_id")  or "").strip(),
        "charge_cents": int(opts.get("square_charge_cents") or 100),
    }
    log.info(
        "Square sandbox=%s  location_id=%r",
        state._square_config["sandbox"], state._square_config["location_id"],
    )

    if not state._square_config["location_id"] and state._square_config["access_token"]:
        try:
            state._square_config["location_id"] = await fetch_first_location_id()
            log.info("Auto-fetched Square location_id: %s", state._square_config["location_id"])
        except Exception as exc:
            log.error("Could not auto-fetch Square location_id: %s", exc)

    # ── Async primitives ───────────────────────────────────────────────────
    state._event_loop   = asyncio.get_running_loop()
    state._session_lock = asyncio.Lock()

    # One queue per topic we subscribe to.
    state._topic_queues = {
        state._booking_response_topic:   asyncio.Queue(),
        state._authorize_response_topic: asyncio.Queue(),
    }
    subscribed_topics = list(state._topic_queues.keys())

    # ── MQTT ───────────────────────────────────────────────────────────────
    state.mqtt_client = build_mqtt_client(opts, subscribed_topics)
    try:
        state.mqtt_client.connect(host, port, keepalive=60)
        state.mqtt_client.loop_start()
        log.info("MQTT loop started, connecting to %s:%s", host, port)
    except Exception as exc:
        log.error("Could not initiate MQTT connection: %s", exc)

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────
    if state.mqtt_client:
        state.mqtt_client.loop_stop()
        state.mqtt_client.disconnect()
        log.info("MQTT client stopped")
