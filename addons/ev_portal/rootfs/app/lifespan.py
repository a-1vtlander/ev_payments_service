"""
FastAPI lifespan – connects MQTT on startup, disconnects on shutdown.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import db
import state
from config import load_config
from finalize import finalize_session_consumer
from mqtt import build_mqtt_client
from square import fetch_first_location_id

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -- Persistent store --------------------------------------------------
    await db.init_db()

    # -- Load & validate config --------------------------------------------
    cfg = load_config()
    mqtt_cfg   = cfg["mqtt"]
    square_cfg = cfg["square"]
    app_cfg    = cfg["app"]

    # Admin config – also set here so that tests using LifespanManager don't
    # need to go through serve.py.
    state._admin_config = cfg["admin"]
    state._app_config   = app_cfg

    # Derive all MQTT topics from charger identity.
    home_id    = app_cfg["home_id"]
    charger_id = app_cfg["charger_id"]
    base_topic = f"ev/charger/{home_id}/{charger_id}/booking"
    state._booking_response_topic   = f"{base_topic}/response"
    state._authorize_request_topic  = f"{base_topic}/authorize_session"
    state._authorize_response_topic = f"{base_topic}/authorize_session/response"
    state._finalize_session_topic   = f"{base_topic}/finalize_session"

    log.info("Booking request topic   : %s/request_session", base_topic)
    log.info("Booking response topic  : %s", state._booking_response_topic)
    log.info("Authorize request topic : %s", state._authorize_request_topic)
    log.info("Authorize response topic: %s", state._authorize_response_topic)
    log.info("Finalize session topic  : %s", state._finalize_session_topic)

    # ── Square config ──────────────────────────────────────────────────────
    state._square_config = square_cfg
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
        state._finalize_session_topic:   asyncio.Queue(),
    }
    subscribed_topics = list(state._topic_queues.keys())

    # ── MQTT ───────────────────────────────────────────────────────────────
    state.mqtt_client = build_mqtt_client(mqtt_cfg, subscribed_topics)
    try:
        state.mqtt_client.connect(mqtt_cfg["host"], mqtt_cfg["port"], keepalive=60)
        state.mqtt_client.loop_start()
        log.info("MQTT loop started, connecting to %s:%s", mqtt_cfg["host"], mqtt_cfg["port"])
    except Exception as exc:
        log.error("Could not initiate MQTT connection: %s", exc)

    # ── Background tasks ─────────────────────────────────────────────────────
    _finalize_task = asyncio.create_task(finalize_session_consumer())

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────────
    _finalize_task.cancel()
    await asyncio.gather(_finalize_task, return_exceptions=True)
    log.info("finalize_session_consumer stopped")

    if state.mqtt_client:
        state.mqtt_client.loop_stop()
        state.mqtt_client.disconnect()
        log.info("MQTT client stopped")
