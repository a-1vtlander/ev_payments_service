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


# ---------------------------------------------------------------------------
# filter_access_to CIDR parsing – all input forms that HA may produce
# ---------------------------------------------------------------------------

_BASE_OPTS = {
    "mqtt_host": "broker.local", "mqtt_port": 1883,
    "mqtt_username": "", "mqtt_password": "",
    "home_id": "h1", "charger_id": "c1",
    "square_sandbox": True,
    "square_sandbox_app_id": "app", "square_sandbox_access_token": "tok",
    "square_production_app_id": "", "square_production_access_token": "",
    "square_location_id": "LOC1", "square_charge_cents": 100,
    "admin_enabled": False, "admin_username": "admin", "admin_password": "",
    "admin_port_https": 8091, "admin_tls_mode": "self_signed",
    "admin_tls_cert_path": "", "admin_tls_key_path": "",
}


@pytest.mark.parametrize("raw,expected", [
    # 1. Normal YAML list (the intended form)
    (["192.168.1.0/24", "10.0.0.0/8"], ["192.168.1.0/24", "10.0.0.0/8"]),
    # 2. Comma-separated string
    ("192.168.1.0/24, 10.0.0.0/8", ["192.168.1.0/24", "10.0.0.0/8"]),
    # 3. JSON-encoded string – HA UI quirk: user types ["192.168.1.0/24"] in the field
    ('["192.168.1.0/24"]', ["192.168.1.0/24"]),
    ('["192.168.1.0/24","10.0.0.0/8"]', ["192.168.1.0/24", "10.0.0.0/8"]),
    # 4. List containing a JSON array string – HA passes the whole JSON string as one element
    (['["192.168.1.0/24"]'], ["192.168.1.0/24"]),
    (['["192.168.1.0/24","10.0.0.0/8"]'], ["192.168.1.0/24", "10.0.0.0/8"]),
    # 5. Empty / not set
    ([], []),
    (None, []),
    ("", []),
])
def test_load_config_filter_access_to_parsing(raw, expected, tmp_path, monkeypatch) -> None:
    """config.load_config() must normalise all filter_access_to input forms to a clean list."""
    import config
    import state

    opts = {**_BASE_OPTS, "filter_access_to": raw}
    f = tmp_path / "options.json"
    f.write_text(json.dumps(opts))
    monkeypatch.setattr(state, "OPTIONS_PATH", str(f))

    cfg = config.load_config()
    assert cfg["access"]["allow_cidrs"] == expected, \
        f"filter_access_to={raw!r} should parse to {expected}, got {cfg['access']['allow_cidrs']}"
