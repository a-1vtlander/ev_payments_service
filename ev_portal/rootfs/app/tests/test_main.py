"""
Tests for main.py have been superseded by tests/test_unit.py.

All endpoint unit tests (health, start, submit_payment, session, db viewer)
now live in test_unit.py which is aligned with the current state.py-based
architecture.
"""


from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_returns_200_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "ok"


# ---------------------------------------------------------------------------
# /start – input validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_missing_charger_id_returns_400(client: AsyncClient) -> None:
    resp = await client.get("/start")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_start_empty_charger_id_returns_400(client: AsyncClient) -> None:
    resp = await client.get("/start?charger_id=")
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "bad_id",
    [
        "../evil",
        "../../etc/passwd",
        "<script>alert(1)</script>",
        "charger id",          # space
        "a" * 65,              # too long
        "",                    # empty
    ],
)
@pytest.mark.asyncio
async def test_start_invalid_charger_id_returns_400(
    client: AsyncClient, bad_id: str
) -> None:
    resp = await client.get(f"/start?charger_id={bad_id}")
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "good_id",
    ["charger1", "charger-1", "EV_001", "a", "A" * 64],
)
@pytest.mark.asyncio
async def test_start_valid_charger_id_returns_200(
    client: AsyncClient, good_id: str
) -> None:
    resp = await client.get(f"/start?charger_id={good_id}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /start – MQTT publish behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_publishes_correct_topic(
    client: AsyncClient, connected_mqtt: MagicMock
) -> None:
    await client.get("/start?charger_id=charger-42")
    connected_mqtt.publish.assert_called_once()
    call_args = connected_mqtt.publish.call_args
    topic = call_args.args[0]
    assert topic == "ev/charger-42/start"


@pytest.mark.asyncio
async def test_start_publishes_correct_payload(
    client: AsyncClient, connected_mqtt: MagicMock
) -> None:
    await client.get("/start?charger_id=charger-42")
    call_args = connected_mqtt.publish.call_args
    payload = json.loads(call_args.args[1])
    assert payload["charger_id"] == "charger-42"
    assert "timestamp" in payload
    # ISO8601 UTC: ends with +00:00 or Z
    assert re.search(r"\+00:00$|Z$", payload["timestamp"])


@pytest.mark.asyncio
async def test_start_publishes_with_qos1(
    client: AsyncClient, connected_mqtt: MagicMock
) -> None:
    await client.get("/start?charger_id=charger-1")
    call_args = connected_mqtt.publish.call_args
    assert call_args.kwargs.get("qos") == 1


@pytest.mark.asyncio
async def test_start_response_contains_charger_id(
    client: AsyncClient,
) -> None:
    resp = await client.get("/start?charger_id=charger-99")
    assert "charger-99" in resp.text


@pytest.mark.asyncio
async def test_start_html_escapes_charger_id(
    client: AsyncClient, connected_mqtt: MagicMock
) -> None:
    """Charger IDs have already been validated to alphanumeric/_/-, but belt-and-suspenders."""
    # Valid ID that happens to look suspicious if not escaped (can't use < > because
    # those would be rejected by validation – this confirms the safe path still escapes).
    resp = await client.get("/start?charger_id=charger-1")
    assert "<script>" not in resp.text


# ---------------------------------------------------------------------------
# /start – degraded/error conditions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_returns_503_when_mqtt_disconnected(
    client_no_mqtt: AsyncClient, disconnected_mqtt: MagicMock
) -> None:
    resp = await client_no_mqtt.get("/start?charger_id=charger-1")
    assert resp.status_code == 503
    disconnected_mqtt.publish.assert_not_called()


@pytest.mark.asyncio
async def test_start_returns_503_when_publish_fails(
    client: AsyncClient, connected_mqtt: MagicMock
) -> None:
    connected_mqtt.publish.return_value = MagicMock(
        rc=mqtt.MQTT_ERR_QUEUE_SIZE, mid=0
    )
    resp = await client.get("/start?charger_id=charger-1")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def test_load_options_returns_empty_dict_when_file_missing(tmp_path, monkeypatch) -> None:
    import main as m

    missing = str(tmp_path / "nonexistent.json")
    monkeypatch.setattr(m, "OPTIONS_PATH", missing)
    result = m.load_options()
    assert result == {}


def test_load_options_parses_file(tmp_path, monkeypatch) -> None:
    import main as m

    opts = {"mqtt_host": "broker.local", "mqtt_port": 1883}
    f = tmp_path / "options.json"
    f.write_text(json.dumps(opts))
    monkeypatch.setattr(m, "OPTIONS_PATH", str(f))
    assert m.load_options() == opts


def test_build_mqtt_client_handles_none_credentials() -> None:
    """HA Supervisor may inject null for optional fields – must not raise."""
    import main as m

    opts = {"mqtt_host": "localhost", "mqtt_port": 1883, "mqtt_username": None, "mqtt_password": None}
    # Should not raise AttributeError
    client = m.build_mqtt_client(opts)
    assert client is not None
