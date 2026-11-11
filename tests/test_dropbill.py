"""Drop a bill: file checks, metadata stripping, extraction parsing, validation, confidence, redaction, the scan
endpoint (with the model mocked) and what the giving agent sees."""

import datetime as dt
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app import db
from app.ai import agent, dropbill, intake, llm, redact
from app.core import billers
from scripts.make_samples import fresh_bill, sample_spec
from tests.conftest import login

TODAY = dt.date.today()


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (400, 300), "white")
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.1"          # Software
    exif[0x8825] = {1: "N", 2: (13.0, 4.0, 0.0)}   # GPSInfo
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _pdf(pages: int, producer: str = "Bill Maker 1.0") -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=300, height=400)
    w.add_metadata({"/Producer": producer, "/Author": "Jane Customer"})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# File checks and metadata ---------------------------------------------------------------------------------

def test_only_photos_and_pdfs_are_accepted():
    assert intake.sniff(fresh_bill("water", seed=1)) == "image"
    png = io.BytesIO()
    Image.new("RGB", (10, 10)).save(png, format="PNG")
    assert intake.sniff(png.getvalue()) == "image"
    assert intake.sniff(_pdf(1)) == "pdf"
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, format="GIF")
    for bad in (b"", b"hello, this is text", gif.getvalue(), b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20,
                b"<svg xmlns='http://www.w3.org/2000/svg'/>"):
        with pytest.raises(intake.DocumentError):
            intake.sniff(bad)
    with pytest.raises(intake.DocumentError, match="8 MB"):
        intake.sniff(b"%PDF-" + b"0" * intake.MAX_BYTES)


def test_photo_copy_has_no_exif_or_gps_but_signals_are_kept():
    doc = intake.read_document(_jpeg_with_gps())
    assert doc.meta["software"].startswith("Adobe Photoshop") and doc.meta["had_gps"] is True
    for blob in (doc.stored, doc.image):
        exif = Image.open(io.BytesIO(blob)).getexif()
        assert len(exif) == 0
        assert b"Photoshop" not in blob


def test_pdf_page_cap_and_metadata_free_copy():
    with pytest.raises(intake.DocumentError, match="pages"):
        intake.read_document(_pdf(intake.MAX_PDF_PAGES + 1))
    doc = intake.read_document(_pdf(2))
    assert doc.kind == "pdf" and doc.meta["producer"] == "Bill Maker 1.0" and doc.meta["pages"] == 2
    kept = PdfReader(io.BytesIO(doc.stored))
    assert len(kept.pages) == 2
    info = kept.metadata or {}
    assert "Jane Customer" not in json.dumps({str(k): str(v) for k, v in info.items()})
    assert b"Jane Customer" not in doc.stored


def test_corrupt_image_is_refused_cleanly():
    data = fresh_bill("clinic", seed=2)[:400]
    with pytest.raises(intake.DocumentError):
        intake.read_document(data)


# Parsing --------------------------------------------------------------------------------------------------

def test_model_output_is_cleaned_and_confidence_clamped():
    out = intake._clean({"biller_name": "  Riverside Water ", "amount": "1,234.50", "currency": "gbp",
                         "due_date": "31/11/2026", "category": "casino", "service_period": "Oct 2026",
                         "confidence": {"amount": 1.7, "due_date": "0.4", "currency": "high", "biller_name": -2},
                         "line_items": ["x", {"label": "Water", "amount": "10"}], "edit_signs": None})
    assert out["biller_name"] == "Riverside Water" and out["amount"] == "1234.50" and out["currency"] == "GBP"
    assert out["due_date"] is None and out["category"] == "other" and out["service_period"] == "Oct 2026"
    assert out["confidence"] == {"amount": 1.0, "due_date": 0.4, "biller_name": 0.0}
    assert out["line_items"] == [{"label": "Water", "amount": "10"}] and out["edit_signs"] == []


def test_regex_reading_of_the_samples():
    for kind in ("electricity", "school", "water", "clinic"):
        spec = sample_spec(kind, seed=11)
        doc = intake.read_document(fresh_bill(kind, seed=11))
        if not doc.text:
            pytest.skip("Tesseract is not installed")
        r = intake.regex_extract(doc.text)
        total = f"{sum(a for _, a in spec['items']):.2f}"
        assert r["amount"] == total, (kind, r)
        assert r["currency"] == spec["cur"] and r["due_date"] == spec["due"]
        assert intake._clean(r)["account_reference"].replace(" ", "") == spec["account"].replace(" ", "")


