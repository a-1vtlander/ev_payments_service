"""
Unit tests for HTTP endpoints.

Both MQTT and Square API are fully mocked:
  - MQTT:   MagicMock paho client (via patched_state / unit_client fixtures)
  - Square: unittest.mock.patch on square module functions

Because /start and /submit_payment BOTH drain a queue then await on it,
we cannot pre-populate the queue before the request (the drain would eat the
message).  Instead we use ``asyncio.create_task(push_after(...))`` – the task
runs while the endpoint is awaiting ``q.get()``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

import db
import state
from tests.conftest import (
    AUTHORIZE_RESPONSE_TOPIC,
    BOOKING_RESPONSE_TOPIC,
    TEST_BOOKING_ID,
    TEST_CHARGER_ID,
    TEST_HOME_ID,
    TEST_SESSION_ID,
    make_authorize_response,
    make_booking_response,
    push_after,
)

# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

async def test_health_returns_200(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/health")
    assert resp.status_code == 200
    assert resp.text == "ok"


# ---------------------------------------------------------------------------
# /  (index)
# ---------------------------------------------------------------------------

async def test_index_returns_html(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# /start – MQTT not connected
# ---------------------------------------------------------------------------

async def test_start_returns_503_when_mqtt_disconnected(
    unit_client_no_mqtt: AsyncClient,
) -> None:
    resp = await unit_client_no_mqtt.get("/start")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /start – Square not configured
# ---------------------------------------------------------------------------

async def test_start_returns_503_when_no_square_token(
    unit_client: AsyncClient,
) -> None:
    original = state._square_config.copy()
    state._square_config = {**original, "access_token": ""}
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    try:
        resp = await unit_client.get("/start")
        assert resp.status_code == 503
    finally:
        state._square_config = original


async def test_start_returns_503_when_no_location_id(
    unit_client: AsyncClient,
) -> None:
    original = state._square_config.copy()
    state._square_config = {**original, "location_id": ""}
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    try:
        resp = await unit_client.get("/start")
        assert resp.status_code == 503
    finally:
        state._square_config = original


# ---------------------------------------------------------------------------
# /start – MQTT timeout
# ---------------------------------------------------------------------------

async def test_start_returns_504_on_mqtt_timeout(
    unit_client: AsyncClient,
) -> None:
    original_timeout = state.RESPONSE_TIMEOUT
    state.RESPONSE_TIMEOUT = 0.05  # tiny timeout; queue stays empty
    try:
        resp = await unit_client.get("/start")
        assert resp.status_code == 504
    finally:
        state.RESPONSE_TIMEOUT = original_timeout


# ---------------------------------------------------------------------------
# /start – happy path: renders card form
# ---------------------------------------------------------------------------

async def test_start_returns_card_form(unit_client: AsyncClient, mock_mqtt: MagicMock) -> None:
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    resp = await unit_client.get("/start")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Card form must reference the Square SDK
    assert "square" in resp.text.lower()


async def test_start_publishes_to_correct_topic(
    unit_client: AsyncClient, mock_mqtt: MagicMock
) -> None:
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    await unit_client.get("/start")
    mock_mqtt.publish.assert_called_once()
    topic = mock_mqtt.publish.call_args.args[0]
    expected = f"ev/charger/{TEST_HOME_ID}/{TEST_CHARGER_ID}/booking/request_session"
    assert topic == expected


async def test_start_stores_pending_session(unit_client: AsyncClient) -> None:
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    await unit_client.get("/start")
    assert len(state._pending_sessions) == 1
    entry = next(iter(state._pending_sessions.values()))
    assert entry["booking_id"] == TEST_BOOKING_ID
    assert entry["amount_cents"] == 100   # $1.00 → 100 cents


async def test_start_converts_dollars_to_cents(unit_client: AsyncClient) -> None:
    asyncio.create_task(
        push_after(
            state._topic_queues[BOOKING_RESPONSE_TOPIC],
            make_booking_response(amount_dollars=2.50),
        )
    )
    await unit_client.get("/start")
    entry = next(iter(state._pending_sessions.values()))
    assert entry["amount_cents"] == 250


async def test_start_writes_ready_to_pay_to_db(unit_client: AsyncClient) -> None:
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    await unit_client.get("/start")
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    row = await db.get_session(ik)
    assert row is not None
    assert row["state"] == "READY_TO_PAY"


# ---------------------------------------------------------------------------
# /start – already authorized: shows session page, not card form
# ---------------------------------------------------------------------------

async def test_start_renders_session_page_if_already_authorized(
    unit_client: AsyncClient, tmp_db: str
) -> None:
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              TEST_SESSION_ID,
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 100,
        "square_environment":      "sandbox",
        "square_payment_id":       "pay_existing",
        "square_card_id":          "card_existing",
    })
    asyncio.create_task(
        push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], make_booking_response())
    )
    resp = await unit_client.get("/start")
    assert resp.status_code == 200
    # Should render the "EV Charger Enabled" page: has payment ID but no card form
    assert "pay_existing" in resp.text
    assert "card-container" not in resp.text   # card form SDK element absent


# ---------------------------------------------------------------------------
# /submit_payment – validation / session not found
# ---------------------------------------------------------------------------

async def test_submit_payment_unknown_uid_returns_400(unit_client: AsyncClient) -> None:
    resp = await unit_client.post("/submit_payment", data={
        "source_id":   "nonce-xyz",
        "uid":         "00000000-nonexistent-uid",
        "given_name":  "Jane",
        "family_name": "Smith",
    })
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# /submit_payment – idempotency (already AUTHORIZED in DB)
# ---------------------------------------------------------------------------

async def test_submit_payment_idempotent_return(unit_client: AsyncClient, tmp_db: str) -> None:
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    uid = str(uuid.uuid4())
    # Pre-authorize in DB
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              uid,
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 100,
        "square_environment":      "sandbox",
        "square_payment_id":       "pay_idem_111",
        "square_card_id":          "card_idem_111",
    })
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    resp = await unit_client.post("/submit_payment", data={
        "source_id":   "nonce-xyz",
        "uid":         uid,
        "given_name":  "Jane",
        "family_name": "Smith",
    })
    body = resp.json()
    assert body["status"]     == "success"
    assert body["payment_id"] == "pay_idem_111"


# ---------------------------------------------------------------------------
# /submit_payment – Square card error
# ---------------------------------------------------------------------------

async def test_submit_payment_square_card_error_returns_card_error_status(
    unit_client: AsyncClient,
) -> None:
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    with patch("square.create_card", new=AsyncMock(side_effect=RuntimeError("card declined"))):
        resp = await unit_client.post("/submit_payment", data={
            "source_id":   "nonce-bad",
            "uid":         uid,
            "given_name":  "Jane",
            "family_name": "Smith",
        })

    body = resp.json()
    assert body["status"] == "card_error"


# ---------------------------------------------------------------------------
# /submit_payment – MQTT not connected after Square success
# ---------------------------------------------------------------------------

async def test_submit_payment_returns_503_if_mqtt_disconnects_after_square(
    unit_client: AsyncClient,
) -> None:
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    card_meta = {
        "square_customer_id": "cust_1", "square_card_id": "card_1",
        "card_brand": "VISA", "card_last4": "4242",
        "card_exp_month": 12, "card_exp_year": 2029,
    }
    payment = {"id": "pay_1", "status": "APPROVED"}

    with (
        patch("square.create_card", new=AsyncMock(return_value=("card_1", "cust_1", card_meta))),
        patch("square.create_payment_authorization", new=AsyncMock(return_value=payment)),
    ):
        state.mqtt_client.is_connected.return_value = False
        resp = await unit_client.post("/submit_payment", data={
            "source_id":   "nonce-ok",
            "uid":         uid,
            "given_name":  "Jane",
            "family_name": "Smith",
        })
        state.mqtt_client.is_connected.return_value = True

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /submit_payment – MQTT authorize timeout
# ---------------------------------------------------------------------------

async def test_submit_payment_returns_504_on_authorize_timeout(
    unit_client: AsyncClient,
) -> None:
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    card_meta = {
        "square_customer_id": "cust_2", "square_card_id": "card_2",
        "card_brand": "MASTERCARD", "card_last4": "1234",
        "card_exp_month": 6, "card_exp_year": 2030,
    }
    payment = {"id": "pay_timeout", "status": "APPROVED"}

    original_timeout = state.RESPONSE_TIMEOUT
    state.RESPONSE_TIMEOUT = 0.05

    try:
        with (
            patch("square.create_card", new=AsyncMock(return_value=("card_2", "cust_2", card_meta))),
            patch("square.create_payment_authorization", new=AsyncMock(return_value=payment)),
        ):
            resp = await unit_client.post("/submit_payment", data={
                "source_id":   "nonce-ok",
                "uid":         uid,
                "given_name":  "Jane",
                "family_name": "Smith",
            })
    finally:
        state.RESPONSE_TIMEOUT = original_timeout

    assert resp.status_code == 504


# ---------------------------------------------------------------------------
# /submit_payment – charger refuses
# ---------------------------------------------------------------------------

async def test_submit_payment_returns_502_when_charger_refuses(
    unit_client: AsyncClient,
) -> None:
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    card_meta = {
        "square_customer_id": "cust_3", "square_card_id": "card_3",
        "card_brand": "AMEX", "card_last4": "9999",
        "card_exp_month": 1, "card_exp_year": 2028,
    }
    payment = {"id": "pay_refused", "status": "APPROVED"}

    asyncio.create_task(
        push_after(state._topic_queues[AUTHORIZE_RESPONSE_TOPIC], make_authorize_response(success=False))
    )

    with (
        patch("square.create_card", new=AsyncMock(return_value=("card_3", "cust_3", card_meta))),
        patch("square.create_payment_authorization", new=AsyncMock(return_value=payment)),
    ):
        resp = await unit_client.post("/submit_payment", data={
            "source_id":   "nonce-ok",
            "uid":         uid,
            "given_name":  "Jane",
            "family_name": "Smith",
        })

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# /submit_payment – full happy path
# ---------------------------------------------------------------------------

async def test_submit_payment_success(unit_client: AsyncClient) -> None:
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    card_meta = {
        "square_customer_id": "cust_ok", "square_card_id": "card_ok",
        "card_brand": "VISA", "card_last4": "4242",
        "card_exp_month": 12, "card_exp_year": 2030,
    }
    payment = {"id": "pay_ok", "status": "APPROVED"}

    asyncio.create_task(
        push_after(state._topic_queues[AUTHORIZE_RESPONSE_TOPIC], make_authorize_response(success=True))
    )

    with (
        patch("square.create_card", new=AsyncMock(return_value=("card_ok", "cust_ok", card_meta))),
        patch("square.create_payment_authorization", new=AsyncMock(return_value=payment)),
    ):
        resp = await unit_client.post("/submit_payment", data={
            "source_id":   "nonce-ok",
            "uid":         uid,
            "given_name":  "Jane",
            "family_name": "Smith",
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"]     == "success"
    assert body["payment_id"] == "pay_ok"
    assert body["card_id"]    == "card_ok"
    assert body["booking_id"] == TEST_BOOKING_ID

    # DB must reflect AUTHORIZED
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    row = await db.get_session(ik)
    assert row["state"] == "AUTHORIZED"


async def test_submit_payment_publishes_authorize_to_correct_topic(
    unit_client: AsyncClient, mock_mqtt: MagicMock
) -> None:
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {"booking_id": TEST_BOOKING_ID, "amount_cents": 100}

    card_meta = {
        "square_customer_id": "cust_pub", "square_card_id": "card_pub",
        "card_brand": "VISA", "card_last4": "0001",
        "card_exp_month": 1, "card_exp_year": 2031,
    }
    payment = {"id": "pay_pub", "status": "APPROVED"}

    asyncio.create_task(
        push_after(state._topic_queues[AUTHORIZE_RESPONSE_TOPIC], make_authorize_response())
    )

    with (
        patch("square.create_card", new=AsyncMock(return_value=("card_pub", "cust_pub", card_meta))),
        patch("square.create_payment_authorization", new=AsyncMock(return_value=payment)),
    ):
        await unit_client.post("/submit_payment", data={
            "source_id":   "nonce-ok",
            "uid":         uid,
            "given_name":  "Jane",
            "family_name": "Smith",
        })

    topic = mock_mqtt.publish.call_args.args[0]
    assert topic == state._authorize_request_topic


# ---------------------------------------------------------------------------
# /session/{session_id} – HTML view
# ---------------------------------------------------------------------------

async def test_session_html_returns_200(unit_client: AsyncClient, tmp_db: str) -> None:
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              TEST_SESSION_ID,
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 100,
        "square_environment":      "sandbox",
        "square_payment_id":       "pay_html_test",
    })
    resp = await unit_client.get(f"/session/{TEST_SESSION_ID}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert TEST_BOOKING_ID in resp.text


async def test_session_html_returns_404_for_missing(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/session/00000000-dead-dead-dead-000000000000")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /session/{session_id}/json
# ---------------------------------------------------------------------------

async def test_session_json_returns_session_data(unit_client: AsyncClient, tmp_db: str) -> None:
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              TEST_SESSION_ID,
        "state":                   "AUTHORIZED",
        "authorized_amount_cents": 150,
        "square_environment":      "sandbox",
    })
    resp = await unit_client.get(f"/session/{TEST_SESSION_ID}/json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["booking_id"]            == TEST_BOOKING_ID
    assert body["state"]                 == "AUTHORIZED"
    assert body["authorized_amount_cents"] == 150


async def test_session_json_returns_404_for_missing(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/session/no-such-uid/json")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /db viewer
# ---------------------------------------------------------------------------

async def test_db_viewer_returns_html(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/db")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_db_viewer_lists_sessions(unit_client: AsyncClient, tmp_db: str) -> None:
    ik = f"ev:{TEST_CHARGER_ID}:{TEST_BOOKING_ID}"
    await db.upsert_session({
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              TEST_BOOKING_ID,
        "session_id":              TEST_SESSION_ID,
        "state":                   "READY_TO_PAY",
        "authorized_amount_cents": 100,
        "square_environment":      "sandbox",
    })
    resp = await unit_client.get("/db")
    assert TEST_BOOKING_ID in resp.text
    assert "READY_TO_PAY" in resp.text


# ---------------------------------------------------------------------------
# /debug
# ---------------------------------------------------------------------------

async def test_debug_returns_html(unit_client: AsyncClient) -> None:
    resp = await unit_client.get("/debug")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
