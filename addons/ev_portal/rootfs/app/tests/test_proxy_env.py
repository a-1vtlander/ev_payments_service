"""
test_proxy_env.py – Tests that verify correct behaviour under a
Cloudflare-style reverse-proxy environment (plain-HTTP origin, HTTPS at edge).

Covers
------
1. URL integrity   – submitUrl and session_url are never absolute (no scheme).
2. Root redirect   – GET / target is always relative.
3. Access-control  – direct IP, Cloudflare-proxied IP, noop mode, bad CIDR.
4. Full workflow   – GET /start → POST /submit_payment → GET /session all work
                     correctly when requests carry realistic Cloudflare headers.

Why this file exists
--------------------
The core bug class it guards against: server-side code that builds absolute
URLs using ``request.base_url`` (which is ``http://`` on a plain-HTTP origin).
Browsers loaded over HTTPS block such URLs as mixed content, silently breaking
the payment flow with a "network error" in the JS console.

All tests use mocked MQTT and Square (no network calls), so they run in any
environment.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import access
import state
from tests.conftest import (
    AUTHORIZE_RESPONSE_TOPIC,
    BOOKING_RESPONSE_TOPIC,
    TEST_BOOKING_ID,
    TEST_CHARGER_ID,
    TEST_HOME_ID,
    make_authorize_response,
    make_booking_response,
    push_after,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A realistic Cloudflare edge IP (from the published Cloudflare IPv4 list).
_CF_EDGE_IP = "104.16.1.1"

# A typical end-user IP that would appear in CF-Connecting-IP.
_USER_IP = "203.0.113.42"  # TEST-NET-3 per RFC 5737 – safe for tests

# Headers Cloudflare adds on every request.
_CF_HEADERS = {
    "host":              "baselander-ev.extravio.co",
    "x-forwarded-proto": "https",
    "cf-connecting-ip":  _USER_IP,
    "cf-ray":            "abc123def456-LHR",
}


class _FakeClientApp:
    """
    Thin ASGI shim that overrides the ``client`` field in the ASGI scope so
    tests can control the TCP remote address (needed for IP-allow-list checks).
    """

    def __init__(self, app: Any, client_ip: str, client_port: int = 12345) -> None:
        self.app        = app
        self.client_ip  = client_ip
        self.client_port = client_port

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            scope = {**scope, "client": (self.client_ip, self.client_port)}
        await self.app(scope, receive, send)


@pytest_asyncio.fixture(autouse=True)
async def _reset_access_cache():
    """Reset the allow-nets cache before and after every test in this module."""
    access._allow_nets_cache = None
    yield
    access._allow_nets_cache = None


def _cf_client(app: Any, client_ip: str = _CF_EDGE_IP) -> AsyncClient:
    """Return an AsyncClient whose requests appear to arrive from *client_ip*."""
    wrapped = _FakeClientApp(app, client_ip)
    return AsyncClient(
        transport=ASGITransport(app=wrapped),
        base_url="http://test",
        headers=_CF_HEADERS,
    )


# ---------------------------------------------------------------------------
# 1. URL integrity: no scheme-prefixed URLs in rendered responses
# ---------------------------------------------------------------------------

_SUBMIT_URL_RE = re.compile(r'submitUrl\s*:\s*"([^"]+)"')
_SESSION_URL_RE = re.compile(r'"session_url"\s*:\s*"([^"]+)"')


async def test_start_page_submit_url_is_relative(
    unit_client: AsyncClient,
) -> None:
    """
    The rendered start page must embed a relative submitUrl.

    Regression guard: if server-side code ever rebuilds an absolute URL from
    request.base_url the result would be ``http://…`` which browsers block as
    mixed content on HTTPS pages.
    """
    booking_resp = make_booking_response()

    async def _inject() -> None:
        await push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], booking_resp)

    asyncio.create_task(_inject())
    resp = await unit_client.get("/start")
    assert resp.status_code == 200

    match = _SUBMIT_URL_RE.search(resp.text)
    assert match, "submitUrl not found in start page output"
    submit_url = match.group(1)
    assert submit_url.startswith("/"), (
        f"submitUrl must be root-relative, got: {submit_url!r}"
    )
    assert "://" not in submit_url, (
        f"submitUrl must not contain a scheme, got: {submit_url!r}"
    )


async def test_start_page_submit_url_correct_path(
    unit_client: AsyncClient,
) -> None:
    """submitUrl must point at /submit_payment."""
    booking_resp = make_booking_response()

    async def _inject() -> None:
        await push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], booking_resp)

    asyncio.create_task(_inject())
    resp = await unit_client.get("/start")
    assert resp.status_code == 200

    match = _SUBMIT_URL_RE.search(resp.text)
    assert match
    assert match.group(1) == "/submit_payment"


async def test_submit_payment_session_url_is_relative(
    unit_client: AsyncClient,
) -> None:
    """
    The JSON from POST /submit_payment must return a relative session_url.
    browser code does ``window.location.href = result.session_url``; if this
    were absolute-http the page would downgrade from HTTPS.
    """
    uid = str(uuid.uuid4())
    state._pending_sessions[uid] = {
        "booking_id":   TEST_BOOKING_ID,
        "amount_cents": 100,
    }

    card_meta = {
        "square_customer_id": "cust_x",
        "square_card_id":     "card_x",
        "card_brand":         "VISA",
        "card_last4":         "1111",
        "card_exp_month":     12,
        "card_exp_year":      2030,
    }
    payment = {"id": "pay_test", "status": "APPROVED"}

    async def _push_auth() -> None:
        await push_after(
            state._topic_queues[AUTHORIZE_RESPONSE_TOPIC],
            make_authorize_response(success=True),
        )

    asyncio.create_task(_push_auth())

    with (
        patch("square.create_card", new=AsyncMock(return_value=("card_x", "cust_x", card_meta))),
        patch("square.create_payment_authorization", new=AsyncMock(return_value=payment)),
    ):
        resp = await unit_client.post("/submit_payment", data={
            "source_id":   "cnon:card-nonce-ok",
            "uid":         uid,
            "given_name":  "Test",
            "family_name": "User",
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"

    session_url = body["session_url"]
    assert session_url.startswith("/"), (
        f"session_url must be root-relative, got: {session_url!r}"
    )
    assert "://" not in session_url, (
        f"session_url must not contain a scheme, got: {session_url!r}"
    )


# ---------------------------------------------------------------------------
# 2. Root redirect is relative
# ---------------------------------------------------------------------------

async def test_root_redirect_target_is_relative(
    unit_client: AsyncClient,
) -> None:
    """GET / must redirect to /start – the redirect target must be relative."""
    resp = await unit_client.get("/", follow_redirects=False)
    # May be a 200 with meta-refresh or a 3xx; either way the target path is /start.
    body = resp.text
    # Check the meta-refresh URL and the JS replace target.
    for pattern in [r'url=([^"\'>\s]+)', r'replace\("([^"]+)"\)']:
        for m in re.finditer(pattern, body):
            url = m.group(1)
            assert "://" not in url, (
                f"Root redirect target must not contain a scheme, got: {url!r}"
            )


# ---------------------------------------------------------------------------
# 3. Start page loads correctly when Cloudflare headers are present
# ---------------------------------------------------------------------------

async def test_start_page_with_cloudflare_headers(
    patched_state: None,
) -> None:
    """
    Simulates a browser request that arrived via Cloudflare:
    - TCP remote IP is a Cloudflare edge address
    - CF-Connecting-IP carries the real user IP
    - The access control middleware is a no-op (filter_access_to not set)

    The start page should load normally and submitUrl must be relative.
    """
    import main as m

    booking_resp = make_booking_response()

    async def _inject() -> None:
        await push_after(state._topic_queues[BOOKING_RESPONSE_TOPIC], booking_resp)

    asyncio.create_task(_inject())

    async with _cf_client(m.app, client_ip=_CF_EDGE_IP) as c:
        resp = await c.get("/start")

    assert resp.status_code == 200

    match = _SUBMIT_URL_RE.search(resp.text)
    assert match, "submitUrl not found in page"
    assert "://" not in match.group(1), (
        f"submitUrl contains a scheme under CF headers: {match.group(1)!r}"
    )


# ---------------------------------------------------------------------------
# 4. Access-control middleware
# ---------------------------------------------------------------------------

async def test_access_control_noop_when_unconfigured(
    unit_client: AsyncClient,
) -> None:
    """With no filter_access_to configured, all IPs can reach the health endpoint."""
    state._access_config["allow_cidrs"] = []
    access._allow_nets_cache = None

    resp = await unit_client.get("/health")
    assert resp.status_code == 200


async def test_access_control_blocks_unlisted_direct_ip(
    patched_state: None,
) -> None:
    """A direct connection from an IP outside filter_access_to gets 403."""
    import main as m

    state._access_config["allow_cidrs"] = ["10.0.0.0/8"]
    access._allow_nets_cache = None

    # Client IP is 203.0.113.42 – NOT in 10.0.0.0/8
    async with _cf_client(m.app, client_ip=_USER_IP) as c:
        # No CF headers – simulate a direct (non-Cloudflare) connection by
        # sending the user IP as the raw TCP address without CF headers.
        resp = await c.get("/health", headers={
            "host": "example.com",
            # Deliberately no cf-connecting-ip – direct connection
        })

    # 173.245.48.1 is only in the CF range when we use _CF_EDGE_IP; here the
    # client_ip IS the user IP directly → not in CF nets → effective_ip = _USER_IP
    # _USER_IP is not in 10.0.0.0/8 → should be denied.
    assert resp.status_code == 403
    assert "restricted" in resp.text.lower()


async def test_access_control_allows_listed_direct_ip(
    patched_state: None,
) -> None:
    """A direct connection from an IP inside filter_access_to passes through."""
    import main as m

    state._access_config["allow_cidrs"] = ["203.0.113.0/24"]
    access._allow_nets_cache = None

    # _USER_IP = 203.0.113.42 – IS in 203.0.113.0/24
    async with _cf_client(m.app, client_ip=_USER_IP) as c:
        resp = await c.get("/health", headers={"host": "example.com"})

    assert resp.status_code == 200


async def test_access_control_cloudflare_ip_reads_cf_header(
    patched_state: None,
) -> None:
    """
    When TCP remote IP is a Cloudflare edge address, the effective IP is taken
    from CF-Connecting-IP.  If that header IP is in the allow list → 200.
    """
    import main as m

    # Allow the real user IP (carried by CF-Connecting-IP = _USER_IP).
    state._access_config["allow_cidrs"] = ["203.0.113.0/24"]
    access._allow_nets_cache = None

    # TCP remote = CF edge IP; real user IP in header.
    async with _cf_client(m.app, client_ip=_CF_EDGE_IP) as c:
        resp = await c.get("/health")  # _CF_HEADERS already include cf-connecting-ip

    assert resp.status_code == 200


async def test_access_control_cloudflare_ip_blocks_via_cf_header(
    patched_state: None,
) -> None:
    """
    When TCP remote is a CF IP but CF-Connecting-IP is not in the allow list,
    the request is denied.
    """
    import main as m

    # Allow only a private range – _USER_IP (203.0.113.x) won't match.
    state._access_config["allow_cidrs"] = ["192.168.0.0/16"]
    access._allow_nets_cache = None

    async with _cf_client(m.app, client_ip=_CF_EDGE_IP) as c:
        resp = await c.get("/health")

    assert resp.status_code == 403


async def test_access_control_cloudflare_ip_missing_header_denied(
    patched_state: None,
) -> None:
    """
    TCP remote is a Cloudflare IP but no CF-Connecting-IP header is present.
    The middleware must deny the request (can't determine real client IP).
    """
    import main as m

    state._access_config["allow_cidrs"] = ["0.0.0.0/0"]  # allow everything
    access._allow_nets_cache = None

    # Use a plain client (no CF headers) so cf-connecting-ip is absent,
    # but with a CF edge IP as the TCP remote address.
    wrapped = _FakeClientApp(m.app, client_ip=_CF_EDGE_IP)
    async with AsyncClient(
        transport=ASGITransport(app=wrapped),
        base_url="http://test",
        # No CF headers — simulates a misconfigured / stripped-header request
    ) as c:
        resp = await c.get("/health")

    assert resp.status_code == 403


async def test_access_control_invalid_cidr_skipped(
    patched_state: None,
) -> None:
    """
    A malformed CIDR in filter_access_to is skipped (logged as ERROR) and the
    remaining valid CIDRs still apply.
    """
    import main as m

    state._access_config["allow_cidrs"] = ["NOT_A_CIDR", "203.0.113.0/24"]
    access._allow_nets_cache = None

    # Should not raise; _USER_IP is in the valid CIDR and request must pass.
    async with _cf_client(m.app, client_ip=_USER_IP) as c:
        resp = await c.get("/health", headers={"host": "example.com"})

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Full payment workflow under Cloudflare headers (no network calls)
# ---------------------------------------------------------------------------

async def test_full_payment_workflow_cloudflare_headers(
    patched_state: None,
) -> None:
    """
    Simulate the complete happy-path workflow as a browser behind Cloudflare:

      GET /start   (CF headers)  → 200, card form with relative submitUrl
      POST /submit_payment  (CF headers, mocked Square + MQTT)  → success JSON
      GET /session/{uid}    (CF headers)  → 200, session confirmation page

    This test ensures that no part of the flow constructs or embeds absolute
    http:// URLs that would be blocked as mixed content in the browser.
    """
    import main as m

    booking_id  = f"wf-{uuid.uuid4().hex[:8]}"
    uid         = str(uuid.uuid4())
    card_meta   = {
        "square_customer_id": "cust_wf",
        "square_card_id":     "card_wf",
        "card_brand":         "VISA",
        "card_last4":         "4242",
        "card_exp_month":     1,
        "card_exp_year":      2030,
    }
    payment = {"id": "pay_wf01", "status": "APPROVED"}

    # ── Step 1: GET /start ────────────────────────────────────────────────
    async def _inject_booking() -> None:
        await push_after(
            state._topic_queues[BOOKING_RESPONSE_TOPIC],
            make_booking_response(booking_id=booking_id, amount_dollars=1.00),
        )

    asyncio.create_task(_inject_booking())

    async with _cf_client(m.app) as c:
        start_resp = await c.get("/start")
        assert start_resp.status_code == 200, (
            f"GET /start failed: {start_resp.status_code} {start_resp.text[:200]}"
        )

        # submitUrl must be relative
        match = _SUBMIT_URL_RE.search(start_resp.text)
        assert match, "submitUrl missing from start page"
        submit_url = match.group(1)
        assert submit_url == "/submit_payment", (
            f"Expected submitUrl='/submit_payment', got {submit_url!r}"
        )

        # Recover the uid that was placed in pending_sessions by /start
        live_uid = next(
            (k for k, v in state._pending_sessions.items()
             if v["booking_id"] == booking_id),
            None,
        )
        assert live_uid is not None, "Session uid not created by /start"

        # ── Step 2: POST /submit_payment ──────────────────────────────────
        async def _inject_auth() -> None:
            await push_after(
                state._topic_queues[AUTHORIZE_RESPONSE_TOPIC],
                make_authorize_response(success=True),
            )

        asyncio.create_task(_inject_auth())

        with (
            patch("square.create_card",
                  new=AsyncMock(return_value=("card_wf", "cust_wf", card_meta))),
            patch("square.create_payment_authorization",
                  new=AsyncMock(return_value=payment)),
        ):
            pay_resp = await c.post("/submit_payment", data={
                "source_id":   "cnon:card-nonce-ok",
                "uid":         live_uid,
                "given_name":  "CF",
                "family_name": "Tester",
            })

        assert pay_resp.status_code == 200, (
            f"POST /submit_payment failed: {pay_resp.status_code} {pay_resp.text}"
        )
        body = pay_resp.json()
        assert body["status"] == "success", f"Unexpected status: {body}"

        # session_url must be relative
        session_url = body.get("session_url", "")
        assert session_url.startswith("/session/"), (
            f"session_url must be /session/<uid>, got: {session_url!r}"
        )
        assert "://" not in session_url, (
            f"session_url must not contain a scheme, got: {session_url!r}"
        )

        # ── Step 3: GET /session/{uid} ────────────────────────────────────
        session_resp = await c.get(session_url)
        assert session_resp.status_code == 200, (
            f"GET {session_url} failed: {session_resp.status_code}"
        )
        # Session page must not contain any absolute http:// references
        # to same-origin resources (scripts, css, form actions).
        same_origin_http = re.findall(
            r'(?:href|src|action)\s*=\s*["\']http://[^"\']+["\']',
            session_resp.text,
        )
        assert not same_origin_http, (
            f"Session page contains absolute http:// URLs: {same_origin_http}"
        )
