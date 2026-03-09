"""
tests/test_acme_tls.py — Tests for ACME DNS-01 cert provisioning (acme_tls.py).

Two tiers:

1. Unit tests (always run, no network):
   - _cert_valid() logic: missing file, expired cert, valid cert, wrong SAN
   - Cloudflare helpers: correct API endpoints, headers, error handling
   - ensure_acme_cert short-circuit when cert is already valid

2. Live integration test (marked `acme`, skipped by default):
   - Full DNS-01 flow against real Let's Encrypt + Cloudflare
   - Requires keymgr_domain and dns_cloudflare_api_token in dev_options.json
   - Run with:  pytest -m acme -v tests/test_acme_tls.py

The live test writes a real cert to a temp directory and verifies it is valid
for the configured domain.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import acme_tls

_OPTIONS_FILE = Path(__file__).parent / "dev_options.json"

# ---------------------------------------------------------------------------
# Helpers — build minimal fake x509 certs for unit tests
# ---------------------------------------------------------------------------

def _make_fake_cert(
    domain: str,
    days_valid: int = 60,
    days_offset: int = 0,
) -> bytes:
    """Return PEM bytes for a self-signed cert covering *domain*."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_offset)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


# ---------------------------------------------------------------------------
# _cert_valid — unit tests
# ---------------------------------------------------------------------------

def test_cert_valid_returns_false_for_missing_file(tmp_path):
    assert acme_tls._cert_valid(str(tmp_path / "nope.crt"), "example.com") is False


def test_cert_valid_returns_true_for_valid_cert(tmp_path):
    pem = _make_fake_cert("example.com", days_valid=60)
    cert_path = str(tmp_path / "test.crt")
    Path(cert_path).write_bytes(pem)
    assert acme_tls._cert_valid(cert_path, "example.com") is True


def test_cert_valid_returns_false_when_expiring_soon(tmp_path):
    # days_valid=20 < RENEW_BEFORE_DAYS=30
    pem = _make_fake_cert("example.com", days_valid=20)
    cert_path = str(tmp_path / "test.crt")
    Path(cert_path).write_bytes(pem)
    assert acme_tls._cert_valid(cert_path, "example.com") is False


def test_cert_valid_returns_false_for_wrong_domain(tmp_path):
    pem = _make_fake_cert("other.com", days_valid=60)
    cert_path = str(tmp_path / "test.crt")
    Path(cert_path).write_bytes(pem)
    assert acme_tls._cert_valid(cert_path, "example.com") is False


def test_cert_valid_returns_false_for_corrupted_file(tmp_path):
    cert_path = str(tmp_path / "test.crt")
    Path(cert_path).write_text("not a cert")
    assert acme_tls._cert_valid(cert_path, "example.com") is False


# ---------------------------------------------------------------------------
# ensure_acme_cert — short-circuits when cert is already valid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_acme_cert_reuses_valid_cert(tmp_path):
    domain = "keymgr.example.com"
    cert_path = str(tmp_path / "keymgr.crt")
    key_path  = str(tmp_path / "keymgr.key")
    Path(cert_path).write_bytes(_make_fake_cert(domain, days_valid=60))
    Path(key_path).write_bytes(b"fake-key")

    with patch.object(acme_tls, "_provision_cert") as mock_provision:
        result = await acme_tls.ensure_acme_cert(domain, "fake-token", str(tmp_path))

    mock_provision.assert_not_called()
    assert result == (cert_path, key_path)


# ---------------------------------------------------------------------------
# Cloudflare helpers — mocked httpx
# ---------------------------------------------------------------------------

def test_cf_get_zone_id_calls_correct_endpoint():
    import httpx
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"result": [{"id": "zone123"}]}

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        zone_id = acme_tls._cf_get_zone_id("mytoken", "keymgr.example.com")

    assert zone_id == "zone123"
    call_args = mock_get.call_args
    assert "/zones" in call_args[0][0]
    assert call_args[1]["headers"]["Authorization"] == "Bearer mytoken"


def test_cf_get_zone_id_walks_up_labels():
    """Should try sub.example.com first, then example.com."""
    call_count = 0
    responses = [
        {"result": []},          # sub.example.com — not found
        {"result": [{"id": "z1"}]},  # example.com — found
    ]

    def fake_get(url, **kwargs):
        nonlocal call_count
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = responses[call_count]
        call_count += 1
        return resp

    with patch("httpx.get", side_effect=fake_get):
        zone_id = acme_tls._cf_get_zone_id("tok", "sub.example.com")

    assert zone_id == "z1"
    assert call_count == 2


def test_cf_create_txt_posts_correct_payload():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"result": {"id": "rec456"}}

    with patch("httpx.post", return_value=mock_resp) as mock_post:
        rec_id = acme_tls._cf_create_txt("mytoken", "zone123", "_acme-challenge.example.com", "abc123")

    assert rec_id == "rec456"
    call_kwargs = mock_post.call_args[1]
    assert call_kwargs["json"]["type"] == "TXT"
    assert call_kwargs["json"]["name"] == "_acme-challenge.example.com"
    assert call_kwargs["json"]["content"] == "abc123"
    assert call_kwargs["headers"]["Authorization"] == "Bearer mytoken"


def test_cf_delete_txt_calls_correct_endpoint():
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.delete", return_value=mock_resp) as mock_del:
        acme_tls._cf_delete_txt("mytoken", "zone123", "rec456")

    url = mock_del.call_args[0][0]
    assert "zone123" in url
    assert "rec456" in url
    assert mock_del.call_args[1]["headers"]["Authorization"] == "Bearer mytoken"


def test_cf_delete_txt_tolerates_404():
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.delete", return_value=mock_resp):
        # Should not raise
        acme_tls._cf_delete_txt("mytoken", "zone123", "gone")


# ---------------------------------------------------------------------------
# Live integration test — real Let's Encrypt + Cloudflare DNS-01
# ---------------------------------------------------------------------------

@pytest.mark.acme
@pytest.mark.asyncio
async def test_acme_cert_provisioning_live(tmp_path):
    """
    Full DNS-01 ACME flow against real Let's Encrypt + Cloudflare.

    Requires in tests/dev_options.json:
      "keymgr_domain": "keymgr.baselander-ev.extravio.co"
      "dns_cloudflare_api_token": "<real token>"

    Run with:
        pytest -m acme -v tests/test_acme_tls.py

    WARNING: consumes a Let's Encrypt rate-limited issuance and takes ~60s.
    """
    opts = json.loads(_OPTIONS_FILE.read_text())
    domain   = opts.get("keymgr_domain", "").strip()
    cf_token = opts.get("dns_cloudflare_api_token", "").strip()

    if not domain or not cf_token:
        pytest.skip("keymgr_domain or dns_cloudflare_api_token not set in dev_options.json")

    cert_path, key_path = await acme_tls.ensure_acme_cert(domain, cf_token, str(tmp_path))

    assert os.path.exists(cert_path), "cert file was not created"
    assert os.path.exists(key_path),  "key file was not created"
    assert os.path.getsize(cert_path) > 0
    assert os.path.getsize(key_path)  > 0

    # Cert must now pass the validity check
    assert acme_tls._cert_valid(cert_path, domain), "newly issued cert failed validity check"

    # Calling again must short-circuit (no re-issuance)
    with patch.object(acme_tls, "_provision_cert") as mock_provision:
        await acme_tls.ensure_acme_cert(domain, cf_token, str(tmp_path))
    mock_provision.assert_not_called()
