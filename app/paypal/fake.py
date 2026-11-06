"""A local fake of the PayPal REST endpoints Billkind uses.

It follows the documented request and response shapes (Orders v2, Payments v2 refunds,
Payouts v1, Vault v3, Webhooks, Transaction Search) closely enough that the same client code
runs against it and against the real sandbox. It signs the webhooks it sends with its own
certificate, so the self-verification path is exercised end to end.

Test hooks:
  payer email containing "decline"      capture returns status DECLINED
  payout receiver containing "unclaimed" payout item ends UNCLAIMED (also "returned", "blocked", "failed")
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import html
import json
import random
import string
import time
import uuid
import zlib
from decimal import Decimal

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ..config import settings
from ..core.money import PAYPAL_CURRENCIES, ZERO_DECIMAL

router = APIRouter(prefix="/fakepaypal")

ORDERS: dict[str, dict] = {}
CAPTURES: dict[str, dict] = {}
REFUNDS: dict[str, dict] = {}
BATCHES: dict[str, dict] = {}
SENDER_BATCH_IDS: set[str] = set()
SETUP_TOKENS: dict[str, dict] = {}
PAYMENT_TOKENS: dict[str, dict] = {}
IDEMPOTENT: dict[tuple[str, str], tuple[int, dict]] = {}
SENT_WEBHOOKS: list[dict] = []
PENDING_TASKS: set[asyncio.Task] = set()

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "fake-paypal.billkind.local")])
_CERT = (
    x509.CertificateBuilder()
    .subject_name(_name).issuer_name(_name).public_key(_KEY.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
    .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365))
    .add_extension(x509.SubjectAlternativeName([x509.DNSName("fake-paypal.billkind.local")]), critical=False)
    .sign(_KEY, hashes.SHA256())
)
CERT_PEM = _CERT.public_bytes(serialization.Encoding.PEM)


def reset() -> None:
    for d in (ORDERS, CAPTURES, REFUNDS, BATCHES, SETUP_TOKENS, PAYMENT_TOKENS, IDEMPOTENT):
        d.clear()
    SENDER_BATCH_IDS.clear()
    SENT_WEBHOOKS.clear()


def _pp_id(n: int = 17) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(status: int, name: str, issue: str, description: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "name": name,
        "message": "The requested action could not be performed, semantically incorrect, or failed business validation.",
        "debug_id": uuid.uuid4().hex[:13],
        "details": [{"issue": issue, "description": description or issue.replace("_", " ").lower()}],
    })


def _auth_ok(request: Request) -> bool:
    return request.headers.get("authorization", "").startswith("Bearer FAKE-")


def _valid_amount(amount: dict | None, key: str = "currency_code") -> str | None:
    if not isinstance(amount, dict):
        return "MISSING_REQUIRED_PARAMETER"
    cur = amount.get(key, "")
    val = str(amount.get("value", ""))
    if cur not in PAYPAL_CURRENCIES:
        return "CURRENCY_NOT_SUPPORTED"
    if cur in ZERO_DECIMAL:
        if not val.isdigit():
            return "DECIMAL_PRECISION"
    else:
        whole, _, frac = val.partition(".")
        if not whole.isdigit() or (frac and (len(frac) > 2 or not frac.isdigit())):
            return "DECIMAL_PRECISION"
    if Decimal(val) <= 0:
        return "INVALID_PARAMETER_VALUE"
    return None


def _idem(request: Request, status: int, body: dict) -> JSONResponse:
    rid = request.headers.get("paypal-request-id")
    if rid:
        IDEMPOTENT[(request.url.path, rid)] = (status, body)
    return JSONResponse(status_code=status, content=body)


def _replay(request: Request) -> JSONResponse | None:
    rid = request.headers.get("paypal-request-id")
    if rid and (request.url.path, rid) in IDEMPOTENT:
        status, body = IDEMPOTENT[(request.url.path, rid)]
        return JSONResponse(status_code=status, content=body)
    return None


# Webhook delivery ---------------------------------------------------------------

async def _deliver(raw: bytes, headers: dict) -> None:
    url = settings.app_base_url + "/webhooks/paypal"
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            await h.post(url, content=raw, headers=headers)
    except httpx.HTTPError:
        pass


deliver = _deliver  # tests may replace this


def emit(event_type: str, resource_type: str, resource: dict, summary: str) -> dict:
    event = {
        "id": "WH-" + _pp_id(17) + "-" + _pp_id(17),
        "event_version": "1.0",
        "create_time": _now_iso(),
        "resource_type": resource_type,
        "resource_version": "2.0",
        "event_type": event_type,
        "summary": summary,
        "resource": resource,
        "links": [],
    }
    raw = json.dumps(event, separators=(",", ":")).encode()
    tid = str(uuid.uuid4())
    ttime = _now_iso()
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    msg = f"{tid}|{ttime}|{settings.webhook_id}|{crc}".encode()
    sig = base64.b64encode(_KEY.sign(msg, padding.PKCS1v15(), hashes.SHA256())).decode()
    headers = {
        "Content-Type": "application/json",
        "PAYPAL-TRANSMISSION-ID": tid,
        "PAYPAL-TRANSMISSION-TIME": ttime,
        "PAYPAL-TRANSMISSION-SIG": sig,
        "PAYPAL-CERT-URL": settings.paypal_base_url + "/certs/fake.pem",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    }
    SENT_WEBHOOKS.append(event)
    _spawn(deliver(raw, headers))
    return event


def _spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return
    PENDING_TASKS.add(task)
    task.add_done_callback(PENDING_TASKS.discard)


async def drain() -> None:
    """Wait for queued webhook deliveries and payout processing (tests)."""
    while PENDING_TASKS:
        await asyncio.gather(*list(PENDING_TASKS), return_exceptions=True)


# OAuth ----------------------------------------------------------------------------

@router.post("/v1/oauth2/token")
async def oauth_token(request: Request):
    if not request.headers.get("authorization", "").startswith("Basic "):
        return JSONResponse(status_code=401, content={"error": "invalid_client",
                                                      "error_description": "Client Authentication failed"})
    return {"scope": "https://uri.paypal.com/services/payments/payment", "access_token": "FAKE-" + _pp_id(40),
            "token_type": "Bearer", "app_id": "APP-FAKE", "expires_in": 32400, "nonce": _pp_id(20)}


@router.get("/certs/fake.pem")
async def cert():
    return PlainTextResponse(CERT_PEM.decode())


# Orders v2 ------------------------------------------------------------------------

def _order_links(oid: str, status: str) -> list[dict]:
    base = settings.paypal_base_url
    links = [{"href": f"{base}/v2/checkout/orders/{oid}", "rel": "self", "method": "GET"}]
    if status in ("CREATED", "PAYER_ACTION_REQUIRED"):
        links.append({"href": f"{base}/checkoutnow?token={oid}",
                      "rel": "payer-action" if status == "PAYER_ACTION_REQUIRED" else "approve", "method": "GET"})
    if status == "APPROVED":
        links.append({"href": f"{base}/v2/checkout/orders/{oid}/capture", "rel": "capture", "method": "POST"})
    return links


def _make_capture(order: dict, unit: dict, payer_email: str) -> dict:
    cid = _pp_id(17)
    amount = unit["amount"]
    gross = Decimal(amount["value"])
    zero = amount["currency_code"] in ZERO_DECIMAL
    fee = (gross * Decimal("0.0349") + Decimal("0.49")).quantize(Decimal("1") if zero else Decimal("0.01"))
    status = "DECLINED" if "decline" in payer_email else "COMPLETED"
    cap = {
        "id": cid,
        "status": status,
        "amount": {"currency_code": amount["currency_code"], "value": amount["value"]},
        "final_capture": True,
        "seller_protection": {"status": "NOT_ELIGIBLE"},
        "seller_receivable_breakdown": {
            "gross_amount": dict(amount),
            "paypal_fee": {"currency_code": amount["currency_code"], "value": str(fee)},
            "net_amount": {"currency_code": amount["currency_code"], "value": str(gross - fee)},
        },
        "invoice_id": unit.get("invoice_id", ""),
        "custom_id": unit.get("custom_id", ""),
        "supplementary_data": {"related_ids": {"order_id": order["id"]}},
        "links": [
            {"href": f"{settings.paypal_base_url}/v2/payments/captures/{cid}", "rel": "self", "method": "GET"},
            {"href": f"{settings.paypal_base_url}/v2/payments/captures/{cid}/refund", "rel": "refund", "method": "POST"},
        ],
        "create_time": _now_iso(),
        "update_time": _now_iso(),
        "_refunded": "0",
    }
    CAPTURES[cid] = cap
    return cap


def _public(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if not k.startswith("_")}


def _order_view(order: dict) -> dict:
    out = _public(order)
    out["purchase_units"] = []
    for pu in order["purchase_units"]:
        pu = dict(pu)
        if pu.get("payments"):
            pu["payments"] = {"captures": [_public(CAPTURES[c["id"]]) for c in pu["payments"]["captures"]]}
        out["purchase_units"].append(pu)
    out["links"] = _order_links(order["id"], order["status"])
    return out


@router.post("/v2/checkout/orders")
async def create_order(request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    if (r := _replay(request)) is not None:
        return r
    body = await request.json()
    if body.get("intent") not in ("CAPTURE", "AUTHORIZE"):
        return _err(400, "INVALID_REQUEST", "INVALID_PARAMETER_VALUE", "intent")
    units = body.get("purchase_units") or []
    if not 1 <= len(units) <= 10:
        return _err(400, "INVALID_REQUEST", "INVALID_ARRAY_MAX_ITEMS", "purchase_units")
    for pu in units:
        if (issue := _valid_amount(pu.get("amount"))):
            return _err(422, "UNPROCESSABLE_ENTITY", issue, "purchase_units/amount")
        items = pu.get("items") or []
        if any(i.get("category") == "DONATION" for i in items):
            # Real sandbox behaviour for accounts without donation eligibility.
            return _err(403, "NOT_AUTHORIZED", "PERMISSION_DENIED_FOR_DONATION_ITEMS")
        if items:
            cur = pu["amount"]["currency_code"]
            total = sum(Decimal(i["unit_amount"]["value"]) * int(i.get("quantity", "1")) for i in items)
            item_total = pu["amount"].get("breakdown", {}).get("item_total")
            if not item_total:
                return _err(422, "UNPROCESSABLE_ENTITY", "ITEM_TOTAL_REQUIRED")
            if any(i["unit_amount"]["currency_code"] != cur for i in items) or item_total["currency_code"] != cur:
                return _err(422, "UNPROCESSABLE_ENTITY", "CURRENCY_MISMATCH")
            if Decimal(item_total["value"]) != total:
                return _err(422, "UNPROCESSABLE_ENTITY", "ITEM_TOTAL_MISMATCH")
            if Decimal(pu["amount"]["value"]) != Decimal(item_total["value"]):
                return _err(422, "UNPROCESSABLE_ENTITY", "AMOUNT_MISMATCH")
    oid = _pp_id(17)
    source = (body.get("payment_source") or {}).get("paypal") or {}
    vault_id = source.get("vault_id")
    order = {
        "id": oid,
        "intent": body["intent"],
        "status": "PAYER_ACTION_REQUIRED" if source.get("experience_context") else "CREATED",
        "purchase_units": [dict(pu, payee={"email_address": "billkind-platform@business.example.com",
                                           "merchant_id": "FAKEMERCHANT1"}) for pu in units],
        "create_time": _now_iso(),
        "_experience": source.get("experience_context", {}),
    }
    ORDERS[oid] = order
    if vault_id:
        token = PAYMENT_TOKENS.get(vault_id)
        if not token:
            return _err(422, "UNPROCESSABLE_ENTITY", "INVALID_VAULT_ID", "payment_source/paypal/vault_id")
        email = token["payment_source"]["paypal"]["email_address"]
        order["payer"] = {"email_address": email, "payer_id": token["_payer_id"],
                          "name": {"given_name": "Sandbox", "surname": "Sponsor"}}
        order["payment_source"] = {"paypal": {"email_address": email, "account_id": token["_payer_id"],
                                              "attributes": {"vault": {"id": vault_id, "status": "VAULTED"}}}}
        for pu in order["purchase_units"]:
            cap = _make_capture(order, pu, email)
            pu["payments"] = {"captures": [{"id": cap["id"]}]}
            if cap["status"] == "COMPLETED":
                emit("PAYMENT.CAPTURE.COMPLETED", "capture", _public(cap),
                     f"Payment completed for {cap['amount']['value']} {cap['amount']['currency_code']}")
        order["status"] = "COMPLETED"
    return _idem(request, 201 if vault_id else 200, _order_view(order))


@router.get("/v2/checkout/orders/{oid}")
async def get_order(oid: str, request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    order = ORDERS.get(oid)
    if not order:
        return _err(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
    return _order_view(order)


@router.post("/v2/checkout/orders/{oid}/capture")
async def capture_order(oid: str, request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    if (r := _replay(request)) is not None:
        return r
    order = ORDERS.get(oid)
    if not order:
        return _err(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
    if order["status"] == "COMPLETED":
        return _err(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_CAPTURED")
    if order["status"] != "APPROVED":
        return _err(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED",
                    "Payer has not yet approved the Order for payment.")
    email = order["payer"]["email_address"]
    declined = False
    for pu in order["purchase_units"]:
        cap = _make_capture(order, pu, email)
        pu["payments"] = {"captures": [{"id": cap["id"]}]}
        if cap["status"] == "COMPLETED":
            emit("PAYMENT.CAPTURE.COMPLETED", "capture", _public(cap),
                 f"Payment completed for {cap['amount']['value']} {cap['amount']['currency_code']}")
        else:
            declined = True
            emit("PAYMENT.CAPTURE.DECLINED", "capture", _public(cap), "A payment capture was declined")
    order["status"] = "COMPLETED"
    view = _order_view(order)
    if declined:
        view["status"] = "COMPLETED"
    return _idem(request, 201, view)


# Buyer approval (stands in for the hosted checkout page) ----------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sandbox payment simulator</title><style>
body{{font-family:system-ui,sans-serif;background:#eef2f7;color:#14213d;display:grid;place-items:center;min-height:100vh;margin:0}}
.box{{background:#fff;border-radius:14px;padding:28px;max-width:380px;box-shadow:0 8px 30px #0b1b3320}}
h1{{font-size:18px;margin:0 0 6px}} p{{color:#4b5875;font-size:14px}} .amt{{font-size:28px;font-weight:700;margin:12px 0}}
label{{font-size:13px;color:#4b5875}} input{{width:100%;padding:10px;border:1px solid #c8d3e6;border-radius:8px;margin:6px 0 14px;box-sizing:border-box}}
button{{width:100%;padding:12px;border:0;border-radius:10px;font-weight:600;font-size:15px;cursor:pointer;margin-top:6px}}
.ok{{background:#14213d;color:#fff}} .no{{background:#e4e9f2;color:#14213d}}
small{{display:block;margin-top:14px;color:#6b7690;font-size:12px}}</style></head><body><div class="box">
<h1>Sandbox payment simulator</h1><p>Local stand-in for the payment provider's buyer approval step. No real money.</p>
<div class="amt">{amount}</div><p>{desc}</p>
<form method="post" action="{action}"><input type="hidden" name="token" value="{oid}"><input type="hidden" name="popup" value="{popup}">
<label>Test buyer email</label><input name="email" value="sponsor-buyer@personal.example.com">
<button class="ok" name="decision" value="approve">Approve payment</button>
<button class="no" name="decision" value="cancel">Cancel</button></form>
<small>Order {oid}</small></div></body></html>"""


