"""
tls.py – TLS certificate management for the admin HTTPS server.

If admin_tls_mode == "self_signed":
  - Ensure EV_TLS_DIR (default /data/tls) exists.
  - If admin.crt / admin.key are absent, generate a new self-signed cert.
  - Return (cert_path, key_path).

If admin_tls_mode == "provided":
  - Validate that admin_tls_cert_path and admin_tls_key_path exist.
  - Return (cert_path, key_path).
"""

import datetime
import ipaddress
import logging
import os
from typing import Tuple

log = logging.getLogger(__name__)

TLS_DIR: str = os.environ.get("EV_TLS_DIR", "/data/tls")


def ensure_guest_cert() -> Tuple[str, str]:
    """
    Return (cert_path, key_path) for the guest HTTPS server.
    Always self-signed; stored alongside the admin cert in TLS_DIR.
    """
    tls_dir   = TLS_DIR
    cert_path = os.path.join(tls_dir, "guest.crt")
    key_path  = os.path.join(tls_dir, "guest.key")

    if os.path.exists(cert_path) and os.path.exists(key_path):
        log.info("TLS: reusing existing guest self-signed cert at %s", cert_path)
        return cert_path, key_path

    os.makedirs(tls_dir, exist_ok=True)
    log.info("TLS: generating new guest self-signed cert in %s", tls_dir)
    _generate_self_signed(cert_path, key_path)
    return cert_path, key_path


def ensure_cert(admin_cfg: dict) -> Tuple[str, str]:
    """
    Return the (cert_path, key_path) to use for the admin HTTPS server.

    Generates a self-signed cert if needed (tls_mode == "self_signed").
    Raises RuntimeError if paths are missing for "provided" mode.
    """
    mode = admin_cfg.get("tls_mode", "self_signed")

    if mode == "provided":
        cert = (admin_cfg.get("tls_cert_path") or "").strip()
        key  = (admin_cfg.get("tls_key_path")  or "").strip()
        if not cert or not os.path.exists(cert):
            raise RuntimeError(f"admin_tls_cert_path not found: {cert!r}")
        if not key or not os.path.exists(key):
            raise RuntimeError(f"admin_tls_key_path not found: {key!r}")
        log.info("TLS: using provided cert %s", cert)
        return cert, key

    # ── self_signed ────────────────────────────────────────────────────────
    tls_dir   = TLS_DIR
    cert_path = os.path.join(tls_dir, "admin.crt")
    key_path  = os.path.join(tls_dir, "admin.key")

    if os.path.exists(cert_path) and os.path.exists(key_path):
        log.info("TLS: reusing existing self-signed cert at %s", cert_path)
        return cert_path, key_path

    os.makedirs(tls_dir, exist_ok=True)
    log.info("TLS: generating new self-signed cert in %s", tls_dir)
    _generate_self_signed(cert_path, key_path)
    return cert_path, key_path


# ---------------------------------------------------------------------------
# Certificate generation
# ---------------------------------------------------------------------------

def _generate_self_signed(cert_path: str, key_path: str) -> None:
    try:
        _generate_with_cryptography(cert_path, key_path)
    except ImportError:
        log.warning("TLS: cryptography package not found – falling back to openssl CLI")
        _generate_with_openssl(cert_path, key_path)


def _generate_with_cryptography(cert_path: str, key_path: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME,       "ev-portal-admin"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EV Portal"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    log.info("TLS: self-signed cert written to %s (cryptography)", cert_path)


def _generate_with_openssl(cert_path: str, key_path: str) -> None:
    import subprocess

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path,
            "-out",    cert_path,
            "-days",   "3650",
            "-nodes",
            "-subj",   "/CN=ev-portal-admin/O=EV Portal",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key_path, 0o600)
    log.info("TLS: self-signed cert written to %s (openssl CLI)", cert_path)
