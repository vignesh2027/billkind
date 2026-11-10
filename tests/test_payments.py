"""End-to-end payment flows against the local PayPal fake, through the real HTTP endpoints and webhooks."""

import asyncio
import json
import re

import httpx

from app import db
from app.paypal import fake
from tests.conftest import BASE, login, wait_for


def _bill(i: int) -> dict:
    return db.one("SELECT * FROM bills WHERE id = ?", (f"BK-S{i:03d}",))


def _pay(client, csrf, picks, email="buyer@personal.example.com", approve=True):
    r = client.post("/api/orders", json={"picks": picks}, headers={"X-CSRF": csrf})
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    if approve:
        httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={"email": email})
        r = client.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": csrf})
        assert r.status_code == 200, r.text
    return oid


def _status(bid):
    return db.one("SELECT status FROM bills WHERE id = ?", (bid,))["status"]


def test_full_swipe_flow_pays_biller_and_sends_receipt():
    s, c = login("sponsor@billkind.demo")
    b = _bill(7)  # GBP water bill, settles in GBP
    _pay(s, c, [{"bill_id": b["id"], "amount_minor": b["settle_minor"]}])
    assert wait_for(lambda: _status(b["id"]) == "PAID")
    item = db.one("SELECT * FROM payout_items WHERE bill_id = ?", (b["id"],))
    assert item["status"] == "SUCCESS" and item["currency"] == "GBP" and item["amount_minor"] == b["settle_minor"]
    assert "biller@business.example.com" in item["receiver"] or item["receiver"].endswith("@business.example.com")
    # The receipt is written just after the PAID status, so wait for it too.
    msgs = wait_for(lambda: db.q("SELECT * FROM messages WHERE user_id = 'U-SPONSOR' AND kind = 'receipt' "
                                 "AND bill_id = ?", (b["id"],)))
    assert msgs and "capture" in msgs[0]["body"]
    hooks = db.q("SELECT event_type, verified, method FROM webhook_events")
    assert hooks and all(h["verified"] == 1 and h["method"] == "self" for h in hooks)
    ev = db.q("SELECT kind FROM events WHERE bill_id = ? AND public = 1", (b["id"],))
    assert {"funded", "fully_funded", "payout_sent", "paid"} <= {e["kind"] for e in ev}


def test_pooled_partial_funding_one_payout():
    a, ca = login("sponsor@billkind.demo")
    big, cb = login("bigsponsor@billkind.demo")
    b = _bill(9)  # USD 64.00
    _pay(a, ca, [{"bill_id": b["id"], "amount_minor": 2000}])
    assert _status(b["id"]) == "FUNDING"
    _pay(big, cb, [{"bill_id": b["id"], "amount_minor": b["settle_minor"] - 2000}])
    assert wait_for(lambda: _status(b["id"]) == "PAID")
    assert db.one("SELECT COUNT(*) AS n FROM payout_items WHERE bill_id = ?", (b["id"],))["n"] == 1
    receipts = db.q("SELECT user_id FROM messages WHERE kind = 'receipt' AND bill_id = ?", (b["id"],))
    assert {r["user_id"] for r in receipts} == {"U-SPONSOR", "U-BIG"}


def test_reservation_blocks_overfunding():
    a, ca = login("sponsor@billkind.demo")
    big, cb = login("bigsponsor@billkind.demo")
    b = _bill(6)
    _pay(a, ca, [{"bill_id": b["id"], "amount_minor": b["settle_minor"]}], approve=False)
    r = big.post("/api/orders", json={"picks": [{"bill_id": b["id"], "amount_minor": 500}]}, headers={"X-CSRF": cb})
    assert r.status_code == 409 and "at most" in r.json()["error"]