def test_account_label_beats_customer_care_line():
    text = "Lakeside Power & Light\nCustomer care 1-800-555-0142\nAccount number: 2186 9779 22\n"
    assert intake.parse_account(text) == "2186 9779 22"
    assert intake.parse_period("Service period: 01 Oct to 31 Oct 2026\nPayment terms: 30 days") == \
        "01 Oct to 31 Oct 2026"


# Validation and confidence --------------------------------------------------------------------------------

def test_validation_uses_the_bill_rules():
    ok = {"amount": "84.20", "currency": "USD", "due_date": (TODAY + dt.timedelta(days=5)).isoformat(),
          "category": "electricity"}
    assert dropbill.validate(ok, TODAY) == {}
    assert "currency" in dropbill.validate(dict(ok, currency="XYZ"), TODAY)
    assert "decimals" in dropbill.validate(dict(ok, currency="JPY", amount="1200.50"), TODAY)["amount"]
    assert "limit" in dropbill.validate(dict(ok, amount="2400.00"), TODAY)["amount"]
    assert "typical" in dropbill.validate(dict(ok, amount="900.00"), TODAY)["amount"]
    assert "zero" in dropbill.validate(dict(ok, amount="0"), TODAY)["amount"]
    assert "ago" in dropbill.validate(dict(ok, due_date=(TODAY - dt.timedelta(days=90)).isoformat()), TODAY)["due_date"]
    assert "four months" in dropbill.validate(dict(ok, due_date=(TODAY + dt.timedelta(days=200)).isoformat()),
                                              TODAY)["due_date"]


def _fields(**kw):
    due = (TODAY + dt.timedelta(days=6)).isoformat()
    f = {"biller_name": "Lakeside Power & Light", "amount": "84.20", "currency": "USD", "due_date": due,
         "category": "electricity", "account_reference": "4455 6677 88", "service_period": "Oct 2026",
         "service_address": None, "source": "ai", "confidence": {},
         "ocr": {"amount": "84.20", "currency": "USD", "due_date": due, "account_reference": "4455667788"}}
    f.update(kw)
    return f


def test_confidence_from_two_readings_and_the_registry():
    biller, score = billers.match("Lakeside Power & Light", "US")
    conf = dropbill.confidence(_fields(), biller=biller, biller_score=score, today=TODAY)
    assert all(conf[k]["level"] == "high" for k in ("biller_name", "amount", "currency", "due_date", "category",
                                                    "account_reference"))
    assert conf["service_period"]["level"] == "medium" and conf["service_address"]["level"] == "low"

    f = _fields(amount="94.20", account_reference=None, confidence={"due_date": 0.3})
    conf = dropbill.confidence(f, biller=None, biller_score=0.2, today=TODAY)
    assert conf["amount"]["level"] == "low" and "disagree" in conf["amount"]["why"]
    assert conf["account_reference"]["level"] == "low"
    assert conf["due_date"]["level"] == "medium"          # readings agree but the model was unsure
    assert conf["biller_name"]["level"] == "medium" and "registry" in conf["biller_name"]["why"]

    conf = dropbill.confidence(_fields(currency="XYZ", ocr={}), biller=None, biller_score=0, today=TODAY)
    assert conf["currency"]["level"] == "low" and "cannot settle" in conf["currency"]["why"]
    assert conf["amount"]["level"] == "medium"
    assert dropbill.summary(conf)["check"] == ["currency"]


def test_redaction_of_accounts_and_periods():
    assert redact.mask_account("4455 6677 88") == "•••• 7788"
    assert redact.mask_account("123") == "••••"
    assert redact.clean_period("Term 3 (Nov to Jan)") == "Term 3 (Nov to Jan)"
    assert redact.clean_period("01 Oct to 31 Oct 2026") == "01 Oct to 31 Oct 2026"
    assert redact.clean_period("Oct 2026 for Priya Raman", names=["Priya Raman"]) is None
    assert redact.clean_period("Account 99887766 Oct") is None
    assert redact.clean_period("mail me at a@b.co") is None


