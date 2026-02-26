"""
GET /session/{session_id}

Returns an HTML page showing the current state of an EV charging session.
Only non-sensitive display fields are rendered (no PAN, CVV, or raw tokens).
Also exposes GET /session/{session_id}/json for programmatic access.
"""

import html
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import db

log = logging.getLogger(__name__)
router = APIRouter()


def render_session_page(row: dict) -> HTMLResponse:
    """Render the 'EV Charger Enabled' confirmation page from a DB session row."""
    booking_id   = html.escape(str(row.get("booking_id")   or ""))
    payment_id   = html.escape(str(row.get("square_payment_id") or ""))
    card_id      = html.escape(str(row.get("square_card_id")    or ""))
    amount_cents = row.get("authorized_amount_cents") or 0
    amount_str   = f"${amount_cents / 100:.2f} USD"
    card_brand   = html.escape(str(row.get("card_brand") or ""))
    card_last4   = html.escape(str(row.get("card_last4") or ""))
    card_exp     = ""
    if row.get("card_exp_month") and row.get("card_exp_year"):
        card_exp = f"{row['card_exp_month']:02d}/{row['card_exp_year']}"

    card_line = ""
    if card_brand or card_last4:
        card_line = f"{card_brand} ending {card_last4}"
        if card_exp:
            card_line += f" (exp {card_exp})"

    def info_card(label: str, value: str) -> str:
        return (
            '<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;'
            'padding:.75rem 1.2rem;margin-bottom:1rem">'
            f'<div style="font-size:.72rem;text-transform:uppercase;color:#777;margin-bottom:.2rem">{label}</div>'
            f'<div style="font-family:monospace;font-size:.9rem;word-break:break-all">{value}</div>'
            '</div>'
        )

    cards_html = info_card("Booking ID", booking_id)
    cards_html += info_card("Authorization hold", amount_str)
    if payment_id:
        cards_html += info_card("Square payment ID", payment_id)
    if card_id:
        cards_html += info_card("Square card ID", card_id)
    if card_line:
        cards_html += info_card("Card", card_line)

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>EV Charger Enabled</title>
  <style>
    body {{ font-family: sans-serif; max-width: 520px; margin: 60px auto;
            padding: 0 1rem; color: #222; text-align: center; }}
    .icon {{ font-size: 4rem; }}
    h1 {{ font-size: 1.6rem; color: #1a7f3c; margin-bottom: .5rem; }}
    .sub {{ color: #555; margin-bottom: 2rem; }}
    .note {{ font-size: .82rem; color: #777; margin-top: 1.5rem; }}
    div[style] {{ text-align: left; }}
  </style>
</head>
<body>
  <div class="icon">&#9889;</div>
  <h1>EV Charger Enabled</h1>
  <p class="sub">Authorization hold placed.<br>You can now plug in your car.</p>
  {cards_html}
  <p class="note">Pre-auth hold only. Final charge reflects actual energy used.</p>
</body>
</html>
""")


@router.get("/session/{session_id}", response_class=HTMLResponse)
async def get_session_page(session_id: str):
    row = await db.get_session_by_uid(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return render_session_page(row)


@router.get("/session/{session_id}/json")
async def get_session_json(session_id: str):
    row = await db.get_session_by_uid(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({
        "booking_id":              row["booking_id"],
        "charger_id":              row["charger_id"],
        "state":                   row["state"],
        "authorized":              bool(row["authorized"]),
        "authorized_amount_cents": row["authorized_amount_cents"],
        "card_brand":              row["card_brand"],
        "card_last4":              row["card_last4"],
        "card_exp_month":          row["card_exp_month"],
        "card_exp_year":           row["card_exp_year"],
        "square_payment_id":       row["square_payment_id"],
        "last_error":              row["last_error"],
    })
