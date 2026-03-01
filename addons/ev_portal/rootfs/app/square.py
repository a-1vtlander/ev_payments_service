"""
Square API helpers.

Covers:
  - Web Payments SDK JS URL (injected into the card form page)
  - POST /v2/cards          – tokenise and store a card on file
  - POST /v2/payments       – create a pre-authorisation hold (autocomplete=false)
  - POST /v2/payments/{id}/complete – capture a pre-auth at the final amount
"""

from __future__ import annotations

import httpx
import json
import logging
import time
import uuid
from typing import Optional

import state
import telemetry

log = logging.getLogger(__name__)


def _square_errors(resp_text: str) -> tuple[str | None, str | None]:
    """Extract (error_code, error_detail) from a Square error response body."""
    try:
        first = json.loads(resp_text).get("errors", [{}])[0]
        return first.get("code"), first.get("detail")
    except Exception:
        return None, None

SQUARE_API_VERSION  = "2026-01-22"
SQUARE_SANDBOX_BASE = "https://connect.squareupsandbox.com"
SQUARE_PROD_BASE    = "https://connect.squareup.com"

SQUARE_SANDBOX_JS = "https://sandbox.web.squarecdn.com/v1/square.js"
SQUARE_PROD_JS    = "https://web.squarecdn.com/v1/square.js"


def sdk_js_url() -> str:
    return SQUARE_SANDBOX_JS if state._square_config["sandbox"] else SQUARE_PROD_JS


def _base_url() -> str:
    return SQUARE_SANDBOX_BASE if state._square_config["sandbox"] else SQUARE_PROD_BASE


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {state._square_config['access_token']}",
        "Content-Type":  "application/json",
        "Square-Version": SQUARE_API_VERSION,
    }


async def fetch_first_location_id() -> str:
    """Fetch the first ACTIVE location from the Square account."""
    url = f"{_base_url()}/v2/locations"
    log.info("GET %s", url)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_headers())
        log.info("GET %s → HTTP %s  body=%s", url, resp.status_code, resp.text)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.error("fetch_first_location_id: HTTP %s from Square: %s", exc.response.status_code, exc.response.text)
        raise
    except Exception:
        log.exception("fetch_first_location_id: unexpected error calling Square")
        raise
    locations = resp.json().get("locations", [])
    active = [loc for loc in locations if loc.get("status") == "ACTIVE"]
    if not active:
        log.error("fetch_first_location_id: no ACTIVE locations in response: %s", resp.text)
        raise RuntimeError("No active Square locations found")
    log.info("fetch_first_location_id: using location_id=%s", active[0]["id"])
    return active[0]["id"]


async def create_customer(booking_id: str, given_name: str, family_name: str) -> str:
    """
    Create a Square customer tied to this booking.

    ``customer_id`` is required by POST /v2/cards.
    Uses ``booking_id`` as the idempotency key so retries for the same booking
    never create duplicate customers.
    Returns the ``customer.id``.
    """
    url = f"{_base_url()}/v2/customers"
    body = {
        "idempotency_key": booking_id,   # stable per booking – safe to retry
        "given_name":      given_name,
        "family_name":     family_name,
        "reference_id":    booking_id[:40],
        "note":            f"EV charger session booking {booking_id}",
    }
    log.info(
        "POST %s\nRequest body:\n%s",
        url, json.dumps(body, indent=2),
    )

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)

    log.info(
        "POST %s → HTTP %s\nResponse body:\n%s",
        url, resp.status_code, resp.text,
    )
    err_code, err_detail = _square_errors(resp.text) if not resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CREATE_CUSTOMER",
        booking_id=booking_id,
        http_status=resp.status_code,
        success=resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        request_json=json.dumps({k: v for k, v in body.items() if k != "idempotency_key"}),
        response_json=resp.text,
    )
    if not resp.is_success:
        log.error(
            "create_customer: Square error HTTP %s for booking_id=%r — %s",
            resp.status_code, booking_id, resp.text,
        )
        raise RuntimeError(
            f"Square /v2/customers error {resp.status_code}: {resp.text}"
        )
    data = resp.json()
    customer_id = data["customer"]["id"]
    log.info("Square customer created: customer_id=%s  booking_id=%r", customer_id, booking_id)
    return customer_id


