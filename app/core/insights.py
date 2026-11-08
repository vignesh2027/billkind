"""Data sources for the AG Studio "Impact studio" dashboards.

Every source is built on the server and scoped before it leaves: a sponsor gets only their own payments, the
bills those payments touched, and their own agent's decisions; the operations view (admin only) gets platform-wide
rows without personal data. No account numbers, names, phone numbers or poster emails are ever included.
Amounts are given in the original currency and as an indicative USD equivalent so widgets can sum across
currencies.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

from .. import db
from ..ai.intake import CATEGORY_LABELS
from . import states
from .money import USD_RATES, convert, exponent

COUNTRY_NAMES = None  # filled lazily from the web views table to avoid an import cycle


def _country(code: str) -> str:
    global COUNTRY_NAMES
    if COUNTRY_NAMES is None:
        from ..web.views import COUNTRIES
        COUNTRY_NAMES = {k: v[0] for k, v in COUNTRIES.items()}
    return COUNTRY_NAMES.get(code or "", code or "Unknown")


def _num(minor: int, currency: str) -> float:
    return float(Decimal(int(minor or 0)) / (Decimal(10) ** exponent(currency)))


def _usd(minor: int, currency: str) -> float:
    if not minor or currency not in USD_RATES:
        return 0.0
    return round(convert(int(minor), currency, "USD") / 100, 2)


def _date(ts: float | None) -> str | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()


def _days(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return round((b - a) / 86400, 2)


def bill_row(b: dict) -> dict:
    return {
        "bill_id": b["id"],
        "category": CATEGORY_LABELS.get(b["category"], b["category"]),
        "country": _country(b["country"]),
        "country_code": b["country"],
        "city": b["city"] or "",
        "biller": b["biller_name"],
        "status": states.BILL_LABELS.get(b["status"], b["status"]),
        "due_date": b["due_date"] or None,
        "bill_amount": _num(b["amount_minor"], b["currency"]),
        "bill_currency": b["currency"],
        "settle_currency": b["settle_currency"],
        "bill_usd": _usd(b["settle_minor"], b["settle_currency"]),
        "listed_on": _date(b["listed_at"]),
        "paid_on": _date(b["paid_at"]),
        "days_to_paid": _days(b["listed_at"], b["paid_at"]),
        "has_thank_you": bool(b["thank_you"]),
    }


def payment_row(c: dict) -> dict:
    net = c["amount_minor"] - c["refunded_minor"]
    return {
        "payment_id": c["id"],
        "bill_id": c["bill_id"],
        "paid_on": _date(c["captured_at"]),
        "amount": _num(c["amount_minor"], c["currency"]),
        "currency": c["currency"],
        "amount_usd": _usd(c["amount_minor"], c["currency"]),
        "refunded_usd": _usd(c["refunded_minor"], c["currency"]),
        "net_usd": _usd(net, c["currency"]),
        "source": {"swipe": "Swipe", "bulk": "Big-sponsor mode", "agent": "Giving agent",
                   "recurring": "Monthly sponsorship"}.get(c["source"], c["source"]),
        "payment_status": c["status"].replace("_", " ").title(),
        "paypal_order_id": c["order_id"],
        "paypal_capture_id": c["capture_id"],
    }


SPONSOR_DESCRIPTION = (
    "One Billkind sponsor's giving. Billkind pays household bills directly to verified billers (schools, clinics, "
    "utilities, landlords), never to the person. 'payments' has one row per PayPal payment this sponsor made "
    "(net_usd is after refunds; amounts in other currencies are converted to USD at an indicative rate). 'bills' "
    "has the bills those payments went to; status 'Paid to biller' means the PayPal payout to the biller succeeded. "
    "'agent_decisions' lists what the sponsor's giving agent did and why."
)

ADMIN_DESCRIPTION = (
    "Billkind platform operations. 'bills' covers every submitted bill with its risk score and status; "
    "'fraud_signals' has one row per risk reason raised on a bill; 'payouts' has one row per PayPal payout item to a "
    "biller; 'payments' has every sponsor payment with refunds; 'webhooks' lists PayPal webhook deliveries and whether "
    "their signatures verified. No personal data is included."
)


def sponsor_sources(user: dict) -> dict:
    contribs = db.q("SELECT * FROM contributions WHERE sponsor_id = ? AND status IN "
                    "('CAPTURED','PARTIALLY_REFUNDED','REFUNDED') ORDER BY captured_at", (user["id"],))
    bill_ids = sorted({c["bill_id"] for c in contribs})
    bills = []
    if bill_ids:
        marks = ",".join("?" for _ in bill_ids)
        bills = db.q(f"SELECT * FROM bills WHERE id IN ({marks})", bill_ids)
    agent = db.q("SELECT d.* FROM agent_decisions d JOIN agents a ON a.id = d.agent_id WHERE a.sponsor_id = ? "
                 "ORDER BY d.created_at", (user["id"],))
    decisions = [{
        "decision_id": d["id"], "bill_id": d["bill_id"] or None, "run_on": _date(d["created_at"]),
        "decision": d["decision"].title(), "paid_usd": _usd(d["paid_minor"], d["currency"]) if d["currency"] else 0.0,
        "rule": d["rule"] or "", "reason": d["reason"][:200],
    } for d in agent]
    return {
        "description": SPONSOR_DESCRIPTION,
        "sources": [
            {"id": "payments", "data": [payment_row(c) for c in contribs]},
            {"id": "bills", "data": [bill_row(b) for b in bills]},
            {"id": "agent_decisions", "data": decisions},
        ],
        "relationships": [
            {"id": "payments-bills", "source": "payments.bill_id", "target": "bills.bill_id", "type": "many-to-one"},
            {"id": "decisions-bills", "source": "agent_decisions.bill_id", "target": "bills.bill_id",
             "type": "many-to-one"},
        ],
    }


def admin_sources() -> dict:
    bills = db.q("SELECT * FROM bills WHERE status != 'DRAFT' ORDER BY created_at")
    bill_rows, signals = [], []
    for b in bills:
        row = bill_row(b)
        row.update(risk_score=b["risk_score"] or 0, decision=b["decision"] or "",
                   submitted_on=_date(b["created_at"]),
                   source=json.loads(b["extraction"] or "{}").get("source", ""))
        bill_rows.append(row)
        for r in json.loads(b["risk_reasons"] or "[]"):
            signals.append({"bill_id": b["id"], "signal": r.get("code", ""), "points": r.get("points", 0)})
    payouts = [{
        "payout_item": p["sender_item_id"], "bill_id": p["bill_id"], "status": p["status"].title(),
        "currency": p["currency"], "amount": _num(p["amount_minor"], p["currency"]),
        "amount_usd": _usd(p["amount_minor"], p["currency"]), "sent_on": _date(p["created_at"]),
        "minutes_to_final": round((p["updated_at"] - p["created_at"]) / 60, 1) if p["status"] != "PENDING" else None,
    } for p in db.q("SELECT * FROM payout_items ORDER BY created_at")]
    payments = [payment_row(c) for c in db.q("SELECT * FROM contributions WHERE status IN "
                                              "('CAPTURED','PARTIALLY_REFUNDED','REFUNDED') ORDER BY captured_at")]
    hooks = [{"event_id": w["id"], "event_type": w["event_type"], "received_on": _date(w["received_at"]),
              "verified": bool(w["verified"]), "method": w["method"] or ""}
             for w in db.q("SELECT id, event_type, received_at, verified, method FROM webhook_events "
                           "ORDER BY received_at DESC LIMIT 2000")]
    return {
        "description": ADMIN_DESCRIPTION,
        "sources": [
            {"id": "bills", "data": bill_rows},
            {"id": "fraud_signals", "data": signals},
            {"id": "payouts", "data": payouts},
            {"id": "payments", "data": payments},
            {"id": "webhooks", "data": hooks},
        ],
        "relationships": [
            {"id": "signals-bills", "source": "fraud_signals.bill_id", "target": "bills.bill_id", "type": "many-to-one"},
            {"id": "payouts-bills", "source": "payouts.bill_id", "target": "bills.bill_id", "type": "many-to-one"},
            {"id": "payments-bills", "source": "payments.bill_id", "target": "bills.bill_id", "type": "many-to-one"},
        ],
    }


def schema_summary(data: dict) -> str:
    """A compact description of tables and fields for the insights agent (no row data beyond types and samples)."""
    lines = [data.get("description", "")]
    for src in data["sources"]:
        rows = src["data"]
        fields = list(rows[0].keys()) if rows else []
        sample = rows[0] if rows else {}
        types = ", ".join(f"{f} ({type(sample.get(f)).__name__})" for f in fields)
        lines.append(f"Table {src['id']} ({len(rows)} rows): {types or 'no rows yet'}")
    for rel in data.get("relationships", []):
        lines.append(f"Relationship: {rel['source']} links to {rel['target']} ({rel['type']})")
    return "\n".join(lines)
