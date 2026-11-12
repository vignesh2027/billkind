"""Unit tests: money, state machines, redaction, webhook signature verification."""

import base64
import datetime as dt
import zlib

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.ai import redact
from app.core import states
from app.core.money import convert, fmt, settlement_for, to_minor, to_value
from app.paypal import webhooks


def test_money_minor_units():
    assert to_minor("84.20", "USD") == 8420
    assert to_minor("1,234.5", "EUR") == 123450
    assert to_minor("500", "JPY") == 500
    assert to_value(8420, "USD") == "84.20"
    assert to_value(5, "USD") == "0.05"
    assert to_value(500, "JPY") == "500"
    assert fmt(345000, "INR").startswith("₹")
    with pytest.raises(ValueError):
        to_minor("abc", "USD")


def test_settlement_and_conversion():
    assert settlement_for("GBP") == "GBP"
    assert settlement_for("INR") == "USD"
    assert settlement_for("KES") == "USD"
    assert convert(845000, "INR", "USD") == 10000
    assert convert(100, "USD", "USD") == 100


def test_bill_state_machine():
    states.check_bill("LISTED", "FUNDING")
    states.check_bill("FUNDED", "PAYOUT_SENT")
    states.check_bill("PAYOUT_SENT", "PAID")
    for a, b in [("PAID", "WITHDRAWN"), ("LISTED", "PAID"), ("REJECTED", "LISTED"), ("PAYOUT_SENT", "WITHDRAWN"),
                 ("DRAFT", "LISTED")]:
        with pytest.raises(states.IllegalTransition):
            states.check_bill(a, b)


def test_contribution_state_machine():
    states.check_contribution("CREATED", "CAPTURED")
    states.check_contribution("CANCELLED", "CAPTURED")  # late approval after the reservation lapsed
    states.check_contribution("CAPTURED", "PARTIALLY_REFUNDED")
    with pytest.raises(states.IllegalTransition):
        states.check_contribution("REFUNDED", "CAPTURED")
    with pytest.raises(states.IllegalTransition):
        states.check_contribution("DENIED", "CAPTURED")


def test_redaction():
    assert redact.mask_account("4455 6677 88") == "•••• 7788"
    assert redact.mask_account("12") == "••••"
    text = "I am Priya Raman, call +91 98765 43210 or priya@example.com, 12 Gandhi Road, acct 445566778899"
    out = redact.scrub(text, names=["Priya Raman"])
    for leaked in ("Priya", "Raman", "98765", "example.com", "445566778899", "Gandhi Road"):
        assert leaked not in out
    assert redact.account_hash("4455-6677", "B1") == redact.account_hash("44556677", "B1")


# Webhook signature verification ------------------------------------------------------------

def _cert(cn: str, days: int = 30):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=days)).sign(key, hashes.SHA256()))
    return key, cert


def _sign(key, msg: bytes) -> str:
    return base64.b64encode(key.sign(msg, padding.PKCS1v15(), hashes.SHA256())).decode()


def test_signed_message_format():
    body = b'{"id":"WH-1"}'
    msg = webhooks.signed_message("tid", "2026-11-06T00:00:00Z", "WH-ID", body)
    assert msg == f"tid|2026-11-06T00:00:00Z|WH-ID|{zlib.crc32(body) & 0xFFFFFFFF}".encode()


def test_signature_valid_and_tampered():
    key, cert = _cert("messageverificationcerts.paypal.com")
    body = b'{"event_type":"PAYMENT.CAPTURE.COMPLETED"}'
    msg = webhooks.signed_message("t1", "2026-11-06T00:00:00Z", "WH-1", body)
    sig = _sign(key, msg)
    assert webhooks.verify_with_cert(cert, msg, sig)
    assert webhooks.cert_is_acceptable(cert, allow_fake=False)
    tampered = webhooks.signed_message("t1", "2026-11-06T00:00:00Z", "WH-1", body.replace(b"COMPLETED", b"REFUNDED"))
    assert not webhooks.verify_with_cert(cert, tampered, sig)
    wrong_hook = webhooks.signed_message("t1", "2026-11-06T00:00:00Z", "WH-2", body)
    assert not webhooks.verify_with_cert(cert, wrong_hook, sig)
    assert not webhooks.verify_with_cert(cert, msg, "not-base64!!")


def test_cert_checks():
    _, other = _cert("evil.example.com")
    assert not webhooks.cert_is_acceptable(other, allow_fake=False)
    assert webhooks.cert_url_allowed("https://api.paypal.com/v1/notifications/certs/CERT-360caa42")
    assert webhooks.cert_url_allowed("https://api.sandbox.paypal.com/v1/notifications/certs/X")
    assert not webhooks.cert_url_allowed("http://api.paypal.com/certs/X")
    assert not webhooks.cert_url_allowed("https://paypal.com.evil.net/certs/X")
    assert not webhooks.cert_url_allowed("https://evilpaypal.com/certs/X")
    assert webhooks.cert_url_allowed("http://localhost:1/fakepaypal/certs/fake.pem", "http://localhost:1/fakepaypal")


