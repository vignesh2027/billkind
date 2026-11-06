"""Poster flow: upload, AI reading, review, checks, listing."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
from pathlib import Path

from .. import db
from ..ai import fraud, intake, redact, story
from ..config import settings
from . import billers, events, notify, states
from .money import USD_RATES, convert, fmt, settlement_for, to_minor


class PostingError(Exception):
    pass


def _bill_id() -> str:
    while True:
        bid = "BK-" + "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5))
        if not db.one("SELECT 1 FROM bills WHERE id = ?", (bid,)):
            return bid


async def upload(poster: dict, data: bytes, filename: str, content_type: str, device: str = "") -> dict:
    if len(data) > 8 * 1024 * 1024:
        raise PostingError("Files up to 8 MB, please.")
    try:
        doc = await asyncio.to_thread(intake.read_document, data, filename, content_type)
    except Exception as exc:
        raise PostingError("That file could not be opened. Please upload a photo (JPG or PNG) or a PDF.") from exc
    fields = await intake.extract(doc)
    biller, score = billers.match(fields.get("biller_name") or "", fields.get("country"))
    bid = _bill_id()
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    ext = ".pdf" if doc.kind == "pdf" else ".jpg"
    path = Path(settings.upload_dir) / f"{bid}{ext}"
    path.write_bytes(data if doc.kind == "pdf" else (doc.image or data))
    currency = fields.get("currency") or poster.get("currency") or "USD"
    amount_minor = 0
    if fields.get("amount"):
        try:
            amount_minor = to_minor(fields["amount"], currency)
        except ValueError:
            amount_minor = 0
    extraction = dict(fields, doc_meta=doc.meta, device=device,
                      biller_match={"id": biller["id"] if biller else None, "score": round(score, 2)})
    now = db.now()
    db.insert("bills", {
        "id": bid, "poster_id": poster["id"], "biller_id": biller["id"] if biller else None,
        "biller_name": (biller["name"] if biller else fields.get("biller_name")) or "Unknown biller",
        "category": fields.get("category") or (biller["category"] if biller else "other"),
        "country": fields.get("country") or (biller["country"] if biller else poster.get("country", "")),
        "city": fields.get("city") or (biller["city"] if biller else ""),
        "currency": currency, "amount_minor": amount_minor, "settle_currency": settlement_for(currency),
        "settle_minor": 0, "due_date": fields.get("due_date") or "", "status": "DRAFT",
        "extraction": json.dumps(extraction), "doc_path": str(path), "doc_sha": doc.sha256, "phash": doc.phash,
        "created_at": now, "updated_at": now,
    })
    return db.one("SELECT * FROM bills WHERE id = ?", (bid,))


async def submit(poster: dict, bill_id: str, form: dict, today: dt.date | None = None) -> dict:
    bill = db.one("SELECT * FROM bills WHERE id = ? AND poster_id = ?", (bill_id, poster["id"]))
    if not bill:
        raise PostingError("Unknown bill.")
    if bill["status"] != "DRAFT":
        raise PostingError("This bill was already submitted.")
    if not form.get("consent"):
        raise PostingError("Please confirm the consent box so we can show your note to sponsors.")
    ext = json.loads(bill["extraction"])
    currency = (form.get("currency") or bill["currency"]).upper().strip()[:3]
    try:
        amount_minor = to_minor(form.get("amount") or "", currency)
    except ValueError:
        raise PostingError("Please enter the amount due as a number, for example 84.20.")
    if amount_minor <= 0:
        raise PostingError("The amount must be more than zero.")
    try:
        dt.date.fromisoformat(form.get("due_date") or "")
    except ValueError:
        raise PostingError("Please enter the due date as YYYY-MM-DD.")
    account_ref = (form.get("account_reference") or ext.get("account_reference") or "").strip()
    if not account_ref:
        raise PostingError("Please enter the account, student or patient number printed on the bill. "
                           "It is only shared with the biller.")
    edited = []
    if ext.get("amount"):
        try:
            if to_minor(ext["amount"], ext.get("currency") or currency) != amount_minor:
                edited.append("amount")
        except ValueError:
            pass
    biller_name = (form.get("biller_name") or bill["biller_name"]).strip()
    if ext.get("biller_name") and billers.norm(biller_name) != billers.norm(ext["biller_name"]) \
            and billers.norm(biller_name) != billers.norm(bill["biller_name"]):
        edited.append("biller_name")
    country = (form.get("country") or bill["country"] or "").upper()[:2]
    biller, score = billers.match(biller_name, country or None)
    settle = settlement_for(currency)
    if settle == currency:
        settle_minor, fx_note = amount_minor, ""
    elif currency in USD_RATES:
        settle_minor = convert(amount_minor, currency, settle)
        fx_note = (f"{currency} is not a PayPal settlement currency, so this bill settles in {settle} at an "
                   f"indicative {USD_RATES[currency]} {currency} per USD ({fmt(settle_minor, settle)}).")
    else:
        raise PostingError(f"We cannot settle bills in {currency} yet.")
    fields = {
        "biller_id": biller["id"] if biller else None,
        "biller_name": biller["name"] if biller else biller_name,
        "category": form.get("category") if form.get("category") in intake.CATEGORIES else bill["category"],
        "country": country or (biller["country"] if biller else ""),
        "city": (form.get("city") or bill["city"] or (biller["city"] if biller else "")).strip()[:60],
        "currency": currency, "amount_minor": amount_minor, "settle_currency": settle, "settle_minor": settle_minor,
        "fx_note": fx_note, "due_date": form["due_date"],
        "account_masked": redact.mask_account(account_ref),
        "account_private": account_ref[:64],
        "account_hash": redact.account_hash(account_ref, biller["id"] if biller else billers.norm(biller_name)),
        "alias": (form.get("alias") or "").strip()[:24], "poster_words": (form.get("words") or "").strip()[:1500],
        "story_consent": 1,
    }
    risk = fraud.assess(poster_id=poster["id"], bill={**bill, **fields}, extraction=ext,
                        doc_meta=ext.get("doc_meta") or {}, doc_sha=bill["doc_sha"], phash=bill["phash"],
                        account_hash=fields["account_hash"], biller=biller, biller_score=score, edited_fields=edited,
                        device=ext.get("device", ""), today=today, exclude_bill=bill_id)
    names = [n for n in [ext.get("customer_name"), poster.get("name")] if n]
    text, source = await story.write(category=fields["category"], city=fields["city"], country=fields["country"],
                                     alias=fields["alias"], words=fields["poster_words"], names=names,
                                     address=ext.get("service_address"))
    ext["story_source"] = source
    ext["edited_fields"] = edited
    # The full account reference is stored in one private column and only ever sent to the biller in the
    # payout note. Public cards and events use the masked form.
    ext["account_reference"] = None
    risk_d = risk.as_dict()
    db.update("bills", "id", bill_id, **fields, story=text, risk_score=risk_d["score"],
              risk_reasons=json.dumps(risk_d["reasons"]), decision=risk_d["decision"],
              extraction=json.dumps(ext), updated_at=db.now())
    bill = db.one("SELECT * FROM bills WHERE id = ?", (bill_id,))
    states.check_bill("DRAFT", "SUBMITTED")
    db.update("bills", "id", bill_id, status="SUBMITTED")
    bill["status"] = "SUBMITTED"
    return _route(bill, risk_d)


def _route(bill: dict, risk: dict) -> dict:
    from .funding import set_status
    decision = risk["decision"]
    if decision == "reject":
        reason = next((r["text"] for r in risk["reasons"] if r["points"] >= 100), "A safety check failed.")
        bill = set_status(bill, "REJECTED", reject_reason=reason)
        notify.send(bill["poster_id"], "notice", f"Bill {bill['id']} was not accepted", reason, bill_id=bill["id"])
    elif decision == "review":
        bill = set_status(bill, "REVIEW")
        notify.send(bill["poster_id"], "notice", f"Bill {bill['id']} is with our review team",
                    "A person will look at it, usually within a day. You will get a message either way.",
                    bill_id=bill["id"])
        for a in db.q("SELECT id FROM users WHERE role = 'admin'"):
            notify.send(a["id"], "review", f"Review: bill {bill['id']} (risk {risk['score']})",
                        "; ".join(r["text"] for r in risk["reasons"])[:600], bill_id=bill["id"])
    else:
        bill = list_bill(bill)
    return bill


def list_bill(bill: dict) -> dict:
    from .funding import set_status
    bill = set_status(bill, "LISTED", listed_at=db.now())
    events.publish("listed", f"New {bill['category'].replace('_', ' ')} bill from {bill['city'] or bill['country']}",
                   bill=bill, amount_minor=bill["amount_minor"], currency=bill["currency"])
    notify.send(bill["poster_id"], "notice", f"Bill {bill['id']} is live",
                "Sponsors can now see your bill card. Money goes straight to the biller when it is covered.",
                bill_id=bill["id"])
    return bill


def admin_decide(bill_id: str, approve: bool, note: str = "") -> dict:
    from .funding import set_status
    bill = db.one("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if not bill or bill["status"] != "REVIEW":
        raise PostingError("Only bills in review can be decided.")
    if approve:
        if not bill["biller_id"]:
            raise PostingError("Verify or add the biller first, then approve.")
        biller = db.one("SELECT * FROM billers WHERE id = ?", (bill["biller_id"],))
        if not biller or not biller["verified"]:
            raise PostingError("The biller's account is not verified yet.")
        return list_bill(bill)
    reason = note or "Our review team could not verify this bill."
    bill = set_status(bill, "REJECTED", reject_reason=reason)
    notify.send(bill["poster_id"], "notice", f"Bill {bill_id} was not accepted", reason, bill_id=bill_id)
    return bill
