"""Money safety: payouts whose outcome is unclear, payouts racing withdrawals, unclaimed payouts, pending captures,
duplicate picks and refunds made outside Billkind. All against the local PayPal fake."""

import asyncio

import httpx
import pytest

from app import db
from app.core import billers, funding, webhook_handlers
from app.paypal import fake
from app.paypal.client import PayPalError, client
from tests.conftest import BASE, login, wait_for

_n = [0]


def _fresh_bill(biller_id: str | None = None) -> dict:
    """A new open USD bill (64.00) with a verified biller."""
    template = db.one("SELECT * FROM bills WHERE id = 'BK-S009'")
    _n[0] += 1
    row = dict(template, id=f"BK-Q{_n[0]:03d}", status="LISTED", funded_minor=0, payout_item_id="",
               payout_batch_id="", payout_txn_id="", thank_you="", paid_at=None, doc_sha="", phash="",
               account_hash="", created_at=db.now(), updated_at=db.now())
    if biller_id:
        row["biller_id"] = biller_id
    db.insert("bills", row)
    return db.one("SELECT * FROM bills WHERE id = ?", (row["id"],))


def _status(bid: str) -> str:
    return db.one("SELECT status FROM bills WHERE id = ?", (bid,))["status"]


def _items(bid: str) -> list[dict]:
    return db.q("SELECT * FROM payout_items WHERE bill_id = ? ORDER BY created_at", (bid,))


def _batches_with(sid: str) -> int:
    return sum(1 for b in fake.BATCHES.values() for it in b["items"] if it["payout_item"]["sender_item_id"] == sid)


def _pay_http(bill: dict, email="buyer@personal.example.com") -> dict:
    s, c = login("sponsor@billkind.demo")
    r = s.post("/api/orders", json={"picks": [{"bill_id": bill["id"], "amount_minor": bill["settle_minor"]}]},
               headers={"X-CSRF": c})
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={"email": email})
    r = s.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": c})
    assert r.status_code == 200, r.text
    return dict(r.json(), order_id=oid)


def _funded_bill(monkeypatch) -> dict:
    """A fully captured bill whose payout has not been sent yet."""
    async def no_payout(*a, **k):
        return None
    monkeypatch.setattr(fake, "deliver", lambda raw, headers: asyncio.sleep(0))
    monkeypatch.setattr(funding, "send_payouts", no_payout)
    bill = _fresh_bill()
    _pay_http(bill)
    monkeypatch.undo()
    assert _status(bill["id"]) == "FUNDED" and not _items(bill["id"])
    return bill


def test_poster_cannot_withdraw_while_the_payout_call_is_in_flight(monkeypatch):
    bill = _funded_bill(monkeypatch)
    orig = client().create_payout

    async def scenario():
        gate = asyncio.Event()

        async def slow(**kw):
            await gate.wait()
            return await orig(**kw)
        monkeypatch.setattr(client(), "create_payout", slow)
        task = asyncio.create_task(funding.send_payouts([bill]))
        await asyncio.sleep(0.05)
        assert _items(bill["id"])[0]["status"] == "CLAIMED" and _status(bill["id"]) == "FUNDED"
        with pytest.raises(funding.FundingError):
            await funding.withdraw(bill["id"], "You withdrew this bill.")
        gate.set()
        await task
    asyncio.run(scenario())
    monkeypatch.undo()
    assert wait_for(lambda: _status(bill["id"]) == "PAID")
    assert db.one("SELECT status FROM contributions WHERE bill_id = ? AND status != 'CANCELLED'",
                  (bill["id"],))["status"] == "CAPTURED"


def test_payout_5xx_after_paypal_created_the_batch_pays_once(monkeypatch):
    bill = _funded_bill(monkeypatch)
    orig = client().create_payout

    async def created_then_503(**kw):
        await orig(**kw)
        raise PayPalError(503, {"name": "SERVICE_UNAVAILABLE"}, "create payout")
    monkeypatch.setattr(client(), "create_payout", created_then_503)
    asyncio.run(funding.send_payouts([bill]))
    monkeypatch.undo()
    item = _items(bill["id"])[0]
    assert item["status"] in ("UNKNOWN", "SUCCESS") and _status(bill["id"]) in ("PAYOUT_SENT", "PAID")
    with pytest.raises(funding.FundingError):
        asyncio.run(funding.withdraw(bill["id"], "You withdrew this bill."))
    # The first batch's own webhook settles it; a resend would replay, never create a second batch.
    assert wait_for(lambda: _status(bill["id"]) == "PAID")
    asyncio.run(funding.resolve_unknown_payouts(now=db.now() + 3600))
    assert len(_items(bill["id"])) == 1 and _items(bill["id"])[0]["status"] == "SUCCESS"
    assert _batches_with(item["sender_item_id"]) == 1


