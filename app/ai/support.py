"""Support assistant for posters and sponsors.

It sees only the signed-in user's own records, Billkind's policies, and PayPal order details fetched through
PayPal's official Agent Toolkit (orders the user owns only). Every amount and ID in an answer is checked against
the tool outputs before the answer is shown; if anything cannot be confirmed, a safe answer built from the records
is shown instead and a human ticket is offered.
"""

from __future__ import annotations

import asyncio
import json
import re

from .. import db
from ..core import notify, states
from ..core.money import fmt
from ..paypal import toolkit
from . import llm

POLICIES = {
    "money_flow": "Billkind never sends money to the person who posted a bill. When a bill is fully funded, Billkind "
                  "sends one PayPal payout to the biller's verified PayPal account, with the customer's account number "
                  "in the payout note.",
    "refunds": "Sponsors are refunded in full through PayPal automatically if a bill is withdrawn by the poster, "
               "reaches its due date before it is fully funded, or cannot be paid out to the biller. If two sponsors "
               "pay the last part of a bill at the same time, the extra amount is refunded automatically. Refunds go "
               "back to the PayPal account that paid. A bill that was already paid to the biller cannot be refunded "
               "through Billkind.",
    "review": "Every bill goes through nine checks: verified biller, editing software traces, visual editing signs, "
              "two independent readings of the amount and date, line items adding up, a typical amount, a sensible "
              "due date, duplicates, and how many bills one account posts. Low-risk bills go live automatically. "
              "Others wait for a person, usually within a day. Bills that were already paid or are exact duplicates "
              "are not accepted.",
    "privacy": "Sponsors see the kind of bill, city, amount, due date, the last four characters of the account "
               "number and a short note written from the poster's words with consent. Names, phone numbers, emails "
               "and addresses are removed. Only the biller receives the full account number.",
    "currencies": "Bills in a currency PayPal supports are paid and paid out in that currency. Other currencies "
                  "settle in USD at an indicative rate shown on the bill card.",
    "timing": "PayPal payouts to billers usually complete within minutes in the sandbox. The sponsor sees the paid "
              "stamp and receipt as soon as PayPal confirms the payout.",
    "agent": "A giving agent pays bills with the sponsor's saved PayPal account within limits the sponsor confirmed: "
             "a monthly budget, a per-bill cap, chosen kinds of bills and countries, and a maximum risk score. The "
             "limits are enforced by the app, not by the AI. Every decision is logged with its reason.",
    "fees": "Billkind charges no fee in this hackathon build. PayPal sandbox fees are shown on the capture but no "
            "real money moves.",
}

SYSTEM = """You are Billkind's support assistant. Billkind lets sponsors pay household bills directly to verified
billers, never to the person. Answer the user's question using only facts returned by the tools in this
conversation. For any question about the user's own bills, payments or refunds, call my_bills or my_payments first.
Never invent amounts, dates, IDs, statuses or promises. If the tools do not contain the answer, say you
cannot see it and offer to pass the question to a person. Keep answers under 90 words, plain and kind. Quote amounts
and IDs exactly as the tools give them."""

