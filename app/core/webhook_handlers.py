"""Dispatch verified PayPal webhook events into the payment state machine. Every handler is idempotent."""

from __future__ import annotations

import json

from .. import db
from ..paypal.client import PayPalError, client
from . import funding

HANDLED = (
    "CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED",
    "PAYMENT.CAPTURE.PENDING", "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED",
    "PAYMENT.PAYOUTSBATCH.PROCESSING", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "PAYMENT.PAYOUTS-ITEM.UNCLAIMED",
    "PAYMENT.PAYOUTS-ITEM.RETURNED", "PAYMENT.PAYOUTS-ITEM.FAILED", "PAYMENT.PAYOUTS-ITEM.BLOCKED",
    "PAYMENT.PAYOUTS-ITEM.DENIED", "PAYMENT.PAYOUTS-ITEM.HELD", "PAYMENT.PAYOUTS-ITEM.REFUNDED",
    "PAYMENT.PAYOUTS-ITEM.CANCELED", "VAULT.PAYMENT-TOKEN.CREATED",
)


def _adopt_order(order_id: str, capture: dict) -> None:
    """A vaulted charge whose create call failed without a clear answer: PayPal completed it, but Billkind never
    learned the order id. The capture's custom_id is the contribution group, so link the order to it."""
    group = capture.get("custom_id", "")
    rows = db.q("SELECT * FROM contributions WHERE group_id = ? AND order_id = ''", (group,)) if group else []
    if not rows:
        return
    db.run("UPDATE contributions SET order_id = ? WHERE group_id = ? AND order_id = ''", (order_id, group))
    db.run("INSERT OR IGNORE INTO orders (order_id, sponsor_id, kind, currency, total_minor, status, group_id, "
           "created_at) VALUES (?, ?, ?, ?, ?, 'CREATED', ?, ?)",
           (order_id, rows[0]["sponsor_id"], rows[0]["source"], rows[0]["currency"],
            sum(r["amount_minor"] for r in rows), group, db.now()))


def record(event: dict, verified: bool, method: str, note: str) -> bool:
    """Store the event. Returns False if this event id was already processed."""
    existing = db.one("SELECT processed, verified FROM webhook_events WHERE id = ?", (event.get("id", ""),))
    if existing and existing["processed"]:
        return False
    # Only verified deliveries keep their payload: anyone can post to the endpoint, so a rejected one is logged as a
    # short line for the admin view, never stored whole.
    payload = json.dumps(event)[:20000] if verified else ""
    if not existing:
        db.insert("webhook_events", {"id": str(event.get("id") or db.new_id("WH"))[:80],
                                     "event_type": str(event.get("event_type", ""))[:80], "received_at": db.now(),
                                     "verified": 1 if verified else 0, "method": method, "note": note[:300],
                                     "payload": payload})
    elif verified and not existing["verified"]:  # the genuine delivery of an id a forged post used first
        db.run("UPDATE webhook_events SET verified = 1, method = ?, note = ?, payload = ? WHERE id = ?",
               (method, note[:300], payload, event.get("id", "")))
    return True


async def handle(event: dict) -> str:
    etype = event.get("event_type", "")
    res = event.get("resource") or {}
    outcome = "ignored"
    if etype == "CHECKOUT.ORDER.APPROVED":
        o = db.one("SELECT * FROM orders WHERE order_id = ?", (res.get("id", ""),))
        if o and o["status"] != "COMPLETED":
            try:  # the buyer approved but the browser never asked us to capture: capture now
                resp = await client().capture_order(o["order_id"])
                outcome = (await funding.record_capture(resp))["status"]
            except PayPalError as exc:
                outcome = "capture skipped: " + (exc.issue or str(exc))
    elif etype == "PAYMENT.CAPTURE.COMPLETED":
        oid = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", "")
        if oid and not db.one("SELECT 1 FROM contributions WHERE order_id = ?", (oid,)):
            _adopt_order(oid, res)
        pending = db.one("SELECT COUNT(*) AS n FROM contributions WHERE order_id = ? AND status IN ('CREATED','CANCELLED')",
                         (oid,))
        if oid and pending and pending["n"]:
            outcome = (await funding.record_capture(await client().get_order(oid)))["status"]
        else:
            outcome = "already recorded"
    elif etype == "PAYMENT.CAPTURE.PENDING":
        outcome = "capture pending"  # nothing counts until PAYMENT.CAPTURE.COMPLETED
    elif etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
        oid = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", "")
        # A capture held as PENDING keeps its contributions in CREATED with the capture id, so match on both.
        n = db.run("UPDATE contributions SET status = 'DENIED' WHERE status = 'CREATED' AND "
                   "(order_id = ? OR (capture_id != '' AND capture_id = ?))", (oid, res.get("id", "")))
        db.run("UPDATE orders SET status = ? WHERE order_id = ?", (res.get("status", "DECLINED"), oid))
        outcome = f"{n} contribution(s) denied"
    elif etype == "PAYMENT.CAPTURE.REFUNDED":
        outcome = await funding.apply_external_refund(res, "refund")
    elif etype == "PAYMENT.CAPTURE.REVERSED":
        outcome = await funding.apply_external_refund(res, "reversal")
    elif etype.startswith("PAYMENT.PAYOUTS-ITEM."):
        outcome = await funding.apply_payout_item(res)
    elif etype.startswith("PAYMENT.PAYOUTSBATCH."):
        bh = res.get("batch_header") or {}
        db.run("UPDATE payout_batches SET status = ?, updated_at = ? WHERE batch_id = ?",
               (bh.get("batch_status", ""), db.now(), bh.get("payout_batch_id", "")))
        outcome = "batch " + bh.get("batch_status", "")
    elif etype == "VAULT.PAYMENT-TOKEN.CREATED":
        outcome = "token noted"
    db.run("UPDATE webhook_events SET processed = 1, note = note || ' | ' || ? WHERE id = ?",
           (str(outcome), event.get("id", "")))
    return str(outcome)
