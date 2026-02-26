"""
admin/router.py – Admin API routes.

Public routes (no auth):
  GET  /admin/login
  POST /admin/login
  GET  /admin/logout

Protected routes (session cookie or Basic Auth):
  GET  /admin/health
  GET  /admin/sessions
  GET  /admin/sessions/{idempotency_key}
  POST /admin/sessions/{idempotency_key}/capture       (AUTHORIZED → CAPTURED)
  POST /admin/sessions/{idempotency_key}/void          (AUTHORIZED → CANCELED)
  POST /admin/sessions/{idempotency_key}/reauthorize   (CAPTURED  → AUTHORIZED)
  POST /admin/sessions/{idempotency_key}/refund        (CAPTURED  → REFUNDED)
  POST /admin/sessions/{idempotency_key}/note
  POST /admin/sessions/{idempotency_key}/soft_delete
"""

import json
import logging
import urllib.parse
import uuid as _uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import db
import square
import state
from admin.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    make_session_token,
    require_admin,
    validate_basic_credentials,
    verify_session_token,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Login / logout (no auth required)
# ---------------------------------------------------------------------------

_LOGIN_PAGE_TMPL = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="icon" href="data:,">
  <title>EV Portal Admin – Login</title>
  <style>
    body{{font-family:sans-serif;max-width:400px;margin:100px auto;padding:0 20px}}
    h1{{font-size:1.4rem;margin-bottom:1.5rem}}
    label{{display:block;margin-bottom:.3rem;font-weight:bold;font-size:.9rem}}
    input[type=text],input[type=password]{{width:100%;box-sizing:border-box;
      padding:8px 10px;border:1px solid #ccc;border-radius:4px;font-size:1rem;margin-bottom:1rem}}
    button{{width:100%;padding:10px;background:#1a73e8;color:#fff;border:none;
      border-radius:4px;font-size:1rem;cursor:pointer}}
    button:hover{{background:#1558b0}}
    .error{{color:#c00;background:#fee;border:1px solid #fcc;padding:8px 12px;
      border-radius:4px;margin-bottom:1rem;font-size:.9rem}}
  </style>
</head>
<body>
  <h1>EV Portal Admin</h1>
  {error_block}
  <form method="post" action="/admin/login">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>"""


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: int = 0):
    """Serve the HTML login form. If already authenticated, redirect to sessions."""
    # If they already have a valid session cookie, skip login
    session_cookie = request.cookies.get(SESSION_COOKIE)
    if session_cookie and verify_session_token(session_cookie):
        return RedirectResponse(url="/admin/sessions", status_code=302)

    error_block = (
        '<div class="error">Invalid username or password. Please try again.</div>'
        if error else ""
    )
    return _LOGIN_PAGE_TMPL.format(error_block=error_block)


@router.post("/login", include_in_schema=False)
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
):
    """Validate credentials and set a signed session cookie."""
    if validate_basic_credentials(username, password):
        token = make_session_token(username)
        response = RedirectResponse(url="/admin/sessions", status_code=303)
        response.set_cookie(
            key=SESSION_COOKIE,
            value=token,
            max_age=SESSION_TTL,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return response
    # Bad credentials – redirect back to login with error flag
    return RedirectResponse(url="/admin/login?error=1", status_code=303)


@router.get("/logout", include_in_schema=False)
async def logout():
    """Clear the session cookie and redirect to the login page."""
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class NoteBody(BaseModel):
    note: str


class RefundBody(BaseModel):
    amount_cents: Optional[int] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _get_or_404(idempotency_key: str) -> dict:
    session = await db.get_session(idempotency_key)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {idempotency_key!r}",
        )
    return session


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin_index(actor: Annotated[str, Depends(require_admin)]):
    """HTML dashboard shown after login."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="icon" href="data:,">
  <title>EV Portal Admin</title>
  <style>
    body{{font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 20px}}
    h1{{font-size:1.5rem}} h2{{font-size:1.1rem;margin-top:1.5rem}}
    ul{{line-height:2}} a{{color:#1a73e8}}
    .logout{{float:right;font-size:.85rem;color:#666}}
  </style>
</head>
<body>
  <h1>EV Portal Admin <span class="logout"><a href="/admin/logout">Sign out</a></span></h1>
  <p>Signed in as <strong>{actor}</strong><br><small style="color:#888">Buttons for Capture / Void / Refund / Reauthorize appear on individual session detail pages.</small></p>
  <h2>Sessions</h2>
  <ul>
    <li><a href="/admin/sessions">GET /admin/sessions</a> – list all sessions</li>
    <li><a href="/admin/sessions?include_deleted=true">GET /admin/sessions?include_deleted=true</a></li>
  </ul>
  <h2>API Docs</h2>
  <ul>
    <li><a href="/admin/docs">Swagger UI</a></li>
  </ul>
</body>
</html>"""


@router.get("/health")
async def admin_health(actor: Annotated[str, Depends(require_admin)]):
    return "ok"


@router.get("/sessions")
async def list_sessions(
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    state: Optional[str] = Query(default=None),
    include_deleted: bool = Query(default=False),
):
    """Return sessions. Returns HTML for browsers, JSON for API clients."""
    rows = await db.list_sessions(
        limit=limit,
        offset=offset,
        state_filter=state,
        include_deleted=include_deleted,
    )

    if "text/html" not in request.headers.get("accept", ""):
        return {"sessions": rows, "count": len(rows), "offset": offset}

    # ── HTML table view ───────────────────────────────────────────────────
    import html as _html

    STATE_COLORS = {
        "CREATED":                "#aaa",
        "AWAITING_PAYMENT_INFO":  "#f29900",
        "AUTHORIZED":             "#1a73e8",
        "CAPTURED":               "#188038",
        "CANCELED":               "#c00",
        "REFUNDED":               "#e37400",
        "ERROR":                  "#c00",
    }

    def badge(s: str) -> str:
        color = STATE_COLORS.get(s, "#666")
        return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
                f'border-radius:10px;font-size:.8rem;font-weight:bold">'
                f'{_html.escape(s)}</span>')

    def cents(v) -> str:
        return f"${v/100:.2f}" if v is not None else "—"

    def esc(v) -> str:
        return _html.escape(str(v)) if v not in (None, "") else "<em style='color:#bbb'>—</em>"

    rows_html = ""
    for r in rows:
        deleted = r.get("is_deleted")
        style = ' style="opacity:.4"' if deleted else ""
        rows_html += f"""<tr{style}>
          <td>{badge(r.get('state',''))}</td>
          <td style="font-family:monospace;font-size:.8rem">{esc(r.get('idempotency_key',''))}</td>
          <td>{esc(r.get('charger_id',''))}</td>
          <td>{esc(r.get('card_brand',''))} {esc(r.get('card_last4',''))}</td>
          <td style="text-align:right">{cents(r.get('authorized_amount_cents'))}</td>
          <td style="text-align:right">{cents(r.get('captured_amount_cents'))}</td>
          <td style="font-size:.8rem">{esc(r.get('created_at',''))}</td>
          <td>{esc(r.get('note',''))}</td>
          <td><a href="/admin/sessions/{urllib.parse.quote(str(r.get('idempotency_key','')), safe='')}">detail</a></td>
        </tr>"""

    filter_opts = ""
    for st in ("", "CREATED", "AWAITING_PAYMENT_INFO", "AUTHORIZED", "CAPTURED", "CANCELED", "REFUNDED", "ERROR"):
        sel = 'selected' if (state or "") == st else ""
        filter_opts += f'<option value="{st}" {sel}>{st or "All states"}</option>'

    del_checked = "checked" if include_deleted else ""

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>EV Portal Admin – Sessions</title>
  <style>
    body{{font-family:sans-serif;margin:0;padding:20px;background:#f5f5f5}}
    h1{{font-size:1.3rem;margin-bottom:1rem}}
    .toolbar{{display:flex;gap:12px;align-items:center;margin-bottom:1rem;flex-wrap:wrap}}
    .toolbar select,.toolbar button{{padding:6px 12px;border:1px solid #ccc;
      border-radius:4px;font-size:.9rem;background:#fff;cursor:pointer}}
    .toolbar label{{font-size:.9rem}}
    .toolbar .logout{{margin-left:auto;color:#666;font-size:.85rem;text-decoration:none}}
    table{{width:100%;border-collapse:collapse;background:#fff;border-radius:6px;
      box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden}}
    th{{background:#f0f0f0;padding:8px 12px;text-align:left;font-size:.85rem;
      border-bottom:2px solid #ddd;white-space:nowrap}}
    td{{padding:8px 12px;border-bottom:1px solid #eee;font-size:.85rem;vertical-align:top}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#fafafa}}
    .count{{color:#666;font-size:.85rem;margin-bottom:.5rem}}
    a{{color:#1a73e8}}
  </style>
</head>
<body>
  <h1>Sessions <a href="/admin/logout" class="logout" style="float:right;font-size:.8rem">Sign out ({_html.escape(actor)})</a></h1>
  <form class="toolbar" method="get" action="/admin/sessions">
    <select name="state">{filter_opts}</select>
    <label><input type="checkbox" name="include_deleted" value="true" {del_checked}> Show deleted</label>
    <button type="submit">Filter</button>
    <a href="/admin/" style="font-size:.85rem">← Dashboard</a>
  </form>
  <div class="count">{len(rows)} session(s) shown</div>
  <table>
    <thead><tr>
      <th>State</th><th>Idempotency Key</th><th>Charger</th>
      <th>Card</th><th>Auth $</th><th>Captured $</th>
      <th>Created</th><th>Note</th><th></th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</body>
</html>""")


@router.get("/sessions/{idempotency_key}")
async def get_session(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """Return a single session. HTML for browsers, JSON for API clients."""
    row = await _get_or_404(idempotency_key)

    if "text/html" not in request.headers.get("accept", ""):
        return row

    import html as _html

    def esc(v) -> str:
        return _html.escape(str(v)) if v not in (None, "", 0) else "<em style='color:#bbb'>—</em>"

    def cents(v) -> str:
        return f"${v/100:.2f}" if v is not None else "—"

    fields = [
        ("State",             row.get("state")),
        ("Idempotency key",   row.get("idempotency_key")),
        ("Charger ID",        row.get("charger_id")),
        ("Booking ID",        row.get("booking_id")),
        ("Session ID",        row.get("session_id")),
        ("Card",              f"{row.get('card_brand','')} ···· {row.get('card_last4','')}"),
        ("Card expires",      f"{row.get('card_exp_month','')}/{row.get('card_exp_year','')}"),
        ("Authorized",        cents(row.get("authorized_amount_cents"))),
        ("Captured",          cents(row.get("captured_amount_cents"))),
        ("Square payment ID", row.get("square_payment_id")),
        ("Square order ID",   row.get("square_order_id")),
        ("Environment",       row.get("square_environment")),
        ("Created",           row.get("created_at")),
        ("Updated",           row.get("updated_at")),
        ("Note",              row.get("note")),
        ("Deleted",           "Yes" if row.get("is_deleted") else "No"),
        ("Last error",        row.get("last_error")),
    ]

    rows_html = "".join(
        f"<tr><td>{_html.escape(k)}</td><td>{esc(v)}</td></tr>"
        for k, v in fields
    )

    ik = _html.escape(idempotency_key)
    url_ik = urllib.parse.quote(idempotency_key, safe='')
    state_val = row.get("state", "")
    auth_cents   = row.get("authorized_amount_cents") or 0
    cap_cents    = row.get("captured_amount_cents")   or 0
    auth_dollars = auth_cents  / 100
    cap_dollars  = cap_cents   / 100

    # ── AUTHORIZED actions ─────────────────────────────────────────────────
    capture_btn = void_btn = ""
    if state_val == "AUTHORIZED":
        capture_btn = f"""
      <button onclick="document.getElementById('captureDialog').showModal()"
        style="background:#188038;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">
        Capture</button>
      <dialog id="captureDialog" style="border-radius:8px;padding:20px;min-width:320px;border:1px solid #ddd">
        <h3 style="margin:0 0 8px">Capture payment</h3>
        <p style="font-size:.85rem;color:#555;margin:0 0 12px">
          Pre-authorized: <strong>${auth_dollars:.2f}</strong>. Enter the final charge amount.</p>
        <form method="post" action="/admin/sessions/{url_ik}/capture">
          <label style="display:block;margin-bottom:10px;font-size:.9rem">Amount&nbsp;($)
            <input type="number" name="amount_dollars" step="0.01" min="0.01"
              value="{auth_dollars:.2f}" required autofocus
              style="display:block;width:100%;box-sizing:border-box;margin-top:4px;
                     padding:7px 9px;border:1px solid #ccc;border-radius:4px;font-size:1rem">
          </label>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
            <button type="button" onclick="this.closest('dialog').close()"
              style="padding:7px 16px;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#fff">
              Cancel</button>
            <button type="submit"
              style="background:#188038;color:#fff;border:none;padding:7px 16px;border-radius:4px;cursor:pointer">
              Confirm capture</button>
          </div>
        </form>
      </dialog>"""

        void_btn = f"""
      <button onclick="document.getElementById('voidDialog').showModal()"
        style="background:#c00;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">
        Void</button>
      <dialog id="voidDialog" style="border-radius:8px;padding:20px;min-width:320px;border:1px solid #ddd">
        <h3 style="margin:0 0 8px">Void authorization</h3>
        <p style="font-size:.85rem;color:#555;margin:0 0 12px">
          This will cancel the pre-auth hold of <strong>${auth_dollars:.2f}</strong>. No charge will be made.</p>
        <form method="post" action="/admin/sessions/{url_ik}/void">
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
            <button type="button" onclick="this.closest('dialog').close()"
              style="padding:7px 16px;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#fff">
              Cancel</button>
            <button type="submit"
              style="background:#c00;color:#fff;border:none;padding:7px 16px;border-radius:4px;cursor:pointer">
              Confirm void</button>
          </div>
        </form>
      </dialog>"""

    # ── CAPTURED actions ───────────────────────────────────────────────────
    reauth_btn = refund_btn = ""
    if state_val == "CAPTURED":
        reauth_btn = f"""
      <button onclick="document.getElementById('reauthDialog').showModal()"
        style="background:#1a73e8;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">
        Reauthorize</button>
      <dialog id="reauthDialog" style="border-radius:8px;padding:20px;min-width:320px;border:1px solid #ddd">
        <h3 style="margin:0 0 8px">New authorization</h3>
        <p style="font-size:.85rem;color:#555;margin:0 0 12px">
          Creates a fresh pre-auth hold on the same stored card.</p>
        <form method="post" action="/admin/sessions/{url_ik}/reauthorize">
          <label style="display:block;margin-bottom:10px;font-size:.9rem">Amount&nbsp;($)
            <input type="number" name="amount_dollars" step="0.01" min="0.01"
              value="{auth_dollars:.2f}" required autofocus
              style="display:block;width:100%;box-sizing:border-box;margin-top:4px;
                     padding:7px 9px;border:1px solid #ccc;border-radius:4px;font-size:1rem">
          </label>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
            <button type="button" onclick="this.closest('dialog').close()"
              style="padding:7px 16px;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#fff">
              Cancel</button>
            <button type="submit"
              style="background:#1a73e8;color:#fff;border:none;padding:7px 16px;border-radius:4px;cursor:pointer">
              Authorize</button>
          </div>
        </form>
      </dialog>"""

        refund_btn = f"""
      <button onclick="document.getElementById('refundDialog').showModal()"
        style="background:#e37400;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer">
        Refund</button>
      <dialog id="refundDialog" style="border-radius:8px;padding:20px;min-width:320px;border:1px solid #ddd">
        <h3 style="margin:0 0 8px">Issue refund</h3>
        <p style="font-size:.85rem;color:#555;margin:0 0 12px">
          Captured: <strong>${cap_dollars:.2f}</strong></p>
        <form method="post" action="/admin/sessions/{url_ik}/refund">
          <label style="display:block;margin-bottom:10px;font-size:.9rem">Amount&nbsp;($)
            <input type="number" name="amount_dollars" step="0.01" min="0.01" max="{cap_dollars:.2f}"
              value="{cap_dollars:.2f}" required
              style="display:block;width:100%;box-sizing:border-box;margin-top:4px;
                     padding:7px 9px;border:1px solid #ccc;border-radius:4px;font-size:1rem">
          </label>
          <label style="display:block;margin-bottom:10px;font-size:.9rem">Reason
            <input type="text" name="reason" value="Admin refund" required
              style="display:block;width:100%;box-sizing:border-box;margin-top:4px;
                     padding:7px 9px;border:1px solid #ccc;border-radius:4px;font-size:1rem">
          </label>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
            <button type="button" onclick="this.closest('dialog').close()"
              style="padding:7px 16px;border:1px solid #ccc;border-radius:4px;cursor:pointer;background:#fff">
              Cancel</button>
            <button type="submit"
              style="background:#e37400;color:#fff;border:none;padding:7px 16px;border-radius:4px;cursor:pointer">
              Confirm refund</button>
          </div>
        </form>
      </dialog>"""

    note_form = f"""<form method="post" action="/admin/sessions/{url_ik}/note" style="margin-top:.5rem">
      <input type="text" name="note" placeholder="Add note…"
        style="padding:6px 10px;border:1px solid #ccc;border-radius:4px;width:300px">
      <button type="submit" style="padding:6px 14px;border:1px solid #ccc;border-radius:4px;cursor:pointer">
        Save note</button></form>"""

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">  <link rel="icon" href="data:,">  <link rel="icon" href="data:,">
  <title>Session {ik}</title>
  <style>
    body{{font-family:sans-serif;max-width:700px;margin:30px auto;padding:0 20px}}
    h1{{font-size:1.2rem}} h2{{font-size:1rem;margin-top:1.5rem}}
    table{{border-collapse:collapse;width:100%}}
    td{{padding:7px 10px;border:1px solid #ddd;font-size:.9rem}}
    td:first-child{{font-weight:bold;background:#f7f7f7;width:180px;white-space:nowrap}}
    .actions{{margin-top:1rem;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
    dialog::backdrop{{background:rgba(0,0,0,.35)}}
    a{{color:#1a73e8}}
  </style>
</head>
<body>
  <h1>Session detail <a href="/admin/logout" style="float:right;font-size:.8rem;color:#666">Sign out ({_html.escape(actor)})</a></h1>
  <p><a href="/admin/sessions">← Back to sessions</a></p>
  <table>{rows_html}</table>
  <h2>Actions</h2>
  <div class="actions">
    {capture_btn}
    {void_btn}
    {reauth_btn}
    {refund_btn}
    {note_form}
  </div>
</body>
</html>""")


@router.post("/sessions/{idempotency_key}/capture")
async def capture_session(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """
    Admin-initiated capture of a pre-auth hold at a specific amount.
    Accepts JSON { "amount_cents": N } or HTML form { "amount_dollars": "N.NN" }.
    """
    ct = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        form = await request.form()
        amount_cents = int(round(float(form.get("amount_dollars", "0")) * 100))
    else:
        data = await request.json()
        amount_cents = data.get("amount_cents") or int(round(float(data.get("amount_dollars", 0)) * 100))

    session = await _get_or_404(idempotency_key)
    payment_id = session.get("square_payment_id")
    if not payment_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Session has no Square payment_id – cannot capture")
    if session.get("state") != "AUTHORIZED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"State is {session.get('state')!r}; can only capture AUTHORIZED sessions")

    try:
        result = await square.capture_payment(payment_id, amount_cents)
    except Exception as exc:
        log.error("admin capture failed for %s: %s", idempotency_key, exc)
        await db.write_audit_log(
            actor=actor, action="capture", idempotency_key=idempotency_key,
            before_json=json.dumps(session),
            result_json=json.dumps({"error": str(exc)}),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    captured_id = result.get("id", payment_id)
    await db.mark_captured(idempotency_key, captured_id, amount_cents)
    after = await db.get_session(idempotency_key)
    await db.write_audit_log(
        actor=actor, action="capture", idempotency_key=idempotency_key,
        before_json=json.dumps(session), after_json=json.dumps(after),
        result_json=json.dumps(result),
    )

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/admin/sessions/{urllib.parse.quote(idempotency_key, safe='')}", status_code=303)
    return {"ok": True, "idempotency_key": idempotency_key,
            "captured_amount_cents": amount_cents, "square_result": result}


@router.post("/sessions/{idempotency_key}/reauthorize")
async def reauthorize_session(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """
    Create a fresh Square pre-auth on the same stored card after a session has been captured.
    Resets the session state to AUTHORIZED with the new payment ID.
    Accepts JSON { "amount_cents": N } or HTML form { "amount_dollars": "N.NN" }.
    """
    ct = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        form = await request.form()
        amount_cents = int(round(float(form.get("amount_dollars", "0")) * 100))
    else:
        data = await request.json()
        amount_cents = data.get("amount_cents") or int(round(float(data.get("amount_dollars", 0)) * 100))

    session = await _get_or_404(idempotency_key)
    if session.get("state") != "CAPTURED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=f"State is {session.get('state')!r}; can only reauthorize CAPTURED sessions")

    card_id     = session.get("square_card_id")
    customer_id = session.get("square_customer_id")
    if not card_id or not customer_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No stored card/customer ID on this session – cannot reauthorize")

    # Use a fresh unique key so Square doesn't deduplicate against the original auth.
    reauth_idem = f"reauth-{idempotency_key[:20]}-{str(_uuid.uuid4())[:8]}"

    try:
        result = await square.create_payment_authorization(
            card_id=card_id,
            customer_id=customer_id,
            booking_id=reauth_idem,
            amount_cents=amount_cents,
        )
    except Exception as exc:
        log.error("admin reauthorize failed for %s: %s", idempotency_key, exc)
        await db.write_audit_log(
            actor=actor, action="reauthorize", idempotency_key=idempotency_key,
            before_json=json.dumps(session),
            result_json=json.dumps({"error": str(exc)}),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    new_payment_id = result.get("id")
    await db.mark_authorized(
        idempotency_key,
        square_payment_id=new_payment_id,
        authorized_amount_cents=amount_cents,
        square_customer_id=customer_id,
        square_card_id=card_id,
        card_brand=session.get("card_brand"),
        card_last4=session.get("card_last4"),
        card_exp_month=session.get("card_exp_month"),
        card_exp_year=session.get("card_exp_year"),
    )
    after = await db.get_session(idempotency_key)
    await db.write_audit_log(
        actor=actor, action="reauthorize", idempotency_key=idempotency_key,
        before_json=json.dumps(session), after_json=json.dumps(after),
        result_json=json.dumps(result),
    )

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/admin/sessions/{urllib.parse.quote(idempotency_key, safe='')}", status_code=303)
    return {"ok": True, "idempotency_key": idempotency_key,
            "new_payment_id": new_payment_id, "amount_cents": amount_cents}


@router.post("/sessions/{idempotency_key}/note")
async def add_note(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """Persist an operator note on the session. Accepts JSON body or HTML form POST."""
    ct = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        form = await request.form()
        note_text = str(form.get("note", ""))
    else:
        data = await request.json()
        note_text = data.get("note", "")

    before = await _get_or_404(idempotency_key)
    await db.add_note(idempotency_key, note_text)
    after = await db.get_session(idempotency_key)
    await db.write_audit_log(
        actor=actor, action="note", idempotency_key=idempotency_key,
        reason=note_text,
        before_json=json.dumps(before), after_json=json.dumps(after),
    )

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/admin/sessions/{urllib.parse.quote(idempotency_key, safe='')}", status_code=303)
    return {"ok": True, "idempotency_key": idempotency_key}


@router.post("/sessions/{idempotency_key}/soft_delete")
async def soft_delete(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """Mark the session as deleted (is_deleted=1). Row is never removed."""
    before = await _get_or_404(idempotency_key)
    await db.soft_delete(idempotency_key)
    after = await db.get_session(idempotency_key)
    await db.write_audit_log(
        actor=actor, action="soft_delete", idempotency_key=idempotency_key,
        before_json=json.dumps(before), after_json=json.dumps(after),
    )

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/admin/sessions/{urllib.parse.quote(idempotency_key, safe='')}", status_code=303)
    return {"ok": True, "idempotency_key": idempotency_key}


@router.post("/sessions/{idempotency_key}/void")
async def void_session(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """
    Admin-initiated void of a pre-auth hold (Square AUTHORIZED payment).
    Calls Square CancelPayment and sets DB state to CANCELED.
    """
    session = await _get_or_404(idempotency_key)
    payment_id = session.get("square_payment_id")
    if not payment_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Session has no Square payment_id – cannot void",
        )
    if session.get("state") not in ("AUTHORIZED",):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session state is {session.get('state')!r}; can only void AUTHORIZED sessions",
        )

    try:
        result = await square.cancel_payment(payment_id)
    except Exception as exc:
        log.error("admin void failed for %s: %s", idempotency_key, exc)
        await db.write_audit_log(
            actor=actor, action="void", idempotency_key=idempotency_key,
            before_json=json.dumps(session),
            result_json=json.dumps({"error": str(exc)}),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    await db.mark_canceled(idempotency_key, payment_id)
    after = await db.get_session(idempotency_key)
    await db.write_audit_log(
        actor=actor, action="void", idempotency_key=idempotency_key,
        before_json=json.dumps(session), after_json=json.dumps(after),
        result_json=json.dumps(result),
    )

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/admin/sessions/{urllib.parse.quote(idempotency_key, safe='')}", status_code=303)
    return {"ok": True, "idempotency_key": idempotency_key, "square_result": result}


@router.post("/sessions/{idempotency_key}/refund")
async def refund_session(
    idempotency_key: str,
    request: Request,
    actor: Annotated[str, Depends(require_admin)],
):
    """
    Issue a Square refund against a completed (CAPTURED) payment.
    Accepts JSON body { "amount_cents": 1234, "reason": "..." } or HTML form POST.
    amount_cents is optional; omit for a full refund.
    """
    ct = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        form = await request.form()
        amount_cents: Optional[int] = None
        raw_amount = form.get("amount_dollars") or form.get("amount_cents")
        if raw_amount:
            try:
                # Dialog sends amount_dollars; legacy JSON sends amount_cents
                if form.get("amount_dollars"):
                    amount_cents = int(round(float(raw_amount) * 100))
                else:
                    amount_cents = int(raw_amount)
            except ValueError:
                pass
        reason: str = str(form.get("reason", ""))
    else:
        data = await request.json()
        amount_cents = data.get("amount_cents")
        reason = data.get("reason", "")

    session = await _get_or_404(idempotency_key)
    payment_id = session.get("square_payment_id")
    if not payment_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Session has no Square payment_id – cannot refund",
        )
    if session.get("state") not in ("CAPTURED",):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session state is {session.get('state')!r}; can only refund CAPTURED sessions",
        )

    try:
        result = await square.refund_payment(
            payment_id=payment_id,
            amount_cents=amount_cents,
            reason=reason,
        )
    except Exception as exc:
        log.error("admin refund failed for %s: %s", idempotency_key, exc)
        await db.write_audit_log(
            actor=actor, action="refund", idempotency_key=idempotency_key,
            reason=reason,
            before_json=json.dumps(session),
            result_json=json.dumps({"error": str(exc)}),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    refund_amount = result.get("amount_money", {}).get("amount", amount_cents or 0)
    refund_id = result.get("id", payment_id)
    await db.mark_refunded(idempotency_key, refund_id, refund_amount)
    after = await db.get_session(idempotency_key)
    await db.write_audit_log(
        actor=actor, action="refund", idempotency_key=idempotency_key,
        reason=reason,
        before_json=json.dumps(session), after_json=json.dumps(after),
        result_json=json.dumps(result),
    )

    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url=f"/admin/sessions/{urllib.parse.quote(idempotency_key, safe='')}", status_code=303)
    return {
        "ok": True,
        "idempotency_key": idempotency_key,
        "refund_id": refund_id,
        "amount_cents": refund_amount,
        "square_result": result,
    }

