"""Dispatch verified PayPal webhook events into the payment state machine. Every handler is idempotent."""

from __future__ import annotations

import json

from .. import db
from ..paypal.client import PayPalError, client
from . import funding

HANDLED = (
    "CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED",
    "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTSBATCH.DENIED",
    "PAYMENT.PAYOUTSBATCH.PROCESSING", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED", "PAYMENT.PAYOUTS-ITEM.UNCLAIMED",
    "PAYMENT.PAYOUTS-ITEM.RETURNED", "PAYMENT.PAYOUTS-ITEM.FAILED", "PAYMENT.PAYOUTS-ITEM.BLOCKED",
    "PAYMENT.PAYOUTS-ITEM.DENIED", "PAYMENT.PAYOUTS-ITEM.HELD", "PAYMENT.PAYOUTS-ITEM.REFUNDED",
    "PAYMENT.PAYOUTS-ITEM.CANCELED", "VAULT.PAYMENT-TOKEN.CREATED",
)


def record(event: dict, verified: bool, method: str, note: str) -> bool:
    """Store the event. Returns False if this event id was already processed."""
    existing = db.one("SELECT processed FROM webhook_events WHERE id = ?", (event.get("id", ""),))
    if existing and existing["processed"]:
        return False
    if not existing:
        db.insert("webhook_events", {"id": event.get("id", db.new_id("WH")), "event_type": event.get("event_type", ""),
                                     "received_at": db.now(), "verified": 1 if verified else 0, "method": method,
                                     "note": note, "payload": json.dumps(event)[:20000]})
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
        pending = db.one("SELECT COUNT(*) AS n FROM contributions WHERE order_id = ? AND status IN ('CREATED','CANCELLED')",
                         (oid,))
        if oid and pending and pending["n"]:
            outcome = (await funding.record_capture(await client().get_order(oid)))["status"]
        else:
            outcome = "already recorded"
    elif etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
        oid = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id", "")
        n = db.run("UPDATE contributions SET status = 'DENIED' WHERE order_id = ? AND status = 'CREATED'", (oid,))
        db.run("UPDATE orders SET status = ? WHERE order_id = ?", (res.get("status", "DECLINED"), oid))
        outcome = f"{n} contribution(s) denied"
    elif etype == "PAYMENT.CAPTURE.REFUNDED":
        outcome = "refund confirmed"
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
