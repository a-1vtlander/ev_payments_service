"""
access.py — IP-based access control middleware for the public guest portal.

Algorithm
---------
1. remote_ip  = request.client.host  (direct TCP socket)

2. Determine effective_ip:
   a) remote_ip is a known Cloudflare egress IP  (direct CF edge → HTTPS reverse proxy):
          effective_ip = CF-Connecting-IP header  — if missing → deny 403
   b) remote_ip is private/loopback AND CF-Connecting-IP header is present
      (cloudflared tunnel daemon running on LAN — proxies from CF edge to local app):
          effective_ip = CF-Connecting-IP header
   c) Otherwise (direct LAN or Tailscale connection, no header):
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
        cf_header  = request.headers.get("cf-connecting-ip", "").strip()

        # Is the remote a private/loopback address? (covers cloudflared tunnel daemon)
        try:
            _raddr = ipaddress.ip_address(remote_ip) if remote_ip else None
            _is_private_remote = _raddr is not None and (_raddr.is_private or _raddr.is_loopback)
        except ValueError:
            _is_private_remote = False

        if _addr_in(remote_ip, _CF_NETS):
            # Direct Cloudflare edge proxy — CF-Connecting-IP is mandatory.
            effective_ip = cf_header
            log.info(
                "AccessControlMiddleware: Cloudflare edge detected remote_ip=%s  CF-Connecting-IP=%r",
                remote_ip, effective_ip or "(missing)",
            )
            if not effective_ip:
                log.warning(
                    "403 access denied: request from Cloudflare IP %s has no "
                    "CF-Connecting-IP header — %s %s",
                    remote_ip, request.method, request.url.path,
                )
                return _DENY
        elif _is_private_remote and cf_header:
            # Cloudflare tunnel (cloudflared) running on LAN — the daemon's local
            # IP is the TCP remote, but CF-Connecting-IP carries the real client IP.
            effective_ip = cf_header
            log.info(
                "AccessControlMiddleware: Cloudflare tunnel detected "
                "remote_ip=%s (LAN daemon)  CF-Connecting-IP=%r",
                remote_ip, effective_ip,
            )
        else:
            # Direct LAN / Tailscale connection with no CF header — use socket IP.
            effective_ip = remote_ip

        log.info(
            "AccessControlMiddleware: remote_ip=%s  effective_ip=%s  allow_nets=%s  path=%s",
            remote_ip, effective_ip, [str(n) for n in allow_nets], request.url.path,
        )

        if not _addr_in(effective_ip, allow_nets):
            log.warning(
                "403 access denied: %s not in filter_access_to — %s %s",
                effective_ip, request.method, request.url.path,
            )
            return _DENY

        return await call_next(request)
