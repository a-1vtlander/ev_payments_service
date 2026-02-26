from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

import db
import state
from portal_templates import templates

router = APIRouter()

_STATE_COLORS = {
    "AWAITING_PAYMENT_INFO": "#f29900",
    "AUTH_REQUESTED":        "#f29900",
    "AUTHORIZED":            "#1a73e8",
    "CAPTURED":              "#188038",
    "CANCELED":              "#c00",
    "REFUNDED":              "#e37400",
    "FAILED":                "#c00",
    "ERROR":                 "#c00",
}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    mqtt_status = "connected" if (state.mqtt_client and state.mqtt_client.is_connected()) else "disconnected"
    home_id    = state._app_config.get("home_id",    "–")
    charger_id = state._app_config.get("charger_id", "–")

    recent = await db.list_sessions(limit=1)
    session_row = recent[0] if recent else None
    session_ctx = None
    if session_row:
        amount_cents = session_row.get("authorized_amount_cents") or 0
        cap_cents    = session_row.get("captured_amount_cents")
        sess_state   = session_row.get("state", "")
        session_ctx = {
            "state":        sess_state,
            "state_color":  _STATE_COLORS.get(sess_state, "#666"),
            "guest_name":   session_row.get("guest_name") or "",
            "auth_display": f"${amount_cents / 100:.2f} USD",
            "cap_display":  f"${cap_cents / 100:.2f} USD" if cap_cents is not None else "",
            "card_brand":   session_row.get("card_brand") or "",
            "card_last4":   session_row.get("card_last4") or "",
            "updated_at":   session_row.get("updated_at") or "",
        }

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "mqtt_status":              mqtt_status,
            "status_color":             "green" if mqtt_status == "connected" else "red",
            "home_id":                  home_id,
            "charger_id":               charger_id,
            "base_topic":               f"ev/charger/{home_id}/{charger_id}/booking",
            "booking_response_topic":   state._booking_response_topic,
            "authorize_request_topic":  state._authorize_request_topic,
            "authorize_response_topic": state._authorize_response_topic,
            "response_timeout":         int(state.RESPONSE_TIMEOUT),
            "session":                  session_ctx,
        },
    )
