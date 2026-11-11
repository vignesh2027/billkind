"""Privacy redaction. Nothing public may contain account numbers, names, phones, emails or addresses."""

from __future__ import annotations

import hashlib
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")
LONG_NUMBER = re.compile(r"\b[A-Z]{0,4}\d{6,}[A-Z0-9]*\b", re.I)
# Direct-payment handles: money must only ever reach the biller, so nothing public may point sponsors elsewhere.
UPI = re.compile(r"(?<![\w.@-])[\w.-]{2,}@[A-Za-z]{2,}\b")          # amma123@okaxis (no dot after the @)
CASHTAG = re.compile(r"(?<!\w)\$[A-Za-z]\w{1,}")                      # $ammaR
PAY_APP = re.compile(r"\b(?:paypal(?:\.me|\s+me)|venmo|cash\s?app|gpay|google\s?pay|phone\s?pe|paytm|upi|zelle)\b"
                     r"[\s:/@-]*(?:\[(?:contact|email)\]|[\w.@-]+)?", re.I)
# Short references with letters and digits (roll, patient or customer numbers such as KV-88213, STU4512, 2023CS114)
# that the long-number pattern misses. Four or more digits and at least one letter, so years and amounts still show.
MIXED_ID = re.compile(r"\b(?=[A-Za-z0-9/-]*[A-Za-z])(?=(?:[A-Za-z/-]*\d){4})[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*\b")
STREET = re.compile(r"\b\d{1,5}\s+[A-Za-z][A-Za-z ]{2,30}\s(street|st|road|rd|avenue|ave|lane|ln|nagar|marg|"
                    r"close|drive|dr|way|block|sector)\b\.?", re.I)


def mask_account(ref: str | None) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", ref or "")
    if not clean:
        return ""
    if len(clean) <= 4:
        return "••••"
    return "•••• " + clean[-4:]


def account_hash(ref: str | None, biller_key: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", ref or "").upper()
    if not clean:
        return ""
    return hashlib.sha256(f"{biller_key}|{clean}".encode()).hexdigest()


def _account_pattern(ref: str) -> re.Pattern | None:
    """The bill's own account reference, however it is typed: case, spaces, dots, dashes or slashes between
    characters do not matter."""
    chars = re.sub(r"[^A-Za-z0-9]", "", ref or "")
    if len(chars) < 4:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s./-]?".join(re.escape(c) for c in chars) + r"(?![A-Za-z0-9])", re.I)


def _is_mixed_id(m: re.Match) -> str:
    token = m.group(0)
    return "[number]" if len(re.sub(r"[^A-Za-z0-9]", "", token)) >= 6 else token


def payment_handles(text: str) -> str:
    out = UPI.sub("[contact]", text or "")
    out = CASHTAG.sub("[contact]", out)
    return PAY_APP.sub("[contact]", out)


def scrub(text: str, names: list[str] | None = None, extra: list[str] | None = None,
          accounts: list[str] | None = None) -> str:
    out = text or ""
    for ref in filter(None, accounts or []):
        pat = _account_pattern(ref)
        if pat:
            out = pat.sub("[number]", out)
    for term in sorted(filter(None, extra or []), key=len, reverse=True):
        if len(term) >= 4:
            out = re.sub(re.escape(term), "[address]", out, flags=re.I)
    out = EMAIL.sub("[email]", out)
    out = payment_handles(out)
    out = STREET.sub("[address]", out)
    out = PHONE.sub("[number]", out)
    out = LONG_NUMBER.sub("[number]", out)
    out = MIXED_ID.sub(_is_mixed_id, out)
    for name in filter(None, names or []):
        parts = [name] + [p for p in re.split(r"\s+", name) if len(p) >= 3]
        for p in parts:
            out = re.sub(rf"\b{re.escape(p)}\b", "[name]", out, flags=re.I)
    out = re.sub(r"(\[name\]\s*){2,}", "[name] ", out)
    return out.strip()


def findings(original: str, scrubbed: str) -> list[str]:
    """What redaction removed, for the poster's preview."""
    kinds = []
    for tag, label in (("[email]", "email address"), ("[number]", "account or phone number"),
                       ("[name]", "personal name"), ("[address]", "street address"),
                       ("[contact]", "payment handle")):
        if tag in scrubbed and tag not in original:
            kinds.append(label)
    return kinds


def clean_period(period: str | None, names: list[str] | None = None) -> str | None:
    """A billing period or school term is safe to show ("Oct 2026", "Term 3"). Anything carrying an email, a long
    number or a person's name is dropped rather than shown."""
    p = re.sub(r"\s+", " ", (period or "")).strip()[:60]
    if not p or EMAIL.search(p) or LONG_NUMBER.search(p) or payment_handles(p) != p:
        return None
    for name in filter(None, names or []):
        for part in [name] + [x for x in re.split(r"\s+", name) if len(x) >= 3]:
            if re.search(rf"\b{re.escape(part)}\b", p, re.I):
                return None
    return p


PLACE_OK = re.compile(r"[^\W\d_]+(?:[ .'-]+[^\W\d_]+)*\.?")


def safe_place(text: str | None, maxlen: int = 60) -> str:
    """A city, town or alias shown in public as typed, but only when it is a plain name: letters, spaces, hyphens,
    apostrophes and periods. Anything with digits, an @, a $ or a payment app name is dropped, so the field cannot
    carry a phone number or a direct-payment handle."""
    t = re.sub(r"\s+", " ", (text or "")).strip()[:maxlen]
    if not t or not PLACE_OK.fullmatch(t) or payment_handles(t) != t:
        return ""
    return t
