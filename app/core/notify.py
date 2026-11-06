"""In-app messages: receipts, thank-you notes, reminders, notices.

Billkind does not send real email in the hackathon build; every message is stored and shown in
the user's inbox, and the receipt pages are printable.
"""

from __future__ import annotations

from .. import db
from . import events


def send(user_id: str, kind: str, subject: str, body: str, *, bill_id: str = "",
         contribution_id: str = "", live: bool = True) -> str:
    mid = db.new_id("MSG")
    db.insert("messages", {"id": mid, "user_id": user_id, "kind": kind, "subject": subject, "body": body,
                           "bill_id": bill_id, "contribution_id": contribution_id, "created_at": db.now(),
                           "read": 0})
    if live:
        events.broker.push({"kind": "message", "public": False, "audience": [user_id], "message": subject,
                            "body": body, "msg_kind": kind, "bill_id": bill_id, "ts": db.now()})
    return mid


def inbox(user_id: str, limit: int = 50) -> list[dict]:
    return db.q("SELECT * FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))


def unread(user_id: str) -> int:
    return db.one("SELECT COUNT(*) AS n FROM messages WHERE user_id = ? AND read = 0", (user_id,))["n"]
