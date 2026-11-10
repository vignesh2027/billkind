"""Hosted demo upkeep: fictional demo bills are re-listed when they run low, and the demo big sponsor's giving agent
gets a fresh budget once it is used up. Each test restores the shared database it touched."""

import asyncio
import datetime as dt
import json

import pytest

from app import db
from app.ai import agent
from app.config import settings
from app.core import automation, funding
from scripts import seed
from tests.conftest import login


@pytest.fixture
def heal_on(monkeypatch):
    monkeypatch.setattr(settings, "demo_self_heal", True)


@pytest.fixture
def few_open_demo_bills():
    """Leave only two open demo bills (neither a school fee), as on a demo that judges have used for weeks."""
    open_bills = automation.open_demo_bills()
    keep = [b["id"] for b in open_bills if b["category"] != "school_fees"][:2]
    hidden = [(b["id"], b["status"]) for b in open_bills if b["id"] not in keep]
    for bid, _ in hidden:
        db.run("UPDATE bills SET status = 'EXPIRED' WHERE id = ?", (bid,))
    before = {b["id"] for b in db.q("SELECT id FROM bills")}
    yield keep
    for b in db.q("SELECT id, poster_id FROM bills"):
        if b["id"] not in before:
            db.run("DELETE FROM bills WHERE id = ?", (b["id"],))
            db.run("DELETE FROM users WHERE id = ?", (b["poster_id"],))
    for bid, status in hidden:
        db.run("UPDATE bills SET status = ? WHERE id = ?", (status, bid))


def test_low_demo_bills_are_relisted_as_fresh_fictional_bills(heal_on, few_open_demo_bills):
    listed = automation.refill_demo_bills()
    assert len(listed) == len(seed.BILLS) - len(few_open_demo_bills)
    rows = [db.one("SELECT * FROM bills WHERE id = ?", (bid,)) for bid in listed]
    today = dt.date.today()
    for b in rows:
        assert b["id"].startswith("BK-D") and b["status"] == "LISTED"
        assert dt.date.fromisoformat(b["due_date"]) > today
        assert json.loads(b["extraction"]) | {"source": "seed", "demo_refill": True} == json.loads(b["extraction"])
        assert db.one("SELECT verified FROM billers WHERE id = ?", (b["biller_id"],))["verified"] == 1
        poster = db.one("SELECT * FROM users WHERE id = ?", (b["poster_id"],))
        assert poster["role"] == "poster" and poster["email"].endswith("@billkind.demo")
        assert funding.remaining(b) > 0
    # Each refill is a new household, so a monthly sponsorship for an earlier household is not charged again.
    assert len({b["poster_id"] for b in rows}) == len(rows)
    # The big-sponsor example ("every school fee bill under 60 USD") has bills to clear again.
    school = [b for b in rows if b["category"] == "school_fees"
              and automation._usd_minor(funding.remaining(b), b["settle_currency"]) < 6000]
    assert len(school) >= automation.DEMO_MIN_SCHOOL_FEES
    # Nothing is listed twice: the deck is full again, so the next check adds nothing.
    assert automation.refill_demo_bills() == []


def test_refill_runs_from_the_background_tick_only_when_switched_on(monkeypatch, few_open_demo_bills):
    monkeypatch.setattr(settings, "demo_self_heal", False)
    out = asyncio.run(automation.run_due(db.now()))
    assert "demo" not in out and len(automation.open_demo_bills()) == len(few_open_demo_bills)
    monkeypatch.setattr(settings, "demo_self_heal", True)
    out = asyncio.run(automation.run_due(db.now()))
    assert len(out["demo"]["bills"]) == len(seed.BILLS) - len(few_open_demo_bills)


def test_demo_upkeep_never_acts_outside_sandbox_or_fake(heal_on, monkeypatch, few_open_demo_bills):
    monkeypatch.setattr(settings, "paypal_mode", "live")
    assert automation.demo_heal(force=True) == {"bills": [], "agent_budget_renewed": False}
    assert len(automation.open_demo_bills()) == len(few_open_demo_bills)