def test_document_check_only_states_recorded_facts():
    ext = _fields()
    d = dropbill.document_check(ext, True)
    assert d["amount_confirmed"] and d["due_confirmed"]
    assert d["summary"].startswith("Amount and due date confirmed by two readings") and "biller verified" in d["summary"]
    d = dropbill.document_check(dict(ext, edited_fields=["amount"]), False)
    assert not d["amount_confirmed"] and "typed by the poster" in d["summary"]
    assert dropbill.document_check({"source": "seed"}, True)["summary"] == "Fictional demo bill; biller verified"
    assert dropbill.document_check({}, False)["summary"] == "Entered by the poster"


# The endpoint --------------------------------------------------------------------------------------------

def _mock_vision(monkeypatch, kind: str, seed: int, **override):
    spec = sample_spec(kind, seed)
    total = f"{sum(a for _, a in spec['items']):.2f}"
    reading = {"biller_name": spec["biller"], "category": "electricity", "amount": total, "currency": spec["cur"],
               "due_date": spec["due"], "account_reference": spec["account"], "customer_name": spec["fields"][1][1],
               "service_address": spec["fields"][2][1], "service_period": spec["fields"][4][1], "city": "Detroit",
               "country": "US", "line_items": [{"label": l, "amount": f"{a:.2f}"} for l, a in spec["items"]],
               "looks_edited": False, "edit_signs": [], "confidence": {"amount": 0.98}}
    reading.update(override)
    calls = []

    async def fake_vision(prompt, image, mime, **kw):
        calls.append(len(image))
        return dict(reading)

    async def no_text(*a, **kw):
        return None

    monkeypatch.setattr(llm, "vision_json", fake_vision)
    monkeypatch.setattr(llm, "chat_json", no_text)
    return spec, total, calls


def _scan(client, csrf, data, name="bill.jpg", mime="image/jpeg"):
    return client.post("/api/post/scan", files={"file": (name, data, mime)}, data={"device": "test|1x1|UTC"},
                       headers={"X-CSRF": csrf})


def test_scan_prefills_and_confirm_posts_with_private_account(monkeypatch):
    spec, total, calls = _mock_vision(monkeypatch, "electricity", 41)
    client, csrf = login("poster@billkind.demo")
    r = _scan(client, csrf, fresh_bill("electricity", seed=41))
    assert r.status_code == 200, r.text
    out = r.json()
    assert calls and calls[0] < 400_000                      # the model gets a downscaled copy
    v = out["values"]
    assert v["biller_name"] == "Lakeside Power & Light" and v["amount"] == total and v["currency"] == "USD"
    assert v["due_date"] == spec["due"] and v["category"] == "electricity" and v["account_reference"] == spec["account"]
    assert v["service_period"].startswith("01 ") and out["verified_biller"] is True
    assert out["confidence"]["amount"]["level"] in ("high", "medium") and out["confidence"]["biller_name"]["level"] == "high"
    assert out["submit_url"] == f"/post/{out['bill_id']}"

    bill = db.one("SELECT * FROM bills WHERE id = ?", (out["bill_id"],))
    assert bill["status"] == "DRAFT" and bill["account_private"] == spec["account"]
    assert spec["account"] not in bill["extraction"] and spec["account"].replace(" ", "") not in bill["extraction"]
    stored = Path(bill["doc_path"]).read_bytes()
    assert len(Image.open(io.BytesIO(stored)).getexif()) == 0

    # Nothing is listed until the poster confirms. The no-JavaScript check page shows the poster their own number.
    assert db.one("SELECT status FROM bills WHERE id = ?", (out["bill_id"],))["status"] == "DRAFT"
    page = client.get(out["submit_url"]).text
    assert f'value="{spec["account"]}"' in page and 'class="conf high"' in page
    form = dict(v, csrf=csrf, consent="1", words="My hours were cut this month.", alias="Luis")
    r = client.post(out["submit_url"], data=form)
    assert r.status_code == 200
    bill = db.one("SELECT * FROM bills WHERE id = ?", (out["bill_id"],))
    assert bill["status"] == "LISTED", bill["risk_reasons"]
    assert bill["account_masked"] == redact.mask_account(spec["account"])
    ext = json.loads(bill["extraction"])
    assert ext["service_period"] == v["service_period"]

    sponsor, _ = login("sponsor@billkind.demo")
    card = sponsor.get(f"/api/bills/{out['bill_id']}").json()
    blob = json.dumps(card)
    digits = spec["account"].replace(" ", "")
    assert spec["account"] not in blob and digits not in blob and card["account"].endswith(digits[-4:])
    assert card["doc_confirmed"] in (True, False) and card["service_period"] == v["service_period"]


