"""
acme_tls.py — Let's Encrypt cert provisioning via DNS-01 + Cloudflare API.

Obtains (or renews) a publicly-trusted TLS cert for keymgr using the ACME
DNS-01 challenge.  No inbound HTTP needed; the domain does not need to be
publicly reachable — only Cloudflare DNS control is required.

Usage:
    cert_path, key_path = await ensure_acme_cert(domain, cf_token, tls_dir)

Cert lifecycle:
  - If a valid cert already exists for the domain it is reused (renewed when
    fewer than RENEW_BEFORE_DAYS remain).
  - Cert and private key are stored in tls_dir as keymgr.crt / keymgr.key.
  - ACME account key is stored as acme_account.key and reused across calls.

Dependencies (added to requirements.txt): acme, josepy
"""

import asyncio
import json
import logging
import os
import time
from typing import Tuple

log = logging.getLogger(__name__)

ACME_DIRECTORY   = "https://acme-v02.api.letsencrypt.org/directory"
RENEW_BEFORE_DAYS = 30
CF_API_BASE       = "https://api.cloudflare.com/client/v4"
DNS_PROPAGATION_WAIT = 30  # seconds to wait after creating TXT record


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ensure_acme_cert(domain: str, cf_token: str, tls_dir: str) -> Tuple[str, str]:
    """
    Return (cert_path, key_path) for *domain*, obtaining/renewing via ACME
    DNS-01 if necessary.

    Raises RuntimeError if cert provisioning fails.
    """
    cert_path = os.path.join(tls_dir, "keymgr.crt")
    key_path  = os.path.join(tls_dir, "keymgr.key")
    os.makedirs(tls_dir, exist_ok=True)

    if _cert_valid(cert_path, domain):
        log.info("ACME: existing cert for %s is valid, reusing", domain)
        return cert_path, key_path

    log.info("ACME: provisioning cert for %s via DNS-01 / Cloudflare", domain)
    await asyncio.to_thread(_provision_cert, domain, cf_token, tls_dir, cert_path, key_path)
    log.info("ACME: cert for %s written to %s", domain, cert_path)
    return cert_path, key_path


# ---------------------------------------------------------------------------
# Cert validity check
# ---------------------------------------------------------------------------

def _cert_valid(cert_path: str, domain: str) -> bool:
    if not os.path.exists(cert_path):
        return False
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        import datetime
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read(), default_backend())
        now = datetime.datetime.now(datetime.timezone.utc)
        days_left = (cert.not_valid_after_utc - now).days
        if days_left < RENEW_BEFORE_DAYS:
            log.info("ACME: cert for %s expires in %d days — renewing", domain, days_left)
            return False
        # Verify domain is in the cert SANs
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names   = san_ext.value.get_values_for_type(x509.DNSName)
        if domain not in names and f"*.{'.'.join(domain.split('.')[1:])}" not in names:
            log.info("ACME: cert doesn't cover %s — reprovisioning", domain)
            return False
        log.info("ACME: cert for %s valid for %d more days", domain, days_left)
        return True
    except Exception as exc:
        log.warning("ACME: cert validity check failed (%s) — reprovisioning", exc)
        return False


# ---------------------------------------------------------------------------
# Provisioning (runs in a thread — blocking ACME + httpx sync calls)
# ---------------------------------------------------------------------------

