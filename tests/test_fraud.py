"""Fraud and authenticity rules."""

import datetime as dt
import json

from app import db
from app.ai import fraud
from app.core import billers

TODAY = dt.date.today()


def _bill(**kw):
    b = {"biller_name": "Sunrise Public School", "category": "school_fees", "country": "IN", "currency": "INR",
         "amount_minor": 345000, "due_date": (TODAY + dt.timedelta(days=6)).isoformat()}
    b.update(kw)
    return b


def _assess(bill=None, extraction=None, meta=None, edited=(), sha="sha-unique", phash="", account_hash="",
            poster="U-POSTER"):
    bill = bill or _bill()
    biller, score = billers.match(bill["biller_name"], bill.get("country"))
    return fraud.assess(poster_id=poster, bill=bill, extraction=extraction or {"source": "ai"}, doc_meta=meta or {},
                        doc_sha=sha, phash=phash, account_hash=account_hash, biller=biller, biller_score=score,
                        edited_fields=list(edited))


def test_clean_bill_lists():
    r = _assess()
    assert r.decision == "list", r.reasons
    assert r.as_dict()["score"] < fraud.REVIEW_AT


def test_unknown_biller_goes_to_review():
    r = _assess(_bill(biller_name="Totally Real Academy of Riches"))
    assert r.decision == "review"
    assert any(x["code"] == "biller_unverified" for x in r.reasons)


def test_unverified_registry_biller_goes_to_review():
    r = _assess(_bill(biller_name="New Leaf Montessori"))
    assert r.decision == "review"


def test_editing_software_and_disagreeing_readings():
    r = _assess(extraction={"source": "ai", "amount": "9800.00", "ocr": {"amount": "980.00"}},
                meta={"software": "Adobe Photoshop 25.1"})
    codes = {x["code"] for x in r.reasons}
    assert {"edit_software", "amount_disagree"} <= codes
    assert r.decision == "review"


def test_line_items_must_add_up():
    r = _assess(extraction={"source": "ai", "amount": "9800.00",
                            "line_items": [{"label": "a", "amount": "1200.00"}, {"label": "b", "amount": "800.00"}]})
    assert any(x["code"] == "items_mismatch" for x in r.reasons)


def test_hand_edited_amount_adds_risk():
    r = _assess(edited=["amount"])
    assert any(x["code"] == "amount_edited" for x in r.reasons)


def test_amount_far_above_typical():
    r = _assess(_bill(category="phone", amount_minor=90000, currency="USD", country="IN"))
    assert any(x["code"] == "amount_high" for x in r.reasons)


def test_exact_duplicate_file_is_rejected():
    db.run("UPDATE bills SET doc_sha = 'dup-sha' WHERE id = 'BK-S000'")
    r = _assess(sha="dup-sha")
    assert r.decision == "reject"


def test_already_paid_same_account_and_amount_is_rejected():
    db.run("UPDATE bills SET account_hash = 'acct-h', status = 'PAID', amount_minor = 345000 WHERE id = 'BK-S001'")
    try:
        r = _assess(account_hash="acct-h")
        assert r.decision == "reject"
        assert any(x["code"] == "already_paid" for x in r.reasons)
    finally:
        db.run("UPDATE bills SET status = 'LISTED' WHERE id = 'BK-S001'")


def test_velocity_limits():
    now = db.now()
    for i in range(4):
        db.insert("bills", {"id": f"BK-V{i}", "poster_id": "U-VEL", "biller_name": "x", "category": "water",
                            "country": "KE", "currency": "KES", "amount_minor": 100000, "settle_currency": "USD",
                            "settle_minor": 775, "due_date": TODAY.isoformat(), "status": "LISTED",
                            "created_at": now, "updated_at": now})
    r = _assess(poster="U-VEL")
    codes = {x["code"] for x in r.reasons}
    assert "velocity_open" in codes and "velocity_30d" in codes
    assert r.decision == "review"


def test_overdue_long_ago_and_far_future():
    r = _assess(_bill(due_date=(TODAY - dt.timedelta(days=90)).isoformat()))
    assert any(x["code"] == "long_overdue" for x in r.reasons)
    r = _assess(_bill(due_date=(TODAY + dt.timedelta(days=200)).isoformat()))
    assert any(x["code"] == "far_future" for x in r.reasons)
