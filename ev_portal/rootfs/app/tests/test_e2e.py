"""
End-to-end tests: real local Mosquitto broker + real Square sandbox API.

Run with:
    pytest -m e2e -v tests/test_e2e.py

Prerequisites
-------------
1. mosquitto on PATH  (macOS: brew install mosquitto)
2. Valid Square sandbox credentials in tests/dev_options.json
3. Network access to connect.squareupsandbox.com

How it works
------------
  - ``live_client`` fixture (conftest.py) starts mosquitto, fires the FastAPI
    lifespan (real MQTT connect, DB init, background tasks), then provides an
    AsyncClient.
  - Tests publish to MQTT topics that the app is subscribed to; the app reacts
    as it would in production.
  - ``aiomqtt`` is used to subscribe and receive messages published by the app.

Idempotency warning
-------------------
Each test gets a fresh DB (tmp_db fixture in conftest's live_client), so there
is no cross-test state leakage within a single run.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import aiomqtt
import pytest
import pytest_asyncio
from httpx import AsyncClient

import db
import square
import state
from tests.conftest import (
    AUTHORIZE_RESPONSE_TOPIC,
    BOOKING_RESPONSE_TOPIC,
    BROKER_HOST,
    BROKER_PORT,
    FINALIZE_TOPIC,
    TEST_BOOKING_ID,
    TEST_CHARGER_ID,
    TEST_HOME_ID,
)

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _mqtt_publish(topic: str, payload: str | dict, qos: int = 1) -> None:
    """Publish one message to the running test broker."""
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    async with aiomqtt.Client(hostname=BROKER_HOST, port=BROKER_PORT) as pub:
        await pub.publish(topic, payload, qos=qos)


async def _collect_one(topic_filter: str, timeout: float = 5.0) -> aiomqtt.Message:
    """Subscribe and return the first matching message."""
    async def _inner() -> aiomqtt.Message:
        async with aiomqtt.Client(hostname=BROKER_HOST, port=BROKER_PORT) as sub:
            await sub.subscribe(topic_filter, qos=1)
            async for msg in sub.messages:
                return msg
        raise RuntimeError("No message received")  # pragma: no cover

    return await asyncio.wait_for(_inner(), timeout=timeout)


async def _ensure_location_id() -> None:
    if not state._square_config.get("location_id"):
        state._square_config["location_id"] = await square.fetch_first_location_id()


# ---------------------------------------------------------------------------
# Basic connectivity / health
# ---------------------------------------------------------------------------

async def test_e2e_health(live_client: AsyncClient) -> None:
    """App is up and MQTT is connected."""
    resp = await live_client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /start – MQTT booking request published to broker
# ---------------------------------------------------------------------------

async def test_e2e_start_publishes_booking_request(live_client: AsyncClient) -> None:
    """GET /start causes the app to publish a booking request_session message."""
    collect = asyncio.create_task(
        _collect_one(f"ev/charger/{TEST_HOME_ID}/{TEST_CHARGER_ID}/#")
    )
    await asyncio.sleep(0.15)

    booking_id = f"e2e-{uuid.uuid4().hex[:8]}"

    async def push_booking_response():
        await asyncio.sleep(0.2)
        await _mqtt_publish(
            BOOKING_RESPONSE_TOPIC,
            {"booking_id": booking_id, "initial_authorization_amount": 1.00},
        )

    asyncio.create_task(push_booking_response())
    resp = await live_client.get("/start")

    request_msg = await collect
    topic_str = str(request_msg.topic)
    assert topic_str.endswith("/booking/request_session")
    assert resp.status_code in (200, 503)  # 503 if Square location not configured yet


async def test_e2e_start_renders_card_form(live_client: AsyncClient) -> None:
    """GET /start with valid Square config returns the card form."""
    await _ensure_location_id()
    booking_id = f"e2e-{uuid.uuid4().hex[:8]}"

    async def push_booking():
        await asyncio.sleep(0.1)
        await _mqtt_publish(
            BOOKING_RESPONSE_TOPIC,
            {"booking_id": booking_id, "initial_authorization_amount": 1.50},
        )

    asyncio.create_task(push_booking())
    resp = await live_client.get("/start")
    assert resp.status_code == 200
    assert "square" in resp.text.lower()


# ---------------------------------------------------------------------------
# Full payment authorize flow (real Square sandbox)
# ---------------------------------------------------------------------------

async def test_e2e_submit_payment_success(live_client: AsyncClient) -> None:
    """
    Full flow:
      GET /start → MQTT booking response → card form
      POST /submit_payment (sandbox nonce) → Square API → MQTT authorize_session
      Simulate HA publishing authorize_session/response → success JSON
    """
    await _ensure_location_id()
    booking_id = f"e2e-{uuid.uuid4().hex[:8]}"

    # Step 1: GET /start
    async def push_booking():
        await asyncio.sleep(0.1)
        await _mqtt_publish(
            BOOKING_RESPONSE_TOPIC,
            {"booking_id": booking_id, "initial_authorization_amount": 1.00},
        )

    asyncio.create_task(push_booking())
    start_resp = await live_client.get("/start")
    assert start_resp.status_code == 200

    # Get session UID from pending_sessions
    uid = next(
        uid for uid, v in state._pending_sessions.items()
        if v["booking_id"] == booking_id
    )

    # Step 2: Simulate HA authorizing after the app publishes to authorize topic
    async def push_authorize_response():
        await asyncio.sleep(0.5)
        await _mqtt_publish(AUTHORIZE_RESPONSE_TOPIC, {"success": True})

    asyncio.create_task(push_authorize_response())

    pay_resp = await live_client.post("/submit_payment", data={
        "source_id":   "cnon:card-nonce-ok",
        "uid":         uid,
        "given_name":  "E2E",
        "family_name": "Tester",
    })

    assert pay_resp.status_code == 200
    body = pay_resp.json()
    assert body["status"]     == "success"
    assert body["booking_id"] == booking_id

    # DB must be AUTHORIZED
    ik = f"ev:{TEST_CHARGER_ID}:{booking_id}"
    row = await db.get_session(ik)
    assert row["state"] == "AUTHORIZED"

    # Cleanup: cancel the pre-auth in Square
    await square.cancel_payment(row["square_payment_id"])


# ---------------------------------------------------------------------------
# Finalize – capture at final amount
# ---------------------------------------------------------------------------

async def test_e2e_finalize_capture(live_client: AsyncClient) -> None:
    """Publishing finalize_session triggers Square capture; DB → CAPTURED."""
    await _ensure_location_id()
    booking_id = f"e2e-{uuid.uuid4().hex[:8]}"
    ik = f"ev:{TEST_CHARGER_ID}:{booking_id}"

    card_id, customer_id, card_meta = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id=booking_id,
        given_name="E2E",
        family_name="Capture",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id=booking_id,
        amount_cents=500,
    )
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              booking_id,
        "session_id":              str(uuid.uuid4()),
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 500,
        "square_environment":      "sandbox",
        "square_payment_id":       payment["id"],
        **card_meta,
    })

    await _mqtt_publish(FINALIZE_TOPIC, {"booking_id": booking_id, "final_amount_cents": 250})

    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        row = await db.get_session(ik)
        if row and row["state"] in ("CAPTURED", "FAILED"):
            break
        await asyncio.sleep(0.25)

    row = await db.get_session(ik)
    assert row["state"] == "CAPTURED"
    assert row["captured_amount_cents"] == 250


# ---------------------------------------------------------------------------
# Finalize – void (amount == 0)
# ---------------------------------------------------------------------------

async def test_e2e_finalize_void(live_client: AsyncClient) -> None:
    """Publishing finalize_session with amount=0 voids the pre-auth; DB → VOIDED."""
    await _ensure_location_id()
    booking_id = f"e2e-{uuid.uuid4().hex[:8]}"
    ik = f"ev:{TEST_CHARGER_ID}:{booking_id}"

    card_id, customer_id, card_meta = await square.create_card(
        source_id="cnon:card-nonce-ok",
        booking_id=booking_id,
        given_name="E2E",
        family_name="Void",
    )
    payment = await square.create_payment_authorization(
        card_id=card_id,
        customer_id=customer_id,
        booking_id=booking_id,
        amount_cents=300,
    )
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              booking_id,
        "session_id":              str(uuid.uuid4()),
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 300,
        "square_environment":      "sandbox",
        "square_payment_id":       payment["id"],
        **card_meta,
    })

    await _mqtt_publish(FINALIZE_TOPIC, {"booking_id": booking_id, "final_amount_cents": 0})

    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        row = await db.get_session(ik)
        if row and row["state"] in ("VOIDED", "FAILED"):
            break
        await asyncio.sleep(0.25)

    row = await db.get_session(ik)
    assert row["state"] == "VOIDED"
    assert row["captured_amount_cents"] == 0


# ---------------------------------------------------------------------------
# Reload resilience
# ---------------------------------------------------------------------------

async def test_e2e_start_returns_session_page_if_already_authorized(
    live_client: AsyncClient,
) -> None:
    """Reload after authorization renders the confirmation page, not the card form."""
    await _ensure_location_id()
    booking_id = f"e2e-{uuid.uuid4().hex[:8]}"
    ik = f"ev:{TEST_CHARGER_ID}:{booking_id}"
    session_id = str(uuid.uuid4())

    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              booking_id,
        "session_id":              session_id,
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 100,
        "square_environment":      "sandbox",
        "square_payment_id":       "pay_already_done",
        "square_card_id":          "card_already_done",
    })

    async def push_booking():
        await asyncio.sleep(0.1)
        await _mqtt_publish(
            BOOKING_RESPONSE_TOPIC,
            {"booking_id": booking_id, "initial_authorization_amount": 1.00},
        )

    asyncio.create_task(push_booking())
    resp = await live_client.get("/start")
    assert resp.status_code == 200
    assert "pay_already_done" in resp.text
    assert "card-container" not in resp.text
