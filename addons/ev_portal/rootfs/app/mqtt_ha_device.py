"""
mqtt_ha_device.py — Home Assistant MQTT Discovery device for the EV Payment Manager.

Exposes a single HA device ("EV Charger Payment Manager") with four entities:

  number  ev_rate_per_kwh          — writable rate ($/kWh)
  sensor  ev_total_bill            — read-only accrued bill ($)
  text    ev_charger_switch_entity — writable HA entity_id of the charger switch
  text    ev_power_usage_entity    — writable HA entity_id of the power-usage sensor

Lifecycle (called from lifespan.py):
  publish_discovery(client)           — publish retained discovery/config payloads
  subscribe_command_topics(client)    — register on_message handler additions
  publish_state(client)               — publish retained state for all entities
  handle_command(topic, payload)      — route an inbound command to the right handler

State lives in _DeviceState (module-level singleton).  Persistence is behind
a single function (_persist) that is currently a no-op but is designed to be
replaced without touching any other code in this module.

Topic scheme (all under homeassistant/ or evpm/):
  Discovery : homeassistant/<component>/ev_payment_manager/<object_id>/config
  State     : evpm/<object_id>/state
  Command   : evpm/<object_id>/set
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device metadata — one place, referenced by all discovery payloads.
# ---------------------------------------------------------------------------

_DEVICE_ID    = "ev_payment_manager"
_DEVICE_NAME  = "EV Charger Payment Manager"
_MANUFACTURER = "Extravio"
_MODEL        = "EV Portal"

_DEVICE_BLOCK = {
    "identifiers":  [_DEVICE_ID],
    "name":         _DEVICE_NAME,
    "manufacturer": _MANUFACTURER,
    "model":        _MODEL,
}

# ---------------------------------------------------------------------------
# Topic helpers — computed once and reused everywhere.
# ---------------------------------------------------------------------------

def _state_topic(object_id: str) -> str:
    return f"evpm/{object_id}/state"

def _command_topic(object_id: str) -> str:
    return f"evpm/{object_id}/set"

def _discovery_topic(component: str, object_id: str) -> str:
    return f"homeassistant/{component}/{_DEVICE_ID}/{object_id}/config"

# Object-IDs for the four entities.  These are also used as unique_id suffixes.
_OID_RATE       = "rate_per_kwh"
_OID_BILL       = "total_bill"
_OID_SWITCH_ENT = "charger_switch_entity"
_OID_POWER_ENT  = "power_usage_entity"

# All writable entities – command topics that on_message should handle.
_COMMAND_TOPICS = {
    _command_topic(_OID_RATE),
    _command_topic(_OID_SWITCH_ENT),
    _command_topic(_OID_POWER_ENT),
}

# ---------------------------------------------------------------------------
# Server-side state model
# ---------------------------------------------------------------------------

@dataclass
class _DeviceState:
    rate_per_kwh:            float = 0.0
    total_bill:              float = 0.0
    charger_switch_entity_id: str  = ""
    power_usage_entity_id:   str   = ""


# Module-level singleton — the single source of truth for this device's state.
_state = _DeviceState()


def get_state() -> _DeviceState:
    """Return the current device state (read-only from callers)."""
    return _state


# ---------------------------------------------------------------------------
# Persistence stub — replace this body to add real persistence.
# ---------------------------------------------------------------------------

def _persist() -> None:
    """
    Persist _state to durable storage.

    Currently a no-op.  To add persistence, update _state from a DB row at
    startup and write the row here.  No other code in this module needs to change.
    """
    pass


# ---------------------------------------------------------------------------
# Discovery payload builders — one function per entity, no magic.
# ---------------------------------------------------------------------------

def _rate_discovery_payload() -> dict:
    return {
        "unique_id":    f"{_DEVICE_ID}_{_OID_RATE}",
        "name":         "Rate per kWh",
        "object_id":    _OID_RATE,
        "device":       _DEVICE_BLOCK,
        "state_topic":  _state_topic(_OID_RATE),
        "command_topic": _command_topic(_OID_RATE),
        "unit_of_measurement": "$/kWh",
        "min":          0.0,
        "max":          99.99,
        "step":         0.01,
        "device_class": "monetary",
        "icon":         "mdi:currency-usd",
        "retain":       True,
    }


def _total_bill_discovery_payload() -> dict:
    return {
        "unique_id":    f"{_DEVICE_ID}_{_OID_BILL}",
        "name":         "Total Bill",
        "object_id":    _OID_BILL,
        "device":       _DEVICE_BLOCK,
        "state_topic":  _state_topic(_OID_BILL),
        "unit_of_measurement": "$",
        "device_class": "monetary",
        "state_class":  "measurement",
        "icon":         "mdi:cash-register",
    }


def _charger_switch_entity_discovery_payload() -> dict:
    return {
        "unique_id":    f"{_DEVICE_ID}_{_OID_SWITCH_ENT}",
        "name":         "Charger Switch Entity",
        "object_id":    _OID_SWITCH_ENT,
        "device":       _DEVICE_BLOCK,
        "state_topic":  _state_topic(_OID_SWITCH_ENT),
        "command_topic": _command_topic(_OID_SWITCH_ENT),
        "icon":         "mdi:toggle-switch",
        "retain":       True,
    }


def _power_usage_entity_discovery_payload() -> dict:
    return {
        "unique_id":    f"{_DEVICE_ID}_{_OID_POWER_ENT}",
        "name":         "Power Usage Entity",
        "object_id":    _OID_POWER_ENT,
        "device":       _DEVICE_BLOCK,
        "state_topic":  _state_topic(_OID_POWER_ENT),
        "command_topic": _command_topic(_OID_POWER_ENT),
        "icon":         "mdi:lightning-bolt",
        "retain":       True,
    }


# ---------------------------------------------------------------------------
# Discovery publishing
# ---------------------------------------------------------------------------

def publish_discovery(client: mqtt.Client) -> None:
    """
    Publish retained HA MQTT discovery payloads for all four entities.
    Safe to call again after a reconnect.
    """
    entities = [
        ("number", _OID_RATE,       _rate_discovery_payload()),
        ("sensor", _OID_BILL,       _total_bill_discovery_payload()),
        ("text",   _OID_SWITCH_ENT, _charger_switch_entity_discovery_payload()),
        ("text",   _OID_POWER_ENT,  _power_usage_entity_discovery_payload()),
    ]
    for component, object_id, payload in entities:
        topic = _discovery_topic(component, object_id)
        client.publish(topic, json.dumps(payload), qos=1, retain=True)
        log.info("Published HA discovery for %s → %s", object_id, topic)


# ---------------------------------------------------------------------------
# State publishing
# ---------------------------------------------------------------------------

def publish_state(client: mqtt.Client) -> None:
    """
    Publish current state to all state topics as retained messages.
    Call once after discovery, and again whenever state changes.
    """
    _publish_single(client, _OID_RATE,       f"{_state.rate_per_kwh:.4f}")
    _publish_single(client, _OID_BILL,       f"{_state.total_bill:.4f}")
    _publish_single(client, _OID_SWITCH_ENT, _state.charger_switch_entity_id)
    _publish_single(client, _OID_POWER_ENT,  _state.power_usage_entity_id)


def _publish_single(client: mqtt.Client, object_id: str, value: str) -> None:
    topic = _state_topic(object_id)
    client.publish(topic, value, qos=1, retain=True)
    log.info("Published state %s = %r", topic, value)


# ---------------------------------------------------------------------------
# Command-topic subscription
# ---------------------------------------------------------------------------

def command_topics() -> list:
    """Return the list of command topics this module needs subscribed."""
    return sorted(_COMMAND_TOPICS)


# ---------------------------------------------------------------------------
# Inbound command handling
# ---------------------------------------------------------------------------

def handle_command(topic: str, raw_payload: str, client: mqtt.Client) -> None:
    """
    Route an inbound command from HA to the appropriate handler.

    Called by mqtt.py's on_message when the topic matches a command topic.
    All validation happens here; _state is mutated only after validation.
    """
    if topic == _command_topic(_OID_RATE):
        _handle_rate(raw_payload, client)
    elif topic == _command_topic(_OID_SWITCH_ENT):
        _handle_entity_ref(_OID_SWITCH_ENT, raw_payload, client)
    elif topic == _command_topic(_OID_POWER_ENT):
        _handle_entity_ref(_OID_POWER_ENT, raw_payload, client)
    else:
        log.warning("handle_command: no handler for topic %r", topic)


def _handle_rate(raw_payload: str, client: mqtt.Client) -> None:
    """Validate and apply a new rate_per_kwh value."""
    value = raw_payload.strip()
    try:
        rate = float(value)
    except ValueError:
        log.warning("Ignoring invalid rate payload %r: not a number", value)
        return
    if rate < 0 or rate > 99.99:
        log.warning("Ignoring out-of-range rate %s (must be 0–99.99)", rate)
        return
    _state.rate_per_kwh = rate
    log.info("rate_per_kwh updated to %.4f", rate)
    _persist()
    _publish_single(client, _OID_RATE, f"{_state.rate_per_kwh:.4f}")


def _handle_entity_ref(object_id: str, raw_payload: str, client: mqtt.Client) -> None:
    """
    Validate and store an HA entity_id reference (e.g. 'switch.ev_charger').

    Accepts any non-empty string that looks like a valid HA entity_id.
    entity_ids are of the form: <domain>.<name>
    """
    value = raw_payload.strip()
    if not value:
        log.warning("Ignoring empty entity_id payload for %s", object_id)
        return
    # Basic structural validation: must contain exactly one dot, both sides non-empty.
    parts = value.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        log.warning(
            "Ignoring malformed entity_id %r for %s (expected <domain>.<name>)",
            value, object_id,
        )
        return
    if object_id == _OID_SWITCH_ENT:
        _state.charger_switch_entity_id = value
        log.info("charger_switch_entity_id set to %r", value)
    elif object_id == _OID_POWER_ENT:
        _state.power_usage_entity_id = value
        log.info("power_usage_entity_id set to %r", value)
    _persist()
    _publish_single(client, object_id, value)
