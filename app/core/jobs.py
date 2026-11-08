"""Background automation: reservations, payout polling, overdue bills, reminders, agents, recurring."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from .. import db
from . import funding, notify
from .money import fmt

log = logging.getLogger("billkind.jobs")


async def tick(now: float | None = None) -> dict:
    now = now or db.now()
    out = {"expired_reservations": funding.expire_reservations(), "healed_payouts": await funding.heal_unpaid_funded()}
    # Payouts that have waited more than 20 seconds without a webhook are polled.
    stale = db.one("SELECT COUNT(*) AS n FROM payout_items WHERE status = 'PENDING' AND created_at < ?", (now - 20,))
    if stale and stale["n"]:
        out["polled"] = await funding.poll_payouts()
    last = float(db.kv_get("jobs.daily", "0"))
    if now - last > 3600:
        db.kv_set("jobs.daily", str(now))
        out["expired_bills"] = await funding.expire_overdue()
        out["reminders"] = due_reminders()
    try:
        from . import automation
        out.update(await automation.run_due(now))
    except ImportError:
        pass
    return out


def due_reminders(today: dt.date | None = None) -> int:
    """Remind posters three days before the due date when the bill is not covered yet."""
    today = today or dt.date.today()
    soon = (today + dt.timedelta(days=3)).isoformat()
    n = 0
    for b in db.q("SELECT * FROM bills WHERE status IN ('LISTED','FUNDING') AND due_date <= ? AND due_date >= ?",
                  (soon, today.isoformat())):
        key = f"reminder.{b['id']}"
        if db.kv_get(key):
            continue
        db.kv_set(key, "1")
        left = funding.remaining(b)
        notify.send(b["poster_id"], "reminder", f"Bill {b['id']} is due {b['due_date']}",
                    "Your bill is shown first to sponsors now because it is due soon. If you can pay part of it "
                    "yourself, you can lower the amount by withdrawing and posting again. Still needed: "
                    f"{fmt(left, b['settle_currency'])}.", bill_id=b["id"])
        n += 1
    return n


async def loop() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("background tick failed")
        await asyncio.sleep(8)
