"""Event log plus an in-process broadcaster for server-sent events.

Every state change that matters writes a row here. Public rows feed the world wall and must
never contain personal data; private rows go only to the listed users.
"""

from __future__ import annotations

import asyncio
import json

from .. import db


class Broker:
    def __init__(self) -> None:
        self.subs: list[tuple[asyncio.Queue, str | None]] = []

    def subscribe(self, user_id: str | None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subs.append((q, user_id))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs = [(s, u) for s, u in self.subs if s is not q]

    def push(self, event: dict) -> None:
        audience = set(event.get("audience") or [])
        for q, user in list(self.subs):
            if event.get("public") or (user and user in audience):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass


broker = Broker()


def publish(kind: str, message: str, *, bill: dict | None = None, amount_minor: int = 0,
            currency: str = "", public: bool = True, audience: list[str] | None = None,
            ref: str = "", extra: dict | None = None) -> dict:
    row = {
        "ts": db.now(), "kind": kind, "bill_id": bill["id"] if bill else "",
        "country": bill["country"] if bill else "", "city": bill["city"] if bill else "",
        "category": bill["category"] if bill else "", "currency": currency,
        "amount_minor": int(amount_minor), "message": message, "public": 1 if public else 0,
        "audience": json.dumps(audience or []), "ref": ref,
    }
    db.insert("events", row)
    event = dict(row)
    event["id"] = db.one("SELECT last_insert_rowid() AS i")["i"]
    event["public"] = public
    event["audience"] = audience or []
    if extra:
        event.update(extra)
    broker.push(event)
    return event


def recent_public(limit: int = 30) -> list[dict]:
    return db.q("SELECT * FROM events WHERE public = 1 ORDER BY id DESC LIMIT ?", (limit,))
