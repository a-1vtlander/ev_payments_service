"""
Integration tests have been superseded by tests/test_e2e.py.

End-to-end tests (real Mosquitto + real Square sandbox) now live in:
    tests/test_e2e.py          -- run with: pytest -m e2e
    tests/test_square_sandbox.py -- run with: pytest -m sandbox
"""


Stack
-----
* mosquitto   – started as a subprocess (must be on PATH; see requirements-test.txt)
* asgi-lifespan – fires FastAPI startup/shutdown so paho actually connects
* aiomqtt     – async MQTT subscriber to receive and assert on published messages
* httpx       – HTTP client driving the endpoints

Run
---
    pytest -v tests/test_integration.py

Prerequisites (one-time install):
    macOS:  brew install mosquitto
    Debian: sudo apt-get install mosquitto
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import aiomqtt
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

BROKER_HOST = "127.0.0.1"
BROKER_PORT = 18830


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mosquitto_bin() -> str:
    path = shutil.which("mosquitto")
    if not path:
        pytest.skip(
            "mosquitto not found on PATH – install it to run integration tests "
            "(macOS: brew install mosquitto; Debian: apt-get install mosquitto)"
        )
    return path  # type: ignore[return-value]


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    """Block until a TCP port accepts connections or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Port {host}:{port} did not open within {timeout}s")


# ---------------------------------------------------------------------------
# Mosquitto broker fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def mosquitto_broker(tmp_path: Path):
    """
    Write a minimal anonymous Mosquitto config to a temp dir,
    start mosquitto as a subprocess, yield, then terminate it.
    """
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


# ---------------------------------------------------------------------------
# App fixture with real lifespan + real MQTT connection
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def live_client(mosquitto_broker, tmp_path: Path, monkeypatch):
    """
    FastAPI app with its lifespan running (paho connects to the subprocess broker).
    OPTIONS_PATH is monkeypatched to a temp file pointing at the test broker.
    """
    import main as m

    options = {
        "mqtt_host": BROKER_HOST,
        "mqtt_port": BROKER_PORT,
        "mqtt_username": "",
        "mqtt_password": "",
    }
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))
    monkeypatch.setattr(m, "OPTIONS_PATH", str(options_file))

    async with LifespanManager(m.app) as manager:
        # paho connects in a background thread via loop_start(); poll is_connected().
        for _ in range(50):
            if m.mqtt_client and m.mqtt_client.is_connected():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("paho MQTT did not connect to test broker within 2.5 s")

        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# Helper: collect exactly one message from a topic filter
# ---------------------------------------------------------------------------

async def _collect_one(topic_filter: str, timeout: float = 5.0) -> aiomqtt.Message:
    async def _inner() -> aiomqtt.Message:
        async with aiomqtt.Client(hostname=BROKER_HOST, port=BROKER_PORT) as sub:
            await sub.subscribe(topic_filter, qos=1)
            async for msg in sub.messages:
                return msg  # type: ignore[return-value]
        raise RuntimeError("No message received")  # pragma: no cover

    return await asyncio.wait_for(_inner(), timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_integration_health(live_client: AsyncClient) -> None:
    """/health returns 200 with a live MQTT connection."""
    resp = await live_client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_integration_start_publishes_message(live_client: AsyncClient) -> None:
    """
    End-to-end: HTTP GET /start → paho publish → Mosquitto → aiomqtt subscriber.
    Asserts the full message flow, topic, and payload shape.
    """
    # Subscribe before making the request so we cannot miss the message.
    collect_task = asyncio.create_task(_collect_one("ev/#"))
    await asyncio.sleep(0.15)  # give aiomqtt time to fully subscribe

    resp = await live_client.get("/start?charger_id=integration-charger")
    assert resp.status_code == 200
    assert "integration-charger" in resp.text

    msg = await collect_task

    assert str(msg.topic) == "ev/integration-charger/start"
    payload = json.loads(msg.payload)
    assert payload["charger_id"] == "integration-charger"
    assert "timestamp" in payload


@pytest.mark.asyncio
async def test_integration_start_correct_qos(live_client: AsyncClient) -> None:
    """Message must be published at QoS 1."""
    collect_task = asyncio.create_task(_collect_one("ev/#"))
    await asyncio.sleep(0.15)

    await live_client.get("/start?charger_id=qos-check")

    msg = await collect_task
    assert msg.qos == 1


@pytest.mark.asyncio
async def test_integration_payload_timestamp_is_iso8601_utc(
    live_client: AsyncClient,
) -> None:
    """Timestamp in published payload must be a UTC ISO-8601 string."""
    collect_task = asyncio.create_task(_collect_one("ev/#"))
    await asyncio.sleep(0.15)

    await live_client.get("/start?charger_id=ts-check")

    msg = await collect_task
    payload = json.loads(msg.payload)
    assert re.search(r"\+00:00$|Z$", payload["timestamp"]), (
        f"Expected UTC ISO-8601 timestamp, got: {payload['timestamp']}"
    )


@pytest.mark.asyncio
async def test_integration_multiple_chargers(live_client: AsyncClient) -> None:
    """Each charger ID must produce a message on its own dedicated topic."""
    charger_ids = ["charger-A", "charger-B", "charger-C"]
    collected: list[aiomqtt.Message] = []

    async def collect_n(n: int) -> None:
        async def _inner() -> None:
            async with aiomqtt.Client(hostname=BROKER_HOST, port=BROKER_PORT) as sub:
                await sub.subscribe("ev/#", qos=1)
                async for msg in sub.messages:
                    collected.append(msg)
                    if len(collected) == n:
                        return

        await asyncio.wait_for(_inner(), timeout=5.0)

    collect_task = asyncio.create_task(collect_n(len(charger_ids)))
    await asyncio.sleep(0.15)

    for cid in charger_ids:
        resp = await live_client.get(f"/start?charger_id={cid}")
        assert resp.status_code == 200

    await collect_task

    topics = {str(m.topic) for m in collected}
    assert topics == {f"ev/{cid}/start" for cid in charger_ids}
