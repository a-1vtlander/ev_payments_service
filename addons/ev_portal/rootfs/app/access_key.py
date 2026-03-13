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


async def _validate_key(key: str, source: str, method: str, path: str) -> bool:
    """Stateless pre-check then DB lookup. Logs the reason on failure.

    ``source`` is a short label for log messages (e.g. 'cookie', 'query key').
    Returns True only when the key is structurally valid *and* live in the DB.
    """
    reason = _rejection_reason(key)
    if reason:
        log.info(
            "AccessKeyMiddleware: denied %s %s — %s stateless check failed (%s)",
            method, path, source, reason,
        )
        return False
    if await db.validate_access_key(key):
        return True
    log.info(
        "AccessKeyMiddleware: denied %s %s — %s key invalid or expired (DB)",
        method, path, source,
    )
    return False


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
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if path in _SKIP_EXACT or any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        # Accept both ?key= (direct) and ?access_key= (forwarded from external portal).
        # If a URL param is present it is the credential; the cookie is only checked when
        # no URL param was supplied at all.
        if "access_key" in request.query_params or "key" in request.query_params:
            key           = (
                request.query_params.get("access_key", "")
                or request.query_params.get("key", "")
            ).strip()
            update_cookie = True
        else:
            key           = request.cookies.get(_COOKIE_NAME, "").strip()
            update_cookie = False

        if not key:
            log.info("AccessKeyMiddleware: denied %s %s — no credentials presented",
                     request.method, path)
            return _DENY

        if not await _validate_key(key, "query key" if update_cookie else "cookie", request.method, path):
            return _DENY

        # Key is valid. If it came from a query param, set cookie and redirect to clean URL.
        if update_cookie:
            clean_url = _strip_key_param(str(request.url))
            resp = RedirectResponse(clean_url, status_code=302)
            resp.set_cookie(
                _COOKIE_NAME,
                key,
                max_age=_COOKIE_MAX_AGE,
                httponly=True,
                samesite="lax",
            )
            log.info(
                "AccessKeyMiddleware: valid query key — setting cookie and redirecting to %s",
                clean_url,
            )
            return resp

        return await call_next(request)
