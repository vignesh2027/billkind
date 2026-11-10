"""The giving agent: plain-words rules become a confirmed policy; the model proposes, code enforces.

Hard caps live in this file and in funding.start_payment, never in the prompt alone:
  monthly budget, per-bill cap, allowed categories and countries, maximum risk score,
  verified billers only, never more than a bill still needs.
Every proposal and every refusal is written to agent_decisions with the rule that applied.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal

from .. import db
from ..core import funding, states
from ..core.money import USD_RATES, convert, fmt, to_minor
from . import llm
from .intake import CATEGORIES, CATEGORY_LABELS

MAX_MONTHLY_USD = 5000
DEFAULT_MAX_RISK = 25

PARSE_SYSTEM = """Turn a sponsor's giving rules into JSON. Keys:
monthly_budget_usd (number), per_bill_max_usd (number), categories (array from: electricity, water, school_fees,
medical, rent, phone; empty means any), countries (array of ISO 3166 alpha-2 codes; empty means any),
max_risk (integer 0 to 60, default 25), urgent_first (boolean, default true), note (one short sentence restating
the rules in plain words). Use only what the sponsor said; do not invent limits. Return only the JSON object."""

COUNTRY_WORDS = {"india": "IN", "kenya": "KE", "nigeria": "NG", "philippines": "PH", "mexico": "MX",
                 "united kingdom": "GB", "uk": "GB", "britain": "GB", "united states": "US", "usa": "US",
                 "ghana": "GH", "south africa": "ZA", "indonesia": "ID", "germany": "DE"}
CATEGORY_WORDS = {"school": "school_fees", "tuition": "school_fees", "education": "school_fees",
                  "medical": "medical", "hospital": "medical", "clinic": "medical", "health": "medical",
                  "electric": "electricity", "power": "electricity", "energy": "electricity",
                  "water": "water", "rent": "rent", "housing": "rent", "phone": "phone", "mobile": "phone"}


def parse_rules_fallback(text: str) -> dict:
    t = text.lower()
    nums = [Decimal(n.replace(",", "")) for n in re.findall(r"(\d[\d,]*(?:\.\d+)?)", t)]
    monthly = per_bill = None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:usd|dollars|\$)?\s*(?:a|per|each)\s*month", t)
    if m:
        monthly = Decimal(m.group(1).replace(",", ""))
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:usd|dollars|\$)?\s*(?:a|per|for each|each)\s*bill", t)
    if m:
        per_bill = Decimal(m.group(1).replace(",", ""))
    if monthly is None and nums:
        monthly = max(nums)
    if per_bill is None and nums:
        per_bill = min(nums)
    cats = sorted({c for w, c in CATEGORY_WORDS.items() if w in t})
    countries = sorted({c for w, c in COUNTRY_WORDS.items() if re.search(rf"\b{w}\b", t)})
    return {"monthly_budget_usd": float(monthly or 50), "per_bill_max_usd": float(per_bill or 25), "categories": cats,
            "countries": countries, "max_risk": DEFAULT_MAX_RISK, "urgent_first": True, "note": ""}


def validate(p: dict) -> dict:
    """Normalise and bound a policy. This is what is enforced, whatever the model said."""
    def num(v, default):
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return default
    monthly = min(num(p.get("monthly_budget_usd"), 50.0), MAX_MONTHLY_USD)
    per_bill = min(num(p.get("per_bill_max_usd"), 25.0), monthly)
    cats = [c for c in (p.get("categories") or []) if c in CATEGORIES and c != "other"]
    countries = [str(c).upper()[:2] for c in (p.get("countries") or []) if re.fullmatch(r"[A-Za-z]{2}", str(c))]
    try:
        risk = int(p.get("max_risk", DEFAULT_MAX_RISK))
    except (TypeError, ValueError):
        risk = DEFAULT_MAX_RISK
    risk = max(0, min(60, risk))
    out = {"monthly_budget_usd": round(monthly, 2), "per_bill_max_usd": round(per_bill, 2), "categories": cats,
           "countries": countries, "max_risk": risk, "urgent_first": bool(p.get("urgent_first", True))}
    out["note"] = describe(out)
    return out


def describe(p: dict) -> str:
    cats = ", ".join(CATEGORY_LABELS[c].lower() for c in p["categories"]) or "any kind of bill"
    where = (" in " + ", ".join(p["countries"])) if p["countries"] else " anywhere"
    return (f"Up to {p['monthly_budget_usd']:.2f} USD a month for {cats}{where}, never more than "
            f"{p['per_bill_max_usd']:.2f} USD per bill, only verified billers and bills with risk {p['max_risk']} or lower.")


async def parse_rules(text: str) -> tuple[dict, str]:
    data = await llm.chat_json(PARSE_SYSTEM, text[:1000], kind="agent-rules", max_tokens=500)
    if data:
        return validate(data), "ai"
    return validate(parse_rules_fallback(text)), "rules"


# Running ------------------------------------------------------------------------------------

def _usd(minor: int, cur: str) -> Decimal:
    if cur not in USD_RATES:
        return Decimal(0)
    return Decimal(convert(minor, cur, "USD")) / 100


def budget_key(sponsor_id: str) -> str:
    return f"agent.budget_from.{sponsor_id}"


def budget_start(sponsor_id: str) -> float:
    """Start of the current budget period: the first of the month, or a later demo renewal in the same month."""
    start = dt.date.today().replace(day=1)
    since = dt.datetime.combine(start, dt.time()).timestamp()
    try:
        renewed = float(db.kv_get(budget_key(sponsor_id), "0"))
    except ValueError:
        renewed = 0.0
    return max(since, renewed)


def budget_renewed(sponsor_id: str) -> bool:
    """True when a demo renewal started the current budget period after the first of the month."""
    start = dt.datetime.combine(dt.date.today().replace(day=1), dt.time()).timestamp()
    return budget_start(sponsor_id) > start


def month_spent_usd(agent_id: str, sponsor_id: str) -> Decimal:
    since = budget_start(sponsor_id)
    rows = db.q("SELECT amount_minor - refunded_minor AS net, currency FROM contributions WHERE sponsor_id = ? "
                "AND source = 'agent' AND status IN ('CAPTURED','PARTIALLY_REFUNDED') AND captured_at >= ?",
                (sponsor_id, since))
    return sum((_usd(r["net"], r["currency"]) for r in rows), Decimal(0))


def eligibility(bill: dict, policy: dict, sponsor_id: str) -> str | None:
    """Return the rule a bill breaks, or None if the agent may pay it."""
    if bill["status"] not in states.OPEN_FOR_FUNDING:
        return "bill is not open"
    if bill["poster_id"] == sponsor_id:
        return "own bill"
    if policy["categories"] and bill["category"] not in policy["categories"]:
        return f"category {CATEGORY_LABELS.get(bill['category'], bill['category']).lower()} is not in your rules"
    if policy["countries"] and bill["country"] not in policy["countries"]:
        return f"country {bill['country']} is not in your rules"
    if (bill["risk_score"] or 0) > policy["max_risk"]:
        return f"risk score {bill['risk_score']} is above your limit {policy['max_risk']}"
    b = db.one("SELECT verified FROM billers WHERE id = ?", (bill["biller_id"],)) if bill["biller_id"] else None
    if not b or not b["verified"]:
        return "biller is not verified"
    if bill["settle_currency"] not in USD_RATES:
        return "no rate for the settlement currency"
    return None


AGENT_SYSTEM = """You are a giving agent that pays household bills for a sponsor. The money goes to the biller,
never to the person. You will get the sponsor's rules, the remaining monthly budget, and candidate bills that already
pass the rules. Choose which to pay and how much (in USD), most urgent and most clearly helpful first. Prefer covering
a bill completely when the budget allows. Give one short plain reason per decision, no names.
Return JSON: {"decisions": [{"bill_id": "...", "action": "pay" or "skip", "amount_usd": number, "reason": "..."}]}"""


async def run(agent_row: dict, *, limit: int = 8) -> dict:
    """One run. Returns {'run_id', 'paid': [...], 'decisions': [...]}."""
    sponsor = db.one("SELECT * FROM users WHERE id = ?", (agent_row["sponsor_id"],))
    policy = validate(json.loads(agent_row["policy"]))
    token = db.one("SELECT * FROM vault_tokens WHERE sponsor_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC",
                   (sponsor["id"],))
    run_id = db.new_id("RUN")
    decisions: list[dict] = []

    def log(bill_id, decision, reason, rule="", proposed=0, paid=0, cur=""):
        d = {"id": db.new_id("DEC"), "agent_id": agent_row["id"], "run_id": run_id, "bill_id": bill_id,
             "decision": decision, "proposed_minor": proposed, "paid_minor": paid, "currency": cur,
             "reason": reason[:300], "rule": rule[:120], "created_at": db.now()}
        db.insert("agent_decisions", d)
        decisions.append(d)

    if not token:
        log("", "blocked", "The agent has no saved PayPal account to pay with.", "vault required")
        return {"run_id": run_id, "paid": [], "decisions": decisions}
    budget_left = Decimal(str(policy["monthly_budget_usd"])) - month_spent_usd(agent_row["id"], sponsor["id"])
    if budget_left <= 0:
        log("", "blocked", "This month's budget is used up.", "monthly budget")
        return {"run_id": run_id, "paid": [], "decisions": decisions}

    open_bills = db.q("SELECT * FROM bills WHERE status IN ('LISTED','FUNDING') AND poster_id != ? ORDER BY due_date",
                      (sponsor["id"],))
    already = {r["bill_id"] for r in db.q("SELECT bill_id FROM contributions WHERE sponsor_id = ? AND source = 'agent' "
                                          "AND status IN ('CAPTURED','PARTIALLY_REFUNDED')", (sponsor["id"],))}
    eligible, near = [], []
    for b in open_bills:
        if b["id"] in already:
            continue
        rule = eligibility(b, policy, sponsor["id"])
        if rule:
            near.append((b, rule))
        elif funding.remaining(b) > 0:
            eligible.append(b)
    eligible = eligible[:limit]
    # Log a few of the nearest bills the rules excluded, so the sponsor sees what was not paid and why.
    for b, rule in near[:3]:
        log(b["id"], "skipped", f"Not eligible: {rule}.", rule)

    cand = [{"bill_id": b["id"], "category": b["category"], "country": b["country"], "city": b["city"],
             "needs_usd": float(_usd(funding.remaining(b), b["settle_currency"])),
             "due_in_days": (dt.date.fromisoformat(b["due_date"]) - dt.date.today()).days, "story": b["story"][:200]}
            for b in eligible]
    proposals = None
    if cand:
        data = await llm.chat_json(AGENT_SYSTEM, json.dumps({"rules": policy["note"], "budget_left_usd": float(budget_left),
                                                              "per_bill_max_usd": policy["per_bill_max_usd"],
                                                              "candidates": cand}), kind="giving-agent", max_tokens=900)
        if data and isinstance(data.get("decisions"), list):
            proposals = data["decisions"]
    if proposals is None:  # rule-based fallback: most urgent first, cover as much as allowed
        proposals = [{"bill_id": c["bill_id"], "action": "pay", "amount_usd": c["needs_usd"],
                      "reason": f"Due in {c['due_in_days']} days and matches your rules."} for c in cand]

    by_id = {b["id"]: b for b in open_bills}
    eligible_ids = {b["id"] for b in eligible}
    paid = []
    per_cap = Decimal(str(policy["per_bill_max_usd"]))
    for p in proposals[:limit]:
        bid = str(p.get("bill_id", ""))
        reason = str(p.get("reason", ""))[:240]
        if p.get("action") != "pay":
            if bid in eligible_ids:
                log(bid, "skipped", reason or "The agent chose not to pay this one.", "model choice")
            continue
        if bid not in eligible_ids:
            bill = by_id.get(bid)
            rule = eligibility(bill, policy, sponsor["id"]) if bill else "not a candidate"
            log(bid, "blocked", f"The model proposed this bill but it breaks a rule: {rule or 'not a candidate'}.",
                rule or "not a candidate")
            continue
        bill = funding.get_bill(bid)
        cur = bill["settle_currency"]
        try:
            want = Decimal(str(p.get("amount_usd", 0)))
        except ArithmeticError:
            want = Decimal(0)
        want_minor = to_minor(want, "USD")
        notes = []
        amount_usd = want
        if amount_usd > per_cap:
            amount_usd = per_cap
            notes.append(f"clamped to the per-bill cap of {per_cap} USD")
        if amount_usd > budget_left:
            amount_usd = budget_left
            notes.append(f"clamped to the {budget_left:.2f} USD left this month")
        amount_minor = convert(to_minor(amount_usd, "USD"), "USD", cur) if cur != "USD" else to_minor(amount_usd, "USD")
        left = funding.remaining(bill)
        if amount_minor > left:
            amount_minor = left
            notes.append("lowered to what the bill still needs")
        if amount_minor < 100:
            log(bid, "blocked", f"{reason} Not paid: the amount after caps is below the minimum.",
                "monthly budget" if budget_left < 1 else "minimum amount", want_minor, 0, "USD")
            continue
        try:
            await funding.start_payment(sponsor, [(bid, amount_minor)], source="agent",
                                        vault_id=token["payment_token_id"])
        except funding.FundingError as exc:
            log(bid, "blocked", f"Payment refused by the funding rules: {exc}.", "funding rule", want_minor, 0, "USD")
            continue
        except Exception as exc:  # PayPal error: log and move on
            log(bid, "blocked", f"PayPal did not complete the payment ({type(exc).__name__}).", "payment error",
                want_minor, 0, "USD")
            continue
        budget_left -= _usd(amount_minor, cur)
        decision = "clamped" if any("cap" in n or "left this month" in n for n in notes) else "paid"
        log(bid, decision, reason + (" " + "; ".join(notes).capitalize() + "." if notes else ""),
            "; ".join(notes), want_minor, amount_minor, cur)
        paid.append(bid)
        if budget_left < 1:
            break
    db.update("agents", "id", agent_row["id"], last_run=db.now())
    return {"run_id": run_id, "paid": paid, "decisions": decisions}


def decision_view(d: dict) -> dict:
    bill = db.one("SELECT category, city, country FROM bills WHERE id = ?", (d["bill_id"],)) if d["bill_id"] else None
    return dict(d, where=(bill["city"] or bill["country"]) if bill else "",
                category=CATEGORY_LABELS.get(bill["category"], "") if bill else "",
                paid=fmt(d["paid_minor"], d["currency"]) if d["paid_minor"] else "",
                proposed=fmt(d["proposed_minor"], "USD") if d["proposed_minor"] else "")