async def create_card(
    source_id: str, booking_id: str, given_name: str, family_name: str
) -> tuple:
    """
    Tokenise and store a card on file via POST /v2/cards.

    Creates a Square customer first (required by the API), using the
    cardholder's name from the payment form.
    Returns ``(card_id, customer_id)`` — both are needed for /v2/payments.
    """
    customer_id = await create_customer(booking_id, given_name, family_name)

    url = f"{_base_url()}/v2/cards"
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "source_id":       source_id,
        "card": {
            "customer_id":  customer_id,
            "reference_id": booking_id[:40],
        },
    }
    log.info(
        "POST %s\nRequest body:\n%s",
        url, json.dumps(body, indent=2),
    )

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)

    log.info(
        "POST %s \u2192 HTTP %s\nResponse body:\n%s",
        url, resp.status_code, resp.text,
    )
    card_id_on_success = resp.json().get("card", {}).get("id") if resp.is_success else None
    err_code, err_detail = _square_errors(resp.text) if not resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CREATE_CARD",
        booking_id=booking_id,
        processor_payment_id=card_id_on_success,
        http_status=resp.status_code,
        success=resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        request_json=json.dumps({k: v for k, v in body.items() if k != "idempotency_key"}),
        response_json=resp.text,
    )
    if not resp.is_success:
        log.error(
            "create_card: Square error HTTP %s for booking_id=%r  source_id=%r — %s",
            resp.status_code, booking_id, source_id, resp.text,
        )
        raise RuntimeError(
            f"Square /v2/cards error {resp.status_code}: {resp.text}"
        )
    data    = resp.json()
    card    = data["card"]
    card_id = card["id"]
    card_meta = {
        "square_customer_id": customer_id,
        "square_card_id":     card_id,
        "card_brand":         card.get("card_brand"),
        "card_last4":         card.get("last_4"),
        "card_exp_month":     card.get("exp_month"),
        "card_exp_year":      card.get("exp_year"),
    }
    log.info("Square card created: card_id=%s customer_id=%s", card_id, customer_id)
    return card_id, customer_id, card_meta


async def create_payment_authorization(
    source_id: str, customer_id: Optional[str], booking_id: str, amount_cents: int
) -> dict:
    """
    Create a pre-authorisation hold via POST /v2/payments (autocomplete=false).

    ``source_id`` is either a stored card ID (card-on-file flow) or a one-time
    digital wallet token (Apple Pay / Google Pay).  ``customer_id`` is required
    by Square for stored cards but must be omitted for wallet tokens.

    The payment is NOT captured immediately; the actual charge (or void/refund)
    happens after the session ends and the final energy usage is known.

    Returns the full ``payment`` object from Square.
    """
    url = f"{_base_url()}/v2/payments"
    amount_dollars = amount_cents / 100
    body = {
        "idempotency_key": booking_id,   # idempotent – safe to retry same booking
        "source_id":       source_id,
        "autocomplete":    False,
        "amount_money": {
            "amount":   amount_cents,
            "currency": "USD",
        },
        "location_id": state._square_config["location_id"],
        "note": (
            f"EV charger authorization hold for booking {booking_id}. "
            f"Pre-auth of ${amount_dollars:.2f}. "
            f"Final charge adjusted after session ends."
        ),
        "reference_id": booking_id[:40],
    }
    # customer_id is required for stored cards, must be absent for wallet tokens.
    if customer_id:
        body["customer_id"] = customer_id
    log.info(
        "POST %s\nRequest body:\n%s",
        url, json.dumps(body, indent=2),
    )

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)

    log.info(
        "POST %s \u2192 HTTP %s\nResponse body:\n%s",
        url, resp.status_code, resp.text,
    )
    payment_id_on_success = resp.json().get("payment", {}).get("id") if resp.is_success else None
    err_code, err_detail = _square_errors(resp.text) if not resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CREATE_PAYMENT",
        booking_id=booking_id,
        processor_payment_id=payment_id_on_success,
        amount_cents=amount_cents,
        http_status=resp.status_code,
        success=resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        request_json=json.dumps({k: v for k, v in body.items() if k != "idempotency_key"}),
        response_json=resp.text,
    )
    if not resp.is_success:
        log.error(
            "create_payment_authorization: Square error HTTP %s for booking_id=%r "
            "source_id=%r  amount_cents=%d  customer_id=%r — %s",
            resp.status_code, booking_id, source_id, amount_cents, customer_id, resp.text,
        )
        raise RuntimeError(
            f"Square /v2/payments error {resp.status_code}: {resp.text}"
        )
    data = resp.json()
    return data["payment"]