def test_scan_refuses_bad_files_wrong_roles_and_missing_csrf(monkeypatch):
    _mock_vision(monkeypatch, "water", 5)
    client, csrf = login("poster@billkind.demo")
    r = _scan(client, csrf, b"MZ\x90\x00 not a bill", name="bill.exe", mime="application/octet-stream")
    assert r.status_code == 400 and "photo" in r.json()["error"]
    r = _scan(client, csrf, _pdf(intake.MAX_PDF_PAGES + 2), name="bill.pdf", mime="application/pdf")
    assert r.status_code == 400 and "pages" in r.json()["error"]
    r = _scan(client, "wrong-token", fresh_bill("water", seed=5))
    assert r.status_code == 400
    sponsor, scsrf = login("sponsor@billkind.demo")
    r = _scan(sponsor, scsrf, fresh_bill("water", seed=5))
    assert r.status_code == 403


def test_scan_is_rate_limited_per_poster(monkeypatch):
    from app import main
    _mock_vision(monkeypatch, "school", 8)
    monkeypatch.setattr(main, "SCAN_LIMIT", (2, 600))
    client, csrf = login("poster@billkind.demo")
    assert _scan(client, csrf, fresh_bill("school", seed=8)).status_code == 200
    assert _scan(client, csrf, fresh_bill("school", seed=9)).status_code == 200
    r = _scan(client, csrf, fresh_bill("school", seed=10))
    assert r.status_code == 429 and "wait" in r.json()["error"]


def test_scan_without_ai_falls_back_to_the_text_reading(monkeypatch):
    async def none(*a, **kw):
        return None
    monkeypatch.setattr(llm, "vision_json", none)
    monkeypatch.setattr(llm, "chat_json", none)
    client, csrf = login("poster@billkind.demo")
    r = _scan(client, csrf, fresh_bill("clinic", seed=12))
    assert r.status_code == 200
    out = r.json()
    if out["source"] == "none":
        pytest.skip("Tesseract is not installed")
    spec = sample_spec("clinic", 12)
    assert out["source"] == "ocr" and out["values"]["amount"] == f"{sum(a for _, a in spec['items']):.2f}"
    assert out["values"]["account_reference"] == spec["account"]


def test_fresh_samples_are_unique_jpegs(server):
    import httpx
    a = httpx.get(server + "/samples/fresh/water.jpg")
    b = httpx.get(server + "/samples/fresh/water.jpg")
    assert a.status_code == 200 and a.headers["content-type"] == "image/jpeg" and a.content != b.content
    assert httpx.get(server + "/samples/fresh/casino.jpg").status_code == 404


def test_agent_sees_the_document_check(monkeypatch):
    seen = {}

    async def fake_model(system, user, **kw):
        if kw.get("kind") == "giving-agent":
            seen["system"], seen["candidates"] = system, json.loads(user)["candidates"]
        return None

    monkeypatch.setattr(llm, "chat_json", fake_model)
    db.insert("vault_tokens", {"id": "VT-DOC", "sponsor_id": "U-ADMIN", "payment_token_id": "fake-token",
                               "status": "ACTIVE", "created_at": db.now()}) \
        if not db.one("SELECT 1 FROM vault_tokens WHERE id = 'VT-DOC'") else None
    db.insert("agents", {"id": "AGT-DOC", "sponsor_id": "U-ADMIN", "rules_text": "x",
                         "policy": json.dumps(agent.validate({"monthly_budget_usd": 1, "per_bill_max_usd": 1})),
                         "active": 0, "created_at": db.now()})
    import asyncio
    asyncio.run(agent.run(db.one("SELECT * FROM agents WHERE id = 'AGT-DOC'"), limit=3))
    assert "document" in seen.get("system", "")
    cands = seen.get("candidates") or []
    assert cands and all({"biller", "due_date", "document"} <= set(c) for c in cands)
    assert all(c["document"] for c in cands)