TOOLS = [
    {"type": "function", "function": {"name": "my_bills", "description": "Bills the user posted, with status and reasons.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "my_payments", "description": "Payments the user made as a sponsor, with refunds and PayPal IDs.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "bill_detail", "description": "Details of one bill the user posted or paid into.",
                                      "parameters": {"type": "object", "properties": {"bill_id": {"type": "string"}},
                                                     "required": ["bill_id"]}}},
    {"type": "function", "function": {"name": "paypal_order", "description": "Live PayPal order status for one of the user's own orders, read through the PayPal Agent Toolkit.",
                                      "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                                                     "required": ["order_id"]}}},
    {"type": "function", "function": {"name": "policy", "description": "Billkind policy text.",
                                      "parameters": {"type": "object", "properties": {"topic": {"type": "string", "enum": list(POLICIES)}},
                                                     "required": ["topic"]}}},
]


# Tools: user-scoped reads ----------------------------------------------------------------------------

def my_bills(user: dict) -> list[dict]:
    out = []
    for b in db.q("SELECT * FROM bills WHERE poster_id = ? AND status != 'DRAFT' ORDER BY created_at DESC LIMIT 10",
                  (user["id"],)):
        reasons = [r["text"] for r in json.loads(b["risk_reasons"] or "[]") if r.get("points", 0) >= 10]
        out.append({"bill_id": b["id"], "status": states.BILL_LABELS.get(b["status"], b["status"]),
                    "amount": fmt(b["amount_minor"], b["currency"]), "biller": b["biller_name"],
                    "due_date": b["due_date"], "funded": fmt(b["funded_minor"], b["settle_currency"]),
                    "reasons_for_review": reasons if b["status"] in ("REVIEW", "REJECTED") else [],
                    "rejection": b["reject_reason"] if b["status"] in ("REJECTED", "WITHDRAWN") else ""})
    return out


def my_payments(user: dict) -> list[dict]:
    out = []
    for c in db.q("SELECT c.*, b.status AS bs, b.biller_name FROM contributions c JOIN bills b ON b.id = c.bill_id "
                  "WHERE c.sponsor_id = ? AND c.status != 'CANCELLED' ORDER BY c.created_at DESC LIMIT 12", (user["id"],)):
        out.append({"bill_id": c["bill_id"], "paid": fmt(c["amount_minor"], c["currency"]), "payment_status": c["status"],
                    "refunded": fmt(c["refunded_minor"], c["currency"]) if c["refunded_minor"] else "none",
                    "bill_status": states.BILL_LABELS.get(c["bs"], c["bs"]), "biller": c["biller_name"],
                    "paypal_order_id": c["order_id"], "paypal_capture_id": c["capture_id"], "via": c["source"]})
    return out


def bill_detail(user: dict, bill_id: str) -> dict:
    b = db.one("SELECT * FROM bills WHERE id = ?", (bill_id.strip().upper(),))
    if not b:
        return {"error": "no such bill"}
    is_sponsor = db.one("SELECT 1 FROM contributions WHERE bill_id = ? AND sponsor_id = ?", (b["id"], user["id"]))
    if b["poster_id"] != user["id"] and not is_sponsor:
        return {"error": "this bill is not linked to your account"}
    item = db.one("SELECT status, payout_item_id FROM payout_items WHERE bill_id = ? ORDER BY created_at DESC", (b["id"],))
    return {"bill_id": b["id"], "status": states.BILL_LABELS.get(b["status"], b["status"]), "biller": b["biller_name"],
            "amount": fmt(b["amount_minor"], b["currency"]), "settles_as": fmt(b["settle_minor"], b["settle_currency"]),
            "funded": fmt(b["funded_minor"], b["settle_currency"]), "due_date": b["due_date"],
            "payout": {"status": item["status"], "payout_item_id": item["payout_item_id"]} if item else "not sent yet",
            "note": b["reject_reason"]}


async def paypal_order(user: dict, order_id: str) -> dict:
    o = db.one("SELECT * FROM orders WHERE order_id = ? AND sponsor_id = ?", (order_id.strip().upper(), user["id"]))
    if not o:
        return {"error": "that PayPal order is not one of yours"}
    try:
        # The toolkit is synchronous (requests); run it off the event loop.
        data = await asyncio.to_thread(toolkit.get_order_details, o["order_id"])
    except Exception as exc:
        return {"error": f"PayPal lookup failed ({type(exc).__name__})"}
    raw = data.get("raw") or {}
    cap = next((c for pu in raw.get("purchase_units", []) for c in pu.get("payments", {}).get("captures", [])), {})
    return {"order_id": o["order_id"], "order_status": data.get("status"), "amount": data.get("amount"),
            "capture_id": cap.get("id", ""), "capture_status": cap.get("status", ""), "source": "PayPal Agent Toolkit"}


async def run_tool(user: dict, name: str, args: dict):
    if name == "my_bills":
        return my_bills(user)
    if name == "my_payments":
        return my_payments(user)
    if name == "bill_detail":
        return bill_detail(user, str(args.get("bill_id", "")))
    if name == "paypal_order":
        return await paypal_order(user, str(args.get("order_id", "")))
    if name == "policy":
        return POLICIES.get(str(args.get("topic")), "no such policy")
    return {"error": "unknown tool"}


# Grounding -------------------------------------------------------------------------------------------

FACT = re.compile(r"\b(BK-[A-Z0-9]{3,8}|[A-Z0-9]{13,17})\b|(\d[\d,]*\.\d{2}|\d{4}-\d{2}-\d{2})")


def ungrounded(answer: str, evidence: str) -> list[str]:
    ev = evidence.replace(",", "")
    missing = []
    for m in FACT.finditer(answer):
        tok = (m.group(1) or m.group(2)).replace(",", "")
        if m.group(1) and not tok.startswith("BK-") and not any(ch.isdigit() for ch in tok):
            continue  # an all-letter word in capitals, not an ID
        if tok not in ev:
            missing.append(tok)
    return missing


def safe_answer(user: dict, question: str) -> str:
    q = question.lower()
    if user["role"] == "poster":
        bills = my_bills(user)
        if not bills:
            return "You have not posted a bill yet. " + POLICIES["money_flow"]
        b = next((x for x in bills if x["bill_id"].lower() in q), bills[0])
        text = f"Bill {b['bill_id']} ({b['biller']}, {b['amount']}) is: {b['status']}."
        if b["reasons_for_review"]:
            text += " Reasons: " + " ".join(b["reasons_for_review"][:3])
        if b["rejection"]:
            text += " " + b["rejection"]
        return text
    pays = my_payments(user)
    if "refund" in q:
        refunded = [p for p in pays if p["refunded"] != "none"]
        base = POLICIES["refunds"]
        if refunded:
            return base + f" Your latest refund: {refunded[0]['refunded']} for bill {refunded[0]['bill_id']}."
        return base + " None of your payments has been refunded so far."
    if pays:
        p = pays[0]
        return (f"Your latest payment of {p['paid']} for bill {p['bill_id']} ({p['biller']}) is {p['payment_status'].lower()}; "
                f"the bill is: {p['bill_status']}. PayPal order {p['paypal_order_id']}.")
    for topic, words in (("agent", "agent"), ("privacy", "privacy"), ("currencies", "currenc"), ("review", "check")):
        if words in q:
            return POLICIES[topic]
    return POLICIES["money_flow"]


async def answer(user: dict, question: str, history: list[dict] | None = None) -> dict:
    question = question.strip()[:600]
    msgs = [{"role": "system", "content": SYSTEM + f"\nThe user is a {user['role']}."}]
    for h in (history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str):
            msgs.append({"role": h["role"], "content": h["content"][:600]})
    msgs.append({"role": "user", "content": question})
    evidence: list[str] = []
    tools_used: list[str] = []
    text = None
    for _ in range(4):
        resp = await llm.chat_raw(msgs, tools=TOOLS, kind="support")
        if resp is None:
            break
        calls = resp.get("tool_calls") or []
        if not calls:
            text = (resp.get("content") or "").strip()
            break
        msgs.append({"role": "assistant", "content": resp.get("content") or "", "tool_calls": calls})
        for call in calls[:4]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            result = await run_tool(user, name, args)
            out = json.dumps(result, ensure_ascii=False)[:4000]
            evidence.append(out)
            tools_used.append(name)
            msgs.append({"role": "tool", "tool_call_id": call["id"], "content": out})
    if not text:
        return {"answer": safe_answer(user, question), "source": "records", "tools": tools_used, "escalate": True}
    # Plain text for the chat bubble, and normalised so look-alike characters cannot slip past the check.
    for a, b in (("\u2011", "-"), ("\u2010", "-"), ("\u2012", "-"), ("\u2013", "-"), ("\u2014", ", "),
                 ("\u202f", " "), ("\u00a0", " "), ("**", ""), ("`", "")):
        text = text.replace(a, b)
    missing = ungrounded(text, " ".join(evidence) + " " + " ".join(POLICIES.values()))
    if missing:
        return {"answer": safe_answer(user, question), "source": "records", "tools": tools_used, "escalate": True,
                "note": "An AI draft mentioned details that are not in your records, so it was replaced."}
    return {"answer": text, "source": "ai", "tools": tools_used,
            "escalate": bool(re.search(r"person|human|team", text, re.I))}


def escalate(user: dict, transcript: list[dict], subject: str) -> str:
    tid = db.new_id("TCK")
    db.insert("tickets", {"id": tid, "user_id": user["id"], "subject": subject[:120] or "Support request",
                          "transcript": json.dumps(transcript[-12:])[:8000], "status": "OPEN", "created_at": db.now()})
    notify.send(user["id"], "notice", f"Ticket {tid} opened",
                "A person from the Billkind team will reply here, usually within a day.")
    for a in db.q("SELECT id FROM users WHERE role = 'admin'"):
        notify.send(a["id"], "review", f"Support ticket {tid}", subject[:300])
    return tid


def resolve(ticket_id: str, reply: str) -> None:
    t = db.one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    if not t:
        return
    db.update("tickets", "id", ticket_id, status="RESOLVED", resolution=reply[:2000])
    notify.send(t["user_id"], "notice", f"Reply to ticket {ticket_id}", reply[:2000])
