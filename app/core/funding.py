"""Pooled funding, captures, payouts to billers, refunds. The payment state machine lives here.

Money path: sponsor pays the platform with Orders v2 (one order may cover many bills); when a
bill's captured total reaches its amount, one PayPal payout goes to the biller's verified account.
Money never goes to the poster.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .. import db
from ..paypal.client import PayPalError, client, first_capture
from . import events, notify, states
from .money import convert, exponent, fmt, to_minor, to_value

log = logging.getLogger("billkind.funding")
RESERVATION_SECONDS = 15 * 60
PENDING_HOLD_SECONDS = 30 * 86400  # a capture PayPal holds as PENDING keeps its reservation this long
# UNCLAIMED is not final: the receiver can still claim it for 30 days, so it counts as live until it is RETURNED.
TERMINAL_PAYOUT = {"SUCCESS", "FAILED", "RETURNED", "BLOCKED", "DENIED", "REFUNDED", "REVERSED"}
FAILED_PAYOUT = TERMINAL_PAYOUT - {"SUCCESS"}
DEAD_PAYOUT_SQL = "('FAILED','RETURNED','BLOCKED','DENIED','REFUNDED','REVERSED')"

_locks: dict[int, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lk = _locks.get(id(loop))
    if lk is None:
        lk = _locks[id(loop)] = asyncio.Lock()
    return lk


class FundingError(Exception):
    pass


def _cat(bill: dict) -> str:
    return bill["category"].replace("_", " ")


# Reads ---------------------------------------------------------------------------------

def get_bill(bill_id: str) -> dict:
    b = db.one("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if not b:
        raise FundingError("unknown bill")
    return b


def captured_net(bill_id: str) -> int:
    r = db.one("SELECT COALESCE(SUM(amount_minor - refunded_minor), 0) AS n FROM contributions "
               "WHERE bill_id = ? AND status IN ('CAPTURED', 'PARTIALLY_REFUNDED')", (bill_id,))
    return int(r["n"])


def reserved(bill_id: str, exclude_group: str = "") -> int:
    r = db.one("SELECT COALESCE(SUM(amount_minor), 0) AS n FROM contributions WHERE bill_id = ? "
               "AND status = 'CREATED' AND expires_at > ? AND group_id != ?", (bill_id, db.now(), exclude_group))
    return int(r["n"])


def remaining(bill: dict) -> int:
    return max(0, bill["settle_minor"] - captured_net(bill["id"]) - reserved(bill["id"]))


def min_contribution(currency: str) -> int:
    return 100 if exponent(currency) == 0 else 100  # 100 JPY or 1.00 of a two-decimal currency


# State changes ---------------------------------------------------------------------------

def set_status(bill: dict, new: str, **fields) -> dict:
    states.check_bill(bill["status"], new)
    fields["status"] = new
    fields["updated_at"] = db.now()
    db.update("bills", "id", bill["id"], **fields)
    bill = get_bill(bill["id"])
    return bill


def set_contribution(c: dict, new: str, **fields) -> None:
    states.check_contribution(c["status"], new)
    db.update("contributions", "id", c["id"], status=new, **fields)


# Paying ----------------------------------------------------------------------------------

async def start_payment(sponsor: dict, picks: list[tuple[str, int]], *, source: str = "swipe",
                        vault_id: str | None = None) -> dict:
    """Reserve amounts on one or more bills and create one PayPal order for them.

    picks: [(bill_id, amount_minor in the bill's settlement currency)].
    Returns {'order_id', 'status', 'approve_url', 'group_id'}.
    """
    if not picks:
        raise FundingError("nothing to pay")
    if len({bid for bid, _ in picks}) != len(picks):  # each pick is checked against what the bill still needs
        raise FundingError("each bill can appear only once in a payment")
    group = db.new_id("GRP")
    async with _lock():
        if vault_id is None:
            # One open checkout per sponsor: a new one releases the holds of any earlier checkout still waiting for
            # approval, so one account cannot keep every bill reserved (and out of every deck) by never approving.
            # An earlier order approved later still counts: record_capture accepts released rows, and any excess
            # is refunded.
            db.run("UPDATE contributions SET status = 'CANCELLED' WHERE sponsor_id = ? AND status = 'CREATED' AND "
                   "order_id != '' AND capture_id = '' AND expires_at > ? AND source NOT IN ('agent','recurring')",
                   (sponsor["id"], db.now()))
        bills = [get_bill(bid) for bid, _ in picks]
        currencies = {b["settle_currency"] for b in bills}
        if len(currencies) != 1:
            raise FundingError("one payment can only cover bills settling in the same currency")
        currency = currencies.pop()
        rows = []
        for bill, (_, amount) in zip(bills, picks):
            if bill["status"] not in states.OPEN_FOR_FUNDING:
                raise FundingError(f"bill {bill['id']} is not open for funding")
            if bill["poster_id"] == sponsor["id"]:
                raise FundingError("you cannot sponsor your own bill")
            left = remaining(bill)
            if amount <= 0 or amount > left:
                raise FundingError(f"bill {bill['id']} needs at most {fmt(left, currency)}")
            if amount < min(min_contribution(currency), left):
                raise FundingError(f"the smallest contribution is {fmt(min_contribution(currency), currency)}")
            rows.append({"id": db.new_id("CON"), "bill_id": bill["id"], "sponsor_id": sponsor["id"],
                         "amount_minor": amount, "currency": currency, "status": "CREATED", "source": source,
                         "group_id": group, "expires_at": db.now() + RESERVATION_SECONDS, "created_at": db.now()})
        for r in rows:
            db.insert("contributions", r)
    total = sum(r["amount_minor"] for r in rows)
    items = [{"name": f"{b['category'].title()} bill {b['id']} ({b['city'] or b['country']})",
              "sku": b["id"], "value": to_value(r["amount_minor"], currency),
              "description": f"Paid to {b['biller_name']}, never to the person"} for b, r in zip(bills, rows)]
    desc = (f"Billkind: {len(rows)} bills paid to their billers" if len(rows) > 1
            else f"Billkind: {bills[0]['category']} bill {bills[0]['id']} paid to {bills[0]['biller_name']}")
    async def create():
        return await client().create_order(currency=currency, items=items, total=to_value(total, currency),
                                           custom_id=group, invoice_id=group, description=desc,
                                           vault_id=vault_id, request_id=group)
    try:
        try:
            order = await create()
        except PayPalError as exc:
            if exc.status < 500:
                raise
            # An unclear failure (a vaulted order is charged on creation): ask again with the same PayPal-Request-Id,
            # which returns the original order instead of charging twice.
            order = await create()
    except PayPalError:
        for r in rows:
            db.update("contributions", "id", r["id"], status="CANCELLED")
        raise
    oid = order["id"]
    db.run("UPDATE contributions SET order_id = ? WHERE group_id = ?", (oid, group))
    db.insert("orders", {"order_id": oid, "sponsor_id": sponsor["id"], "kind": source, "currency": currency,
                         "total_minor": total, "status": order.get("status", ""), "group_id": group,
                         "created_at": db.now()})
    from ..paypal.client import approve_link
    result = {"order_id": oid, "status": order.get("status"), "approve_url": approve_link(order),
              "group_id": group, "currency": currency, "total_minor": total}
    if order.get("status") == "COMPLETED":  # vaulted payment, captured on creation
        await record_capture(order)
    return result


async def capture(order_id: str, sponsor: dict) -> dict:
    o = db.one("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    if not o or o["sponsor_id"] != sponsor["id"]:
        raise FundingError("unknown order")
    if o["status"] == "COMPLETED":  # already captured (for example by the CHECKOUT.ORDER.APPROVED webhook)
        rows = db.q("SELECT c.capture_id, b.id, b.status FROM contributions c JOIN bills b ON b.id = c.bill_id "
                    "WHERE c.order_id = ?", (order_id,))
        return {"status": "COMPLETED", "already": True, "capture_id": rows[0]["capture_id"] if rows else "",
                "funded": [r["id"] for r in rows if r["status"] in ("FUNDED", "PAYOUT_SENT", "PAID")]}
    try:
        resp = await client().capture_order(order_id)
    except PayPalError as exc:
        if exc.issue == "ORDER_ALREADY_CAPTURED":
            resp = await client().get_order(order_id)
        else:
            raise
    return await record_capture(resp)


def order_outcome(order_id: str, sponsor: dict) -> dict:
    """What Billkind has recorded for one of this sponsor's orders, for the browser to confirm against."""
    o = db.one("SELECT * FROM orders WHERE order_id = ? AND sponsor_id = ?", (order_id, sponsor["id"]))
    if not o:
        return {"status": "UNKNOWN"}
    rows = db.q("SELECT c.capture_id, c.status AS cs, b.id, b.status FROM contributions c JOIN bills b ON b.id = c.bill_id "
                "WHERE c.order_id = ?", (order_id,))
    captured = [r for r in rows if r["cs"] in ("CAPTURED", "PARTIALLY_REFUNDED", "REFUNDED")]
    return {"status": "COMPLETED" if captured else o["status"], "capture_id": captured[0]["capture_id"] if captured else "",
            "funded": [r["id"] for r in captured if r["status"] in ("FUNDED", "PAYOUT_SENT", "PAID")]}


async def record_capture(order: dict) -> dict:
    """Apply a captured order (from the capture response, a vault order, or a webhook). Idempotent."""
    oid = order["id"]
    cap = first_capture(order)
    if not cap:
        return {"status": order.get("status", "")}
    payer = (order.get("payer") or {}).get("email_address", "")
    funded: list[dict] = []
    async with _lock():
        contribs = db.q("SELECT * FROM contributions WHERE order_id = ?", (oid,))
        if cap["status"] == "PENDING":
            # PayPal is holding the money (review, eCheck, a currency the account must accept by hand). It is not
            # received yet: keep the reservation so nobody overfunds the bill, but fund nothing and pay out nothing.
            # PAYMENT.CAPTURE.COMPLETED or DENIED settles it later.
            for c in contribs:
                if c["status"] in ("CREATED", "CANCELLED"):
                    if c["status"] == "CANCELLED":
                        set_contribution(c, "CREATED")
                    db.update("contributions", "id", c["id"], capture_id=cap["id"], payer_email=payer,
                              expires_at=db.now() + PENDING_HOLD_SECONDS)
            db.update("orders", "order_id", oid, status="PENDING")
            return {"status": "PENDING", "capture_id": cap["id"], "funded": []}
        if cap["status"] == "COMPLETED":
            for c in contribs:
                if c["status"] in ("CREATED", "CANCELLED"):
                    set_contribution(c, "CAPTURED", capture_id=cap["id"], payer_email=payer,
                                     captured_at=db.now())
                    bill = get_bill(c["bill_id"])
                    events.publish("funded", f"{fmt(c['amount_minor'], c['currency'])} toward a "
                                   f"{_cat(bill)} bill in {bill['city'] or bill['country']}",
                                   bill=bill, amount_minor=c["amount_minor"], currency=c["currency"],
                                   ref=c["id"])
            db.update("orders", "order_id", oid, status="COMPLETED", captured_at=db.now())
            for bill_id in dict.fromkeys(c["bill_id"] for c in contribs):
                before = get_bill(bill_id)["status"]
                b = await _after_change(bill_id)
                if b and b["status"] == "FUNDED" and before != "FUNDED":  # only the call that funded it pays out
                    funded.append(b)
        else:
            for c in contribs:
                if c["status"] == "CREATED":
                    set_contribution(c, "DENIED")
            db.update("orders", "order_id", oid, status=cap["status"])
            return {"status": cap["status"], "capture_id": cap["id"]}
    if funded:
        try:
            await send_payouts(funded)
        except Exception:  # the capture stands; the automation loop retries funded bills that have no payout
            log.exception("payout after capture %s failed", oid)
    funded_ids = [r["id"] for r in db.q("SELECT DISTINCT b.id, b.status FROM contributions c JOIN bills b ON b.id = c.bill_id "
                                        "WHERE c.order_id = ?", (oid,)) if r["status"] in ("FUNDED", "PAYOUT_SENT", "PAID")]
    return {"status": "COMPLETED", "capture_id": cap["id"], "funded": funded_ids}


async def _after_change(bill_id: str) -> dict | None:
    """Recompute a bill after a capture. Refunds overfunding. Caller holds the lock."""
    bill = get_bill(bill_id)
    net = captured_net(bill_id)
    db.update("bills", "id", bill_id, funded_minor=net)
    if bill["status"] == "LISTED" and 0 < net < bill["settle_minor"]:
        bill = set_status(bill, "FUNDING")
    if bill["status"] in ("FUNDED", "PAYOUT_SENT", "PAID", "PAYOUT_FAILED"):
        if net > bill["settle_minor"]:  # a late capture after the bill was covered
            await _refund_excess_or_notice(bill, net - bill["settle_minor"])
        return get_bill(bill_id)
    if bill["status"] in ("WITHDRAWN", "EXPIRED", "REJECTED"):
        if net > 0:  # money arrived after the bill closed: return it
            await refund_all(bill, "The bill closed before your payment arrived, so it was returned in full.")
        return get_bill(bill_id)
    if net >= bill["settle_minor"] and bill["status"] in states.OPEN_FOR_FUNDING:
        excess = net - bill["settle_minor"]
        # Funded first: a refund PayPal refuses must not leave a covered bill open for more payments.
        bill = set_status(bill, "FUNDED", funded_minor=bill["settle_minor"])
        events.publish("fully_funded", f"A {_cat(bill)} bill in {bill['city'] or bill['country']} "
                       f"is fully funded", bill=bill, amount_minor=bill["settle_minor"],
                       currency=bill["settle_currency"])
        if excess > 0:
            await _refund_excess_or_notice(bill, excess)
            bill = get_bill(bill_id)
    return bill


async def _refund_excess_or_notice(bill: dict, excess: int) -> None:
    try:
        await _refund_excess(bill, excess)
    except PayPalError as exc:
        log.warning("excess refund on %s failed: %s", bill["id"], exc)
        _admin_notice(f"Refund the extra on bill {bill['id']}",
                      f"Bill {bill['id']} received {fmt(excess, bill['settle_currency'])} more than it needs and "
                      f"PayPal refused the automatic refund ({exc}). Refund it from the PayPal dashboard.")


async def _refund_excess(bill: dict, excess: int) -> None:
    latest = db.q("SELECT * FROM contributions WHERE bill_id = ? AND status IN ('CAPTURED','PARTIALLY_REFUNDED') "
                  "ORDER BY captured_at DESC", (bill["id"],))
    for c in latest:
        if excess <= 0:
            break
        take = min(excess, c["amount_minor"] - c["refunded_minor"])
        if take > 0:
            await refund_contribution(c, take, "This bill was already covered by other sponsors, so the extra "
                                      "part of your payment was returned.")
            excess -= take


# Refunds ---------------------------------------------------------------------------------

async def refund_contribution(c: dict, amount_minor: int | None, reason: str) -> None:
    c = db.one("SELECT * FROM contributions WHERE id = ?", (c["id"],))
    open_amount = c["amount_minor"] - c["refunded_minor"]
    amount = open_amount if amount_minor is None else min(amount_minor, open_amount)
    if amount <= 0 or c["status"] not in ("CAPTURED", "PARTIALLY_REFUNDED"):
        return
    shared = db.one("SELECT COUNT(*) AS n FROM contributions WHERE capture_id = ?", (c["capture_id"],))["n"] > 1
    full = amount == c["amount_minor"] and c["refunded_minor"] == 0 and not shared
    await client().refund_capture(c["capture_id"], amount=None if full else to_value(amount, c["currency"]),
                                  currency=c["currency"], invoice_id=f"{c['id']}-R{c['refunded_minor']}",
                                  note=reason, request_id=f"refund-{c['id']}-{c['refunded_minor']}-{amount}")
    new_refunded = c["refunded_minor"] + amount
    new_status = "REFUNDED" if new_refunded >= c["amount_minor"] else "PARTIALLY_REFUNDED"
    set_contribution(c, new_status, refunded_minor=new_refunded)
    bill = get_bill(c["bill_id"])
    db.update("bills", "id", bill["id"], funded_minor=captured_net(bill["id"]))
    notify.send(c["sponsor_id"], "refund", f"Refund of {fmt(amount, c['currency'])} is on its way",
                f"{reason}\n\nBill {bill['id']} ({_cat(bill)}, {bill['city'] or bill['country']}). "
                f"PayPal capture {c['capture_id']}. Refunds usually show in your PayPal account within minutes "
                f"in the sandbox.", bill_id=bill["id"], contribution_id=c["id"])
    events.publish("refund", f"Refunded {fmt(amount, c['currency'])}", bill=bill, amount_minor=amount,
                   currency=c["currency"], public=False, audience=[c["sponsor_id"]], ref=c["id"])


def _capture_of(resource: dict) -> str:
    """The capture a refund or reversal resource belongs to (its rel=up link), or the resource itself."""
    for link in resource.get("links") or []:
        if link.get("rel") == "up" and "/captures/" in link.get("href", ""):
            return link["href"].rstrip("/").split("/")[-1]
    return resource.get("id", "")


async def apply_external_refund(resource: dict, kind: str = "refund") -> str:
    """A refund or reversal that Billkind did not make (PayPal dashboard, dispute, chargeback). Record it so the
    bill's funded total reflects money the platform no longer holds, and tell the admins."""
    if str(resource.get("invoice_id", "")).startswith("CON-"):
        return "own refund"  # Billkind's own refunds carry the contribution id; they are recorded when made
    ref = resource.get("id", "")
    if ref and db.kv_get(f"extrefund.{ref}"):
        return "already recorded"
    cid = _capture_of(resource)
    amt = resource.get("amount") or {}
    async with _lock():
        rows = db.q("SELECT * FROM contributions WHERE capture_id = ? AND status IN ('CAPTURED','PARTIALLY_REFUNDED') "
                    "ORDER BY created_at", (cid,)) if cid else []
        if not rows:
            return "no matching contribution"
        try:
            left = to_minor(amt["value"], amt["currency_code"]) if amt.get("value") else \
                sum(r["amount_minor"] - r["refunded_minor"] for r in rows)
        except (KeyError, ValueError):
            left = sum(r["amount_minor"] - r["refunded_minor"] for r in rows)
        if ref:
            db.kv_set(f"extrefund.{ref}", "1")
        touched = []
        for c in rows:
            take = min(left, c["amount_minor"] - c["refunded_minor"])
            if take <= 0:
                continue
            new_refunded = c["refunded_minor"] + take
            set_contribution(c, "REFUNDED" if new_refunded >= c["amount_minor"] else "PARTIALLY_REFUNDED",
                             refunded_minor=new_refunded)
            left -= take
            touched.append(c["bill_id"])
        for bill_id in dict.fromkeys(touched):
            db.update("bills", "id", bill_id, funded_minor=captured_net(bill_id))
    for bill_id in dict.fromkeys(touched):
        b = get_bill(bill_id)
        _admin_notice(f"Outside {kind} on bill {bill_id}",
                      f"PayPal reports a {kind} on capture {cid} that Billkind did not make. Bill {bill_id} is "
                      f"{b['status']} with {fmt(b['funded_minor'], b['settle_currency'])} of "
                      f"{fmt(b['settle_minor'], b['settle_currency'])} still held. Check it before any payout or refund.")
    return f"{kind} recorded on {len(touched)} bill(s)"


async def refund_all(bill: dict, reason: str) -> int:
    n = 0
    for c in db.q("SELECT * FROM contributions WHERE bill_id = ? AND status IN ('CAPTURED','PARTIALLY_REFUNDED')",
                  (bill["id"],)):
        await refund_contribution(c, None, reason)
        n += 1
    for c in db.q("SELECT * FROM contributions WHERE bill_id = ? AND status = 'CREATED'", (bill["id"],)):
        set_contribution(c, "CANCELLED")
    db.update("bills", "id", bill["id"], funded_minor=captured_net(bill["id"]))
    return n


async def withdraw(bill_id: str, reason: str, by_admin: bool = False) -> dict:
    async with _lock():
        bill = get_bill(bill_id)
        if bill["status"] in ("PAYOUT_SENT", "PAID"):
            raise FundingError("this bill is already being paid to the biller and cannot be withdrawn")
        # A payout request may be in flight (the bill turns PAYOUT_SENT only once PayPal answers), unconfirmed, or
        # unclaimed but still claimable. Refunding then would pay the bill twice.
        live = live_payout(bill_id)
        if live and live["status"] == "UNCLAIMED":
            await cancel_unclaimed(live)
        elif live:
            raise FundingError("this bill is already being paid to the biller and cannot be withdrawn")
        n = await refund_all(bill, "The bill you supported was withdrawn before it was paid, so your money "
                             "was returned in full.")
        bill = set_status(get_bill(bill_id), "WITHDRAWN", reject_reason=reason)
    notify.send(bill["poster_id"], "notice", f"Bill {bill_id} withdrawn",
                f"{reason}\n\n{n} sponsor payment(s) were refunded in full.", bill_id=bill_id)
    events.publish("withdrawn", f"A {_cat(bill)} bill was withdrawn and its sponsors refunded",
                   bill=bill, public=False, audience=[bill["poster_id"]])
    return bill


async def expire_overdue(today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    done = []
    for bill in db.q("SELECT * FROM bills WHERE status IN ('LISTED','FUNDING') AND due_date < ?",
                     (today.isoformat(),)):
        async with _lock():
            await refund_all(bill, "The bill you supported reached its due date before it was fully funded, "
                             "so your money was returned in full.")
            set_status(get_bill(bill["id"]), "EXPIRED")
        notify.send(bill["poster_id"], "notice", f"Bill {bill['id']} expired",
                    "Your bill reached its due date before it was fully funded. Any sponsor payments were "
                    "refunded. You can post the next bill when it arrives.", bill_id=bill["id"])
        done.append(bill["id"])
    return done


def expire_reservations() -> int:
    return db.run("UPDATE contributions SET status = 'CANCELLED' WHERE status = 'CREATED' AND expires_at < ?",
                  (db.now(),))


# Payouts ---------------------------------------------------------------------------------

def _note(bill: dict) -> str:
    return (f"Billkind bill {bill['id']}: please apply to account {bill['account_private'] or bill['account_masked']} "
            f"({_cat(bill)}, due {bill['due_date']}). Paid on behalf of your customer by Billkind sponsors.")


CURRENCY_ISSUES = {"INSUFFICIENT_FUNDS", "CURRENCY_NOT_SUPPORTED", "CURRENCY_NOT_SUPPORTED_FOR_RECEIVER",
                   "RECEIVER_UNCONFIRMED_CURRENCY", "CURRENCY_COMPLIANCE"}
PAYOUT_SUBJECT = "You received a bill payment from Billkind"
PAYOUT_MESSAGE = "A Billkind sponsor paid a customer's bill. The account reference is in the payment note."
UNKNOWN_RETRY_AFTER = 60       # seconds before an unconfirmed payout batch is sent again (same sender_batch_id)
CLAIM_STALE_AFTER = 300        # a claim this old never got an answer at all (for example the process restarted)


def live_payout(bill_id: str) -> dict | None:
    """The bill's payout item that may still deliver money (claimed, unknown, pending, unclaimed or paid), if any."""
    return db.one(f"SELECT * FROM payout_items WHERE bill_id = ? AND status NOT IN {DEAD_PAYOUT_SQL} "
                  "ORDER BY created_at DESC", (bill_id,))


def _payout_item(row: dict, bill: dict) -> dict:
    return {"receiver": row["receiver"], "value": to_value(row["amount_minor"], row["currency"]),
            "currency": row["currency"], "note": _note(bill), "sender_item_id": row["sender_item_id"]}


def _is_duplicate(exc: PayPalError) -> bool:
    text = str(exc.body).upper()
    return "DUPLICATE" in text or "ALREADY EXISTS" in text


async def send_payouts(bills: list[dict], force_usd: bool = False) -> str | None:
    """One payout batch, one item per funded bill, each to the bill's verified biller.

    Items go out in the bill's settlement currency. If PayPal refuses the batch for a currency reason (for example
    the platform account holds no balance in that currency), the batch is retried once with those items in USD at
    the indicative rate, and the bill records why.

    The sender_batch_id is chosen before the call and stored with the claimed items, so a call whose outcome is
    unclear (a timeout, a 5xx, a crash) is sent again with the same id and can never pay a biller twice.
    """
    items, ready = [], []
    sender_batch_id = db.new_id("BATCH")
    async with _lock():
        # Claim each bill under the lock by writing its payout item first, so a webhook and a browser capture that
        # finish together can never pay the same bill twice.
        for bill in bills:
            bill = get_bill(bill["id"])
            if bill["status"] not in ("FUNDED", "PAYOUT_FAILED"):
                continue
            if live_payout(bill["id"]):
                continue
            biller = db.one("SELECT * FROM billers WHERE id = ?", (bill["biller_id"],))
            if not biller or not biller["verified"]:
                if bill["status"] != "PAYOUT_FAILED":
                    set_status(bill, "PAYOUT_FAILED", reject_reason="biller is not verified")
                continue
            attempt = db.one("SELECT COUNT(*) AS n FROM payout_items WHERE bill_id = ?", (bill["id"],))["n"]
            sid = f"{bill['id']}-P{attempt + 1}"
            cur, amount = bill["settle_currency"], bill["settle_minor"]
            if force_usd and cur != "USD":
                amount, cur = convert(amount, cur, "USD"), "USD"
            row = {"sender_item_id": sid, "batch_id": "", "bill_id": bill["id"], "amount_minor": amount,
                   "currency": cur, "receiver": biller["paypal_email"], "status": "CLAIMED",
                   "sender_batch_id": sender_batch_id, "created_at": db.now(), "updated_at": db.now()}
            db.insert("payout_items", row)
            items.append(_payout_item(row, bill))
            ready.append((bill, biller, sid))
    if not items:
        return None
    try:
        resp = await client().create_payout(sender_batch_id=sender_batch_id, items=items, subject=PAYOUT_SUBJECT,
                                            message=PAYOUT_MESSAGE, request_id=sender_batch_id)
    except PayPalError as exc:
        if exc.status >= 500 or exc.status == 0:
            await _payout_unknown([sid for _, _, sid in ready], sender_batch_id, str(exc))
            return None
        reason = exc.issue or (exc.body.get("name") if isinstance(exc.body, dict) else "")
        for _, _, sid in ready:  # PayPal refused the batch outright: release the claims; a retry writes new items
            db.update("payout_items", "sender_item_id", sid, status="FAILED", updated_at=db.now())
        if not force_usd and reason in CURRENCY_ISSUES and any(it["currency"] != "USD" for it in items):
            for bill, _, _ in ready:
                db.update("bills", "id", bill["id"], fx_note=(bill["fx_note"] + " " if bill["fx_note"] else "") +
                          f"Paid out in USD because PayPal refused a {bill['settle_currency']} payout ({reason}).")
            return await send_payouts([b for b, _, _ in ready], force_usd=True)
        for bill, _, _ in ready:
            _set_status_or_notice(get_bill(bill["id"]), "PAYOUT_FAILED", reject_reason=str(exc))
        _admin_notice("Payout batch failed", str(exc))
        return None
    except Exception as exc:  # timeout, connection reset: PayPal may or may not have created the batch
        await _payout_unknown([sid for _, _, sid in ready], sender_batch_id, f"{type(exc).__name__}: {exc}")
        return None
    return _payout_accepted(resp, sender_batch_id)


def _set_status_or_notice(bill: dict, new: str, **fields) -> dict:
    """Payout bookkeeping must never stop half way: an unexpected bill state is reported to the admins instead."""
    try:
        return set_status(bill, new, **fields)
    except states.IllegalTransition as exc:
        log.error("bill %s: %s", bill["id"], exc)
        _admin_notice(f"Check bill {bill['id']}",
                      f"A payout update wanted to move bill {bill['id']} from {bill['status']} to {new}, which is not "
                      f"allowed. Check the payout items for this bill in PayPal before changing anything.")
        return bill


def _payout_accepted(resp: dict, sender_batch_id: str) -> str:
    batch_id = resp["batch_header"]["payout_batch_id"]
    db.run("INSERT OR IGNORE INTO payout_batches (batch_id, sender_batch_id, status, created_at, updated_at) "
           "VALUES (?, ?, ?, ?, ?)", (batch_id, sender_batch_id, resp["batch_header"].get("batch_status", "PENDING"),
                                      db.now(), db.now()))
    for row in db.q("SELECT * FROM payout_items WHERE sender_batch_id = ? AND status IN ('CLAIMED','UNKNOWN')",
                    (sender_batch_id,)):
        db.update("payout_items", "sender_item_id", row["sender_item_id"], batch_id=batch_id, status="PENDING",
                  updated_at=db.now())
        bill = get_bill(row["bill_id"])
        if bill["status"] in ("FUNDED", "PAYOUT_FAILED"):
            bill = _set_status_or_notice(bill, "PAYOUT_SENT", payout_batch_id=batch_id)
        else:
            db.update("bills", "id", bill["id"], payout_batch_id=batch_id)
        biller = db.one("SELECT name FROM billers WHERE id = ?", (bill["biller_id"],))
        events.publish("payout_sent", f"Paying {biller['name'] if biller else bill['biller_name']} for a "
                       f"{_cat(bill)} bill in {bill['city'] or bill['country']}", bill=bill,
                       amount_minor=bill["settle_minor"], currency=bill["settle_currency"], ref=row["sender_item_id"])
    return batch_id


async def _payout_unknown(sids: list[str], sender_batch_id: str, why: str) -> None:
    """The payout call ended without a clear answer. Keep the claim, and hold the bill as being paid so it can be
    neither withdrawn nor paid again until the same batch is resent (see resolve_unknown_payouts)."""
    async with _lock():
        for sid in sids:
            row = db.one("SELECT * FROM payout_items WHERE sender_item_id = ?", (sid,))
            if not row or row["status"] not in ("CLAIMED", "UNKNOWN"):
                continue  # a webhook already reported the real outcome
            db.update("payout_items", "sender_item_id", sid, status="UNKNOWN", updated_at=db.now())
            bill = get_bill(row["bill_id"])
            if bill["status"] in ("FUNDED", "PAYOUT_FAILED"):
                _set_status_or_notice(bill, "PAYOUT_SENT")
    log.warning("payout batch %s outcome unknown: %s", sender_batch_id, why)
    _admin_notice("Payout not confirmed yet",
                  f"PayPal did not confirm payout batch {sender_batch_id} ({why[:200]}). Billkind will send the same "
                  "batch again, which PayPal never pays twice. No action is needed unless this repeats.")


async def resolve_unknown_payouts(now: float | None = None) -> int:
    """Resend payout batches whose first call had no clear outcome, with the same sender_batch_id.

    PayPal either accepts it (the first call never arrived), replays its first answer, or refuses it as a duplicate.
    A duplicate with no replay is left for a person to check: Billkind never pays the biller a second time on a guess.
    """
    now = now or db.now()
    rows = db.q("SELECT * FROM payout_items WHERE (status = 'UNKNOWN' AND updated_at < ?) OR "
                "(status = 'CLAIMED' AND created_at < ?) ORDER BY created_at",
                (now - UNKNOWN_RETRY_AFTER, now - CLAIM_STALE_AFTER))
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["sender_batch_id"] or "", []).append(r)
    done = 0
    for sbid, group in groups.items():
        if not sbid:  # claimed by an older release that did not store the batch id: only a person can check
            await _payout_manual_check(group, "the payout batch id was not recorded")
            continue
        items = [_payout_item(r, get_bill(r["bill_id"])) for r in group]
        try:
            resp = await client().create_payout(sender_batch_id=sbid, items=items, subject=PAYOUT_SUBJECT,
                                                message=PAYOUT_MESSAGE, request_id=sbid)
        except PayPalError as exc:
            if exc.status >= 500 or exc.status == 0:
                await _payout_unknown([r["sender_item_id"] for r in group], sbid, str(exc))
            elif _is_duplicate(exc):
                await _payout_manual_check(group, f"PayPal already has batch {sbid}")
            else:  # a clean refusal: the batch was never created
                async with _lock():
                    for r in group:
                        db.update("payout_items", "sender_item_id", r["sender_item_id"], status="FAILED",
                                  updated_at=db.now())
                        bill = get_bill(r["bill_id"])
                        if bill["status"] in ("FUNDED", "PAYOUT_SENT"):
                            _set_status_or_notice(bill, "PAYOUT_FAILED", reject_reason=str(exc))
                _admin_notice("Payout batch failed", str(exc))
            continue
        except Exception as exc:
            await _payout_unknown([r["sender_item_id"] for r in group], sbid, f"{type(exc).__name__}: {exc}")
            continue
        _payout_accepted(resp, sbid)
        done += 1
    return done


async def _payout_manual_check(group: list[dict], why: str) -> None:
    async with _lock():
        for r in group:
            db.update("payout_items", "sender_item_id", r["sender_item_id"], status="MANUAL_CHECK",
                      updated_at=db.now())
            bill = get_bill(r["bill_id"])
            if bill["status"] in ("FUNDED", "PAYOUT_SENT"):
                _set_status_or_notice(bill, "PAYOUT_FAILED", reject_reason=f"payout needs a manual check: {why}")
    _admin_notice("Payout needs a manual check",
                  f"Billkind could not confirm payout items {', '.join(r['sender_item_id'] for r in group)} ({why}). "
                  "Look them up in PayPal before retrying or refunding: retry and refund stay locked until then.")


async def apply_payout_item(resource: dict) -> str:
    """Apply a payout item status from a webhook resource or a batch GET item. Idempotent."""
    sid = (resource.get("payout_item") or {}).get("sender_item_id", "")
    status = resource.get("transaction_status", "")
    row = db.one("SELECT * FROM payout_items WHERE sender_item_id = ?", (sid,))
    if not row:
        return "unknown item"
    if row["status"] == "SUCCESS" and status in ("REFUNDED", "REVERSED", "RETURNED"):
        # Money that reached the biller came back (a refund or reversal of the payout). The bill stays as it was:
        # record the item's new state and tell the admins, who decide what happens next.
        db.update("payout_items", "sender_item_id", sid, status=status, updated_at=db.now())
        _admin_notice(f"Payout {status} after it was paid: bill {row['bill_id']}",
                      f"PayPal reports payout item {sid} for bill {row['bill_id']} as {status} after it was paid to "
                      f"the biller, so the biller no longer holds that money. Check the bill before doing anything.")
        return status
    if row["status"] == status or row["status"] in TERMINAL_PAYOUT:
        return "no change"
    db.update("payout_items", "sender_item_id", sid, status=status, updated_at=db.now(),
              batch_id=row["batch_id"] or resource.get("payout_batch_id", ""),
              payout_item_id=resource.get("payout_item_id", "") or row["payout_item_id"],
              transaction_id=resource.get("transaction_id", ""))
    if status not in TERMINAL_PAYOUT and status != "UNCLAIMED":
        return "pending"
    async with _lock():
        bill = get_bill(row["bill_id"])
        if status == "SUCCESS":
            if bill["status"] == "FUNDED":  # the claim never heard back, but the payout went through
                bill = set_status(bill, "PAYOUT_SENT")
            if bill["status"] not in ("PAYOUT_SENT", "PAYOUT_FAILED"):
                _admin_notice(f"Payout paid for bill {bill['id']} in state {bill['status']}",
                              f"PayPal reports payout item {sid} as paid to the biller, but bill {bill['id']} is "
                              f"{bill['status']}. Check whether its sponsors were also refunded.")
                return "bill not awaiting payout"
            # PAYOUT_FAILED here means an unclaimed payout was claimed later: it still counts.
            bill = set_status(bill, "PAID", paid_at=db.now(), payout_item_id=resource.get("payout_item_id", ""),
                              payout_txn_id=resource.get("transaction_id", ""))
        elif bill["status"] == "PAYOUT_SENT":
            bill = set_status(bill, "PAYOUT_FAILED", reject_reason=f"payout {status.lower()}")
        else:
            return "bill not awaiting payout"
    if status == "SUCCESS":
        _on_paid(bill)
    else:
        extra = (" An unclaimed payout stays claimable for 30 days; Retry or Refund cancels it first."
                 if status == "UNCLAIMED" else "")
        _admin_notice(f"Payout {status} for bill {bill['id']}",
                      f"The payout to the biller ended {status}. Retry after checking the biller account, "
                      f"or refund the sponsors.{extra}")
        events.publish("payout_failed", f"Payout for a {_cat(bill)} bill needs attention", bill=bill,
                       public=False, audience=_sponsors(bill["id"]) + [bill["poster_id"]])
    return status


async def cancel_unclaimed(row: dict) -> None:
    """Cancel an UNCLAIMED payout item so its money comes back before any retry or refund. Caller holds the lock."""
    if not row["payout_item_id"]:
        raise FundingError("the unclaimed payout has no PayPal item id yet; try again in a minute")
    try:
        resp = await client().cancel_payout_item(row["payout_item_id"])
    except PayPalError as exc:
        raise FundingError(f"PayPal could not cancel the unclaimed payout ({exc.issue or exc.status}), so nothing "
                           "else was changed") from exc
    if resp.get("transaction_status") != "RETURNED":
        raise FundingError("PayPal has not returned the unclaimed payout yet, so nothing else was changed")
    db.update("payout_items", "sender_item_id", row["sender_item_id"], status="RETURNED", updated_at=db.now())


def _sponsors(bill_id: str) -> list[str]:
    return [r["sponsor_id"] for r in db.q("SELECT DISTINCT sponsor_id FROM contributions WHERE bill_id = ? AND "
                                          "status IN ('CAPTURED','PARTIALLY_REFUNDED')", (bill_id,))]


def _admin_notice(subject: str, body: str) -> None:
    for a in db.q("SELECT id FROM users WHERE role = 'admin'"):
        notify.send(a["id"], "notice", subject, body)


def _on_paid(bill: dict) -> None:
    where = bill["city"] or bill["country"]
    for c in db.q("SELECT * FROM contributions WHERE bill_id = ? AND status IN ('CAPTURED','PARTIALLY_REFUNDED')",
                  (bill["id"],)):
        net = c["amount_minor"] - c["refunded_minor"]
        notify.send(c["sponsor_id"], "receipt", f"Receipt: {bill['biller_name']} was paid",
                    f"Your {fmt(net, c['currency'])} helped pay a {_cat(bill)} bill in {where}.\n"
                    f"The biller {bill['biller_name']} received {fmt(bill['settle_minor'], bill['settle_currency'])} "
                    f"for account {bill['account_masked']}.\nPayPal order {c['order_id']}, capture {c['capture_id']}, "
                    f"payout item {bill['payout_item_id']}.", bill_id=bill["id"], contribution_id=c["id"])
    notify.send(bill["poster_id"], "paid", f"Your {_cat(bill)} bill is paid",
                f"{bill['biller_name']} has received {fmt(bill['settle_minor'], bill['settle_currency'])} for your "
                f"account {bill['account_masked']}. Would you like to send your sponsors a thank-you?",
                bill_id=bill["id"])
    biller = db.one("SELECT * FROM billers WHERE id = ?", (bill["biller_id"],))
    if biller and biller["user_id"]:
        notify.send(biller["user_id"], "payout", f"Payment received for account {bill['account_masked']}",
                    f"{fmt(bill['settle_minor'], bill['settle_currency'])} for bill {bill['id']} "
                    f"({_cat(bill)}). Payout item {bill['payout_item_id']}.", bill_id=bill["id"])
    events.publish("paid", f"{bill['biller_name']} was paid: a {_cat(bill)} bill in {where} is cleared",
                   bill=bill, amount_minor=bill["settle_minor"], currency=bill["settle_currency"],
                   audience=_sponsors(bill["id"]) + [bill["poster_id"]], ref=bill["payout_item_id"])


async def poll_payouts() -> int:
    """Fallback when webhooks cannot reach us: read pending batches from PayPal."""
    n = 0
    for b in db.q("SELECT DISTINCT batch_id FROM payout_items WHERE batch_id != '' AND status NOT IN "
                  "('SUCCESS','FAILED','RETURNED','BLOCKED','DENIED','REFUNDED','REVERSED','CLAIMED','UNKNOWN',"
                  "'MANUAL_CHECK')"):
        try:
            data = await client().get_payout_batch(b["batch_id"])
        except PayPalError:
            continue
        for item in data.get("items", []):
            if (await apply_payout_item(item)) not in ("no change", "unknown item", "pending"):
                n += 1
    return n


async def retry_payout(bill_id: str) -> str | None:
    async with _lock():
        bill = get_bill(bill_id)
        if bill["status"] != "PAYOUT_FAILED":
            raise FundingError("only failed payouts can be retried")
        live = live_payout(bill_id)
        if live and live["status"] == "UNCLAIMED":
            await cancel_unclaimed(live)
        elif live:
            raise FundingError("a payout for this bill may still reach the biller, so it cannot be retried yet")
    last = db.one("SELECT currency FROM payout_items WHERE bill_id = ? ORDER BY created_at DESC", (bill_id,))
    # A failed payout in a foreign currency is retried in USD; a failed USD payout is retried as is.
    return await send_payouts([bill], force_usd=bool(last and last["currency"] != "USD"))


def thank(bill_id: str, poster_id: str, text: str) -> dict:
    bill = get_bill(bill_id)
    if bill["poster_id"] != poster_id:
        raise FundingError("not your bill")
    if bill["status"] != "PAID":
        raise FundingError("thank-you notes open once the biller is paid")
    if bill["thank_you"]:
        raise FundingError("a thank-you was already sent for this bill")
    db.update("bills", "id", bill_id, thank_you=text)
    for sid in _sponsors(bill_id):
        notify.send(sid, "thanks", "A thank-you from the household you helped", text, bill_id=bill_id)
    events.publish("thanks", f"Thank-you from {bill['city'] or bill['country']}: \"{text[:140]}\"", bill=bill,
                   audience=_sponsors(bill_id))
    return get_bill(bill_id)


async def heal_unpaid_funded(older_than: float = 60) -> int:
    """Funded bills with no live payout (for example a payout call that crashed) get their payout sent."""
    rows = db.q("SELECT * FROM bills WHERE status = 'FUNDED' AND updated_at < ? AND id NOT IN (SELECT bill_id FROM "
                f"payout_items WHERE status NOT IN {DEAD_PAYOUT_SQL})", (db.now() - older_than,))
    if rows:
        await send_payouts(rows)
    return len(rows)
