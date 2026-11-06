"""Check the real PayPal sandbox with Billkind's own client (APIMatic SDK path).

Usage: python -m scripts.sandbox_check [payout_receiver_email]
Reads PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET from .env. Prints statuses and PayPal error names only,
never credentials. Creates orders (no buyer approval, so nothing is charged), a vault setup token, and
1.00 payouts to the receiver.
"""
import asyncio, json, os, sys, uuid  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["PAYPAL_MODE"] = "sandbox"
from app.config import settings
settings.paypal_mode = "sandbox"
from app.paypal import client as pp
import httpx

def short(e): return f"HTTP {e.status} {e.body.get('name') if isinstance(e.body, dict) else ''} {e.issue} {(e.body.get('details') or [{}])[0].get('description','') if isinstance(e.body, dict) else str(e.body)[:200]}"

async def main():
    c = pp.PayPalClient(base_url="https://api-m.sandbox.paypal.com")
    print("sdk backend:", c.sdk is not None)
    tok = await c.token(); print("oauth ok")
    async with httpx.AsyncClient(base_url="https://api-m.sandbox.paypal.com", timeout=30) as h:
        H = {"Authorization": f"Bearer {tok}"}
        r = await h.get("/v1/reporting/balances", headers=H, params={"currency_code": "ALL"})
        print("balances", r.status_code, json.dumps([{b['currency']: b.get('available_balance',{}).get('value')} for b in r.json().get('balances', [])]) if r.status_code == 200 else r.text[:300])
        r = await h.get("/v1/identity/oauth2/userinfo", headers=H, params={"schema": "paypalv1.1"})
        print("userinfo", r.status_code)
    for cur, total in [("USD", "12.50"), ("GBP", "10.00"), ("EUR", "10.00"), ("PHP", "500.00"), ("MXN", "200.00"), ("JPY", "1500")]:
        gid = "PROBE-" + uuid.uuid4().hex[:8]
        try:
            o = await c.create_order(currency=cur, items=[{"name": "Probe bill", "sku": "BK-P", "value": total}], total=total,
                                     custom_id=gid, invoice_id=gid, description="probe")
            g = await c.get_order(o["id"])
            print("order", cur, o["status"], "approve link:", bool(pp.approve_link(o)), "get:", g["status"])
        except pp.PayPalError as e:
            print("order", cur, "FAILED", short(e))
    try:
        st = await c.create_setup_token(return_url="https://example.com/r", cancel_url="https://example.com/c")
        print("vault setup token", st["status"], [l["rel"] for l in st.get("links", [])])
    except pp.PayPalError as e:
        print("vault setup token FAILED", short(e))
    receiver = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BILLER_SANDBOX_EMAIL", "")
    if not receiver:
        print("no payout receiver given; skipping payouts")
        return
    for cur, val, rcv in [("USD", "1.00", receiver), ("GBP", "1.00", receiver), ("EUR", "1.00", receiver)]:
        sbid = "PROBE-" + uuid.uuid4().hex[:10]
        try:
            r = await c.create_payout(sender_batch_id=sbid, items=[{"receiver": rcv, "value": val, "currency": cur, "note": "probe", "sender_item_id": sbid + "-1"}], subject="probe", message="probe")
            bid = r["batch_header"]["payout_batch_id"]
            await asyncio.sleep(6)
            b = await c.get_payout_batch(bid)
            it = b["items"][0]
            print("payout", cur, rcv.split("@")[0][:14], "batch", b["batch_header"]["batch_status"], "item", it["transaction_status"], (it.get("errors") or {}).get("name", ""))
        except pp.PayPalError as e:
            print("payout", cur, "FAILED", short(e))

asyncio.run(main())
