"""Drop a bill: turn one AI reading of a bill into a pre-filled, editable form.

Every field gets a confidence level that code works out, not the model alone:
  high    two independent readings agree (vision model and OCR), or the biller is in the verified registry
  medium  one reading found it and nothing contradicts it
  low     missing, the readings disagree, or it breaks a bill rule (currency we cannot settle, odd amount, old date)
The poster always confirms before anything is posted. Nothing here moves money or lists a bill.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from ..core.money import USD_RATES, ZERO_DECIMAL, exponent, to_minor
from . import redact
from .intake import CATEGORY_LABELS, CATEGORIES

LEVELS = ("low", "medium", "high")
FIELDS = ("biller_name", "amount", "currency", "due_date", "category", "account_reference", "service_period",
          "service_address")


def _alnum(v) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(v or "")).upper()


def _same_amount(a, b) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (ArithmeticError, ValueError):
        return False


def _model_says_unsure(fields: dict, key: str) -> bool:
    c = (fields.get("confidence") or {}).get(key)
    return c is not None and c < 0.6


def validate(fields: dict, today: dt.date | None = None) -> dict[str, str]:
    """Problems with the extracted values under the existing bill rules, as {field: plain-words reason}.

    These are the same rules posting and the fraud checks apply later (settlement currencies, amount format,
    typical amounts, the open-bill limit for new accounts, due date window). A problem never blocks the form; it
    lowers the field's confidence and tells the poster what to check.
    """
    from .fraud import LIMITS, TYPICAL_USD
    today = today or dt.date.today()
    issues: dict[str, str] = {}
    cur = (fields.get("currency") or "").upper()
    if cur and cur not in USD_RATES:
        issues["currency"] = f"Billkind cannot settle bills in {cur} yet."
    amount = fields.get("amount")
    if amount:
        try:
            minor = to_minor(amount, cur if cur in USD_RATES else "USD")
            if minor <= 0:
                issues["amount"] = "The amount must be more than zero."
            elif cur in ZERO_DECIMAL and "." in str(amount) and Decimal(str(amount)) % 1:
                issues["amount"] = f"{cur} amounts have no decimals."
            elif cur in USD_RATES:
                usd = Decimal(minor) / (Decimal(10) ** exponent(cur)) / USD_RATES[cur]
                low, high = TYPICAL_USD.get(fields.get("category") or "other", TYPICAL_USD["other"])
                if usd > LIMITS["open_usd"]:
                    issues["amount"] = (f"About {usd:,.0f} USD, above the {LIMITS['open_usd']:,} USD limit for open "
                                        "bills on a new account. Check it, or post one part of the bill.")
                elif usd > high:
                    issues["amount"] = "Higher than a typical bill of this kind. Please check it."
        except ValueError:
            issues["amount"] = "The amount could not be read as a number."
    due = fields.get("due_date")
    if due:
        try:
            days = (dt.date.fromisoformat(due) - today).days
            if days < -60:
                issues["due_date"] = f"This date is {-days} days ago. Is the bill still payable?"
            elif days > 120:
                issues["due_date"] = "More than four months away. Please check the due date."
        except ValueError:
            issues["due_date"] = "Please enter the date as YYYY-MM-DD."
    if fields.get("category") and fields["category"] not in CATEGORIES:
        issues["category"] = "Unknown kind of bill."
    return issues


def confidence(fields: dict, *, biller: dict | None, biller_score: float,
               today: dt.date | None = None) -> dict[str, dict]:
    """{field: {"level": low|medium|high, "why": short reason}} for each pre-filled field."""
    ocr = fields.get("ocr") or {}
    ai = fields.get("source") == "ai"
    issues = validate(fields, today)
    out: dict[str, dict] = {}

    def put(key: str, level: str, why: str) -> None:
        if key in issues:
            level, why = "low", issues[key]
        elif level == "high" and _model_says_unsure(fields, key):
            level, why = "medium", "Printed faintly or partly hidden. Please check it."
        out[key] = {"level": level, "why": why}

    # Biller: the registry is the strongest signal.
    if not fields.get("biller_name"):
        put("biller_name", "low", "Not found on the bill. Please type the biller's name.")
    elif biller and biller.get("verified"):
        put("biller_name", "high", "Matches a verified biller in the registry.")
    elif biller:
        put("biller_name", "medium", "In the registry, waiting for account verification.")
    else:
        put("biller_name", "medium" if ai else "low", "Read from the bill. Not in the verified registry yet, "
                                                      "so a person checks the biller first.")

    # Amount and due date: two independent readings.
    for key, label in (("amount", "amount"), ("due_date", "due date")):
        v = fields.get(key)
        other = ocr.get(key)
        if not v:
            put(key, "low", f"No {label} found. Please type it from the bill.")
        elif other and (_same_amount(v, other) if key == "amount" else v == other):
            put(key, "high", "Two independent readings agree.")
        elif other:
            put(key, "low", f"Two readings disagree ({v} and {other}). Please check the {label}.")
        else:
            put(key, "medium", "Read once. Please check it against the bill.")

    cur = fields.get("currency")
    if not cur:
        put("currency", "low", "No currency found. Please choose one.")
    elif ocr.get("currency") == cur:
        put("currency", "high", "Printed on the bill.")
    else:
        put("currency", "medium", "Worked out from the bill and the country.")

    cat = fields.get("category") or "other"
    if biller and biller.get("category") == cat:
        put("category", "high", "Matches the biller.")
    elif cat == "other":
        put("category", "low", "Could not tell the kind of bill. Please choose one.")
    else:
        put("category", "medium", f"Looks like a {CATEGORY_LABELS.get(cat, cat).lower()} bill.")

    acc = fields.get("account_reference")
    if not acc:
        put("account_reference", "low", "No account number found. Please type it from the bill.")
    elif ocr.get("account_reference") and _alnum(ocr["account_reference"]) == _alnum(acc):
        put("account_reference", "high", "Two independent readings agree. Sponsors see only the last four.")
    else:
        put("account_reference", "medium", "Read once. Please check it. Sponsors see only the last four.")

    for key, label in (("service_period", "service period"), ("service_address", "service address")):
        if fields.get(key):
            put(key, "medium", "Read from the bill." + (" Kept private, never shown to sponsors."
                                                         if key == "service_address" else ""))
        else:
            put(key, "low", f"No {label} printed. Optional.")
    return out


def summary(conf: dict[str, dict]) -> dict:
    counts = {lvl: sum(1 for c in conf.values() if c["level"] == lvl) for lvl in LEVELS}
    check = [k for k in ("biller_name", "amount", "currency", "due_date", "account_reference")
             if conf.get(k, {}).get("level") == "low"]
    return {"counts": counts, "check": check}


def scan_view(bill: dict, fields: dict, conf: dict[str, dict], account_full: str) -> dict:
    """What the poster's own browser receives after a scan. The full account number goes only to the poster who
    uploaded the bill (so they can check it); everything stored for later shows the last four only."""
    values = {
        "biller_name": bill["biller_name"] if bill.get("biller_id") else (fields.get("biller_name") or ""),
        "amount": fields.get("amount") or "",
        "currency": (fields.get("currency") or bill.get("currency") or "").upper(),
        "due_date": fields.get("due_date") or "",
        "category": fields.get("category") or "other",
        "account_reference": account_full,
        "service_period": fields.get("service_period") or "",
        "service_address": fields.get("service_address") or "",
        "city": bill.get("city") or "",
        "country": bill.get("country") or "",
    }
    return {
        "bill_id": bill["id"], "values": values, "confidence": conf, "summary": summary(conf),
        "source": fields.get("source"), "reader": fields.get("reader"),
        "account_masked": redact.mask_account(account_full),
        "verified_biller": bool(bill.get("biller_id")),
        "line_items": fields.get("line_items") or [],
        "submit_url": f"/post/{bill['id']}",
    }


def document_check(extraction: dict, verified_biller: bool) -> dict:
    """What the bill document itself confirms, for sponsors and the giving agent. Only facts the app recorded."""
    ocr = extraction.get("ocr") or {}
    edited = extraction.get("edited_fields") or []
    ai = extraction.get("source") == "ai"
    amount_ok = bool(ai and extraction.get("amount") and ocr.get("amount")
                     and _same_amount(extraction["amount"], ocr["amount"]) and "amount" not in edited)
    due_ok = bool(ai and extraction.get("due_date") and ocr.get("due_date") == extraction.get("due_date"))
    parts = []
    if amount_ok and due_ok:
        parts.append("amount and due date confirmed by two readings of the bill")
    elif amount_ok:
        parts.append("amount confirmed by two readings of the bill")
    if "amount" in edited:
        parts.append("amount typed by the poster")
    if verified_biller:
        parts.append("biller verified")
    if extraction.get("source") == "seed":
        text = "Fictional demo bill" + ("; biller verified" if verified_biller else "")
    else:
        text = "; ".join(parts).capitalize() if parts else "Entered by the poster"
    return {"read_by": extraction.get("source") or "none", "amount_confirmed": amount_ok, "due_confirmed": due_ok,
            "edited": edited, "biller_verified": verified_biller, "summary": text}
