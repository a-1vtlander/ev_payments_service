"""
Shared mutable state for the EV Portal.

All globals are initialised to safe defaults here and written by lifespan.py
at startup.  Endpoints import this module and read/write the values directly.
"""

import asyncio
import os
from typing import Optional

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Allow overriding the options path via env var for local development / testing.
OPTIONS_PATH: str = os.environ.get("EV_OPTIONS_PATH", "/data/options.json")

RESPONSE_TIMEOUT: float = 15.0  # seconds to wait for any broker response

# ---------------------------------------------------------------------------
# Runtime state (initialised in lifespan)
# ---------------------------------------------------------------------------

mqtt_client: Optional[mqtt.Client] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# topic -> asyncio.Queue; populated at startup with all subscribed response topics.
_topic_queues: dict[str, asyncio.Queue] = {}

# Single lock that serialises the whole request→MQTT→response flow.
_session_lock: Optional[asyncio.Lock] = None

# Charger identity loaded from options.json.
_app_config: dict = {}

# Derived topic strings, set in lifespan.
_booking_response_topic: str = ""        # booking/response
_authorize_request_topic: str = ""       # booking/authorize_session  (publish)
_authorize_response_topic: str = ""      # booking/authorize_session/response  (subscribe)
_finalize_session_topic: str = ""        # booking/finalize_session  (subscribe)

# Square credentials and settings.
_square_config: dict = {}

# Admin interface config (username, password, port, tls settings).
_admin_config: dict = {}

# session_uid -> {booking_id, amount_cents}  – one-time tokens issued by /start.
_pending_sessions: dict[str, dict] = {}

# Square object state is now persisted to SQLite via db.py.
