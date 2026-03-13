"""
tests/test_payment_post_process.py

Functional coverage for endpoints/payment_post_process.py.

This module is not mounted on main.app; a lightweight test app is constructed
here that includes only the router, bypassing the auth/access-key middleware.

Cases covered:
  - Missing uid query parameter → 400
  - Unknown / already-used uid → 400
  - MQTT not connected → 503
  - Session lock not initialised → 503
  - MQTT publish failure (rc != 0) → 503
  - Response timeout → 504
  - Non-JSON MQTT response → graceful (treated as failure)
  - Success response (success=true) → 200 charger-ready HTML
  - Failure response (success=false) → 200 failure HTML with error detail
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import state
from tests.conftest import (
    AUTHORIZE_RESPONSE_TOPIC,
    TEST_BOOKING_ID,
    TEST_CHARGER_ID,
    TEST_HOME_ID,
    push_after,
)


# ---------------------------------------------------------------------------
# Minimal test app that mounts only the payment_post_process router
# ---------------------------------------------------------------------------

def _make_test_app() -> FastAPI:
    from endpoints.payment_post_process import router
    app = FastAPI()
    app.include_router(router)
    return app


_TEST_APP = _make_test_app()


@pytest_asyncio.fixture
async def ppp_client(patched_state) -> AsyncClient:
    """AsyncClient wired to the payment_post_process-only test app."""
    async with AsyncClient(
        transport=ASGITransport(app=_TEST_APP),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_session(uid: str, booking_id: str = TEST_BOOKING_ID) -> None:
    state._pending_sessions[uid] = {
        "booking_id":    booking_id,
        "payment_token": "tok_test_123",
    }


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

async def test_missing_uid_returns_400(ppp_client: AsyncClient):
    resp = await ppp_client.get("/payment_post_process")
    assert resp.status_code == 400
    assert "Missing session token" in resp.text


async def test_empty_uid_returns_400(ppp_client: AsyncClient):
    resp = await ppp_client.get("/payment_post_process?uid=")
    assert resp.status_code == 400
    assert "Missing session token" in resp.text


async def test_unknown_uid_returns_400(ppp_client: AsyncClient):
    resp = await ppp_client.get("/payment_post_process?uid=not-a-real-uid")
    assert resp.status_code == 400
    assert "Session not found" in resp.text


async def test_uid_consumed_on_first_use(ppp_client: AsyncClient):
    """A valid uid is removed from pending_sessions on first use; a second
    request with the same uid must get a 400."""
    _add_session("uid-once")
    # Use a very short timeout so the test doesn't stall waiting for MQTT response
    original_timeout = state.RESPONSE_TIMEOUT
    state.RESPONSE_TIMEOUT = 0.05
    try:
        resp1 = await ppp_client.get("/payment_post_process?uid=uid-once")
    finally:
        state.RESPONSE_TIMEOUT = original_timeout
    # Any server-side response is fine — the key point is the session was consumed
    assert resp1.status_code in (200, 400, 503, 504)
    # Second request must now see "Session not found"
    resp2 = await ppp_client.get("/payment_post_process?uid=uid-once")
    assert resp2.status_code == 400


# ---------------------------------------------------------------------------
# Infrastructure guards
# ---------------------------------------------------------------------------

async def test_mqtt_not_connected_returns_503(ppp_client: AsyncClient):
    _add_session("uid-no-mqtt")
    state.mqtt_client.is_connected.return_value = False
    resp = await ppp_client.get("/payment_post_process?uid=uid-no-mqtt")
    assert resp.status_code == 503
    assert "MQTT" in resp.text or "unavailable" in resp.text.lower()


async def test_session_lock_none_returns_503(ppp_client: AsyncClient):
    _add_session("uid-no-lock")
    state._session_lock = None
    resp = await ppp_client.get("/payment_post_process?uid=uid-no-lock")
    assert resp.status_code == 503


async def test_mqtt_publish_failure_returns_503(ppp_client: AsyncClient):
    _add_session("uid-pub-fail")
    publish_result = MagicMock()
    publish_result.rc = mqtt.MQTT_ERR_NO_CONN  # non-zero → failure
    state.mqtt_client.publish.return_value = publish_result
    resp = await ppp_client.get("/payment_post_process?uid=uid-pub-fail")
    assert resp.status_code == 503
    assert "authorize" in resp.text.lower() or "failed" in resp.text.lower()


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

async def test_response_timeout_returns_504(ppp_client: AsyncClient):
    """When the charger never responds, a 504 is returned."""
    _add_session("uid-timeout")
    original_timeout = state.RESPONSE_TIMEOUT
    state.RESPONSE_TIMEOUT = 0.05   # 50 ms – expires immediately in tests
    try:
        resp = await ppp_client.get("/payment_post_process?uid=uid-timeout")
    finally:
        state.RESPONSE_TIMEOUT = original_timeout
    assert resp.status_code == 504
    assert "Charger did not respond" in resp.text


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

async def test_success_response_returns_charger_ready_html(ppp_client: AsyncClient):
    """When the broker replies {success: true}, the response is 200 with 'Charger Enabled'."""
    uid = "uid-success"
    _add_session(uid, booking_id="bk-success")

    async def _inject():
        await push_after(
            state._topic_queues[AUTHORIZE_RESPONSE_TOPIC],
            json.dumps({"success": True}),
        )

    asyncio.create_task(_inject())
    resp = await ppp_client.get(f"/payment_post_process?uid={uid}&order_id=ord-001")
    assert resp.status_code == 200
    assert "EV Charger Enabled" in resp.text
    assert "bk-success" in resp.text
    assert "ord-001" in resp.text


async def test_success_response_escapes_html_in_booking_id(ppp_client: AsyncClient):
    """Booking ID is HTML-escaped in the success page (XSS guard)."""
    uid = "uid-xss"
    _add_session(uid, booking_id="<script>alert(1)</script>")

    async def _inject():
        await push_after(
            state._topic_queues[AUTHORIZE_RESPONSE_TOPIC],
            json.dumps({"success": True}),
        )

    asyncio.create_task(_inject())
    resp = await ppp_client.get(f"/payment_post_process?uid={uid}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

async def test_failure_response_returns_html_with_error(ppp_client: AsyncClient):
    """{success: false} from broker → 502 page describes the failure."""
    uid = "uid-fail-resp"
    _add_session(uid)

    async def _inject():
        await push_after(
            state._topic_queues[AUTHORIZE_RESPONSE_TOPIC],
            json.dumps({"success": False, "error": "charger busy"}),
        )

    asyncio.create_task(_inject())
    resp = await ppp_client.get(f"/payment_post_process?uid={uid}")
    assert resp.status_code == 502
    assert "charger busy" in resp.text or "failed" in resp.text.lower()


async def test_non_json_response_handled_gracefully(ppp_client: AsyncClient):
    """If the broker sends non-JSON, the endpoint returns a failure page, not a 500."""
    uid = "uid-bad-json"
    _add_session(uid)

    async def _inject():
        await push_after(
            state._topic_queues[AUTHORIZE_RESPONSE_TOPIC],
            "THIS IS NOT JSON",
        )

    asyncio.create_task(_inject())
    resp = await ppp_client.get(f"/payment_post_process?uid={uid}")
    # Non-JSON treated as a failed authorization → 502, not 500
    assert resp.status_code == 502
    assert "THIS IS NOT JSON" in resp.text