def test_payout_timeout_is_resent_with_the_same_batch_id(monkeypatch):
    bill = _funded_bill(monkeypatch)

    async def timeout(**kw):
        raise httpx.ReadTimeout("no answer")
    monkeypatch.setattr(client(), "create_payout", timeout)
    asyncio.run(funding.send_payouts([bill]))
    monkeypatch.undo()
    item = _items(bill["id"])[0]
    assert item["status"] == "UNKNOWN" and item["sender_batch_id"] and _status(bill["id"]) == "PAYOUT_SENT"
    assert asyncio.run(funding.heal_unpaid_funded(older_than=0)) == 0
    with pytest.raises(funding.FundingError):
        asyncio.run(funding.retry_payout(bill["id"]))
    assert asyncio.run(funding.resolve_unknown_payouts(now=db.now() + 120)) == 1
    assert wait_for(lambda: _status(bill["id"]) == "PAID")
    assert len(_items(bill["id"])) == 1 and _batches_with(item["sender_item_id"]) == 1
    batch = db.one("SELECT * FROM payout_batches WHERE batch_id = ?", (_items(bill["id"])[0]["batch_id"],))
    assert batch["sender_batch_id"] == item["sender_batch_id"]


def test_duplicate_batch_without_replay_waits_for_a_person(monkeypatch):
    bill = _funded_bill(monkeypatch)
    orig = client().create_payout

    async def created_then_timeout(**kw):
        await orig(**kw)
        raise httpx.ReadTimeout("no answer")
    monkeypatch.setattr(fake, "deliver", lambda raw, headers: asyncio.sleep(0))  # no webhook arrives
    monkeypatch.setattr(client(), "create_payout", created_then_timeout)
    asyncio.run(funding.send_payouts([bill]))
    monkeypatch.setattr(client(), "create_payout", orig)
    fake.IDEMPOTENT.clear()  # PayPal no longer replays: the resend is refused as a duplicate
    asyncio.run(funding.resolve_unknown_payouts(now=db.now() + 120))
    item = _items(bill["id"])[0]
    assert item["status"] == "MANUAL_CHECK" and _status(bill["id"]) == "PAYOUT_FAILED"
    for action in (funding.retry_payout(bill["id"]), funding.withdraw(bill["id"], "refund")):
        with pytest.raises(funding.FundingError):
            asyncio.run(action)
    assert len(_items(bill["id"])) == 1 and _batches_with(item["sender_item_id"]) == 1
    monkeypatch.undo()
    # When PayPal does report the original item, the bill is recorded as paid.
    paid = next(it for b in fake.BATCHES.values() for it in b["items"]
                if it["payout_item"]["sender_item_id"] == item["sender_item_id"])
    asyncio.run(funding.apply_payout_item(dict(paid, transaction_status="SUCCESS")))
    assert _status(bill["id"]) == "PAID"


def test_a_claim_that_never_got_an_answer_is_resent(monkeypatch):
    bill = _funded_bill(monkeypatch)
    biller = db.one("SELECT * FROM billers WHERE id = ?", (bill["biller_id"],))
    old = db.now() - 3600
    db.insert("payout_items", {"sender_item_id": f"{bill['id']}-P1", "batch_id": "", "bill_id": bill["id"],
                               "amount_minor": bill["settle_minor"], "currency": "USD", "receiver": biller["paypal_email"],
                               "status": "CLAIMED", "sender_batch_id": db.new_id("BATCH"), "created_at": old,
                               "updated_at": old})
    with pytest.raises(funding.FundingError):  # a claimed payout blocks the withdrawal
        asyncio.run(funding.withdraw(bill["id"], "You withdrew this bill."))
    assert asyncio.run(funding.heal_unpaid_funded(older_than=0)) == 0
    assert asyncio.run(funding.resolve_unknown_payouts()) == 1
    assert wait_for(lambda: _status(bill["id"]) == "PAID")
    assert len(_items(bill["id"])) == 1


def _unclaimed_bill() -> tuple[dict, dict]:
    bid = billers.add(f"Unclaimed Power {_n[0]}", "electricity", "US", "Detroit",
                      f"unclaimed-{_n[0]}@business.example.com", verified=True)
    bill = _fresh_bill(biller_id=bid)
    _pay_http(bill)
    assert wait_for(lambda: _status(bill["id"]) == "PAYOUT_FAILED")
    item = wait_for(lambda: (_items(bill["id"]) or [{}])[0].get("payout_item_id") and _items(bill["id"])[0])
    assert item["status"] == "UNCLAIMED"
    return bill, item


def test_unclaimed_payout_claimed_later_still_pays_the_bill():
    bill, item = _unclaimed_bill()
    out = asyncio.run(funding.apply_payout_item({"payout_item": {"sender_item_id": item["sender_item_id"]},
                                                 "payout_item_id": item["payout_item_id"],
                                                 "transaction_status": "SUCCESS", "transaction_id": "TXN-LATE"}))
    assert out == "SUCCESS" and _status(bill["id"]) == "PAID"


