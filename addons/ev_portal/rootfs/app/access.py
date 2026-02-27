"""
access.py — IP-based access control middleware for the public guest portal.

Algorithm
---------
1. remote_ip  = request.client.host  (direct TCP socket – always Cloudflare or
                                       a LAN / Tailscale peer in practice)

2. If remote_ip is a known Cloudflare egress address:
       effective_ip = CF-Connecting-IP header  (the real client IP)
       If header missing → deny 403
   Else:
       effective_ip = remote_ip

3. If effective_ip NOT within filter_access_to → 403 text/plain "Access restricted"
4. Otherwise continue the request.

Cloudflare egress CIDRs are hardcoded (source: https://www.cloudflare.com/ips/).
X-Forwarded-For is intentionally ignored.
"""

import ipaddress
import logging
from typing import List, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

import state

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded Cloudflare egress CIDRs
# ---------------------------------------------------------------------------

_CLOUDFLARE_CIDRS: List[str] = [
    # IPv4  (from https://www.cloudflare.com/ips-v4)
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    # IPv6  (from https://www.cloudflare.com/ips-v6)
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
]

_CF_NETS: List[ipaddress.IPv4Network] = [ipaddress.ip_network(c, strict=False) for c in _CLOUDFLARE_CIDRS]

_DENY = Response("Access restricted", status_code=403, media_type="text/plain")

# ---------------------------------------------------------------------------
# Allow-list cache (built once on first request; config is static at runtime)
# ---------------------------------------------------------------------------

_allow_nets_cache: Optional[List[ipaddress.IPv4Network]] = None


def _get_allow_nets() -> list:
    global _allow_nets_cache
    if _allow_nets_cache is None:
        cidrs = state._access_config.get("allow_cidrs", [])
        parsed: list = []
        for c in cidrs:
            try:
                parsed.append(ipaddress.ip_network(c, strict=False))
            except ValueError as exc:
                log.error("filter_access_to: invalid CIDR %r — skipped (%s)", c, exc)
        _allow_nets_cache = parsed
        if parsed:
            log.info("AccessControlMiddleware: allow-list active — %d CIDR(s): %s",
                     len(parsed), ", ".join(str(n) for n in parsed))
        else:
            log.info("AccessControlMiddleware: filter_access_to not set — all IPs allowed")
    return _allow_nets_cache


def _addr_in(ip_str: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in nets)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class AccessControlMiddleware(BaseHTTPMiddleware):
    """
    Block requests whose effective IP is not in filter_access_to.

    When filter_access_to is empty (not configured) the middleware is a no-op,
    which keeps tests and local-dev environments working without any config.
    """

    async def dispatch(self, request: Request, call_next):
        allow_nets = _get_allow_nets()

        # Fast-path: middleware disabled when no allow_cidrs configured.
        if not allow_nets:
            return await call_next(request)

        remote_ip = request.client.host if request.client else ""

        if _addr_in(remote_ip, _CF_NETS):
            # Request arrived via Cloudflare — trust CF-Connecting-IP for real IP.
            effective_ip = request.headers.get("cf-connecting-ip", "").strip()
            if not effective_ip:
                log.warning(
                    "403 access denied: request from Cloudflare IP %s has no "
                    "CF-Connecting-IP header — %s %s",
                    remote_ip, request.method, request.url.path,
                )
                return _DENY
        else:
            effective_ip = remote_ip

        if not _addr_in(effective_ip, allow_nets):
            log.warning(
                "403 access denied: %s not in filter_access_to — %s %s",
                effective_ip, request.method, request.url.path,
            )
            return _DENY

        return await call_next(request)
