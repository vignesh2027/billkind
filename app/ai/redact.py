"""Privacy redaction. Nothing public may contain account numbers, names, phones, emails or addresses."""

from __future__ import annotations

import hashlib
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{6,}\d(?![\w])")
LONG_NUMBER = re.compile(r"\b[A-Z]{0,4}\d{6,}[A-Z0-9]*\b", re.I)
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


def scrub(text: str, names: list[str] | None = None, extra: list[str] | None = None) -> str:
    out = text or ""
    for term in sorted(filter(None, extra or []), key=len, reverse=True):
        if len(term) >= 4:
            out = re.sub(re.escape(term), "[address]", out, flags=re.I)
    out = EMAIL.sub("[email]", out)
    out = STREET.sub("[address]", out)
    out = PHONE.sub("[number]", out)
    out = LONG_NUMBER.sub("[number]", out)
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
                       ("[name]", "personal name"), ("[address]", "street address")):
        if tag in scrubbed and tag not in original:
            kinds.append(label)
    return kinds


def clean_period(period: str | None, names: list[str] | None = None) -> str | None:
    """A billing period or school term is safe to show ("Oct 2026", "Term 3"). Anything carrying an email, a long
    number or a person's name is dropped rather than shown."""
    p = re.sub(r"\s+", " ", (period or "")).strip()[:60]
    if not p or EMAIL.search(p) or LONG_NUMBER.search(p):
        return None
    for name in filter(None, names or []):
        for part in [name] + [x for x in re.split(r"\s+", name) if len(x) >= 3]:
            if re.search(rf"\b{re.escape(part)}\b", p, re.I):
                return None
    return p
