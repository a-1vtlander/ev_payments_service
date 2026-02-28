"""
Shared pytest fixtures for the EV Charger Portal test suite.

Architecture
------------
All runtime globals live in ``state.py``, not on ``main.py``.
Unit/mock tests never trigger the FastAPI lifespan; instead they inject state
directly and use ``ASGITransport`` (which skips lifespan).

Integration/e2e tests use ``asgi-lifespan.LifespanManager`` with the real
lifespan but with options.json monkeypatched to point at a local broker.

Marker convention
-----------------
  (none)        pure unit/mock tests  – always run
  @sandbox      real Square sandbox API calls; network required
  @e2e          real mosquitto + real Square sandbox; mosquitto on PATH required
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import access
import db
import state

# ---------------------------------------------------------------------------
# Stable test identities
# ---------------------------------------------------------------------------

TEST_HOME_ID    = "test-home"
TEST_CHARGER_ID = "test-charger"
TEST_BOOKING_ID = "test-booking-42"
TEST_SESSION_ID = "aaaa0000-bbbb-cccc-dddd-eeeeeeeeeeee"

BOOKING_RESPONSE_TOPIC   = f"ev/charger/{TEST_HOME_ID}/{TEST_CHARGER_ID}/booking/response"
AUTHORIZE_REQUEST_TOPIC  = f"ev/charger/{TEST_HOME_ID}/{TEST_CHARGER_ID}/booking/authorize_session"
AUTHORIZE_RESPONSE_TOPIC = f"ev/charger/{TEST_HOME_ID}/{TEST_CHARGER_ID}/booking/authorize_session/response"
FINALIZE_TOPIC           = f"ev/charger/{TEST_HOME_ID}/{TEST_CHARGER_ID}/booking/finalize_session"

# Real sandbox credentials (sandbox env) – read by sandbox / e2e tests only.
SANDBOX_APP_ID    = "sandbox-sq0idb-d3YNYX4Uu3FOuC5nuWK1KA"
SANDBOX_TOKEN     = "EAAAlwAYpAv5_iEXxRYaU5wUCaQLBhGq8MzvUU20QNACgjk2I0jAfJ00hip2dt-f"

# ---------------------------------------------------------------------------
# Session-level ephemeral DB guard
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _guard_ephemeral_db(tmp_path_factory):
    """
    Redirect db.DB_PATH to a temp directory for the entire test session.

    This is a safety net on top of the per-test ``tmp_db`` fixture.  Even if a
    test bypasses ``tmp_db`` it will still write to a throwaway location rather
    than polluting the source tree or /data/ev_portal.db.

    Yields the session-wide temp DB path so tests can inspect it if needed.
    """
    session_tmp = tmp_path_factory.mktemp("session_db")
    session_db  = str(session_tmp / "ev_portal_session.db")
    original    = db.DB_PATH
    db.DB_PATH  = session_db
    yield session_db
    db.DB_PATH  = original

    # Assert no stale .db files were left inside the tests source tree.
    tests_dir = Path(__file__).parent
    stale = [
        p for p in tests_dir.rglob("*.db")
        # Ignore anything already inside a pytest tmp dir (not under tests/)
    ]
    if stale:
        raise AssertionError(
            f"Test run left persistent .db files in the tests source tree:\n"
            + "\n".join(f"  {p}" for p in stale)
        )


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def tmp_db(tmp_path: Path):
    """Fresh SQLite DB in a temp directory; restores db.DB_PATH afterwards."""
    db_file = str(tmp_path / "test_ev_portal.db")
    original = db.DB_PATH
    db.DB_PATH = db_file
    await db.init_db()
    yield db_file
    db.DB_PATH = original


# ---------------------------------------------------------------------------
# MQTT mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_mqtt() -> MagicMock:
    """A mock paho Client that reports connected and publishes successfully."""
    m = MagicMock(spec=mqtt.Client)
    m.is_connected.return_value = True
    m.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS, mid=42)
    return m


@pytest.fixture
def disconnected_mqtt() -> MagicMock:
    m = MagicMock(spec=mqtt.Client)
    m.is_connected.return_value = False
    return m


# ---------------------------------------------------------------------------
# State injection
# ---------------------------------------------------------------------------

class _StateSnapshot:
    """Save/restore all mutable state.* globals."""
    _ATTRS = (
        "mqtt_client", "_topic_queues", "_session_lock", "_app_config",
        "_square_config", "_admin_config", "_access_config",
        "_booking_response_topic",
        "_authorize_request_topic", "_authorize_response_topic",
        "_finalize_session_topic", "_pending_sessions", "_event_loop",
    )

    def __init__(self) -> None:
        self._saved = {a: getattr(state, a) for a in self._ATTRS}

    def restore(self) -> None:
        for attr, val in self._saved.items():
            setattr(state, attr, val)
        # Invalidate the access-control allow-list cache so the next test
        # rebuilds it from the restored state._access_config.
        access._allow_nets_cache = None


def _build_queues() -> dict:
    return {
        BOOKING_RESPONSE_TOPIC:   asyncio.Queue(),
        AUTHORIZE_RESPONSE_TOPIC: asyncio.Queue(),
        FINALIZE_TOPIC:           asyncio.Queue(),
    }


def _test_square_config(*, location_id: str = "LTEST00000000") -> dict:
    return {
        "sandbox":      True,
        "app_id":       SANDBOX_APP_ID,
        "access_token": SANDBOX_TOKEN,
        "location_id":  location_id,
        "charge_cents": 100,
    }


@pytest_asyncio.fixture
async def patched_state(mock_mqtt: MagicMock, tmp_db: str):
    """Inject all state globals for unit tests; restore on teardown."""
    snap = _StateSnapshot()

    state.mqtt_client               = mock_mqtt
    state._topic_queues             = _build_queues()
    state._session_lock             = asyncio.Lock()
    state._app_config               = {"home_id": TEST_HOME_ID, "charger_id": TEST_CHARGER_ID}
    state._square_config            = _test_square_config()
    state._admin_config             = {"enabled": False, "username": "admin", "password": "test", "port_https": 8091}
    state._access_config            = {"allow_cidrs": [], "default_charger_id": "", "applepay_domain_association": ""}
    state._booking_response_topic   = BOOKING_RESPONSE_TOPIC
    state._authorize_request_topic  = AUTHORIZE_REQUEST_TOPIC
    state._authorize_response_topic = AUTHORIZE_RESPONSE_TOPIC
    state._finalize_session_topic   = FINALIZE_TOPIC
    state._pending_sessions         = {}
    state._event_loop               = asyncio.get_running_loop()

    yield

    snap.restore()


# ---------------------------------------------------------------------------
# HTTP client fixtures (no lifespan)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def unit_client(patched_state) -> AsyncClient:
    """
    AsyncClient backed by ASGITransport (lifespan NOT triggered).
    State is injected by patched_state.
    """
    import main as m
    async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def unit_client_no_mqtt(disconnected_mqtt: MagicMock, tmp_db: str) -> AsyncClient:
    """AsyncClient with a disconnected MQTT mock."""
    snap = _StateSnapshot()
    state.mqtt_client               = disconnected_mqtt
    state._topic_queues             = _build_queues()
    state._session_lock             = asyncio.Lock()
    state._app_config               = {"home_id": TEST_HOME_ID, "charger_id": TEST_CHARGER_ID}
    state._square_config            = _test_square_config()
    state._access_config            = {"allow_cidrs": [], "default_charger_id": "", "applepay_domain_association": ""}
    state._booking_response_topic   = BOOKING_RESPONSE_TOPIC
    state._authorize_request_topic  = AUTHORIZE_REQUEST_TOPIC
    state._authorize_response_topic = AUTHORIZE_RESPONSE_TOPIC
    state._finalize_session_topic   = FINALIZE_TOPIC
    state._pending_sessions         = {}
    state._event_loop               = asyncio.get_running_loop()

    import main as m
    try:
        async with AsyncClient(transport=ASGITransport(app=m.app), base_url="http://test") as c:
            yield c
    finally:
        snap.restore()


# ---------------------------------------------------------------------------
# Helpers used by unit tests
# ---------------------------------------------------------------------------

def make_booking_response(
    booking_id: str = TEST_BOOKING_ID,
    amount_dollars: float = 1.00,
) -> str:
    return json.dumps({
        "booking_id": booking_id,
        "initial_authorization_amount": amount_dollars,
    })


def make_authorize_response(success: bool = True) -> str:
    return json.dumps({"success": success})


async def push_after(queue: asyncio.Queue, payload: str, delay: float = 0.05) -> None:
    """Push a payload into a queue after a short delay (used from test tasks)."""
    await asyncio.sleep(delay)
    queue.put_nowait(payload)


# ---------------------------------------------------------------------------
# Local Mosquitto broker (used by e2e tests)
# ---------------------------------------------------------------------------

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18832


def _mosquitto_bin() -> str:
    path = shutil.which("mosquitto")
    if not path:
        pytest.skip(
            "mosquitto not found on PATH — install it to run e2e tests "
            "(macOS: brew install mosquitto; Debian: apt-get install mosquitto)"
        )
    return path  # type: ignore[return-value]


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Port {host}:{port} did not open within {timeout}s")


@pytest_asyncio.fixture
async def mosquitto_broker(tmp_path: Path):
    """Start a local anonymous mosquitto broker and yield; terminate on teardown."""
    config_file = tmp_path / "mosquitto.conf"
    config_file.write_text(
        f"listener {BROKER_PORT} {BROKER_HOST}\n"
        "allow_anonymous true\n"
        "log_type none\n"
    )
    proc = subprocess.Popen(
        [_mosquitto_bin(), "-c", str(config_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(BROKER_HOST, BROKER_PORT)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest_asyncio.fixture
async def live_client(mosquitto_broker, tmp_path: Path, monkeypatch, tmp_db: str):
    """
    FastAPI app with its full lifespan (real MQTT, real Square sandbox).
    OPTIONS_PATH is monkeypatched to a temp file pointing at the test broker.
    square_sandbox is forced True regardless of config.yaml.
    """
    import main as m

    options = {
        "mqtt_host":            BROKER_HOST,
        "mqtt_port":            BROKER_PORT,
        "mqtt_username":        "",
        "mqtt_password":        "",
        "home_id":              TEST_HOME_ID,
        "charger_id":           TEST_CHARGER_ID,
        "square_sandbox":       True,          # always sandbox
        "square_sandbox_app_id":        SANDBOX_APP_ID,
        "square_sandbox_access_token":  SANDBOX_TOKEN,
        "square_production_app_id":      "",
        "square_production_access_token": "",
        "square_location_id":   "",            # auto-fetched from Square
        "square_charge_cents":  100,
        # Admin disabled in integration tests to avoid binding a second port.
        "admin_enabled":        False,
        "admin_username":       "admin",
        "admin_password":       "",
        "admin_port_https":     8091,
        "admin_tls_mode":       "self_signed",
        "admin_tls_cert_path":  "",
        "admin_tls_key_path":   "",
    }
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))
    monkeypatch.setattr(state, "OPTIONS_PATH", str(options_file))

    from asgi_lifespan import LifespanManager
    async with LifespanManager(m.app) as manager:
        for _ in range(50):
            if state.mqtt_client and state.mqtt_client.is_connected():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("paho MQTT did not connect within 2.5s")

        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
        ) as c:
            yield c
