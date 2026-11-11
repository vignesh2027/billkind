"""A real bill from a biller outside the seeded registry can be onboarded and approved by an admin, and loose name
matches never route a real bill to a registry biller."""

from app import db
from app.core import billers
from tests.conftest import login


def _review_bill(biller_name: str) -> dict:
    template = db.one("SELECT * FROM bills WHERE id = 'BK-S009'")
    bid = "BK-RV" + str(db.one("SELECT COUNT(*) AS n FROM bills")["n"]).zfill(3)
    db.insert("bills", dict(template, id=bid, status="REVIEW", biller_id=None, biller_name=biller_name,
                            funded_minor=0, payout_item_id="", payout_batch_id="", payout_txn_id="", thank_you="",
                            paid_at=None, doc_sha="", phash="", account_hash="", created_at=db.now(),
                            updated_at=db.now()))
    return db.one("SELECT * FROM bills WHERE id = ?", (bid,))


def test_admin_adds_links_verifies_and_approves_a_new_biller():
    bill = _review_bill("Great Lakes Gas Cooperative")
    a, csrf = login("admin@billkind.demo")
    r = a.post(f"/admin/bills/{bill['id']}/decide", data={"decision": "approve", "csrf": csrf})
    assert "Verify or add the biller first" in r.text
    r = a.post("/admin/billers", data={"name": "Great Lakes Gas Cooperative", "category": "electricity",
                                       "country": "US", "city": "Detroit", "paypal_email": "pay@greatlakes.example.com",
                                       "bill_id": bill["id"], "csrf": csrf})
    assert r.status_code == 200 and r.url.path == "/admin"
    new = db.one("SELECT * FROM billers WHERE name = 'Great Lakes Gas Cooperative'")
    assert new and new["verified"] == 0 and new["paypal_email"] == "pay@greatlakes.example.com"
    linked = db.one("SELECT * FROM bills WHERE id = ?", (bill["id"],))
    assert linked["biller_id"] == new["id"]
    r = a.post(f"/admin/bills/{bill['id']}/decide", data={"decision": "approve", "csrf": csrf})
    assert "not verified yet" in r.text  # adding a biller is not verifying it
    a.post(f"/admin/billers/{new['id']}/verify", data={"csrf": csrf})
    a.post(f"/admin/bills/{bill['id']}/decide", data={"decision": "approve", "csrf": csrf})
    assert db.one("SELECT status FROM bills WHERE id = ?", (bill["id"],))["status"] == "LISTED"


def test_admin_can_link_a_review_bill_to_an_existing_biller():
    bill = _review_bill("Lakeside P&L")
    a, csrf = login("admin@billkind.demo")
    a.post(f"/admin/bills/{bill['id']}/biller", data={"biller_id": "BLR-LAKESIDE", "csrf": csrf})
    row = db.one("SELECT biller_id, biller_name FROM bills WHERE id = ?", (bill["id"],))
    assert row["biller_id"] == "BLR-LAKESIDE" and row["biller_name"] == "Lakeside Power & Light"


def test_adding_a_biller_needs_admin_and_valid_fields():
    s, csrf = login("sponsor@billkind.demo")
    r = s.post("/admin/billers", data={"name": "X Co", "category": "water", "country": "US",
                                       "paypal_email": "x@example.com", "csrf": csrf}, follow_redirects=False)
    assert r.status_code in (303, 403)
    assert not db.one("SELECT 1 FROM billers WHERE name = 'X Co'")
    a, ca = login("admin@billkind.demo")
    r = a.post("/admin/billers", data={"name": "Bad Email Water", "category": "water", "country": "US",
                                       "paypal_email": "not-an-email", "csrf": ca})
    assert "PayPal email" in r.text and not db.one("SELECT 1 FROM billers WHERE name = 'Bad Email Water'")


def test_short_or_foreign_name_inside_a_real_biller_name_is_not_a_match():
    # One word that happens to appear in a registry name is not that biller (before: a 0.9 "substring" match).
    assert billers.match("Riverside", "GB")[0] is None
    assert billers.match("Water", "GB")[0] is None
    assert billers.match("Grid", "NG")[0] is None
    # A registry name across a border is not a match either.
    assert billers.match("Riverside Water Authority of Ohio", "US")[0] is None
    # Same country, a name of two or more words inside the printed name: still a match.
    b, _ = billers.match("Riverside Water Ltd, Leeds", "GB")
    assert b and b["id"] == "BLR-RIVERSIDE"
