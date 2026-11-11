"""Sign-in, session and request-level protections."""

import re

import httpx

from app import db
from app.config import settings
from tests.conftest import BASE, login


def _client(ip: str | None = None) -> httpx.Client:
    """A browser. With ip, it arrives through the proxy from that address (the tests reach the app on loopback)."""
    return httpx.Client(base_url=BASE, follow_redirects=False, timeout=30,
                        headers={"X-Forwarded-For": ip} if ip else None)


def _signed_in_as(c: httpx.Client) -> str | None:
    r = c.get("/inbox")
    if r.status_code != 200:
        return None
    import re
    m = re.search(r'data-user-id="([^"]+)"', r.text)
    return m.group(1) if m else "signed-in"


def test_one_click_login_only_opens_the_listed_demo_accounts():
    c = _client()
    r = c.post("/login", data={"email": "poster01@billkind.demo", "demo": "1"})
    assert r.status_code == 200 and "not right" in r.text  # the login page again, with an error
    assert _signed_in_as(c) is None
    # A per-browser demo poster account cannot be opened by typing its email either.
    first = _client()
    assert first.post("/login", data={"email": "poster@billkind.demo", "demo": "1"}).status_code == 303
    own = first.cookies.get("bk_demo_poster")
    email = db.one("SELECT email FROM users WHERE id = ?", (own,))["email"]
    other = _client()
    r = other.post("/login", data={"email": email, "demo": "1"})
    assert r.status_code == 200 and _signed_in_as(other) is None


def test_public_demo_password_never_opens_admin_or_a_testers_account():
    from scripts.seed import DEMO_PASSWORD
    first = _client()
    first.post("/login", data={"email": "poster@billkind.demo", "demo": "1"})
    email = db.one("SELECT email FROM users WHERE id = ?", (first.cookies.get("bk_demo_poster"),))["email"]
    for target in (email, "poster02@billkind.demo"):
        c = _client()
        r = c.post("/login", data={"email": target, "password": DEMO_PASSWORD})
        assert r.status_code == 200 and _signed_in_as(c) is None, target


def test_admin_and_biller_one_click_follow_the_flag(monkeypatch):
    monkeypatch.setattr(settings, "demo_admin_login", False)
    page = _client().get("/login").text
    assert "admin@billkind.demo" not in page and "biller@billkind.demo" not in page
    assert "sponsor@billkind.demo" in page
    for email in ("admin@billkind.demo", "biller@billkind.demo"):
        c = _client()
        c.post("/login", data={"email": email, "demo": "1"})
        assert c.get("/admin").status_code in (303, 307) or "login" in c.get("/admin").headers.get("location", "")
        assert _signed_in_as(c) is None
    c = _client()
    assert c.post("/login", data={"email": "sponsor@billkind.demo", "demo": "1"}).headers["location"] == "/deck"
    monkeypatch.setattr(settings, "demo_admin_login", True)
    c = _client()
    assert c.post("/login", data={"email": "admin@billkind.demo", "demo": "1"}).headers["location"] == "/admin"
    assert c.get("/admin").status_code == 200


def test_a_long_support_chat_cannot_break_the_admin_console():
    s, csrf = login("sponsor@billkind.demo")
    history = [{"role": "user" if i % 2 else "assistant", "content": "x" * 700} for i in range(12)]
    r = s.post("/api/help/escalate", json={"history": history, "subject": "long chat"}, headers={"X-CSRF": csrf})
    assert r.status_code == 200
    r = s.post("/api/help/escalate", json={"history": "y" * 9000, "subject": "odd"}, headers={"X-CSRF": csrf})
    assert r.status_code == 200
    a, _ = login("admin@billkind.demo")
    assert a.get("/admin").status_code == 200
    tid = db.one("SELECT id FROM tickets WHERE subject = 'long chat'")["id"]
    db.run("UPDATE tickets SET transcript = '[{\"role\": \"user\", \"content\": \"cut' WHERE id = ?", (tid,))
    assert a.get("/admin").status_code == 200  # a transcript damaged some other way is skipped, not fatal


