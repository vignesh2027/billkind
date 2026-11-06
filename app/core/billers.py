"""Verified biller registry. Money can only move to a biller in this table with verified = 1."""

from __future__ import annotations

import difflib
import json
import re

from .. import db

STOP = {"the", "ltd", "limited", "inc", "co", "company", "corp", "corporation", "llc", "plc", "pvt", "private",
        "and", "of", "board", "dept", "department"}


def norm(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in STOP)


def match(name: str, country: str | None = None) -> tuple[dict | None, float]:
    """Best registry match for a biller name as printed. Returns (biller, similarity 0..1)."""
    target = norm(name)
    if not target:
        return None, 0.0
    best, best_score = None, 0.0
    for b in db.q("SELECT * FROM billers"):
        names = [b["name"], *json.loads(b["aliases"] or "[]")]
        for n in names:
            cand = norm(n)
            if not cand:
                continue
            score = difflib.SequenceMatcher(None, target, cand).ratio()
            if cand in target or target in cand:
                score = max(score, 0.9)
            if country and b["country"] != country:
                score -= 0.15
            if score > best_score:
                best, best_score = b, score
    if best_score < 0.78:
        return None, best_score
    return best, best_score


def add(name: str, category: str, country: str, city: str, paypal_email: str, verified: bool,
        aliases: list[str] | None = None, user_id: str | None = None, biller_id: str | None = None) -> str:
    bid = biller_id or db.new_id("BLR")
    db.insert("billers", {"id": bid, "name": name, "category": category, "country": country, "city": city,
                          "paypal_email": paypal_email, "verified": 1 if verified else 0,
                          "aliases": json.dumps(aliases or []), "user_id": user_id, "created_at": db.now()})
    return bid
