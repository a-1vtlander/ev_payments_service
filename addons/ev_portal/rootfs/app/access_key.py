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
import re
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
    "<html><body><h1>Service Unavailable</h1>"
    "<p>This service is temporarily unavailable. Please try again later.</p>"
    "</body></html>",
    status_code=503,
    media_type="text/html",
)


# UUID4 canonical form: xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx (lowercase)
_UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)


def _rejection_reason(value: str):
    """Return a short human-readable string explaining why value is not a plausible
    access key, or None if it passes all structural checks.

    Checks are ordered cheapest-first so we bail as early as possible.
    """
    if not value:
        return "empty value"
    if len(value) != 36:
        return f"wrong length ({len(value)}, expected 36)"
    if not _UUID4_RE.match(value):
        return "not a valid UUID4 format (must be lowercase, version 4)"
    return None


def _is_plausible_key(value: str) -> bool:
    """Return True only if value passes all structural UUID4 checks."""
    return _rejection_reason(value) is None


def _strip_key_param(url_str: str) -> str:
    """Return the URL with both `key` and `access_key` query parameters removed."""
    parsed     = urlparse(url_str)
    params     = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("key", None)
    params.pop("access_key", None)
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
        # Accept both ?key= (direct) and ?access_key= (forwarded from external portal)
        query_key  = (
            request.query_params.get("access_key", "")
            or request.query_params.get("key", "")
        ).strip()

        # Cookie check first (avoids a DB round-trip on every request once set).
        if cookie_key:
            _reason = _rejection_reason(cookie_key)
            if _reason:
                log.info(
                    "AccessKeyMiddleware: cookie key pre-validation failed (%s) for %s",
                    _reason, path,
                )
            elif await db.validate_access_key(cookie_key):
                return await call_next(request)
            else:
                log.info("AccessKeyMiddleware: invalid/expired cookie key for %s", path)

        # Query-param fallback: validate, set cookie, redirect to clean URL.
        if query_key:
            _reason = _rejection_reason(query_key)
            if _reason:
                log.info(
                    "AccessKeyMiddleware: query key pre-validation failed (%s) for %s",
                    _reason, path,
                )
            elif await db.validate_access_key(query_key):
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
            else:
                log.info("AccessKeyMiddleware: invalid/expired query key for %s", path)

        log.info("AccessKeyMiddleware: denied %s %s", request.method, path)
        return _DENY
