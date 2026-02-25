"""
Shared pytest fixtures for the EV Charger Portal test suite.

Design note
-----------
`httpx.ASGITransport` sends only `http`-scoped ASGI events, so FastAPI's lifespan
(which would try to open a real MQTT connection) never fires during unit tests.
Instead, each test fixture sets module-level state directly:
  - main.mqtt_client       → MagicMock paho client
  - main._response_queue   → real asyncio.Queue (pre-populated by tests as needed)
  - main._session_lock     → real asyncio.Lock
  - main._app_config       → dict with home_id / charger_id
  - main._response_topic   → string
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

DEFAULT_HOME_ID = "test-home"
DEFAULT_CHARGER_ID = "test-charger"
DEFAULT_RESPONSE_TOPIC = f"ev/charger/{DEFAULT_HOME_ID}/{DEFAULT_CHARGER_ID}/booking/response"


# ---------------------------------------------------------------------------
# Reusable MQTT mock
# ---------------------------------------------------------------------------

@pytest.fixture
def connected_mqtt() -> MagicMock:
    """A mock paho Client that reports itself as connected and publishes successfully."""
    mock = MagicMock(spec=mqtt.Client)
    mock.is_connected.return_value = True
    mock.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS, mid=42)
    return mock


@pytest.fixture
def disconnected_mqtt() -> MagicMock:
    """A mock paho Client that reports itself as *not* connected."""
    mock = MagicMock(spec=mqtt.Client)
    mock.is_connected.return_value = False
    return mock


# ---------------------------------------------------------------------------
# App fixture helpers
# ---------------------------------------------------------------------------

def _inject_module_state(m, mqtt_mock: MagicMock, response_payload: str | None = None):
    """
    Inject all module-level state that lifespan normally sets up.
    Returns a dict of (attr, old_value) for teardown.
    """
    queue: asyncio.Queue = asyncio.Queue()
    if response_payload is not None:
        queue.put_nowait(response_payload)

    saved = {
        "mqtt_client": m.mqtt_client,
        "_response_queue": m._response_queue,
        "_session_lock": m._session_lock,
        "_app_config": m._app_config,
        "_response_topic": m._response_topic,
    }
    m.mqtt_client = mqtt_mock
    m._response_queue = queue
    m._session_lock = asyncio.Lock()
    m._app_config = {"home_id": DEFAULT_HOME_ID, "charger_id": DEFAULT_CHARGER_ID}
    m._response_topic = DEFAULT_RESPONSE_TOPIC
    return saved


def _restore_module_state(m, saved: dict):
    for attr, val in saved.items():
        setattr(m, attr, val)


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(connected_mqtt: MagicMock) -> AsyncClient:
    """
    AsyncClient with a connected MQTT mock and a pre-populated response queue
    containing a generic JSON response, so /start resolves immediately.
    """
    import main as m

    saved = _inject_module_state(
        m, connected_mqtt,
        response_payload='{"status": "ok", "session_id": "test-session-1"}',
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as c:
            yield c
    finally:
        _restore_module_state(m, saved)


@pytest_asyncio.fixture
async def client_no_response(connected_mqtt: MagicMock) -> AsyncClient:
    """
    AsyncClient with a connected MQTT mock but an empty response queue.
    Use to test timeout behaviour (override RESPONSE_TIMEOUT to a tiny value).
    """
    import main as m

    saved = _inject_module_state(m, connected_mqtt, response_payload=None)
    try:
        async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as c:
            yield c
    finally:
        _restore_module_state(m, saved)


@pytest_asyncio.fixture
async def client_no_mqtt(disconnected_mqtt: MagicMock) -> AsyncClient:
    """
    AsyncClient with a *disconnected* MQTT mock.
    Use to assert degraded-state behaviour.
    """
    import main as m

    saved = _inject_module_state(m, disconnected_mqtt, response_payload=None)
    try:
        async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as c:
            yield c
    finally:
        _restore_module_state(m, saved)
