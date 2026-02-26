"""
admin/auth.py – Auth dependency for admin routes.

Supports two auth methods (checked in order):
  1. Session cookie  (ev_admin_session) – set by POST /admin/login
  2. HTTP Basic Auth                    – for API / curl clients

Session tokens are HMAC-SHA256 signed, keyed on the current admin password, so
changing the password immediately invalidates all existing sessions.

Browser clients (Accept: text/html) are redirected to /admin/login on failure.
API clients receive 401 JSON with WWW-Authenticate: Basic.
"""

import hashlib
import hmac as _hmac
import secrets
import time
from typing import Annotated, Optional

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import state

SESSION_COOKIE = "ev_admin_session"
SESSION_TTL = 8 * 3600  # 8 hours

# auto_error=False so we can handle unauthenticated requests ourselves.
_security = HTTPBasic(auto_error=False)


# ---------------------------------------------------------------------------
# Token helpers (used by login/logout routes too)
# ---------------------------------------------------------------------------

def _cookie_key() -> bytes:
    """HMAC key derived from the current admin password (changes → invalidates all sessions)."""
    pw = state._admin_config.get("password", "")
    return hashlib.sha256(f"ev_admin_session:{pw}".encode()).digest()


def make_session_token(username: str) -> str:
    """Return a signed session token: {username}:{expires}:{hmac_hex}."""
    expires = int(time.time()) + SESSION_TTL
    payload = f"{username}:{expires}"
    sig = _hmac.new(_cookie_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def verify_session_token(token: str) -> Optional[str]:
    """Return username if the token is valid and not expired, else None."""
    try:
        # rsplit from the right so a ':' inside the username doesn't break parsing
        parts = token.rsplit(":", 2)
        if len(parts) != 3:
            return None
        username, expires_str, sig = parts
        expires = int(expires_str)
        if time.time() > expires:
            return None
        payload = f"{username}:{expires}"
        expected = _hmac.new(_cookie_key(), payload.encode(), hashlib.sha256).hexdigest()
        if secrets.compare_digest(sig, expected):
            return username
    except Exception:
        pass
    return None


def validate_basic_credentials(username: str, password: str) -> bool:
    """Constant-time check of username/password against admin config."""
    expected_user = state._admin_config.get("username", "")
    expected_pass = state._admin_config.get("password", "")
    ok_user = secrets.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    ok_pass = secrets.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return ok_user and ok_pass


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

def require_admin(
    request: Request,
    credentials: Annotated[Optional[HTTPBasicCredentials], Depends(_security)] = None,
    session_cookie: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> str:
    """FastAPI dependency – returns the authenticated username or raises.

    Check order: session cookie → Basic Auth → redirect/401.
    """
    # 1. Session cookie
    if session_cookie:
        user = verify_session_token(session_cookie)
        if user:
            return user

    # 2. HTTP Basic Auth (for curl/API clients)
    if credentials and validate_basic_credentials(credentials.username, credentials.password):
        return credentials.username

    # 3. Unauthorized – redirect browsers to login, return 401 for API clients
    accepts_html = "text/html" in request.headers.get("accept", "")
    if accepts_html:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"location": "/admin/login"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin credentials",
        headers={"WWW-Authenticate": "Basic"},
    )