def test_retry_after_unclaimed_cancels_the_first_payout():
    bill, item = _unclaimed_bill()
    admin, cad = login("admin@billkind.demo")
    r = admin.post(f"/admin/bills/{bill['id']}/payout", data={"action": "retry", "csrf": cad})
    assert r.status_code == 200
    rows = _items(bill["id"])
    assert rows[0]["status"] == "RETURNED" and len(rows) == 2
    first = next(it for b in fake.BATCHES.values() for it in b["items"]
                 if it["payout_item_id"] == item["payout_item_id"])
    assert first["transaction_status"] == "RETURNED"


def test_pending_capture_funds_nothing_until_paypal_completes_it():
    bill = _fresh_bill()
    out = _pay_http(bill, email="pending-buyer@personal.example.com")
    assert out["status"] == "PENDING"
    c = db.one("SELECT * FROM contributions WHERE bill_id = ?", (bill["id"],))
    assert c["status"] == "CREATED" and c["capture_id"] and c["expires_at"] > db.now() + 86400
    assert _status(bill["id"]) == "LISTED" and not _items(bill["id"])
    assert funding.remaining(funding.get_bill(bill["id"])) == 0  # still held, so nobody overfunds it
    assert funding.expire_reservations() >= 0 and \
        db.one("SELECT status FROM contributions WHERE id = ?", (c["id"],))["status"] == "CREATED"
    cap = fake.CAPTURES[c["capture_id"]]
    cap["status"] = "COMPLETED"
    cap.pop("status_details", None)
    event = {"id": f"WH-T-{c['id']}", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": fake._public(cap)}
    asyncio.run(webhook_handlers.handle(event))
    assert db.one("SELECT status FROM contributions WHERE id = ?", (c["id"],))["status"] == "CAPTURED"
    assert wait_for(lambda: _status(bill["id"]) == "PAID")
    assert len(_items(bill["id"])) == 1


def test_pending_capture_then_denied_is_never_paid_out():
    bill = _fresh_bill()
    _pay_http(bill, email="pending-buyer@personal.example.com")
    c = db.one("SELECT * FROM contributions WHERE bill_id = ?", (bill["id"],))
    cap = fake.CAPTURES[c["capture_id"]]
    cap["status"] = "DECLINED"
    event = {"id": f"WH-D-{c['id']}", "event_type": "PAYMENT.CAPTURE.DENIED",
             "resource": dict(fake._public(cap), supplementary_data={})}  # matched by capture id alone
    assert asyncio.run(webhook_handlers.handle(event)).startswith("1 ")
    assert db.one("SELECT status FROM contributions WHERE id = ?", (c["id"],))["status"] == "DENIED"
    assert _status(bill["id"]) == "LISTED" and not _items(bill["id"])
    assert funding.remaining(funding.get_bill(bill["id"])) == bill["settle_minor"]


def test_the_same_bill_twice_in_one_payment_is_refused():
    bill = _fresh_bill()
    s, c = login("sponsor@billkind.demo")
    pick = {"bill_id": bill["id"], "amount_minor": bill["settle_minor"]}
    orders = len(fake.ORDERS)
    r = s.post("/api/orders", json={"picks": [pick, pick]}, headers={"X-CSRF": c})
    assert r.status_code == 409 and "only once" in r.json()["error"]
    assert len(fake.ORDERS) == orders and not db.q("SELECT 1 FROM contributions WHERE bill_id = ?", (bill["id"],))


def test_refund_made_outside_billkind_is_recorded_once():
    bill = _fresh_bill()
    s, c = login("sponsor@billkind.demo")
    r = s.post("/api/orders", json={"picks": [{"bill_id": bill["id"], "amount_minor": 2000}]}, headers={"X-CSRF": c})
    oid = r.json()["id"]
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})
    s.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": c})
    con = db.one("SELECT * FROM contributions WHERE order_id = ?", (oid,))
    assert con["status"] == "CAPTURED" and _status(bill["id"]) == "FUNDING"
    refund = {"id": "REF-OUTSIDE-1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "5.00"},
              "links": [{"rel": "up", "href": f"https://api.paypal.com/v2/payments/captures/{con['capture_id']}"}]}
    event = {"id": "WH-REF-OUTSIDE-1", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": refund}
    assert "recorded" in asyncio.run(webhook_handlers.handle(event))
    con = db.one("SELECT * FROM contributions WHERE id = ?", (con["id"],))
    assert con["status"] == "PARTIALLY_REFUNDED" and con["refunded_minor"] == 500
    assert funding.get_bill(bill["id"])["funded_minor"] == 1500
    assert asyncio.run(webhook_handlers.handle(event)) == "already recorded"
    own = dict(refund, id="REF-OWN", invoice_id=f"{con['id']}-R0")
    assert asyncio.run(webhook_handlers.handle({"id": "WH-OWN", "event_type": "PAYMENT.CAPTURE.REFUNDED",
                                                "resource": own})) == "own refund"
    assert db.one("SELECT refunded_minor FROM contributions WHERE id = ?", (con["id"],))["refunded_minor"] == 500


def test_unclear_order_creation_is_asked_again_with_the_same_request_id(monkeypatch):
    bill = _fresh_bill()
    sponsor = db.one("SELECT * FROM users WHERE id = 'U-SPONSOR'")
    orig = client().create_order
    calls = []

    async def first_fails(**kw):
        calls.append(kw["request_id"])
        out = await orig(**kw)
        if len(calls) == 1:
            raise PayPalError(500, {"name": "INTERNAL_SERVER_ERROR"}, "create order")
        return out
    monkeypatch.setattr(client(), "create_order", first_fails)
    orders = len(fake.ORDERS)
    result = asyncio.run(funding.start_payment(sponsor, [(bill["id"], 1000)]))
    monkeypatch.undo()
    assert len(calls) == 2 and calls[0] == calls[1] and len(fake.ORDERS) == orders + 1
    assert db.one("SELECT order_id FROM contributions WHERE bill_id = ?", (bill["id"],))["order_id"] == result["order_id"]


def test_capture_webhook_adopts_an_order_billkind_never_heard_back_about(monkeypatch):
    bill = _fresh_bill()
    sponsor = db.one("SELECT * FROM users WHERE id = 'U-SPONSOR'")
    orig = client().create_order
    made = []

    async def always_fails(**kw):
        made.append(await orig(**kw))
        raise PayPalError(502, {"name": "BAD_GATEWAY"}, "create order")
    monkeypatch.setattr(client(), "create_order", always_fails)
    with pytest.raises(PayPalError):
        asyncio.run(funding.start_payment(sponsor, [(bill["id"], 1000)]))
    monkeypatch.undo()
    con = db.one("SELECT * FROM contributions WHERE bill_id = ?", (bill["id"],))
    assert con["status"] == "CANCELLED" and con["order_id"] == ""
    oid = made[0]["id"]
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})
    asyncio.run(client().capture_order(oid))  # PayPal completes it; its webhook reaches Billkind
    assert wait_for(lambda: db.one("SELECT status FROM contributions WHERE id = ?", (con["id"],))["status"] == "CAPTURED")
    assert db.one("SELECT order_id FROM contributions WHERE id = ?", (con["id"],))["order_id"] == oid
    assert _status(bill["id"]) == "FUNDING"