def test_scrub_removes_the_bills_own_account_and_short_ids():
    from app.ai.redact import scrub
    assert scrub("Student no KV-88213 at the school") == "Student no [number] at the school"
    assert "STU4512" not in scrub("Patient STU4512 needs care")
    assert "2023CS114" not in scrub("roll 2023CS114")
    assert scrub("acct ab 12 cd 34 ef due", accounts=["AB12CD34EF"]) == "acct [number] due"
    assert scrub("account 12345 due", accounts=["12345"]) == "account [number] due"
    # Years, amounts, terms and periods still show.
    assert scrub("Term 3 of 2026, 84.20 USD, Oct 2026") == "Term 3 of 2026, 84.20 USD, Oct 2026"


def test_scrub_removes_direct_payment_handles():
    from app.ai.redact import findings, scrub
    for text in ("GPay amma123@okaxis", "cashapp $ammaR", "paypal me ammaraman22", "pay me at kavya@paytm"):
        out = scrub(text, names=["Kavya Raman"])
        assert "@" not in out and "$" not in out and "amma" not in out.lower(), (text, out)
        assert "payment handle" in findings(text, out)


def test_city_and_alias_keep_plain_names_only():
    from app.ai.redact import safe_place
    for ok in ("Chennai", "St. John's", "Quezon City", "São Paulo", "Aarav"):
        assert safe_place(ok) == ok
    for bad in ("Chennai GPay amma123@okaxis", "Leeds 07700900123", "cashapp $ammaR", "Paytm"):
        assert safe_place(bad) == ""


def test_story_never_quotes_the_account_reference():
    import asyncio
    from app.ai import story
    text, source = asyncio.run(story.write(category="school_fees", city="Chennai", country="IN", alias="",
                                           words="Fees for account ab-12cd-34ef are due", names=[],
                                           accounts=["AB12CD34EF"]))
    assert source == "template" and "34" not in text and "[number]" in text


def test_webhook_cert_url_must_be_paypals_cert_path():
    from app.paypal import webhooks
    assert not webhooks.cert_url_allowed("https://www.paypal.com/x")
    assert not webhooks.cert_url_allowed("https://api.paypal.com/v1/notifications/certs/X?a=1")


def test_webhook_falls_back_to_the_verify_api_only_on_network_errors(monkeypatch):
    import asyncio
    import httpx
    from app.paypal import client as client_mod, webhooks
    calls = []

    async def fake_verify(**kw):
        calls.append(kw)
        return "SUCCESS"
    monkeypatch.setattr(client_mod.client(), "verify_webhook_signature", fake_verify)
    headers = {"PAYPAL-TRANSMISSION-ID": "t", "PAYPAL-TRANSMISSION-TIME": "now", "PAYPAL-TRANSMISSION-SIG": "AAAA",
               "PAYPAL-AUTH-ALGO": "SHA256withRSA",
               "PAYPAL-CERT-URL": "https://api.paypal.com/v1/notifications/certs/CERT-missing"}
    not_found = httpx.MockTransport(lambda req: httpx.Response(404))
    ok, method, _ = asyncio.run(webhooks.verify(headers, b"{}", {}, webhook_id="WH", transport=not_found))
    assert not ok and method == "self" and not calls  # a 404 is not a reason to spend a PayPal API call

    def down(req):
        raise httpx.ConnectError("down")
    headers["PAYPAL-CERT-URL"] = "https://api.paypal.com/v1/notifications/certs/CERT-other"
    ok, method, _ = asyncio.run(webhooks.verify(headers, b"{}", {}, webhook_id="WH", transport=httpx.MockTransport(down)))
    assert ok and method == "api" and len(calls) == 1


def test_single_model_call_retries_once_after_a_rate_limit(monkeypatch):
    import asyncio
    import httpx
    from app.ai import llm
    from app.config import settings
    monkeypatch.setattr(settings, "ai_disabled", False)
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    answers = [httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}}),
               httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})]
    sent = []

    async def fake_post(self, url, **kw):
        sent.append(kw["json"]["model"])
        r = answers.pop(0)
        r.request = httpx.Request("POST", url)
        return r
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    out = asyncio.run(llm.chat_raw([{"role": "user", "content": "hello"}], kind="test"))
    assert out == {"role": "assistant", "content": "hi"} and len(sent) == 2


def test_backup_copies_the_database_and_keeps_the_newest(tmp_path):
    import datetime as dt
    import sqlite3
    import tarfile
    from app import db
    from scripts import backup_db
    for day in (1, 2, 3):
        out, tar = backup_db.backup(tmp_path, keep=2, now=dt.datetime(2026, 11, day, 12, 0))
    copy = sqlite3.connect(out)
    assert copy.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == db.one("SELECT COUNT(*) AS n FROM bills")["n"]
    copy.close()
    assert sorted(p.name for p in tmp_path.glob("billkind-*.db")) == ["billkind-20261102-1200.db",
                                                                     "billkind-20261103-1200.db"]
    if tar:
        with tarfile.open(tar) as t:
            assert t.getnames()[0] == "uploads"
