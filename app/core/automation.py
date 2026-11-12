"""Scheduled automation: giving agents and recurring monthly sponsorships (both pay with PayPal Vault), and the
hosted demo's upkeep so a visitor never finds an empty deck or a giving agent with no budget left."""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal

from .. import db
from ..config import settings
from ..paypal.client import PayPalError
from . import funding, notify
from .money import convert, fmt

log = logging.getLogger("billkind.automation")
AGENT_INTERVAL = 24 * 3600


async def run_due(now: float) -> dict:
    out = {"recurring": await run_recurring(), "agents": 0}
    if settings.demo_self_heal:
        out["demo"] = demo_heal(now=now)
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


def month_start(now: float | None = None) -> float:
    d = dt.date.fromtimestamp(now or db.now())
    return dt.datetime(d.year, d.month, 1).timestamp()


def pledge_spent(p: dict, now: float | None = None) -> int:
    """What this pledge has charged (or is charging) this calendar month, in the pledge currency."""
    rows = db.q("SELECT c.amount_minor - c.refunded_minor AS net, c.currency FROM contributions c JOIN bills b ON "
                "b.id = c.bill_id WHERE c.sponsor_id = ? AND c.source = 'recurring' AND b.poster_id = ? AND "
                "b.category = ? AND (? = '' OR b.biller_id = ?) AND c.created_at >= ? AND "
                "(c.status IN ('CAPTURED','PARTIALLY_REFUNDED') OR (c.status = 'CREATED' AND c.expires_at > ?))",
                (p["sponsor_id"], p["poster_id"], p["category"], p["biller_id"], p["biller_id"], month_start(now),
                 now or db.now()))
    return sum(r["net"] if r["currency"] == p["currency"] else convert(r["net"], r["currency"], p["currency"])
               for r in rows)


async def run_recurring() -> int:
    """Charge each active pledge for new bills from the same household, biller and category, never more than the
    pledge's monthly maximum in one calendar month."""
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
            headroom = p["max_minor"] - pledge_spent(p)
            if headroom < 100:
                break  # this month's maximum is reached; the next bill waits for next month
            cap = headroom if p["currency"] == b["settle_currency"] else convert(headroom, p["currency"],
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



# Hosted demo upkeep ---------------------------------------------------------------------------
# Judges open the hosted demo weeks after it was seeded. Without this, the seeded bills expire or get paid and the
# demo big sponsor's agent reports "This month's budget is used up". Everything here touches only fictional demo
# data, never changes an earlier payment or decision, and refuses to act outside the sandbox and the local fake.

DEMO_BIG_SPONSOR = "U-BIG"
DEMO_MIN_OPEN = 6            # re-list when fewer open demo bills than this remain
DEMO_MIN_SCHOOL_FEES = 2     # ...or fewer open school fee bills under 60 USD (the big-sponsor mode example)
DEMO_SCHOOL_FEE_MAX_USD = 60
DEMO_RENEW_GAP = 15 * 60     # at most one budget renewal per 15 minutes


def demo_mode() -> bool:
    return settings.paypal_mode in ("fake", "sandbox")


def _usd_minor(minor: int, cur: str) -> int:
    try:
        return convert(minor, cur, "USD")
    except ValueError:
        return 0


def open_demo_bills() -> list[dict]:
    return db.q("SELECT * FROM bills WHERE status IN ('LISTED','FUNDING') "
                "AND json_extract(extraction, '$.source') = 'seed'")


def refill_demo_bills(*, now: float | None = None, force: bool = False) -> list[str]:
    """Re-list every fictional demo bill that has no open copy, when the open ones run low (or when forced)."""
    if not demo_mode():
        return []
    from scripts import seed
    now = now or db.now()
    open_bills = open_demo_bills()
    school = [b for b in open_bills if b["category"] == "school_fees"
              and _usd_minor(funding.remaining(b), b["settle_currency"]) < DEMO_SCHOOL_FEE_MAX_USD * 100]
    if not force and len(open_bills) >= DEMO_MIN_OPEN and len(school) >= DEMO_MIN_SCHOOL_FEES:
        return []
    have = {(b["biller_id"], b["currency"], b["amount_minor"]) for b in open_bills}
    listed = []
    for i, (_alias, biller_id, amount, cur, _days, _story) in enumerate(seed.BILLS):
        if (biller_id, cur, seed.to_minor(amount, cur)) in have:
            continue
        listed.append(seed.refill_demo_bill(i, now))
    if listed:
        log.info("demo: re-listed %d fictional bills", len(listed))
    return listed


def renew_demo_agent_budget(sponsor_id: str = DEMO_BIG_SPONSOR, *, now: float | None = None,
                            force: bool = False) -> bool:
    """Start a fresh monthly budget for the demo big sponsor's agent once it is used up, and log it."""
    if not demo_mode() or sponsor_id != DEMO_BIG_SPONSOR:
        return False
    from ..ai import agent
    a = db.one("SELECT * FROM agents WHERE sponsor_id = ? AND active = 1 ORDER BY created_at DESC", (sponsor_id,))
    if not a:
        return False
    policy = agent.validate(json.loads(a["policy"]))
    left = Decimal(str(policy["monthly_budget_usd"])) - agent.month_spent_usd(a["id"], sponsor_id)
    if left >= 1:
        return False
    now = now or db.now()
    if not force and now - agent.budget_start(sponsor_id) < DEMO_RENEW_GAP:
        return False
    db.kv_set(agent.budget_key(sponsor_id), str(now))
    db.insert("agent_decisions", {
        "id": db.new_id("DEC"), "agent_id": a["id"], "run_id": db.new_id("RUN"), "bill_id": "", "decision": "renewed",
        "proposed_minor": 0, "paid_minor": 0, "currency": "",
        "reason": (f"Demo account: the budget was used up, so a fresh {policy['monthly_budget_usd']:.2f} USD budget "
                   "started for the next visitor. Earlier payments and decisions are unchanged."),
        "rule": "demo budget renewal", "created_at": now})
    log.info("demo: renewed the giving agent budget for %s", sponsor_id)
    return True


def demo_heal(*, now: float | None = None, force: bool = False) -> dict:
    return {"bills": refill_demo_bills(now=now, force=force),
            "agent_budget_renewed": renew_demo_agent_budget(now=now, force=force)}