@pytest.fixture
def big_agent_used_up():
    """An active agent for the demo big sponsor whose monthly budget is already spent, with a saved PayPal token.
    Its rules match no bill, so running it never pays anything during the test."""
    prev = [a["id"] for a in db.q("SELECT id FROM agents WHERE sponsor_id = 'U-BIG' AND active = 1")]
    db.run("UPDATE agents SET active = 0 WHERE sponsor_id = 'U-BIG'")
    policy = agent.validate({"monthly_budget_usd": 50, "per_bill_max_usd": 20, "categories": ["phone"],
                             "countries": ["ZZ"], "max_risk": 25})
    aid = db.new_id("AGT")
    db.insert("agents", {"id": aid, "sponsor_id": "U-BIG", "rules_text": "test", "policy": json.dumps(policy),
                         "active": 1, "created_at": db.now(), "last_run": db.now()})
    tid = db.new_id("VT")
    db.insert("vault_tokens", {"id": tid, "sponsor_id": "U-BIG", "payment_token_id": "TEST-TOKEN",
                               "payer_email": "agent-buyer@personal.example.com", "status": "ACTIVE",
                               "created_at": db.now() + 3600})
    cid = db.new_id("C")
    db.insert("contributions", {"id": cid, "bill_id": "BK-TEST", "sponsor_id": "U-BIG", "amount_minor": 5000,
                                "currency": "USD", "status": "CAPTURED", "source": "agent", "created_at": db.now(),
                                "captured_at": db.now() - 3600})
    key = agent.budget_key("U-BIG")
    old_key = db.kv_get(key, None)
    if old_key is not None:
        db.run("DELETE FROM kv WHERE k = ?", (key,))
    yield aid
    db.run("DELETE FROM vault_tokens WHERE id = ?", (tid,))
    db.run("DELETE FROM contributions WHERE id = ?", (cid,))
    db.run("DELETE FROM agent_decisions WHERE agent_id = ?", (aid,))
    db.run("DELETE FROM agents WHERE id = ?", (aid,))
    db.run("DELETE FROM kv WHERE k = ?", (key,))
    if old_key is not None:
        db.kv_set(key, old_key)
    for a in prev:
        db.run("UPDATE agents SET active = 1 WHERE id = ?", (a,))


def test_used_up_agent_budget_is_renewed_once_and_logged(heal_on, big_agent_used_up):
    aid = big_agent_used_up
    assert agent.month_spent_usd(aid, "U-BIG") >= 50
    assert automation.renew_demo_agent_budget() is True
    assert agent.month_spent_usd(aid, "U-BIG") == 0 and agent.budget_renewed("U-BIG")
    logged = db.q("SELECT * FROM agent_decisions WHERE agent_id = ? AND decision = 'renewed'", (aid,))
    assert len(logged) == 1 and "Earlier payments and decisions are unchanged" in logged[0]["reason"]
    # The earlier payment is untouched.
    assert db.one("SELECT status FROM contributions WHERE sponsor_id = 'U-BIG' AND bill_id = 'BK-TEST'")["status"] == "CAPTURED"
    # A budget with money left is never renewed, and only the demo big sponsor is ever renewed.
    assert automation.renew_demo_agent_budget() is False
    assert automation.renew_demo_agent_budget("U-SPONSOR", force=True) is False


def test_renewals_are_rate_limited(heal_on, big_agent_used_up):
    aid = big_agent_used_up
    now = db.now()
    assert automation.renew_demo_agent_budget(now=now)
    db.run("UPDATE contributions SET captured_at = ? WHERE sponsor_id = 'U-BIG' AND bill_id = 'BK-TEST'", (now + 1,))
    assert automation.renew_demo_agent_budget(now=now + 60) is False
    assert automation.renew_demo_agent_budget(now=now + automation.DEMO_RENEW_GAP + 1) is True


def test_run_now_on_a_used_up_demo_agent_renews_instead_of_refusing(heal_on, big_agent_used_up):
    aid = big_agent_used_up
    big, csrf = login("bigsponsor@billkind.demo")
    html = big.post("/sponsor/agent/run", data={"csrf": csrf}).text
    decisions = [d["decision"] + ":" + d["reason"] for d in
                 db.q("SELECT * FROM agent_decisions WHERE agent_id = ? ORDER BY created_at", (aid,))]
    assert decisions and decisions[0].startswith("renewed:")
    assert not any("budget is used up" in d for d in decisions)
    assert "since the demo budget was renewed" in html and "renewed" in html


def test_run_now_still_refuses_when_upkeep_is_off(monkeypatch, big_agent_used_up):
    aid = big_agent_used_up
    monkeypatch.setattr(settings, "demo_self_heal", False)
    big, csrf = login("bigsponsor@billkind.demo")
    big.post("/sponsor/agent/run", data={"csrf": csrf})
    reasons = [d["reason"] for d in db.q("SELECT reason FROM agent_decisions WHERE agent_id = ?", (aid,))]
    assert reasons == ["This month's budget is used up."]
