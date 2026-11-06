"""Match bills to a sponsor's stated preferences. Pure scoring; no model needed to rank."""

from __future__ import annotations

import datetime as dt


def score(bill: dict, prefs: dict) -> float:
    """0..100. Preferences: categories, countries, max_usd, urgent_first."""
    s = 50.0
    cats = prefs.get("categories") or []
    if cats:
        s += 25 if bill["category"] in cats else -20
    countries = prefs.get("countries") or []
    if countries:
        s += 15 if bill["country"] in countries else -10
    try:
        days = (dt.date.fromisoformat(bill["due_date"]) - dt.date.today()).days
        if prefs.get("urgent_first", True):
            s += max(0, 10 - max(days, 0)) * 1.5  # due soon rises
    except (TypeError, ValueError):
        pass
    s -= (bill.get("risk_score") or 0) / 10
    return max(0.0, min(100.0, s))


def why(bill: dict, prefs: dict) -> str:
    parts = []
    if bill["category"] in (prefs.get("categories") or []):
        parts.append("matches the causes you chose")
    if bill["country"] in (prefs.get("countries") or []):
        parts.append("in a country you follow")
    try:
        days = (dt.date.fromisoformat(bill["due_date"]) - dt.date.today()).days
        if days <= 3:
            parts.append("due very soon")
    except (TypeError, ValueError):
        pass
    return ", ".join(parts)
