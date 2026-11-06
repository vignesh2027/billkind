"""Authenticity and fraud checks. Deterministic rules decide; model signals only add weight.

Output: a 0 to 100 risk score, plain-language reasons, and a decision:
  list    low risk, verified biller: goes live automatically
  review  a person checks it first (medium risk or unknown biller)
  reject  a hard rule failed (exact duplicate, bill already paid); the reason is shown to the poster
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from .. import db
from ..core.money import USD_RATES, exponent, fmt
from .intake import EDITING_SOFTWARE, hamming

REVIEW_AT = 30
REJECT_AT = 80

# Typical bill sizes in USD by category: (low, high). Outside the band adds risk, it never rejects.
TYPICAL_USD = {"electricity": (3, 600), "water": (3, 300), "school_fees": (5, 3000), "medical": (5, 5000),
               "rent": (30, 4000), "phone": (2, 200), "other": (3, 1000)}

LIMITS = {"open_bills": 3, "bills_30d": 4, "open_usd": 1500, "device_30d": 5}


@dataclass
class Risk:
    score: int = 0
    reasons: list[dict] = field(default_factory=list)
    hard: list[str] = field(default_factory=list)

    def add(self, points: int, code: str, text: str) -> None:
        self.score += points
        self.reasons.append({"code": code, "points": points, "text": text})

    def fail(self, code: str, text: str) -> None:
        self.hard.append(text)
        self.reasons.append({"code": code, "points": 100, "text": text})

    @property
    def decision(self) -> str:
        if self.hard:
            return "reject"
        if self.score >= REVIEW_AT or any(r["code"] == "biller_unverified" for r in self.reasons):
            return "review"
        return "list"

    def as_dict(self) -> dict:
        return {"score": min(100, self.score if not self.hard else 100), "reasons": self.reasons,
                "decision": self.decision}


def _usd(minor: int, currency: str) -> Decimal:
    rate = USD_RATES.get(currency)
    if not rate:
        return Decimal(0)
    return Decimal(minor) / (Decimal(10) ** exponent(currency)) / rate


def assess(*, poster_id: str, bill: dict, extraction: dict, doc_meta: dict, doc_sha: str, phash: str,
           account_hash: str, biller: dict | None, biller_score: float, edited_fields: list[str],
           device: str = "", today: dt.date | None = None, exclude_bill: str = "") -> Risk:
    today = today or dt.date.today()
    r = Risk()
    cur = bill["currency"]

    # Biller registry
    if biller is None:
        r.add(35, "biller_unverified", f"We have not verified \"{bill['biller_name']}\" yet. A person confirms "
              "the biller's payment account before any money can move.")
    elif not biller["verified"]:
        r.add(35, "biller_unverified", f"{biller['name']} is waiting for account verification.")
    elif bill.get("country") and biller["country"] != bill["country"]:
        r.add(15, "biller_country", f"The biller is registered in {biller['country']} but the bill says {bill['country']}.")

    # Document signals
    software = (doc_meta.get("software") or doc_meta.get("producer") or doc_meta.get("creator") or "").lower()
    hit = next((s for s in EDITING_SOFTWARE if s in software), None)
    if hit:
        r.add(30, "edit_software", f"The file was last saved by editing software ({software[:40]}).")
    if doc_meta.get("created") and doc_meta.get("modified") and doc_meta["created"][:10] != doc_meta["modified"][:10]:
        r.add(10, "pdf_modified", "The PDF was modified after it was first created.")
    if extraction.get("looks_edited"):
        signs = "; ".join(extraction.get("edit_signs") or []) or "visual inconsistencies"
        r.add(20, "visual_edit", f"The image shows possible editing: {signs}.")
    if extraction.get("source") == "none":
        r.add(15, "unreadable", "The document could not be read automatically.")

    ocr = extraction.get("ocr") or {}
    if ocr.get("amount") and extraction.get("amount") and extraction.get("source") == "ai":
        try:
            if Decimal(ocr["amount"]) != Decimal(extraction["amount"]):
                r.add(25, "amount_disagree", f"Two independent readings of the amount disagree "
                      f"({extraction['amount']} and {ocr['amount']}).")
        except ArithmeticError:
            pass
    if ocr.get("due_date") and extraction.get("due_date") and ocr["due_date"] != extraction["due_date"] \
            and extraction.get("source") == "ai":
        r.add(10, "date_disagree", "Two independent readings of the due date disagree.")
    if "amount" in edited_fields:
        r.add(20, "amount_edited", "The amount was changed by hand after the bill was read.")
    if "biller_name" in edited_fields:
        r.add(10, "biller_edited", "The biller name was changed by hand after the bill was read.")

    items = extraction.get("line_items") or []
    if len(items) >= 2 and extraction.get("amount"):
        try:
            total = sum(Decimal(str(i.get("amount", "0")).replace(",", "")) for i in items)
            if abs(total - Decimal(extraction["amount"])) > Decimal("0.01") and \
                    abs(total - Decimal(extraction["amount"])) > Decimal(extraction["amount"]) * Decimal("0.005"):
                r.add(20, "items_mismatch", f"The line items add up to {total}, not the printed total "
                      f"{extraction['amount']}.")
        except (ArithmeticError, ValueError):
            pass

    # Sanity
    usd = _usd(bill["amount_minor"], cur)
    low, high = TYPICAL_USD.get(bill["category"], TYPICAL_USD["other"])
    if usd and usd > high * 2:
        r.add(30, "amount_high", f"{fmt(bill['amount_minor'], cur)} is far above a typical {bill['category'].replace('_', ' ')} bill.")
    elif usd and usd > high:
        r.add(15, "amount_high", f"{fmt(bill['amount_minor'], cur)} is above a typical {bill['category'].replace('_', ' ')} bill.")
    elif usd and usd < low:
        r.add(5, "amount_low", "The amount is unusually small for this kind of bill.")
    if cur not in USD_RATES:
        r.add(10, "currency_unknown", f"We do not have a rate for {cur}; a person will confirm the settlement amount.")
    try:
        due = dt.date.fromisoformat(bill["due_date"])
        days = (due - today).days
        if days < -60:
            r.add(20, "long_overdue", f"The bill was due {-days} days ago.")
        elif days < 0:
            r.add(5, "overdue", f"The bill is {-days} days overdue (still payable).")
        elif days > 120:
            r.add(10, "far_future", "The due date is more than four months away.")
    except (TypeError, ValueError):
        r.add(15, "no_due_date", "The due date could not be confirmed.")

    # Duplicates
    same_file = db.one("SELECT id, status FROM bills WHERE doc_sha = ? AND id != ?", (doc_sha, exclude_bill))
    if same_file:
        r.fail("duplicate_file", f"This exact file was already posted as bill {same_file['id']}.")
    if phash and len(phash) == 64:
        for other in db.q("SELECT id, phash FROM bills WHERE phash != '' AND id != ? AND status NOT IN ('REJECTED')",
                          (exclude_bill,)):
            if other["id"] != (same_file or {}).get("id") and hamming(phash, other["phash"]) <= 10:
                r.add(40, "near_duplicate", f"The image is nearly identical to bill {other['id']}.")
                break
    if account_hash:
        since = db.now() - 60 * 86400
        prior = db.q("SELECT id, status, amount_minor FROM bills WHERE account_hash = ? AND id != ? AND created_at > ? "
                     "AND status NOT IN ('REJECTED','WITHDRAWN','EXPIRED')", (account_hash, exclude_bill, since))
        for p in prior:
            if p["amount_minor"] == bill["amount_minor"]:
                if p["status"] == "PAID":
                    r.fail("already_paid", f"The same bill (same account and amount) was already paid as {p['id']}.")
                else:
                    r.add(40, "same_account_amount", f"Bill {p['id']} has the same account and amount.")
                break
        else:
            if prior:
                r.add(10, "same_account", "Another recent bill exists for the same account.")

    # Velocity
    open_rows = db.q("SELECT amount_minor, currency FROM bills WHERE poster_id = ? AND id != ? AND status IN "
                     "('SUBMITTED','REVIEW','LISTED','FUNDING','FUNDED')", (poster_id, exclude_bill))
    if len(open_rows) >= LIMITS["open_bills"]:
        r.add(25, "velocity_open", f"You already have {len(open_rows)} open bills.")
    recent = db.one("SELECT COUNT(*) AS n FROM bills WHERE poster_id = ? AND id != ? AND created_at > ?",
                    (poster_id, exclude_bill, db.now() - 30 * 86400))["n"]
    if recent >= LIMITS["bills_30d"]:
        r.add(20, "velocity_30d", f"{recent} bills were posted from this account in the last 30 days.")
    open_usd = sum(_usd(o["amount_minor"], o["currency"]) for o in open_rows) + usd
    if open_usd > LIMITS["open_usd"]:
        r.add(20, "velocity_amount", "The total of your open bills is above the limit for new accounts.")
    user = db.one("SELECT created_at FROM users WHERE id = ?", (poster_id,))
    if user and db.now() - user["created_at"] < 86400:
        r.add(5, "new_account", "This account is less than a day old.")
    if device:
        n = db.one("SELECT COUNT(DISTINCT poster_id) AS n FROM bills WHERE extraction LIKE ? AND created_at > ?",
                   (f'%"device": "{device}"%', db.now() - 30 * 86400))["n"]
        if n >= LIMITS["device_30d"]:
            r.add(25, "velocity_device", "Several accounts posted bills from the same device this month.")
    return r

