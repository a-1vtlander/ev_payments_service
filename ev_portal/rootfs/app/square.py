"""
Square Online Checkout API helpers.
"""

import httpx
import json
import logging

import state

log = logging.getLogger(__name__)

SQUARE_API_VERSION = "2026-01-22"
SQUARE_SANDBOX_BASE = "https://connect.squareupsandbox.com"
SQUARE_PROD_BASE    = "https://connect.squareup.com"


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
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_base_url()}/v2/locations", headers=_headers())
        resp.raise_for_status()
        locations = resp.json().get("locations", [])
    active = [loc for loc in locations if loc.get("status") == "ACTIVE"]
    if not active:
        raise RuntimeError("No active Square locations found")
    return active[0]["id"]


async def create_payment_link(
    booking_id: str, amount_cents: int, redirect_url: str
) -> tuple[str, str]:
    """
    Call the Square Online Checkout API.

    Returns ``(payment_url, payment_token)`` where:
      - ``payment_url``   is the short Square link to redirect the customer to.
      - ``payment_token`` is ``payment_link.id`` – store for reconciliation.

    ``booking_id`` is used as the idempotency key so retries for the same
    booking return the same link rather than creating a duplicate.

    ``amount_cents`` is the pre-authorization hold.  The actual charge may
    differ; Square adjusts or refunds the difference after the session closes.
    """
    url = f"{_base_url()}/v2/online-checkout/payment-links"
    amount_dollars = amount_cents / 100

    body: dict = {
        "idempotency_key": booking_id,
        "quick_pay": {
            "name": "EV Charging \u2013 Authorization Hold",
            "price_money": {
                "amount":   amount_cents,
                "currency": "USD",
            },
            "location_id": state._square_config["location_id"],
        },
        "payment_note": (
            f"EV charger authorization hold for booking {booking_id}. "
            f"This is a pre-authorization of ${amount_dollars:.2f}. "
            f"Your final charge may be higher or lower depending on actual "
            f"energy consumed; any difference will be adjusted or refunded "
            f"after your session ends."
        ),
        "checkout_options": {
            "redirect_url": redirect_url,
        },
    }

    log.info(
        "Calling Square API: %s (sandbox=%s booking_id=%s amount_cents=%s)",
        url, state._square_config["sandbox"], booking_id, amount_cents,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json=body, headers=_headers())
        resp.raise_for_status()
        data = resp.json()

    link = data["payment_link"]
    log.info("Square full response: %s", json.dumps(data, indent=2))
    payment_url   = link["url"]
    payment_token = link["id"]
    log.info("Square payment link created: url=%s token=%s", payment_url, payment_token)
    return payment_url, payment_token