async def capture_payment(payment_id: str, final_amount_cents: int) -> dict:
    """
    Complete (capture) a pre-authorisation hold at the final amount.

    Square's /complete endpoint does NOT accept amount_money — it captures
    whatever amount is currently on the payment object.  To capture at a
    different amount we must first PUT /v2/payments/{id} to update the amount,
    then POST /v2/payments/{id}/complete.

    Returns the full updated ``payment`` object from the /complete response.
    """
    # Step 1: update the payment amount to the final value.
    update_url = f"{_base_url()}/v2/payments/{payment_id}"
    update_body = {
        "idempotency_key": str(uuid.uuid4()),
        "payment": {
            "amount_money": {
                "amount":   final_amount_cents,
                "currency": "USD",
            },
        },
    }
    log.info(
        "PUT %s\nRequest body:\n%s",
        update_url, json.dumps(update_body, indent=2),
    )
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        update_resp = await client.put(update_url, json=update_body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "PUT %s -> HTTP %s\nResponse body:\n%s",
        update_url, update_resp.status_code, update_resp.text,
    )
    err_code, err_detail = _square_errors(update_resp.text) if not update_resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "UPDATE_PAYMENT",
        processor_payment_id=payment_id,
        amount_cents=final_amount_cents,
        http_status=update_resp.status_code,
        success=update_resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        request_json=json.dumps({k: v for k, v in update_body.items() if k != "idempotency_key"}),
        response_json=update_resp.text,
    )
    if not update_resp.is_success:
        log.error(
            "capture_payment: Square PUT error HTTP %s for payment_id=%r "
            "final_amount_cents=%d — %s",
            update_resp.status_code, payment_id, final_amount_cents, update_resp.text,
        )
        raise RuntimeError(
            f"Square PUT /v2/payments/{payment_id} error "
            f"{update_resp.status_code}: {update_resp.text}"
        )

    # Step 2: capture (complete) the payment at the updated amount.
    complete_url = f"{_base_url()}/v2/payments/{payment_id}/complete"
    complete_body: dict = {}
    log.info("POST %s (no body)", complete_url)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        complete_resp = await client.post(complete_url, json=complete_body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "POST %s -> HTTP %s\nResponse body:\n%s",
        complete_url, complete_resp.status_code, complete_resp.text,
    )
    err_code, err_detail = _square_errors(complete_resp.text) if not complete_resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "COMPLETE_PAYMENT",
        processor_payment_id=payment_id,
        amount_cents=final_amount_cents,
        http_status=complete_resp.status_code,
        success=complete_resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        response_json=complete_resp.text,
    )
    if not complete_resp.is_success:
        log.error(
            "capture_payment: Square complete error HTTP %s for payment_id=%r — %s",
            complete_resp.status_code, payment_id, complete_resp.text,
        )
        raise RuntimeError(
            f"Square /v2/payments/{payment_id}/complete error "
            f"{complete_resp.status_code}: {complete_resp.text}"
        )
    return complete_resp.json()["payment"]


async def cancel_payment(payment_id: str) -> dict:
    """
    Void (cancel) a pre-authorisation hold without charging anything.

    Called when final_amount_cents == 0.  Uses POST /v2/payments/{id}/cancel.
    Returns the full updated ``payment`` object.
    """
    url = f"{_base_url()}/v2/payments/{payment_id}/cancel"
    log.info("POST %s (void pre-auth, no charge)", url)
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json={}, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "POST %s -> HTTP %s\nResponse body:\n%s",
        url, resp.status_code, resp.text,
    )
    err_code, err_detail = _square_errors(resp.text) if not resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CANCEL_PAYMENT",
        processor_payment_id=payment_id,
        http_status=resp.status_code,
        success=resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        response_json=resp.text,
    )
    if not resp.is_success:
        log.error(
            "cancel_payment: Square error HTTP %s for payment_id=%r — %s",
            resp.status_code, payment_id, resp.text,
        )
        raise RuntimeError(
            f"Square /v2/payments/{payment_id}/cancel error "
            f"{resp.status_code}: {resp.text}"
        )
    return resp.json()["payment"]