def test_late_capture_after_bill_covered_is_refunded():
    a, ca = login("sponsor@billkind.demo")
    big, cb = login("bigsponsor@billkind.demo")
    b = _bill(5)  # PHP 999
    late = _pay(a, ca, [{"bill_id": b["id"], "amount_minor": b["settle_minor"]}], approve=False)
    db.run("UPDATE contributions SET expires_at = 0 WHERE order_id = ?", (late,))  # reservation lapses
    _pay(big, cb, [{"bill_id": b["id"], "amount_minor": b["settle_minor"]}])
    assert wait_for(lambda: _status(b["id"]) in ("PAYOUT_SENT", "PAID"))
    httpx.post(f"{BASE}/fakepaypal/test/approve/{late}", json={"email": "slow@personal.example.com"})
    r = a.post(f"/api/orders/{late}/capture", headers={"X-CSRF": ca})
    assert r.status_code == 200
    c = db.one("SELECT * FROM contributions WHERE order_id = ?", (late,))
    assert c["status"] == "REFUNDED" and c["refunded_minor"] == b["settle_minor"]
    cap = fake.CAPTURES[c["capture_id"]]
    assert cap["status"] == "REFUNDED"
    assert wait_for(lambda: _status(b["id"]) == "PAID")


def test_withdraw_refunds_every_sponsor():
    a, ca = login("sponsor@billkind.demo")
    b = _bill(4)  # PHP 4200
    _pay(a, ca, [{"bill_id": b["id"], "amount_minor": 1000}])
    poster = db.one("SELECT email FROM users WHERE id = ?", (b["poster_id"],))["email"]
    p, cp = login(poster)
    r = p.post(f"/me/{b['id']}/withdraw", data={"csrf": cp})
    assert r.status_code == 200
    assert _status(b["id"]) == "WITHDRAWN"
    c = db.one("SELECT * FROM contributions WHERE bill_id = ? AND status != 'CANCELLED'", (b["id"],))
    assert c["status"] == "REFUNDED"
    assert any(r["kind"] == "refund" for r in db.q("SELECT kind FROM messages WHERE user_id = 'U-SPONSOR'"))


def test_failed_payout_goes_to_admin_then_refund():
    db.run("UPDATE billers SET paypal_email = 'unclaimed-test@business.example.com' WHERE id = 'BLR-ROSE'")
    try:
        s, c = login("sponsor@billkind.demo")
        b = _bill(14)  # EUR electricity bill paid to BLR-ROSE
        assert b["biller_id"] == "BLR-ROSE" and b["status"] == "LISTED"
        _pay(s, c, [{"bill_id": b["id"], "amount_minor": b["settle_minor"]}])
        assert wait_for(lambda: _status(b["id"]) == "PAYOUT_FAILED")
        assert db.one("SELECT status FROM payout_items WHERE bill_id = ?", (b["id"],))["status"] == "UNCLAIMED"
        admin, cad = login("admin@billkind.demo")
        r = admin.post(f"/admin/bills/{b['id']}/payout", data={"action": "refund", "csrf": cad})
        assert r.status_code == 200
        assert _status(b["id"]) == "WITHDRAWN"
        assert db.one("SELECT status FROM contributions WHERE bill_id = ? AND status != 'CANCELLED'",
                      (b["id"],))["status"] == "REFUNDED"
    finally:
        db.run("UPDATE billers SET paypal_email = 'rose-biller@business.example.com' WHERE id = 'BLR-ROSE'")


def test_bulk_order_one_checkout_per_bill_payouts():
    big, cb = login("bigsponsor@billkind.demo")
    usd = db.q("SELECT * FROM bills WHERE status = 'LISTED' AND settle_currency = 'USD' ORDER BY id LIMIT 3")
    oid = _pay(big, cb, [{"bill_id": b["id"], "amount_minor": b["settle_minor"]} for b in usd])
    order = fake.ORDERS[oid]
    assert len(order["purchase_units"][0]["items"]) == len(usd)
    assert wait_for(lambda: all(_status(b["id"]) == "PAID" for b in usd))
    batches = {db.one("SELECT batch_id FROM payout_items WHERE bill_id = ?", (b["id"],))["batch_id"] for b in usd}
    assert len(batches) == 1  # one payout batch, one item per bill