def test_paypal_mode_is_checked_and_the_simulator_never_runs_with_real_credentials(monkeypatch):
    import pytest
    from app.config import Settings
    for bad in ("live", "production", "Sandboxx"):
        monkeypatch.setenv("PAYPAL_MODE", bad)
        with pytest.raises(ValueError, match="PAYPAL_MODE"):
            Settings()
    monkeypatch.setenv("PAYPAL_MODE", "Sandbox")  # case and spaces do not matter
    assert Settings().paypal_mode == "sandbox" and not Settings().is_fake
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "an-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "a-secret")
    monkeypatch.setenv("PAYPAL_MODE", "fake")
    with pytest.raises(ValueError, match="cannot run with"):
        Settings()
    monkeypatch.setenv("PAYPAL_MODE", "")  # unset with credentials: the sandbox, never the simulator
    assert Settings().paypal_mode == "sandbox"
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "")
    assert Settings().is_fake  # no credentials: the local simulator, as before


def test_oversized_bodies_are_refused_before_anything_reads_them():
    c = _client()
    big = b"x" * (9 * 1024 * 1024)
    r = c.post("/api/post/scan", files={"file": ("a.pdf", big, "application/pdf")})  # no session
    assert r.status_code == 413
    r = c.post("/webhooks/paypal", content=b"{" + b" " * (100 * 1024) + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413

    def chunks():  # no Content-Length: counted as it arrives
        for _ in range(40):
            yield b"y" * (64 * 1024)
    r = c.post("/webhooks/paypal", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert c.get("/healthz").status_code == 200


def test_ordinary_upload_still_passes_the_body_limit():
    from tests.conftest import login
    s, csrf = login("poster@billkind.demo")
    data = s.get("/samples/fresh/water.jpg").content
    r = s.post("/api/post/scan", files={"file": ("water.jpg", data, "image/jpeg")}, headers={"X-CSRF": csrf})
    assert r.status_code == 200, r.text


def test_pages_cannot_be_framed_by_other_sites():
    r = _client().get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_thank_you_note_is_scrubbed_of_the_bills_details_and_sent_once():
    import json as _json
    from tests.conftest import login
    template = db.one("SELECT * FROM bills WHERE id = 'BK-S009'")
    ext = dict(_json.loads(template["extraction"] or "{}"), customer_name="Aarav Menon",
               service_address="12 Lake View Road")
    db.insert("bills", dict(template, id="BK-THX01", poster_id="U-P01", status="PAID", thank_you="",
                            account_private="KV-88213", extraction=_json.dumps(ext), paid_at=db.now(),
                            created_at=db.now(), updated_at=db.now()))
    db.insert("contributions", {"id": "CON-THX01", "bill_id": "BK-THX01", "sponsor_id": "U-SPONSOR",
                                "amount_minor": 100, "currency": "USD", "status": "CAPTURED", "source": "swipe",
                                "group_id": "GRP-THX01", "expires_at": 0, "created_at": db.now(),
                                "captured_at": db.now()})
    p, csrf = login("poster01@billkind.demo")
    p.post("/me/BK-THX01/thanks", data={"csrf": csrf, "text": "Thank you! Aarav can sit his exams, roll KV 88213. "
                                                               "GPay amma@okaxis if you want to help more."})
    note = db.one("SELECT thank_you FROM bills WHERE id = 'BK-THX01'")["thank_you"]
    assert note and "Aarav" not in note and "88213" not in note and "okaxis" not in note, note
    sent = db.one("SELECT COUNT(*) AS n FROM messages WHERE kind = 'thanks' AND bill_id = 'BK-THX01'")["n"]
    assert sent == 1
    p.post("/me/BK-THX01/thanks", data={"csrf": csrf, "text": "Second note"})
    assert db.one("SELECT thank_you FROM bills WHERE id = 'BK-THX01'")["thank_you"] == note
    assert db.one("SELECT COUNT(*) AS n FROM messages WHERE kind = 'thanks' AND bill_id = 'BK-THX01'")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM events WHERE kind = 'thanks' AND bill_id = 'BK-THX01'")["n"] == 1


def test_many_byte_ranges_on_a_static_file_are_cheap():
    import time
    ranges = ",".join(f"{i}-{i}" for i in range(0, 3200, 2))
    t0 = time.time()
    r = _client().get("/static/og-image.png", headers={"Range": f"bytes={ranges}"})
    assert r.status_code == 200  # served whole instead of parsing thousands of ranges
    assert time.time() - t0 < 1.0
    r = _client().get("/static/og-image.png", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100  # ordinary range requests still work


def test_scan_limit_covers_both_upload_routes_and_counts_readings_in_flight(monkeypatch):
    import asyncio as _asyncio
    from app import main
    from app.core import posting
    from tests.conftest import login
    s, csrf = login("poster@billkind.demo")
    data = s.get("/samples/fresh/water.jpg").content
    gate = {"n": 0}
    real = posting.upload

    async def slow_upload(*a, **kw):
        gate["n"] += 1
        await _asyncio.sleep(0.3)
        return await real(*a, **kw)
    monkeypatch.setattr(posting, "upload", slow_upload)
    monkeypatch.setattr(main, "SCAN_LIMIT", (3, 600))

    async def burst():
        async with httpx.AsyncClient(base_url=BASE, cookies=s.cookies, timeout=60) as ac:
            return await _asyncio.gather(*[ac.post("/api/post/scan", files={"file": ("w.jpg", data, "image/jpeg")},
                                                   headers={"X-CSRF": csrf}) for _ in range(6)])
    codes = sorted(r.status_code for r in _asyncio.run(burst()))
    assert codes.count(200) <= 3 and 429 in codes, codes
    r = s.post("/post", data={"csrf": csrf}, files={"file": ("w.jpg", data, "image/jpeg")})
    assert "a lot of bills" in r.text  # the no-JavaScript route enforces the same limit


def test_model_calls_past_the_limit_use_the_fallback(monkeypatch):
    from app.ai import llm
    from app.web import limits
    from tests.conftest import login
    from app.config import settings
    monkeypatch.setattr(settings, "ai_disabled", False)
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    seen = []

    async def fake_post(self, url, **kw):
        seen.append(url)
        raise httpx.ConnectError("offline in tests")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(limits.AI_CALLS, "limit", 2)
    s, csrf = login("sponsor@billkind.demo")
    for _ in range(4):
        r = s.post("/api/help", json={"question": "How do refunds work?"}, headers={"X-CSRF": csrf})
        assert r.status_code == 200 and r.json()["answer"]
    assert len(seen) == 2  # the third and fourth questions never reached the model
    assert not llm._blocked.get()  # the block was for those requests only


def test_demo_poster_accounts_from_one_address_are_capped(monkeypatch):
    from app.web import limits
    monkeypatch.setattr(limits.DEMO_POSTERS, "limit", 2)
    ids = set()
    for _ in range(4):
        c = _client("203.0.113.7")
        c.post("/login", data={"email": "poster@billkind.demo", "demo": "1"})
        ids.add(c.cookies.get("bk_demo_poster"))
    assert len(ids) == 2  # the third and fourth logins reuse the latest account from this address
    c = _client("203.0.113.8")
    c.post("/login", data={"email": "poster@billkind.demo", "demo": "1"})
    assert c.cookies.get("bk_demo_poster") not in ids  # another address is not affected


def test_repeated_wrong_passwords_are_cut_off(monkeypatch):
    from app.web import limits
    from tests.conftest import TEST_PASSWORD, login
    login("poster02@billkind.demo")  # gives the account a known private password
    monkeypatch.setattr(limits.LOGIN_FAILURES, "limit", 3)
    c = _client("203.0.113.9")
    for _ in range(3):
        c.post("/login", data={"email": "poster02@billkind.demo", "password": "wrong"})
    r = c.post("/login", data={"email": "poster02@billkind.demo", "password": TEST_PASSWORD})
    assert r.status_code == 200 and _signed_in_as(c) is None  # even the right password waits now


def test_login_from_another_site_is_refused():
    for origin in ("https://evil.example.com", "null"):
        c = _client()
        c.post("/login", data={"email": "sponsor@billkind.demo", "demo": "1"}, headers={"Origin": origin})
        assert _signed_in_as(c) is None, origin
    c = _client()
    c.post("/login", data={"email": "sponsor@billkind.demo", "demo": "1"}, headers={"Origin": BASE})
    assert _signed_in_as(c) is not None
    c = _client()
    c.post("/login", data={"email": "sponsor@billkind.demo", "demo": "1"}, headers={"Referer": "https://evil.example/x"})
    assert _signed_in_as(c) is None


def test_signing_in_starts_a_fresh_session():
    c = _client()
    before = re.search(r'name="csrf" content="([0-9a-f]+)"', c.get("/how").text).group(1)
    c.post("/login", data={"email": "sponsor@billkind.demo", "demo": "1"})
    after = re.search(r'name="csrf" content="([0-9a-f]+)"', c.get("/inbox").text).group(1)
    assert before != after


def test_signing_out_retires_copies_of_the_session_cookie():
    from tests.conftest import login
    p, csrf = login("poster03@billkind.demo")
    copy = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30, cookies=dict(p.cookies))
    assert copy.get("/me").status_code == 200
    p.post("/logout")
    assert copy.get("/me").status_code == 303  # the copied cookie no longer works
    # A shared one-click account stays signed in for everyone else when one visitor signs out.
    a, _ = login("sponsor@billkind.demo")
    b, _ = login("sponsor@billkind.demo")
    a.post("/logout")
    assert b.get("/sponsor", follow_redirects=False).status_code == 200


def test_vault_setup_is_never_started_from_another_site():
    from tests.conftest import login
    s, _ = login("sponsor@billkind.demo")
    n = db.one("SELECT COUNT(*) AS n FROM vault_tokens")["n"]
    r = s.get("/sponsor/vault/connect?next=/sponsor", headers={"Sec-Fetch-Site": "cross-site"},
              follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sponsor"
    assert db.one("SELECT COUNT(*) AS n FROM vault_tokens")["n"] == n


def test_shared_demo_accounts_never_show_a_visitors_paypal_login():
    from app import main
    assert main.mask_email("amma.raman@example.com") == "a***@example.com"
    from tests.conftest import login
    s, _ = login("bigsponsor@billkind.demo")
    db.insert("vault_tokens", {"id": db.new_id("VLT"), "sponsor_id": "U-BIG", "setup_token_id": "x", "status": "ACTIVE",
                               "payment_token_id": "PT-SECRET-1234", "payer_email": "visitor.one@example.com",
                               "created_at": db.now() + 1})
    html = s.get("/sponsor/agent").text
    db.run("UPDATE vault_tokens SET status = 'REPLACED' WHERE payment_token_id = 'PT-SECRET-1234'")
    assert "visitor.one@example.com" not in html and "v***@example.com" in html and "PT-SECRET" not in html


def test_sample_bill_rendering_is_bounded(monkeypatch):
    import asyncio as _asyncio
    import time as _time
    from app import main
    from app.web import limits
    from scripts import make_samples
    running, peak = [0], [0]

    def slow(kind):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        _time.sleep(0.2)
        running[0] -= 1
        return b"jpeg"
    monkeypatch.setattr(make_samples, "fresh_bill", slow)

    async def burst():
        async with httpx.AsyncClient(base_url=BASE, timeout=60) as ac:
            return await _asyncio.gather(*[ac.get("/samples/fresh/water.jpg") for _ in range(6)])
    assert all(r.status_code == 200 for r in _asyncio.run(burst()))
    assert peak[0] <= main.SAMPLE_RENDERS
    monkeypatch.setattr(limits.SAMPLES, "limit", 1)
    limits.SAMPLES.reset()
    c = _client("203.0.113.10")
    assert c.get("/samples/fresh/water.jpg").status_code == 200
    assert c.get("/samples/fresh/water.jpg").status_code == 429
    assert _client().get("/samples/fresh/water.jpg").status_code == 200  # no known address: no per-address limit


def test_paypal_call_log_keeps_only_recent_calls():
    from app.paypal.client import PayPalClient
    pc = PayPalClient(base_url="http://127.0.0.1:1", client_id="x", secret="y")
    for i in range(500):
        pc.calls.append(("GET", f"/x/{i}", 200))
    assert len(pc.calls) == 200 and pc.calls[-1][1] == "/x/499"


def test_live_stream_connections_are_capped_per_address(monkeypatch):
    from app.core import events
    monkeypatch.setattr(events.Broker, "MAX_PER_CLIENT", 2)
    held = [events.broker.subscribe(None, "203.0.113.11") for _ in range(2)]
    try:
        r = _client("203.0.113.11").get("/events/stream")
        assert r.status_code == 503 and r.headers["retry-after"] == "30"
    finally:
        for q in held:
            events.broker.unsubscribe(q)


def test_hot_queries_have_indexes():
    names = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_bills_status", "ix_bills_poster_created", "ix_contrib_sponsor", "ix_payout_bill"} <= names


def test_placeholder_or_short_session_secret_is_never_used(monkeypatch):
    from app.config import Settings
    for weak in ("change-me", "short", ""):
        monkeypatch.setenv("SESSION_SECRET", weak)
        key = Settings().session_secret
        assert key != weak and len(key) >= 32
    monkeypatch.setenv("SESSION_SECRET", "k" * 40)
    assert Settings().session_secret == "k" * 40