async def charge_card_payment(
    card_id: str,
    customer_id: str,
    booking_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """
    Create a direct (immediate) charge against a stored card (autocomplete=True).

    Used when the final amount exceeds the pre-authorised amount — the
    pre-auth is voided first, then this function charges the card for the
    full final amount.

    Returns the full ``payment`` object from Square.
    """
    url = f"{_base_url()}/v2/payments"
    amount_dollars = amount_cents / 100
    body = {
        "idempotency_key": idempotency_key,
        "source_id":       card_id,
        "customer_id":     customer_id,
        "autocomplete":    True,
        "amount_money": {
            "amount":   amount_cents,
            "currency": "USD",
        },
        "location_id": state._square_config["location_id"],
        "note": (
            f"EV charger final charge for booking {booking_id}. "
            f"${amount_dollars:.2f} (exceeded pre-auth; prior hold voided)."
        ),
        "reference_id": booking_id[:40],
    }
    log.info(
        "POST %s\nRequest body:\n%s",
        url, json.dumps(body, indent=2),
    )

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)

    log.info(
        "POST %s \u2192 HTTP %s\nResponse body:\n%s",
        url, resp.status_code, resp.text,
    )
    charge_id_on_success = resp.json().get("payment", {}).get("id") if resp.is_success else None
    err_code, err_detail = _square_errors(resp.text) if not resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "CHARGE_PAYMENT",
        booking_id=booking_id,
        processor_payment_id=charge_id_on_success,
        amount_cents=amount_cents,
        http_status=resp.status_code,
        success=resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        request_json=json.dumps({k: v for k, v in body.items() if k != "idempotency_key"}),
        response_json=resp.text,
    )
    if not resp.is_success:
        log.error(
            "charge_card_payment: Square error HTTP %s for booking_id=%r "
            "card_id=%r  amount_cents=%d — %s",
            resp.status_code, booking_id, card_id, amount_cents, resp.text,
        )
        raise RuntimeError(
            f"Square /v2/payments (direct charge) error {resp.status_code}: {resp.text}"
        )
    data = resp.json()
    return data["payment"]


async def refund_payment(
    payment_id: str,
    amount_cents: Optional[int],
    reason: str = "",
    idempotency_key: Optional[str] = None,
) -> dict:
    """
    Issue a Square RefundPayment against a completed (captured) payment.

    ``amount_cents=None`` means full refund; Square requires the amount_money
    field to be present, so we fetch the payment first if amount is not given.
    Returns the full ``refund`` object from the response.
    """
    if amount_cents is None:
        # Fetch the payment to get the captured amount.
        log.info("refund_payment: fetching payment %r to determine refund amount", payment_id)
        try:
            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=10.0) as client:
                pay_resp = await client.get(
                    f"{_base_url()}/v2/payments/{payment_id}", headers=_headers()
                )
            duration_ms = int((time.monotonic() - t0) * 1000)
            log.info("GET /v2/payments/%s \u2192 HTTP %s", payment_id, pay_resp.status_code)
            await telemetry.record_event(
                "PAYMENT_PROCESSOR", "FETCH_PAYMENT",
                processor_payment_id=payment_id,
                http_status=pay_resp.status_code,
                success=pay_resp.is_success,
                duration_ms=duration_ms,
                response_json=pay_resp.text,
            )
            pay_resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.error(
                "refund_payment: could not fetch payment %r — HTTP %s: %s",
                payment_id, exc.response.status_code, exc.response.text,
            )
            raise
        payment = pay_resp.json()["payment"]
        amount_cents = payment["amount_money"]["amount"]

    url = f"{_base_url()}/v2/refunds"
    body: dict = {
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "payment_id":      payment_id,
        "amount_money": {
            "amount":   amount_cents,
            "currency": "USD",
        },
    }
    if reason:
        body["reason"] = reason

    log.info("POST %s\nRequest body:\n%s", url, json.dumps(body, indent=2))
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=_headers())
    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info("POST %s -> HTTP %s\nResponse body:\n%s", url, resp.status_code, resp.text)

    refund_id_on_success = resp.json().get("refund", {}).get("id") if resp.is_success else None
    err_code, err_detail = _square_errors(resp.text) if not resp.is_success else (None, None)
    await telemetry.record_event(
        "PAYMENT_PROCESSOR", "REFUND_PAYMENT",
        processor_payment_id=refund_id_on_success or payment_id,
        amount_cents=amount_cents,
        http_status=resp.status_code,
        success=resp.is_success,
        error_code=err_code,
        error_detail=err_detail,
        duration_ms=duration_ms,
        request_json=json.dumps({k: v for k, v in body.items() if k not in ("idempotency_key",)}),
        response_json=resp.text,
    )
    if not resp.is_success:
        log.error(
            "refund_payment: Square error HTTP %s for payment_id=%r "
            "amount_cents=%d — %s",
            resp.status_code, payment_id, amount_cents, resp.text,
        )
        raise RuntimeError(
            f"Square POST /v2/refunds error {resp.status_code}: {resp.text}"
        )
    return resp.json()["refund"]

