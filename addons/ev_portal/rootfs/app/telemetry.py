"""
telemetry.py
------------
Single canonical API for recording all observable events in the EV portal.

Call ``record_event(...)`` from anywhere in the codebase.  The implementation
today writes to the local SQLite ``events`` table; to swap to Datadog, Segment,
CloudWatch, etc. change only the body of ``record_event`` — all call sites
remain unchanged.

Design constraints:
  - ``record_event`` MUST NEVER raise.  A telemetry failure must never affect
    business logic or the caller's control flow.
  - ``record_event`` is fire-and-forget.  Callers do not need to await a
    meaningful result.
  - All parameters are keyword-only (except ``source`` and ``event_type``) to
    make call sites self-documenting and resilient to future field additions.

Sources
-------
  PAYMENT_PROCESSOR  Automated call to an external payment API (Square, etc.)
  ADMIN              Human-initiated action via the admin dashboard
  SYSTEM             Internal state transitions and lifecycle events

Event types (examples — not exhaustive)
-----------------------------------------
  Payment processor : CREATE_CUSTOMER, CREATE_CARD, CREATE_PAYMENT,
                      UPDATE_PAYMENT, COMPLETE_PAYMENT, CANCEL_PAYMENT,
                      CHARGE_PAYMENT, FETCH_PAYMENT, REFUND_PAYMENT
  Admin             : CAPTURE, VOID, REFUND, RETRY, REAUTHORIZE, NOTE,
                      SOFT_DELETE
  System            : STATE_CHANGE, STARTUP, SHUTDOWN
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


async def record_event(
    source: str,
    event_type: str,
    *,
    actor: str | None = None,
    idempotency_key: str | None = None,
    booking_id: str | None = None,
    processor_payment_id: str | None = None,
    amount_cents: int | None = None,
    http_status: int | None = None,
    success: bool = True,
    error_code: str | None = None,
    error_detail: str | None = None,
    duration_ms: int | None = None,
    request_json: str | None = None,
    response_json: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Record an observable event.  Never raises.

    Parameters
    ----------
    source               : "PAYMENT_PROCESSOR" | "ADMIN" | "SYSTEM"
    event_type           : short ALL_CAPS verb describing what happened
    actor                : None for automated events; username/IP for humans
    idempotency_key      : internal session key (ev:<charger>:<booking>)
    booking_id           : booking identifier
    processor_payment_id : payment/refund/card ID from the payment processor
    amount_cents         : monetary amount in the smallest currency unit
    http_status          : HTTP response status code from external call
    success              : True if the operation succeeded
    error_code           : processor-specific error code (e.g. INVALID_CARD_DATA)
    error_detail         : human-readable error message from the processor
    duration_ms          : wall-clock time of the external HTTP call
    request_json         : serialised request body (secrets must be redacted)
    response_json        : serialised response body
    metadata             : arbitrary extra context (stored as JSON)
    """
    # Local import breaks potential circular dependency at module load time.
    import db  # noqa: PLC0415

    try:
        await db.write_event(
            source=source,
            event_type=event_type,
            actor=actor,
            idempotency_key=idempotency_key,
            booking_id=booking_id,
            processor_payment_id=processor_payment_id,
            amount_cents=amount_cents,
            http_status=http_status,
            success=1 if success else 0,
            error_code=error_code,
            error_detail=error_detail,
            duration_ms=duration_ms,
            request_json=request_json,
            response_json=response_json,
            metadata_json=json.dumps(metadata) if metadata is not None else None,
        )
    except Exception:
        log.exception(
            "telemetry: failed to record event source=%r type=%r key=%r — ignoring",
            source, event_type, idempotency_key,
        )