def test_a_new_checkout_releases_the_sponsors_earlier_unapproved_holds():
    s, c = login("sponsor@billkind.demo")
    first, second = _fresh_bill(), _fresh_bill()
    r = s.post("/api/orders", json={"picks": [{"bill_id": first["id"], "amount_minor": first["settle_minor"]}]},
               headers={"X-CSRF": c})
    oid = r.json()["id"]
    assert funding.remaining(funding.get_bill(first["id"])) == 0
    s.post("/api/orders", json={"picks": [{"bill_id": second["id"], "amount_minor": 1000}]}, headers={"X-CSRF": c})
    assert funding.remaining(funding.get_bill(first["id"])) == first["settle_minor"]  # visible to others again
    httpx.post(f"{BASE}/fakepaypal/test/approve/{oid}", json={})  # approved late: still counts
    r = s.post(f"/api/orders/{oid}/capture", headers={"X-CSRF": c})
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    assert wait_for(lambda: _status(first["id"]) in ("FUNDED", "PAYOUT_SENT", "PAID"))


def test_payout_reversed_after_success_is_recorded_and_reported():
    bill, item = _unclaimed_bill()
    asyncio.run(funding.apply_payout_item({"payout_item": {"sender_item_id": item["sender_item_id"]},
                                           "payout_item_id": item["payout_item_id"],
                                           "transaction_status": "SUCCESS", "transaction_id": "TXN-1"}))
    out = asyncio.run(funding.apply_payout_item({"payout_item": {"sender_item_id": item["sender_item_id"]},
                                                 "payout_item_id": item["payout_item_id"],
                                                 "transaction_status": "REVERSED"}))
    assert out == "REVERSED"
    assert db.one("SELECT status FROM payout_items WHERE sender_item_id = ?", (item["sender_item_id"],))["status"] \
        == "REVERSED"
    assert _status(bill["id"]) == "PAID"
    assert db.one("SELECT 1 FROM messages WHERE user_id = 'U-ADMIN' AND subject LIKE ?",
                  (f"Payout REVERSED after it was paid: bill {bill['id']}",))