def _provision_cert(
    domain: str,
    cf_token: str,
    tls_dir: str,
    cert_path: str,
    key_path: str,
) -> None:
    import httpx
    import josepy as jose
    from acme import challenges, client, crypto_util, errors, messages
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend

    # ── Account key ──────────────────────────────────────────────────────
    acct_key_path = os.path.join(tls_dir, "acme_account.key")
    if os.path.exists(acct_key_path):
        with open(acct_key_path, "rb") as fh:
            acct_pem = fh.read()
        acct_rsa = serialization.load_pem_private_key(acct_pem, password=None,
                                                       backend=default_backend())
        log.info("ACME: reusing existing account key")
    else:
        acct_rsa = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        acct_pem = acct_rsa.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        with open(acct_key_path, "wb") as fh:
            fh.write(acct_pem)
        os.chmod(acct_key_path, 0o600)
        log.info("ACME: generated new account key")

    acct_key = jose.JWKRSA(key=acct_rsa)

    # ── ACME client + register ────────────────────────────────────────────
    net = client.ClientNetwork(acct_key, user_agent="ev-portal-acme/1.0")
    directory = client.ClientV2.get_directory(ACME_DIRECTORY, net)
    acme_client = client.ClientV2(directory, net)

    reg = messages.NewRegistration.from_data(
        email=f"acme@{domain}", terms_of_service_agreed=True
    )
    try:
        acme_client.new_account(reg)
        log.info("ACME: registered new account")
    except errors.ConflictError as conflict:
        # Account already exists.  We MUST set the account URI on the client's
        # network layer so every subsequent request is signed with the correct
        # JWS Key ID header.  Without this, LE rejects the next request with
        # "No Key ID in JWS header".
        acme_client.net.account = messages.RegistrationResource(
            uri=conflict.location,
            body=messages.Registration(),
        )
        log.info("ACME: account already registered at %s", conflict.location)

    # ── Domain private key + CSR ──────────────────────────────────────────
    domain_rsa = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    domain_pem_key = domain_rsa.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    csr_pem = crypto_util.make_csr(domain_pem_key, [domain])

    # ── Order ─────────────────────────────────────────────────────────────
    order = acme_client.new_order(csr_pem)

    # ── DNS-01 challenge ──────────────────────────────────────────────────
    dns_challenge = None
    authz = None
    for a in order.authorizations:
        for ch in a.body.challenges:
            if isinstance(ch.chall, challenges.DNS01):
                dns_challenge = ch
                authz         = a
                break
        if dns_challenge:
            break

    if not dns_challenge:
        raise RuntimeError("ACME: no DNS-01 challenge found in order")

    txt_name  = f"_acme-challenge.{domain}"
    txt_value = dns_challenge.validation(acme_client.net.key)

    log.info("ACME: creating DNS TXT %s = %s", txt_name, txt_value)
    zone_id  = _cf_get_zone_id(cf_token, domain)
    record_id = _cf_create_txt(cf_token, zone_id, txt_name, txt_value)

    try:
        log.info("ACME: waiting %ds for DNS propagation…", DNS_PROPAGATION_WAIT)
        time.sleep(DNS_PROPAGATION_WAIT)

        acme_client.answer_challenge(dns_challenge, dns_challenge.response(acme_client.net.key))

        # Poll until the CA has validated the challenge and issued the cert.
        order = acme_client.poll_and_finalize(order)

    finally:
        log.info("ACME: cleaning up DNS TXT record %s", record_id)
        _cf_delete_txt(cf_token, zone_id, record_id)

    # ── Write cert + key ──────────────────────────────────────────────────
    tmp_cert = cert_path + ".tmp"
    tmp_key  = key_path  + ".tmp"
    with open(tmp_cert, "wb") as fh:
        fh.write(order.fullchain_pem if isinstance(order.fullchain_pem, bytes)
                 else order.fullchain_pem.encode())
    with open(tmp_key, "wb") as fh:
        fh.write(domain_pem_key)
    os.chmod(tmp_key, 0o600)
    os.replace(tmp_cert, cert_path)
    os.replace(tmp_key,  key_path)
    log.info("ACME: cert and key written for %s", domain)


# ---------------------------------------------------------------------------
# Cloudflare DNS helpers
# ---------------------------------------------------------------------------

def _cf_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _cf_get_zone_id(token: str, domain: str) -> str:
    """Return the Cloudflare zone ID for the TLD+1 of *domain*."""
    import httpx
    # Walk up the labels to find the zone (handles subdomains)
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        r = httpx.get(
            f"{CF_API_BASE}/zones",
            params={"name": candidate},
            headers=_cf_headers(token),
            timeout=15,
        )
        # Cloudflare returns 400 when the supplied name is not a valid zone
        # (e.g. a subdomain label).  That is not an error — continue walking
        # up the label hierarchy.  Only raise on unexpected server errors.
        if r.status_code >= 500:
            r.raise_for_status()
        if not r.is_success:
            continue
        data = r.json()
        if data.get("result"):
            zone_id = data["result"][0]["id"]
            log.info("ACME/CF: zone %r → id %s", candidate, zone_id)
            return zone_id
    raise RuntimeError(f"ACME/CF: no Cloudflare zone found for {domain!r}")


def _cf_create_txt(token: str, zone_id: str, name: str, value: str) -> str:
    """Create a DNS TXT record; return the record ID."""
    import httpx
    r = httpx.post(
        f"{CF_API_BASE}/zones/{zone_id}/dns_records",
        headers=_cf_headers(token),
        json={"type": "TXT", "name": name, "content": value, "ttl": 60},
        timeout=15,
    )
    r.raise_for_status()
    record_id = r.json()["result"]["id"]
    log.info("ACME/CF: created TXT record %s (id=%s)", name, record_id)
    return record_id


def _cf_delete_txt(token: str, zone_id: str, record_id: str) -> None:
    """Delete a DNS TXT record by ID."""
    import httpx
    r = httpx.delete(
        f"{CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}",
        headers=_cf_headers(token),
        timeout=15,
    )
    if r.status_code not in (200, 404):
        log.warning("ACME/CF: failed to delete TXT record %s: %s", record_id, r.text)
    else:
        log.info("ACME/CF: deleted TXT record %s", record_id)
