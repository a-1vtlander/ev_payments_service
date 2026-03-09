"""
access_key.py — Token-based browser access control for the guest portal (port 8090).

A one-month access key must be present as either:
  • Query param  ?key=<uuid>  — validated, cookie set, URL stripped via redirect
  • Cookie        ev_access_key=<uuid>

Keys are issued by the key-management server on port 8092 and stored in the DB.

When no keys have ever been issued the middleware is a no-op (fail-open),
preventing lockout on first deployment.
"""

import logging
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse

import db

log = logging.getLogger(__name__)

_COOKIE_NAME    = "ev_access_key"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days

# Paths that bypass the key check.
_SKIP_PREFIXES = ("/health", "/static/", "/.well-known/")
_SKIP_EXACT    = {"/favicon.ico"}

_DENY = Response(
    "Access key required or expired — obtain a key from the portal administrator",
    status_code=403,
    media_type="text/plain",
)


def _strip_key_param(url_str: str) -> str:
    """Return the URL with the `key` query parameter removed."""
    parsed     = urlparse(url_str)
    params     = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("key", None)
    new_query  = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


class AccessKeyMiddleware(BaseHTTPMiddleware):
    """
    Require a valid access key on all guest-portal routes.

    Skipped entirely when no keys have ever been issued (safe on first deploy).
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if path in _SKIP_EXACT or any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Fail-open: allow all traffic until the first key is issued.
        if not await db.any_access_keys_exist():
            log.debug("AccessKeyMiddleware: no keys issued yet — allowing %s", path)
            return await call_next(request)

        cookie_key = request.cookies.get(_COOKIE_NAME, "").strip()
        query_key  = request.query_params.get("key", "").strip()

        # Cookie check first (avoids a DB round-trip on every request once set).
        if cookie_key and await db.validate_access_key(cookie_key):
            return await call_next(request)

        if cookie_key:
            log.info("AccessKeyMiddleware: invalid/expired cookie key for %s", path)

        # Query-param fallback: validate, set cookie, redirect to clean URL.
        if query_key:
            if await db.validate_access_key(query_key):
                clean_url = _strip_key_param(str(request.url))
                resp = RedirectResponse(clean_url, status_code=302)
                resp.set_cookie(
                    _COOKIE_NAME,
                    query_key,
                    max_age=_COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="lax",
                )
                log.info(
                    "AccessKeyMiddleware: valid query key — setting cookie and redirecting to %s",
                    clean_url,
                )
                return resp
            log.info("AccessKeyMiddleware: invalid/expired query key for %s", path)

        log.info("AccessKeyMiddleware: denied %s %s", request.method, path)
        return _DENY