@router.get("/checkoutnow", response_class=HTMLResponse)
async def checkout_page(token: str, popup: int = 0):
    order = ORDERS.get(token)
    if not order:
        return HTMLResponse("Unknown order", status_code=404)
    pu = order["purchase_units"][0]
    amount = f"{pu['amount']['value']} {pu['amount']['currency_code']}"
    return _PAGE.format(amount=html.escape(amount), desc=html.escape(pu.get("description", "")),
                        oid=html.escape(token), popup=popup,
                        action=settings.paypal_base_url + "/checkoutnow/decide")


def approve(oid: str, email: str = "sponsor-buyer@personal.example.com") -> dict:
    order = ORDERS[oid]
    if order["status"] in ("CREATED", "PAYER_ACTION_REQUIRED"):
        payer_id = "FAKEPAYER" + str(abs(hash(email)) % 10**8)
        order["status"] = "APPROVED"
        order["payer"] = {"email_address": email, "payer_id": payer_id,
                          "name": {"given_name": "Sandbox", "surname": "Sponsor"}}
        emit("CHECKOUT.ORDER.APPROVED", "checkout-order", _order_view(order), "An order has been approved by buyer")
    return order


@router.post("/checkoutnow/decide")
async def checkout_decide(request: Request):
    form = await request.form()
    oid = str(form.get("token"))
    order = ORDERS.get(oid)
    if not order:
        return HTMLResponse("Unknown order", status_code=404)
    popup = str(form.get("popup")) == "1"
    ok = form.get("decision") == "approve"
    if ok:
        approve(oid, str(form.get("email") or "sponsor-buyer@personal.example.com"))
    if popup:
        msg = json.dumps({"fakepaypal": True, "orderID": oid, "approved": ok})
        return HTMLResponse(f"<script>window.opener&&window.opener.postMessage({msg},'*');window.close()</script>")
    exp = order.get("_experience", {})
    url = exp.get("return_url" if ok else "cancel_url") or settings.app_base_url
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}token={oid}", status_code=303)


