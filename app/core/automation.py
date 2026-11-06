"""Scheduled automation: giving agents and recurring monthly sponsorships (both pay with PayPal Vault)."""

from __future__ import annotations

import datetime as dt
import logging

from .. import db
from ..paypal.client import PayPalError
from . import funding, notify
from .money import convert, fmt

log = logging.getLogger("billkind.automation")
AGENT_INTERVAL = 24 * 3600


async def run_due(now: float) -> dict:
    out = {"recurring": await run_recurring(), "agents": 0}
    from ..ai import agent
    for a in db.q("SELECT * FROM agents WHERE active = 1 AND (last_run IS NULL OR last_run < ?)", (now - AGENT_INTERVAL,)):
        try:
            await agent.run(a)
            out["agents"] += 1
        except Exception:
            log.exception("agent run failed")
    return out


def active_token(sponsor_id: str) -> dict | None:
    return db.one("SELECT * FROM vault_tokens WHERE sponsor_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC",
                  (sponsor_id,))


def create_pledge(sponsor: dict, bill_id: str, max_minor: int) -> str:
    bill = funding.get_bill(bill_id)
    if not db.one("SELECT 1 FROM contributions WHERE sponsor_id = ? AND bill_id = ? AND status IN "
                  "('CAPTURED','PARTIALLY_REFUNDED')", (sponsor["id"], bill_id)):
        raise funding.FundingError("you can set up a monthly sponsorship for households you have already helped")
    if max_minor <= 0:
        raise funding.FundingError("choose a monthly amount")
    existing = db.one("SELECT id FROM recurring WHERE sponsor_id = ? AND poster_id = ? AND category = ? AND active = 1",
                      (sponsor["id"], bill["poster_id"], bill["category"]))
    if existing:
        db.update("recurring", "id", existing["id"], max_minor=max_minor, currency=bill["settle_currency"])
        return existing["id"]
    rid = db.new_id("REC")
    db.insert("recurring", {"id": rid, "sponsor_id": sponsor["id"], "poster_id": bill["poster_id"],
                            "category": bill["category"], "biller_id": bill["biller_id"] or "",
                            "max_minor": max_minor, "currency": bill["settle_currency"], "active": 1,
                            "created_at": db.now()})
    notify.send(bill["poster_id"], "notice", "A sponsor will cover your bill every month",
                f"A sponsor set up a monthly sponsorship of up to {fmt(max_minor, bill['settle_currency'])} for your "
                f"{bill['category'].replace('_', ' ')} bill. When your next bill is listed, it is paid to the biller "
                "automatically.", bill_id=bill_id)
    return rid


async def run_recurring() -> int:
    """Charge each active pledge once per new bill from the same household, biller and category."""
    n = 0
    for p in db.q("SELECT * FROM recurring WHERE active = 1"):
        token = active_token(p["sponsor_id"])
        if not token:
            continue
        sponsor = db.one("SELECT * FROM users WHERE id = ?", (p["sponsor_id"],))
        bills = db.q("SELECT * FROM bills WHERE poster_id = ? AND category = ? AND status IN ('LISTED','FUNDING') "
                     "AND (? = '' OR biller_id = ?) AND id NOT IN (SELECT bill_id FROM contributions WHERE sponsor_id = ? "
                     "AND source = 'recurring' AND status IN ('CREATED','CAPTURED','PARTIALLY_REFUNDED'))",
                     (p["poster_id"], p["category"], p["biller_id"], p["biller_id"], p["sponsor_id"]))
        for b in bills:
            cap = p["max_minor"] if p["currency"] == b["settle_currency"] else convert(p["max_minor"], p["currency"],
                                                                                         b["settle_currency"])
            amount = min(cap, funding.remaining(b))
            if amount < 100:
                continue
            try:
                await funding.start_payment(sponsor, [(b["id"], amount)], source="recurring",
                                            vault_id=token["payment_token_id"])
            except (funding.FundingError, PayPalError) as exc:
                log.warning("recurring charge for %s failed: %s", b["id"], exc)
                continue
            db.update("recurring", "id", p["id"], last_charged=db.now())
            notify.send(p["sponsor_id"], "notice", "Your monthly sponsorship paid a new bill",
                        f"{fmt(amount, b['settle_currency'])} toward bill {b['id']} ({b['category'].replace('_', ' ')}, "
                        f"{b['city'] or b['country']}), paid to {b['biller_name']}.", bill_id=b["id"])
            n += 1
    return n


def month_key(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    return f"{d.year}-{d.month:02d}"


def pledges_for(sponsor_id: str) -> list[dict]:
    rows = db.q("SELECT * FROM recurring WHERE sponsor_id = ? AND active = 1 ORDER BY created_at DESC", (sponsor_id,))
    for r in rows:
        r["max"] = fmt(r["max_minor"], r["currency"])
        b = db.one("SELECT city, country, biller_name FROM bills WHERE poster_id = ? AND category = ? ORDER BY created_at "
                   "DESC", (r["poster_id"], r["category"]))
        r["where"] = (b["city"] or b["country"]) if b else ""
        r["biller"] = b["biller_name"] if b else ""
    return rows

