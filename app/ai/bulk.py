"""Big-sponsor mode: a plain-words filter becomes a preview of bills, paid in one PayPal checkout."""

from __future__ import annotations

import re
from decimal import Decimal

from .. import db
from ..core import funding
from ..core.money import USD_RATES, convert
from . import llm
from .agent import CATEGORY_WORDS, COUNTRY_WORDS

SYSTEM = """Turn a sponsor's request to clear many household bills into a JSON filter with keys:
category (one of electricity, water, school_fees, medical, rent, phone, or null for any), country (ISO 3166 alpha-2
or null), city (string or null), max_bill_usd (number or null), max_total_usd (number or null). Use only what the
request says. Return only the JSON object."""

CITIES = ["nairobi", "lagos", "chennai", "quezon city", "cebu", "guadalajara", "leeds", "baltimore", "tampa", "accra",
          "johannesburg", "surabaya", "leipzig", "detroit"]


def parse_fallback(text: str) -> dict:
    t = text.lower()
    cat = next((c for w, c in CATEGORY_WORDS.items() if w in t), None)
    country = next((c for w, c in COUNTRY_WORDS.items() if re.search(rf"\b{w}\b", t)), None)
    city = next((c.title() for c in CITIES if c in t), None)
    m = re.search(r"(?:under|below|less than|up to)\s*\$?\s*(\d+(?:\.\d+)?)", t)
    m2 = re.search(r"(?:total|budget|spend)\D{0,12}(\d+(?:\.\d+)?)", t)
    return {"category": cat, "country": country, "city": city, "max_bill_usd": float(m.group(1)) if m else None,
            "max_total_usd": float(m2.group(1)) if m2 else None}


def clean(f: dict) -> dict:
    def num(v):
        try:
            return max(0.0, float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    cat = f.get("category") if f.get("category") in CATEGORY_WORDS.values() else None
    country = str(f["country"]).upper()[:2] if f.get("country") else None
    city = str(f["city"]).strip()[:40] if f.get("city") else None
    return {"category": cat, "country": country, "city": city, "max_bill_usd": num(f.get("max_bill_usd")),
            "max_total_usd": min(num(f.get("max_total_usd")) or 2000.0, 2000.0)}


async def parse(text: str) -> tuple[dict, str]:
    data = await llm.chat_json(SYSTEM, text[:500], kind="bulk-filter", max_tokens=300)
    if data:
        return clean(data), "ai"
    return clean(parse_fallback(text)), "rules"


def select(f: dict, sponsor_id: str) -> dict:
    """Bills matching the filter, grouped by settlement currency; the largest group is the checkout."""
    rows = db.q("SELECT b.* FROM bills b JOIN billers r ON r.id = b.biller_id WHERE b.status IN ('LISTED','FUNDING') "
                "AND r.verified = 1 AND b.risk_score <= 30 AND b.poster_id != ? ORDER BY b.due_date", (sponsor_id,))
    groups: dict[str, list] = {}
    for b in rows:
        if f["category"] and b["category"] != f["category"]:
            continue
        if f["country"] and b["country"] != f["country"]:
            continue
        if f["city"] and f["city"].lower() not in (b["city"] or "").lower():
            continue
        left = funding.remaining(b)
        if left <= 0 or b["settle_currency"] not in USD_RATES:
            continue
        usd = Decimal(convert(left, b["settle_currency"], "USD")) / 100
        if f["max_bill_usd"] is not None and usd > Decimal(str(f["max_bill_usd"])):
            continue
        groups.setdefault(b["settle_currency"], []).append((b, left, usd))
    if not groups:
        return {"currency": None, "picks": [], "others": {}}
    cur = max(groups, key=lambda c: len(groups[c]))
    budget = Decimal(str(f["max_total_usd"]))
    picks, total_usd = [], Decimal(0)
    for b, left, usd in groups[cur][:25]:
        if total_usd + usd > budget:
            continue
        picks.append({"bill": b, "amount_minor": left, "usd": usd})
        total_usd += usd
    others = {c: len(v) for c, v in groups.items() if c != cur}
    return {"currency": cur, "picks": picks, "total_minor": sum(p["amount_minor"] for p in picks),
            "total_usd": total_usd, "others": others}

