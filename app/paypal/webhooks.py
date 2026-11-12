"""PayPal webhook signature verification.

Primary path is self-verification as documented by PayPal: the signed message is
    <transmission_id>|<transmission_time>|<webhook_id>|<crc32 of the raw body, decimal>
signed with SHA256withRSA by the certificate at PAYPAL-CERT-URL. If self-verification cannot
complete (certificate fetch fails), we fall back to POST /v1/notifications/verify-webhook-signature.
"""

from __future__ import annotations

import base64
import datetime as dt
import time
import zlib
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from ..config import settings
from .client import PayPalError, client

_cert_cache: dict[str, x509.Certificate] = {}
_cert_failures: dict[str, float] = {}
CERT_CACHE_SIZE = 16
CERT_FAILURE_TTL = 60
CERT_PATH = "/v1/notifications/certs/"

REQUIRED = ("paypal-transmission-id", "paypal-transmission-time", "paypal-transmission-sig",
            "paypal-cert-url", "paypal-auth-algo")


def signed_message(transmission_id: str, transmission_time: str, webhook_id: str, raw_body: bytes) -> bytes:
    crc = zlib.crc32(raw_body) & 0xFFFFFFFF
    return f"{transmission_id}|{transmission_time}|{webhook_id}|{crc}".encode()


def cert_url_allowed(url: str, fake_base: str | None = None) -> bool:
    p = urlparse(url)
    if p.scheme == "https" and p.hostname and (p.hostname == "paypal.com" or p.hostname.endswith(".paypal.com")) \
            and p.path.startswith(CERT_PATH) and not p.query and not p.fragment:
        return True
    if fake_base and url.startswith(fake_base.rstrip("/") + "/certs/"):
        return True
    return False


def cert_is_acceptable(cert: x509.Certificate, allow_fake: bool) -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        return False
    names = [a.value for a in cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)]
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names += san.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        pass
    if any(str(n).endswith("paypal.com") for n in names):
        return True
    return allow_fake and any(str(n) == "fake-paypal.billkind.local" for n in names)


def verify_with_cert(cert: x509.Certificate, message: bytes, sig_b64: str) -> bool:
    try:
        sig = base64.b64decode(sig_b64)
        cert.public_key().verify(sig, message, padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class CertUnavailable(Exception):
    """PayPal could not be reached for the certificate (a network problem, not a bad URL or a bad certificate)."""


def _cert_key(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.hostname}{p.path}"


async def _fetch_cert(url: str, transport: httpx.AsyncBaseTransport | None) -> x509.Certificate:
    """The certificate at an allowed URL. Raises CertUnavailable on network errors, ValueError otherwise."""
    key = _cert_key(url)
    if key in _cert_cache:
        return _cert_cache[key]
    if time.time() - _cert_failures.get(key, 0) < CERT_FAILURE_TTL:
        raise ValueError("certificate fetch failed recently")
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as h:
            r = await h.get(url)
    except httpx.TransportError as exc:
        raise CertUnavailable(type(exc).__name__) from exc
    try:
        r.raise_for_status()
        cert = x509.load_pem_x509_certificate(r.content)
    except Exception as exc:
        if len(_cert_failures) > 256:
            _cert_failures.clear()
        _cert_failures[key] = time.time()
        raise ValueError(f"certificate could not be read ({type(exc).__name__})") from exc
    while len(_cert_cache) >= CERT_CACHE_SIZE:
        _cert_cache.pop(next(iter(_cert_cache)))
    _cert_cache[key] = cert
    return cert


async def verify(headers: dict, raw_body: bytes, event: dict, *, webhook_id: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> tuple[bool, str, str]:
    """Returns (verified, method, note)."""
    h = {k.lower(): v for k, v in headers.items()}
    missing = [k for k in REQUIRED if not h.get(k)]
    if missing:
        return False, "none", "missing headers: " + ", ".join(missing)
    webhook_id = webhook_id or settings.webhook_id
    if not webhook_id:
        return False, "none", "PAYPAL_WEBHOOK_ID is not configured"
    if h["paypal-auth-algo"].upper() != "SHA256WITHRSA":
        return False, "self", "unexpected auth algo " + h["paypal-auth-algo"]
    fake_base = settings.paypal_base_url if settings.is_fake else None
    url = h["paypal-cert-url"]
    if not cert_url_allowed(url, fake_base):
        return False, "self", "certificate URL is not a PayPal domain"
    message = signed_message(h["paypal-transmission-id"], h["paypal-transmission-time"], webhook_id, raw_body)
    try:
        cert = await _fetch_cert(url, transport)
    except ValueError as exc:  # PayPal answered, but with no usable certificate: a forged or broken delivery
        return False, "self", str(exc)
    except CertUnavailable as exc:  # PayPal could not be reached: ask its verify API instead
        try:
            status = await client().verify_webhook_signature(headers=h, event=event, webhook_id=webhook_id)
        except PayPalError as perr:
            return False, "api", f"cert fetch failed ({exc}); verify API failed ({perr})"
        return status == "SUCCESS", "api", f"cert fetch failed ({type(exc).__name__}); verify API said {status}"
    if not cert_is_acceptable(cert, allow_fake=settings.is_fake):
        return False, "self", "certificate is expired or not issued to paypal.com"
    if verify_with_cert(cert, message, h["paypal-transmission-sig"]):
        return True, "self", "signature valid"
    return False, "self", "signature does not match"