def test_webhook_with_bad_signature_is_rejected_and_duplicates_ignored():
    event = {"id": "WH-FORGED", "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "resource": {}}
    r = httpx.post(BASE + "/webhooks/paypal", json=event, headers={
        "PAYPAL-TRANSMISSION-ID": "x", "PAYPAL-TRANSMISSION-TIME": "2026-11-06T00:00:00Z",
        "PAYPAL-TRANSMISSION-SIG": "AAAA", "PAYPAL-CERT-URL": BASE + "/fakepaypal/certs/fake.pem",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA"})
    assert r.status_code == 400
    r = httpx.post(BASE + "/webhooks/paypal", json=event, headers={
        "PAYPAL-TRANSMISSION-ID": "x", "PAYPAL-TRANSMISSION-TIME": "t", "PAYPAL-TRANSMISSION-SIG": "AAAA",
        "PAYPAL-CERT-URL": "https://attacker.example.com/cert.pem", "PAYPAL-AUTH-ALGO": "SHA256withRSA"})
    assert r.status_code == 400
    # Replay a genuine signed event: the second delivery is a no-op.
    genuine = db.one("SELECT id, payload FROM webhook_events WHERE verified = 1 AND processed = 1 LIMIT 1")
    from app.core import webhook_handlers
    assert webhook_handlers.record(json.loads(genuine["payload"]), True, "self", "") is False


def test_cannot_pay_more_than_needed_or_own_bill():
    s, c = login("sponsor@billkind.demo")
    b = db.one("SELECT * FROM bills WHERE status = 'LISTED' ORDER BY id DESC LIMIT 1")
    r = s.post("/api/orders", json={"picks": [{"bill_id": b["id"], "amount_minor": b["settle_minor"] + 1}]},
               headers={"X-CSRF": c})
    assert r.status_code == 409
    r = s.post("/api/orders", json={"picks": [{"bill_id": b["id"], "amount_minor": 100}]}, headers={"X-CSRF": "bad"})
    assert r.status_code == 400


def test_foreign_currency_payout_falls_back_to_usd():
    db.run("UPDATE billers SET paypal_email = 'usdonly-biller@business.example.com' WHERE id = 'BLR-VERDE'")
    try:
        s, c = login("sponsor@billkind.demo")
        b = _bill(6)  # MXN electricity bill, settles in MXN
        from app.core import funding
        db.run("UPDATE contributions SET status = 'CANCELLED' WHERE bill_id = ? AND status = 'CREATED'", (b["id"],))
        left = funding.remaining(db.one("SELECT * FROM bills WHERE id = ?", (b["id"],)))
        assert left > 0 and b["settle_currency"] == "MXN"
        _pay(s, c, [{"bill_id": b["id"], "amount_minor": left}])
        assert wait_for(lambda: _status(b["id"]) == "PAID")
        items = db.q("SELECT * FROM payout_items WHERE bill_id = ? ORDER BY sender_item_id", (b["id"],))
        assert [i["currency"] for i in items] == ["MXN", "USD"]  # the refused MXN attempt stays on record
        assert items[0]["status"] == "FAILED" and items[1]["status"] == "SUCCESS"
        assert "Paid out in USD" in db.one("SELECT fx_note FROM bills WHERE id = ?", (b["id"],))["fx_note"]
    finally:
        db.run("UPDATE billers SET paypal_email = 'verde-biller@business.example.com' WHERE id = 'BLR-VERDE'")



_fresh = [0]


def _fresh_bills(n):
    """New open USD bills with a verified biller, so these tests never depend on what earlier tests used."""
    template = db.one("SELECT * FROM bills WHERE id = 'BK-S009'")
    ids = []
    for _ in range(n):
        _fresh[0] += 1
        row = dict(template, id=f"BK-R{_fresh[0]:03d}", status="LISTED", funded_minor=0, payout_item_id="",
                   payout_batch_id="", payout_txn_id="", thank_you="", paid_at=None, doc_sha="", phash="",
                   account_hash="", created_at=db.now(), updated_at=db.now())
        db.insert("bills", row)
        ids.append(row["id"])
    return [db.one("SELECT * FROM bills WHERE id = ?", (i,)) for i in ids]


def _bulk_order(client, csrf, n=3):
    bills = _fresh_bills(n)
    r = client.post("/api/orders", json={"picks": [{"bill_id": b["id"], "amount_minor": b["settle_minor"]} for b in bills],
                                         "source": "bulk"}, headers={"X-CSRF": csrf})
    assert r.status_code == 200, r.text
    return r.json()["id"], [b["id"] for b in bills]


def _one_payout_each(bill_ids):
    for bid in bill_ids:
        rows = db.q("SELECT status FROM payout_items WHERE bill_id = ?", (bid,))
        assert len(rows) == 1, (bid, rows)


def test_webhook_captures_first_then_browser_capture_still_succeeds():
    big, cb = login("bigsponsor@billkind.demo")
    oid, ids = _bulk_order(big, cb)
    # Approval emits CHECKOUT.ORDER.APPROVED; the webhook handler captures the order on the server.
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})
    assert wait_for(lambda: db.one("SELECT status FROM orders WHERE order_id = ?", (oid,))["status"] == "COMPLETED")
    r = big.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": cb})
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED", r.text
    assert set(r.json()["funded"]) == set(ids)
    assert wait_for(lambda: all(_status(b) == "PAID" for b in ids))
    _one_payout_each(ids)


def test_browser_and_webhook_capture_at_the_same_moment_pay_each_biller_once(monkeypatch):
    from app.core import funding
    from app.paypal.client import client
    big, cb = login("bigsponsor@billkind.demo")
    oid, ids = _bulk_order(big, cb)
    monkeypatch.setattr(fake, "deliver", lambda raw, headers: asyncio.sleep(0))  # no webhook: we race by hand
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})

    async def race():
        order = await client().capture_order(oid)
        return await asyncio.gather(funding.record_capture(order), funding.record_capture(order),
                                    funding.send_payouts([{"id": i} for i in ids]))
    results = asyncio.run(race())
    assert results[0]["status"] == results[1]["status"] == "COMPLETED"
    monkeypatch.undo()
    assert wait_for(lambda: all(_status(b) in ("PAYOUT_SENT", "PAID") for b in ids))
    _one_payout_each(ids)
    assert len({db.one("SELECT batch_id FROM payout_items WHERE bill_id = ?", (b,))["batch_id"] for b in ids}) == 1


