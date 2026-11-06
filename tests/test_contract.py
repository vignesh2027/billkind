"""Contract tests for every PayPal call Billkind makes, run through both client backends:
the APIMatic-generated PayPal Server SDK (orders, payments, vault) and plain REST.

Against the local fake by default. With PAYPAL_CONTRACT_SANDBOX=1 and real credentials in the environment,
the steps that need no buyer approval also run against the real sandbox (see README).
"""

import asyncio
import os
import uuid

import httpx
import pytest

from app.config import settings
from app.paypal import client as pp
from tests.conftest import BASE

BACKENDS = ["apimatic", "rest"]


def _client(backend: str) -> pp.PayPalClient:
    old = settings.paypal_sdk
    settings.paypal_sdk = backend
    try:
        return pp.PayPalClient()
    finally:
        settings.paypal_sdk = old


def _order_kwargs(total="12.50", cur="USD"):
    gid = "T-" + uuid.uuid4().hex[:8]
    return dict(currency=cur, items=[{"name": "Water bill", "sku": "BK-T1", "value": total}], total=total,
                custom_id=gid, invoice_id=gid, description="contract test")


@pytest.mark.parametrize("backend", BACKENDS)
def test_orders_capture_refund_contract(backend):
    c = _client(backend)
    assert (c.sdk is not None) == (backend == "apimatic")

    async def flow():
        order = await c.create_order(**_order_kwargs())
        assert order["id"] and order["status"] in ("CREATED", "PAYER_ACTION_REQUIRED")
        assert pp.approve_link(order)
        with pytest.raises(pp.PayPalError) as e:
            await c.capture_order(order["id"], request_id="early-" + order["id"])
        assert e.value.status == 422 and e.value.issue == "ORDER_NOT_APPROVED"
        httpx.post(f"{BASE}/fakepaypal/test/approve/{order['id']}", json={})
        cap_order = await c.capture_order(order["id"])
        cap = pp.first_capture(cap_order)
        assert cap_order["status"] == "COMPLETED" and cap["status"] == "COMPLETED"
        assert cap["amount"] == {"currency_code": "USD", "value": "12.50"}
        assert "net_amount" in cap["seller_receivable_breakdown"]
        again = await c.get_order(order["id"])
        assert again["status"] == "COMPLETED"
        r1 = await c.refund_capture(cap["id"], amount="2.50", currency="USD", invoice_id="R1", note="partial",
                                    request_id="r1-" + cap["id"])
        assert r1["status"] == "COMPLETED" and r1["amount"]["value"] == "2.50"
        with pytest.raises(pp.PayPalError) as e:
            await c.refund_capture(cap["id"], amount="50.00", currency="USD", invoice_id="R2", note="too much",
                                   request_id="r2-" + cap["id"])
        assert e.value.issue == "REFUND_AMOUNT_EXCEEDED"
        rest = await c.refund_capture(cap["id"], amount=None, currency="USD", invoice_id="R3", note="rest",
                                      request_id="r3-" + cap["id"])
        assert rest["amount"]["value"] == "10.00"
    asyncio.run(flow())


@pytest.mark.parametrize("backend", BACKENDS)
def test_order_validation_errors(backend):
    c = _client(backend)

    async def flow():
        bad = _order_kwargs()
        bad["total"] = "13.00"  # item_total mismatch
        with pytest.raises(pp.PayPalError) as e:
            await c.create_order(**bad)
        assert e.value.status == 422
        with pytest.raises(pp.PayPalError) as e:
            await c.create_order(**_order_kwargs(cur="INR"))
        assert e.value.issue == "CURRENCY_NOT_SUPPORTED"
        zero = _order_kwargs(total="500", cur="JPY")
        order = await c.create_order(**zero)
        assert order["purchase_units"][0]["amount"]["value"] == "500"
    asyncio.run(flow())


@pytest.mark.parametrize("backend", BACKENDS)
def test_vault_then_merchant_initiated_order(backend):
    c = _client(backend)

    async def flow():
        st = await c.create_setup_token(return_url=BASE + "/r", cancel_url=BASE + "/c")
        assert st["status"] == "PAYER_ACTION_REQUIRED"
        approve = next(l["href"] for l in st["links"] if l["rel"] == "approve")
        assert "approval_session_id=" in approve
        httpx.post(f"{BASE}/fakepaypal/agreements/decide", data={"token": st["id"], "decision": "approve"})
        pt = await c.create_payment_token(st["id"])
        assert pt["id"] and pt["payment_source"]["paypal"]["email_address"]
        order = await c.create_order(**_order_kwargs(), vault_id=pt["id"])
        assert order["status"] == "COMPLETED" and pp.first_capture(order)["status"] == "COMPLETED"
    asyncio.run(flow())


def test_payouts_contract():
    c = _client("rest")

    async def flow():
        sbid = "B-" + uuid.uuid4().hex[:10]
        items = [{"receiver": "school@business.example.com", "value": "40.00", "currency": "USD", "note": "acct 1",
                  "sender_item_id": sbid + "-1"},
                 {"receiver": "clinic@business.example.com", "value": "1200", "currency": "JPY", "note": "acct 2",
                  "sender_item_id": sbid + "-2"}]
        r = await c.create_payout(sender_batch_id=sbid, items=items, subject="s", message="m")
        bid = r["batch_header"]["payout_batch_id"]
        assert r["batch_header"]["batch_status"] == "PENDING"
        with pytest.raises(pp.PayPalError) as e:
            await c.create_payout(sender_batch_id=sbid, items=items, subject="s", message="m")
        assert e.value.status == 400
        batch = await c.get_payout_batch(bid)
        assert len(batch["items"]) == 2
        assert {i["payout_item"]["sender_item_id"] for i in batch["items"]} == {sbid + "-1", sbid + "-2"}
    asyncio.run(flow())


@pytest.mark.skipif(os.environ.get("PAYPAL_CONTRACT_SANDBOX") != "1", reason="real sandbox contract run not requested")
def test_real_sandbox_smoke():
    """Run with real credentials in .env: PAYPAL_CONTRACT_SANDBOX=1 pytest -k sandbox"""
    c = pp.PayPalClient(base_url="https://api-m.sandbox.paypal.com", client_id=os.environ["SANDBOX_CLIENT_ID"],
                        secret=os.environ["SANDBOX_CLIENT_SECRET"])

    async def flow():
        order = await c.create_order(**_order_kwargs())
        assert order["status"] in ("CREATED", "PAYER_ACTION_REQUIRED") and pp.approve_link(order)
        assert (await c.get_order(order["id"]))["id"] == order["id"]
        st = await c.create_setup_token(return_url="https://example.com/r", cancel_url="https://example.com/c")
        assert st["status"] == "PAYER_ACTION_REQUIRED"
    asyncio.run(flow())
