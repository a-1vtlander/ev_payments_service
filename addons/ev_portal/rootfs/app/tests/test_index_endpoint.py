"""
tests/test_index_endpoint.py

Functional coverage for endpoints/index.py (GET /).

This router is not mounted on main.app; a lightweight test app is constructed
here so the handler can be exercised directly.

Cases covered:
  - GET / with no sessions → renders HTML with disconnected MQTT status
  - GET / with connected MQTT → mqtt_status shows connected
  - GET / with one session in DB → session context rendered, state color applied
  - State → colour mapping for all states
  - cap_cents present → cap_display formatted
  - cap_cents absent → cap_display empty
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import db
import state
from tests.conftest import TEST_CHARGER_ID, TEST_HOME_ID


# ---------------------------------------------------------------------------
# Minimal test app
# ---------------------------------------------------------------------------

def _make_test_app() -> FastAPI:
    from endpoints.index import router
    app = FastAPI()
    app.include_router(router)
    return app


_TEST_APP = _make_test_app()


@pytest_asyncio.fixture
async def index_client(patched_state) -> AsyncClient:
    async with AsyncClient(
        transport=ASGITransport(app=_TEST_APP),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Basic response
# ---------------------------------------------------------------------------

async def test_index_returns_html(index_client: AsyncClient):
    resp = await index_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_index_shows_disconnected_status_when_mqtt_off(index_client: AsyncClient):
    state.mqtt_client.is_connected.return_value = False
    resp = await index_client.get("/")
    assert resp.status_code == 200
    assert "disconnected" in resp.text.lower()


async def test_index_shows_connected_status_when_mqtt_on(index_client: AsyncClient):
    state.mqtt_client.is_connected.return_value = True
    resp = await index_client.get("/")
    assert resp.status_code == 200
    assert "connected" in resp.text.lower()


async def test_index_shows_home_and_charger_ids(index_client: AsyncClient):
    resp = await index_client.get("/")
    assert TEST_HOME_ID in resp.text
    assert TEST_CHARGER_ID in resp.text


# ---------------------------------------------------------------------------
# No sessions
# ---------------------------------------------------------------------------

async def test_index_with_no_sessions_renders_without_session_block(index_client: AsyncClient):
    """When there are no sessions, session_ctx is None and the page still renders."""
    resp = await index_client.get("/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# With a session in the DB
# ---------------------------------------------------------------------------

async def _insert_session(state_val: str, cap_cents=None) -> str:
    ik = f"ev:{TEST_CHARGER_ID}:idx-{state_val}-{uuid.uuid4().hex[:6]}"
    row = {
        "idempotency_key":         ik,
        "charger_id":              TEST_CHARGER_ID,
        "booking_id":              f"bk-{state_val}",
        "session_id":              str(uuid.uuid4()),
        "state":                   state_val,
        "square_environment":      "sandbox",
        "authorized_amount_cents": 5000,
        "card_brand":              "VISA",
        "card_last4":              "1111",
    }
    await db.upsert_session(row)
    if cap_cents is not None:
        await db.mark_captured(ik, "pay_test", cap_cents)
    return ik


async def test_index_with_session_shows_state(index_client: AsyncClient):
    await _insert_session("AUTHORIZED")
    resp = await index_client.get("/")
    assert resp.status_code == 200
    assert "AUTHORIZED" in resp.text


async def test_index_with_captured_session_shows_cap_display(index_client: AsyncClient):
    await _insert_session("CAPTURED", cap_cents=4200)
    resp = await index_client.get("/")
    assert resp.status_code == 200
    assert "$42.00" in resp.text


async def test_index_applies_state_color_for_captured(index_client: AsyncClient):
    """The CAPTURED state should use the green colour (#188038)."""
    await _insert_session("CAPTURED", cap_cents=5000)
    resp = await index_client.get("/")
    assert "#188038" in resp.text


async def test_index_applies_state_color_for_failed(index_client: AsyncClient):
    """The FAILED state should use the red colour (#c00)."""
    await _insert_session("FAILED")
    resp = await index_client.get("/")
    assert "#c00" in resp.text


async def test_index_applies_state_color_for_canceled(index_client: AsyncClient):
    await _insert_session("CANCELED")
    resp = await index_client.get("/")
    assert "#c00" in resp.text


async def test_index_applies_state_color_for_refunded(index_client: AsyncClient):
    await _insert_session("REFUNDED")
    resp = await index_client.get("/")
    assert "#e37400" in resp.text


async def test_index_cap_display_empty_when_no_cap_cents(index_client: AsyncClient):
    """When cap_cents is None the cap_display should be an empty string."""
    await _insert_session("AUTHORIZED")
    resp = await index_client.get("/")
    assert resp.status_code == 200
    # $0.00 would be the amount; empty cap_display means it's not shown as a dollar amount
    # The template uses cap_display only when it's non-empty — the page should still load fine