@router.post("/test/approve/{oid}")
async def test_approve(oid: str, request: Request):
    if oid not in ORDERS:
        return _err(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    order = approve(oid, body.get("email", "sponsor-buyer@personal.example.com"))
    return {"id": oid, "status": order["status"]}


# Payments v2 refunds ----------------------------------------------------------------

@router.post("/v2/payments/captures/{cid}/refund")
async def refund_capture(cid: str, request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    if (r := _replay(request)) is not None:
        return r
    cap = CAPTURES.get(cid)
    if not cap:
        return _err(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
    if cap["status"] not in ("COMPLETED", "PARTIALLY_REFUNDED"):
        return _err(422, "UNPROCESSABLE_ENTITY", "CAPTURE_FULLY_REFUNDED" if cap["status"] == "REFUNDED"
                    else "INVALID_RESOURCE_ID")
    raw = await request.body()
    body = json.loads(raw) if raw else {}
    cur = cap["amount"]["currency_code"]
    remaining = Decimal(cap["amount"]["value"]) - Decimal(cap["_refunded"])
    if body.get("amount"):
        if (issue := _valid_amount(body["amount"])):
            return _err(422, "UNPROCESSABLE_ENTITY", issue)
        if body["amount"]["currency_code"] != cur:
            return _err(422, "UNPROCESSABLE_ENTITY", "REFUND_CURRENCY_MISMATCH")
        amt = Decimal(body["amount"]["value"])
    else:
        amt = remaining
    if amt > remaining:
        return _err(422, "UNPROCESSABLE_ENTITY", "REFUND_AMOUNT_EXCEEDED")
    cap["_refunded"] = str(Decimal(cap["_refunded"]) + amt)
    cap["status"] = "REFUNDED" if Decimal(cap["_refunded"]) == Decimal(cap["amount"]["value"]) else "PARTIALLY_REFUNDED"
    rid = _pp_id(17)
    refund = {
        "id": rid, "status": "COMPLETED",
        "amount": {"currency_code": cur, "value": str(amt)},
        "invoice_id": body.get("invoice_id", ""), "note_to_payer": body.get("note_to_payer", ""),
        "create_time": _now_iso(), "update_time": _now_iso(),
        "links": [
            {"href": f"{settings.paypal_base_url}/v2/payments/refunds/{rid}", "rel": "self", "method": "GET"},
            {"href": f"{settings.paypal_base_url}/v2/payments/captures/{cid}", "rel": "up", "method": "GET"},
        ],
    }
    REFUNDS[rid] = refund
    emit("PAYMENT.CAPTURE.REFUNDED", "refund", refund, f"A {amt} {cur} capture payment was refunded")
    return _idem(request, 201, refund)


# Payouts v1 -------------------------------------------------------------------------

_OUTCOMES = {"unclaimed": ("UNCLAIMED", "PAYMENT.PAYOUTS-ITEM.UNCLAIMED"),
             "returned": ("RETURNED", "PAYMENT.PAYOUTS-ITEM.RETURNED"),
             "blocked": ("BLOCKED", "PAYMENT.PAYOUTS-ITEM.BLOCKED"),
             "failed": ("FAILED", "PAYMENT.PAYOUTS-ITEM.FAILED")}


@router.post("/v1/payments/payouts")
async def create_payout(request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    body = await request.json()
    header = body.get("sender_batch_header") or {}
    sbid = header.get("sender_batch_id")
    items = body.get("items") or []
    if not sbid:
        return _err(400, "VALIDATION_ERROR", "MISSING_REQUIRED_PARAMETER", "sender_batch_header.sender_batch_id")
    if sbid in SENDER_BATCH_IDS:
        return _err(400, "USER_BUSINESS_ERROR", "DUPLICATE_SENDER_BATCH_ID",
                    "Batch with given sender_batch_id already exists")
    if not 1 <= len(items) <= 15000:
        return _err(400, "VALIDATION_ERROR", "INVALID_ARRAY_LENGTH", "items")
    for it in items:
        if (issue := _valid_amount(it.get("amount"), key="currency")):
            return _err(422, "VALIDATION_ERROR", issue, "items/amount")
        if not it.get("receiver"):
            return _err(400, "VALIDATION_ERROR", "MISSING_REQUIRED_PARAMETER", "items/receiver")
    if any("usdonly" in it["receiver"] and it["amount"]["currency"] != "USD" for it in items):
        # Test hook: the platform account holds no balance in that currency.
        return _err(422, "INSUFFICIENT_FUNDS", "INSUFFICIENT_FUNDS", "Sender does not have sufficient funds.")
    SENDER_BATCH_IDS.add(sbid)
    bid = _pp_id(13)
    batch = {
        "batch_header": {"payout_batch_id": bid, "batch_status": "PENDING", "time_created": _now_iso(),
                         "sender_batch_header": {"sender_batch_id": sbid, "email_subject": header.get("email_subject", "")}},
        "items": [],
    }
    for it in items:
        batch["items"].append({
            "payout_item_id": _pp_id(13), "transaction_status": "PENDING", "payout_batch_id": bid,
            "payout_item_fee": {"currency": it["amount"]["currency"], "value": "0.00" if it["amount"]["currency"] not in ZERO_DECIMAL else "0"},
            "payout_item": {"recipient_type": it.get("recipient_type", "EMAIL"), "amount": it["amount"],
                            "note": it.get("note", ""), "receiver": it["receiver"],
                            "sender_item_id": it.get("sender_item_id", "")},
        })
    BATCHES[bid] = batch
    _spawn(_process_batch(bid))
    return JSONResponse(status_code=201, content={
        "batch_header": {"payout_batch_id": bid, "batch_status": "PENDING",
                         "sender_batch_header": {"sender_batch_id": sbid, "email_subject": header.get("email_subject", "")}},
        "links": [{"href": f"{settings.paypal_base_url}/v1/payments/payouts/{bid}", "rel": "self", "method": "GET",
                   "encType": "application/json"}],
    })


async def _process_batch(bid: str) -> None:
    await asyncio.sleep(settings.fake_payout_delay)
    batch = BATCHES[bid]
    for item in batch["items"]:
        receiver = item["payout_item"]["receiver"].lower()
        status, event = "SUCCESS", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"
        for marker, outcome in _OUTCOMES.items():
            if marker in receiver:
                status, event = outcome
        item["transaction_status"] = status
        item["transaction_id"] = _pp_id(17)
        item["time_processed"] = _now_iso()
        emit(event, "payouts_item", dict(item), f"Payout item {status.lower()}")
        await asyncio.sleep(0.4)
    batch["batch_header"]["batch_status"] = "SUCCESS"
    emit("PAYMENT.PAYOUTSBATCH.SUCCESS", "payouts", {"batch_header": batch["batch_header"]},
         "Payouts batch completed successfully.")


@router.get("/v1/payments/payouts/{bid}")
async def get_payout(bid: str, request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    batch = BATCHES.get(bid)
    if not batch:
        return _err(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
    return batch


# Vault v3 ---------------------------------------------------------------------------

@router.post("/v3/vault/setup-tokens")
async def setup_token(request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    body = await request.json()
    pp = (body.get("payment_source") or {}).get("paypal")
    if not pp or pp.get("usage_type") not in ("MERCHANT", "PLATFORM"):
        return _err(422, "UNPROCESSABLE_ENTITY", "INVALID_PARAMETER_VALUE", "payment_source/paypal/usage_type")
    sid = _pp_id(17)
    SETUP_TOKENS[sid] = {"id": sid, "status": "PAYER_ACTION_REQUIRED", "_exp": pp.get("experience_context", {})}
    return JSONResponse(status_code=201, content={
        "id": sid, "customer": {"id": "CUST" + _pp_id(8)}, "status": "PAYER_ACTION_REQUIRED",
        "payment_source": {"paypal": {"description": pp.get("description", ""), "usage_pattern": pp.get("usage_pattern"),
                                      "usage_type": pp["usage_type"], "customer_type": pp.get("customer_type", "CONSUMER")}},
        "links": [{"href": f"{settings.paypal_base_url}/agreements/approve?approval_session_id={sid}",
                   "rel": "approve", "method": "GET"},
                  {"href": f"{settings.paypal_base_url}/v3/vault/setup-tokens/{sid}", "rel": "self", "method": "GET"}],
    })


@router.get("/agreements/approve", response_class=HTMLResponse)
async def vault_approve_page(approval_session_id: str):
    st = SETUP_TOKENS.get(approval_session_id)
    if not st:
        return HTMLResponse("Unknown setup token", status_code=404)
    return (_PAGE.replace("Approve payment", "Save this account for future payments")
            .format(amount="Save payment method", desc="Billkind giving agent: pays verified bills within your limits",
                    oid=html.escape(approval_session_id), popup=0,
                    action=settings.paypal_base_url + "/agreements/decide"))


@router.post("/agreements/decide")
async def vault_decide(request: Request):
    form = await request.form()
    sid = str(form.get("token"))
    st = SETUP_TOKENS.get(sid)
    if not st:
        return HTMLResponse("Unknown setup token", status_code=404)
    ok = form.get("decision") == "approve"
    if ok:
        st["status"] = "APPROVED"
        st["_email"] = str(form.get("email") or "sponsor-buyer@personal.example.com")
    url = st["_exp"].get("return_url" if ok else "cancel_url") or settings.app_base_url
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}approval_token_id={sid}", status_code=303)


@router.post("/v3/vault/payment-tokens")
async def payment_token(request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    if (r := _replay(request)) is not None:
        return r
    body = await request.json()
    tok = (body.get("payment_source") or {}).get("token") or {}
    st = SETUP_TOKENS.get(tok.get("id", ""))
    if tok.get("type") != "SETUP_TOKEN" or not st:
        return _err(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "payment_source/token/id")
    if st["status"] != "APPROVED":
        return _err(422, "UNPROCESSABLE_ENTITY", "SETUP_TOKEN_NOT_APPROVED")
    pid = _pp_id(10)
    email = st["_email"]
    token = {"id": pid, "customer": {"id": "CUST" + _pp_id(8)},
             "payment_source": {"paypal": {"email_address": email, "usage_type": "MERCHANT",
                                           "customer_type": "CONSUMER"}},
             "links": [{"href": f"{settings.paypal_base_url}/v3/vault/payment-tokens/{pid}", "rel": "self",
                        "method": "GET"}],
             "_payer_id": "FAKEPAYER" + str(abs(hash(email)) % 10**8)}
    PAYMENT_TOKENS[pid] = token
    st["status"] = "VAULTED"
    emit("VAULT.PAYMENT-TOKEN.CREATED", "payment_token", _public(token), "A payment token has been created.")
    return _idem(request, 200, _public(token))


# Webhooks verify API ------------------------------------------------------------------

@router.post("/v1/notifications/verify-webhook-signature")
async def verify_sig(request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    body = await request.json()
    raw = json.dumps(body.get("webhook_event"), separators=(",", ":")).encode()
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    msg = f"{body.get('transmission_id')}|{body.get('transmission_time')}|{body.get('webhook_id')}|{crc}".encode()
    try:
        _KEY.public_key().verify(base64.b64decode(body.get("transmission_sig", "")), msg,
                                 padding.PKCS1v15(), hashes.SHA256())
        status = "SUCCESS"
    except Exception:
        status = "FAILURE"
    return {"verification_status": status}


# Transaction Search (used through the PayPal Agent Toolkit) ------------------------------

@router.get("/v1/reporting/transactions")
async def transactions(request: Request):
    if not _auth_ok(request):
        return JSONResponse(status_code=401, content={"name": "AUTHENTICATION_FAILURE"})
    details = []
    for cap in CAPTURES.values():
        details.append({"transaction_info": {
            "transaction_id": cap["id"], "transaction_event_code": "T0006",
            "transaction_initiation_date": cap["create_time"], "transaction_updated_date": cap["update_time"],
            "transaction_amount": {"currency_code": cap["amount"]["currency_code"], "value": cap["amount"]["value"]},
            "transaction_status": "S" if cap["status"] == "COMPLETED" else "V",
            "invoice_id": cap.get("invoice_id", ""), "custom_field": cap.get("custom_id", ""),
        }})
    return {"transaction_details": details, "account_number": "FAKEMERCHANT1", "page": 1,
            "total_items": len(details), "total_pages": 1,
            "last_refreshed_datetime": _now_iso()}


def stats() -> dict:
    return {"orders": len(ORDERS), "captures": len(CAPTURES), "refunds": len(REFUNDS),
            "batches": len(BATCHES), "webhooks": len(SENT_WEBHOOKS), "since": time.time()}