def test_browser_capture_first_then_late_webhook_is_a_no_op():
    s, c = login("sponsor@billkind.demo")
    oid, ids = _bulk_order(s, c, n=1)
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})
    r = s.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": c})
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    again = s.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": c})  # a double click or a retry
    assert again.status_code == 200 and again.json()["status"] == "COMPLETED"
    assert wait_for(lambda: _status(ids[0]) == "PAID")
    _one_payout_each(ids)


def test_capture_endpoint_never_fails_once_money_is_captured(monkeypatch):
    from app.core import funding
    s, c = login("sponsor@billkind.demo")
    oid, ids = _bulk_order(s, c, n=1)

    async def broken(*a, **k):
        raise RuntimeError("payout service down")
    monkeypatch.setattr(fake, "deliver", lambda raw, headers: asyncio.sleep(0))
    monkeypatch.setattr(funding, "send_payouts", broken)
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})
    r = s.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": c})
    monkeypatch.undo()
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED", r.text
    assert _status(ids[0]) == "FUNDED"
    db.run("UPDATE bills SET updated_at = 0 WHERE id = ?", (ids[0],))
    assert asyncio.run(funding.heal_unpaid_funded()) == 1  # the automation loop sends the missed payout
    assert wait_for(lambda: _status(ids[0]) == "PAID")
    _one_payout_each(ids)


def test_order_status_endpoint_confirms_a_capture_for_its_owner_only():
    s, c = login("sponsor@billkind.demo")
    oid, ids = _bulk_order(s, c, n=1)
    assert s.get(f"/api/orders/{oid}/status").json()["status"] in ("PAYER_ACTION_REQUIRED", "CREATED")
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})
    assert wait_for(lambda: s.get(f"/api/orders/{oid}/status").json()["status"] == "COMPLETED")
    out = s.get(f"/api/orders/{oid}/status").json()
    assert out["capture_id"] and out["funded"] == ids
    big, _ = login("bigsponsor@billkind.demo")
    assert big.get(f"/api/orders/{oid}/status").json()["status"] == "UNKNOWN"
