"""
Surviving tests from the original test_main.py.

Endpoint tests (health, start, etc.) were superseded by tests/test_unit.py.
Only config and MQTT client construction tests remain here.
"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def test_load_config_raises_when_file_missing(tmp_path, monkeypatch) -> None:
    """config.load_config() must raise RuntimeError when the options file is absent."""
    import config
    import state

    missing = str(tmp_path / "nonexistent.json")
    monkeypatch.setattr(state, "OPTIONS_PATH", missing)
    with pytest.raises(RuntimeError, match="Options file not found"):
        config.load_config()


def test_load_config_returns_structured_dict(tmp_path, monkeypatch) -> None:
    """config.load_config() returns a structured dict when all required fields are present."""
    import config
    import state

    opts = {
        "mqtt_host": "broker.local",
        "mqtt_port": 1883,
        "mqtt_username": "user",
        "mqtt_password": "pass",
        "home_id": "home1",
        "charger_id": "charger-1",
        "square_sandbox": True,
        "square_sandbox_app_id": "sandbox-app-id",
        "square_sandbox_access_token": "sandbox-token",
        "square_production_app_id": "",
        "square_production_access_token": "",
        "square_location_id": "LOCATION1",
        "square_charge_cents": 150,
        "admin_enabled": True,
        "admin_username": "admin",
        "admin_password": "secret",
        "admin_port_https": 8091,
        "admin_tls_mode": "self_signed",
        "admin_tls_cert_path": "",
        "admin_tls_key_path": "",
    }
    f = tmp_path / "options.json"
    f.write_text(json.dumps(opts))
    monkeypatch.setattr(state, "OPTIONS_PATH", str(f))

    cfg = config.load_config()
    assert cfg["mqtt"]["host"] == "broker.local"
    assert cfg["mqtt"]["port"] == 1883
    assert cfg["square"]["sandbox"] is True
    assert cfg["square"]["charge_cents"] == 150
    assert cfg["app"]["home_id"] == "home1"
    assert cfg["admin"]["enabled"] is True
    assert cfg["admin"]["username"] == "admin"


def test_build_mqtt_client_handles_none_credentials() -> None:
    """HA Supervisor may inject null for optional fields – must not raise."""
    import mqtt as m

    mqtt_cfg = {"host": "localhost", "port": 1883, "username": None, "password": ""}
    # Should not raise AttributeError
    client = m.build_mqtt_client(mqtt_cfg, [])
    assert client is not None
