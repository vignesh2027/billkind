"""Presentation helpers shared by pages: bill cards, labels, countries."""

from __future__ import annotations

import datetime as dt
import json

from .. import db
from ..ai import dropbill
from ..ai.intake import CATEGORY_LABELS
from ..core import funding, states
from ..core.money import convert, fmt

COUNTRIES = {
    "US": ("United States", 39.8, -98.6), "GB": ("United Kingdom", 54.0, -2.0), "IN": ("India", 21.0, 78.0),
    "KE": ("Kenya", 0.2, 37.9), "NG": ("Nigeria", 9.1, 8.7), "PH": ("Philippines", 12.9, 121.8),
    "MX": ("Mexico", 23.6, -102.5), "DE": ("Germany", 51.2, 10.4), "CA": ("Canada", 56.1, -106.3),
    "AU": ("Australia", -25.3, 133.8), "ZA": ("South Africa", -30.6, 22.9), "GH": ("Ghana", 7.9, -1.0),
    "PK": ("Pakistan", 30.4, 69.3), "BD": ("Bangladesh", 23.7, 90.4), "LK": ("Sri Lanka", 7.9, 80.8),
    "NP": ("Nepal", 28.4, 84.1), "ID": ("Indonesia", -0.8, 113.9), "CO": ("Colombia", 4.6, -74.3),
    "FR": ("France", 46.2, 2.2), "ES": ("Spain", 40.5, -3.7), "IT": ("Italy", 41.9, 12.6),
    "JP": ("Japan", 36.2, 138.3), "SG": ("Singapore", 1.35, 103.8), "IE": ("Ireland", 53.4, -8.2),
    "PL": ("Poland", 51.9, 19.1), "TH": ("Thailand", 15.9, 100.9), "MY": ("Malaysia", 4.2, 102.0),
    "UG": ("Uganda", 1.4, 32.3), "TZ": ("Tanzania", -6.4, 34.9), "EG": ("Egypt", 26.8, 30.8),
}


def country_name(code: str) -> str:
    return COUNTRIES.get(code, (code or "Unknown",))[0]


def category_label(cat: str) -> str:
    return CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())


def days_to(due: str) -> int | None:
    try:
        return (dt.date.fromisoformat(due) - dt.date.today()).days
    except (TypeError, ValueError):
        return None


def due_text(due: str) -> str:
    d = days_to(due)
    if d is None:
        return "No due date"
    if d < 0:
        return f"Overdue {-d} day{'s' if d != -1 else ''}"
    if d == 0:
        return "Due today"
    if d == 1:
        return "Due tomorrow"
    return f"Due in {d} days"


def card(bill: dict, viewer_currency: str = "USD") -> dict:
    left = funding.remaining(bill) if bill["status"] in states.OPEN_FOR_FUNDING else 0
    net = funding.captured_net(bill["id"])
    settle = bill["settle_currency"]
    pct = 0 if not bill["settle_minor"] else min(100, round(100 * net / bill["settle_minor"]))
    try:
        mine = fmt(convert(bill["amount_minor"], bill["currency"], viewer_currency), viewer_currency)
    except ValueError:
        mine = ""
    try:
        left_mine = fmt(convert(left, settle, viewer_currency), viewer_currency) if left else ""
    except ValueError:
        left_mine = ""
    reasons = json.loads(bill.get("risk_reasons") or "[]")
    biller = db.one("SELECT verified FROM billers WHERE id = ?", (bill["biller_id"],)) if bill["biller_id"] else None
    d = days_to(bill["due_date"])
    try:
        ext = json.loads(bill.get("extraction") or "{}")
    except ValueError:
        ext = {}
    doc = dropbill.document_check(ext, bool(biller and biller["verified"]))
    return {
        "id": bill["id"], "status": bill["status"], "status_label": states.BILL_LABELS.get(bill["status"], bill["status"]),
        "category": bill["category"], "category_label": category_label(bill["category"]),
        "city": bill["city"], "country": bill["country"], "country_name": country_name(bill["country"]),
        "biller": bill["biller_name"], "biller_verified": bool(biller and biller["verified"]),
        "amount": fmt(bill["amount_minor"], bill["currency"]), "currency": bill["currency"],
        "amount_mine": mine if viewer_currency != bill["currency"] else "",
        "settle_currency": settle, "settle_amount": fmt(bill["settle_minor"], settle),
        "settle_minor": bill["settle_minor"], "left_minor": left, "left": fmt(left, settle) if left else "",
        "left_mine": left_mine if viewer_currency != settle else "", "funded_pct": pct,
        "funded": fmt(net, settle), "due": due_text(bill["due_date"]), "due_date": bill["due_date"],
        "urgent": d is not None and d <= 3, "story": bill["story"], "alias": bill["alias"],
        "account": bill["account_masked"], "risk": bill["risk_score"],
        "checks": max(0, 9 - len([r for r in reasons if r.get("points", 0) >= 10])), "fx_note": bill["fx_note"],
        "thank_you": bill["thank_you"], "paid": bill["status"] == "PAID",
        "doc_confirmed": doc["amount_confirmed"], "doc_summary": doc["summary"],
        "service_period": ext.get("service_period") or "",
        "sponsors": db.one("SELECT COUNT(DISTINCT sponsor_id) AS n FROM contributions WHERE bill_id = ? AND "
                           "status IN ('CAPTURED','PARTIALLY_REFUNDED')", (bill["id"],))["n"],
    }


def deck_for(sponsor: dict, limit: int = 12) -> list[dict]:
    prefs = json.loads(sponsor.get("prefs") or "{}")
    rows = db.q("SELECT * FROM bills WHERE status IN ('LISTED','FUNDING') AND poster_id != ? AND id NOT IN "
                "(SELECT bill_id FROM passes WHERE sponsor_id = ?) ORDER BY due_date ASC", (sponsor["id"], sponsor["id"]))
    from ..ai.matching import score
    ranked = sorted(rows, key=lambda b: -score(b, prefs))
    cards = []
    for b in ranked:
        c = card(b, sponsor.get("currency") or "USD")
        if c["left_minor"] > 0:
            c["match"] = score(b, prefs)
            cards.append(c)
        if len(cards) >= limit:
            break
    return cards
