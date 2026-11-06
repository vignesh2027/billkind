"""Bill intake: read a photo or PDF, extract structured fields, collect authenticity signals.

Primary path: an open vision model (photos) or text model (PDF text) returns structured JSON.
Fallback path: Tesseract OCR or PDF text plus a regex parser. Both run when possible, so the
two readings can be cross-checked for the fraud model.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from PIL import ExifTags, Image, ImageOps
from pypdf import PdfReader

from . import llm

CATEGORIES = ["electricity", "water", "school_fees", "medical", "rent", "phone", "other"]
CATEGORY_LABELS = {"electricity": "Electricity", "water": "Water", "school_fees": "School fees",
                   "medical": "Medical", "rent": "Rent", "phone": "Phone", "other": "Other"}
EDITING_SOFTWARE = ("photoshop", "gimp", "canva", "pixelmator", "affinity", "paint.net", "snapseed",
                    "picsart", "illustrator", "inkscape", "photopea", "word", "pages", "libreoffice writer")


@dataclass
class Doc:
    kind: str                      # image or pdf
    sha256: str
    text: str = ""                 # OCR or PDF text
    meta: dict = field(default_factory=dict)
    phash: str = ""
    image: bytes = b""             # normalized JPEG for the vision model
    mime: str = "image/jpeg"


def _ahash(img: Image.Image) -> str:
    small = img.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    bits = "".join("1" if p > avg else "0" for p in px)
    return f"{int(bits, 2):064x}"


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 256
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _ocr(img: Image.Image) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    buf = io.BytesIO()
    img.convert("L").save(buf, format="PNG")
    try:
        out = subprocess.run([exe, "stdin", "stdout", "--psm", "4"], input=buf.getvalue(),
                             capture_output=True, timeout=25)
        return out.stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return ""


def read_document(data: bytes, filename: str = "", content_type: str = "") -> Doc:
    sha = hashlib.sha256(data).hexdigest()
    is_pdf = data[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
    if is_pdf:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:4])
        info = reader.metadata or {}
        meta = {"producer": str(info.get("/Producer", "") or ""), "creator": str(info.get("/Creator", "") or ""),
                "created": str(info.get("/CreationDate", "") or ""), "modified": str(info.get("/ModDate", "") or ""),
                "pages": len(reader.pages)}
        return Doc(kind="pdf", sha256=sha, text=text, meta=meta, phash=hashlib.md5(text.encode()).hexdigest()[:16])
    img = Image.open(io.BytesIO(data))
    exif = {}
    try:
        raw = img.getexif()
        exif = {ExifTags.TAGS.get(k, str(k)): str(v) for k, v in raw.items()}
    except Exception:
        pass
    img = ImageOps.exif_transpose(img).convert("RGB")
    meta = {"software": exif.get("Software", ""), "camera": exif.get("Model", ""),
            "datetime": exif.get("DateTime", ""), "width": img.width, "height": img.height}
    phash = _ahash(img)
    text = _ocr(img)
    small = img.copy()
    small.thumbnail((1600, 1600))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=85)
    return Doc(kind="image", sha256=sha, text=text, meta=meta, phash=phash, image=buf.getvalue())


# Regex fallback ------------------------------------------------------------------------

SYMBOL_CCY = {"₹": "INR", "Rs.": "INR", "Rs": "INR", "€": "EUR", "£": "GBP", "₱": "PHP",
              "₦": "NGN", "KSh": "KES", "MX$": "MXN", "CA$": "CAD", "A$": "AUD", "S$": "SGD", "$": "USD",
              "¥": "JPY", "R": "ZAR"}
ISO = r"USD|EUR|GBP|INR|KES|NGN|PHP|MXN|CAD|AUD|SGD|JPY|ZAR|GHS|PKR|BDT|LKR|NPR|IDR|COP|PLN|THB|MYR|BRL"
AMOUNT_KEYS = r"(amount\s+due|total\s+due|amount\s+payable|balance\s+due|total\s+payable|net\s+payable|" \
              r"amount\s+to\s+pay|total\s+amount|fees\s+due|grand\s+total|total)"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}


def _num(s: str) -> str | None:
    s = s.replace(",", "").strip()
    return s if re.fullmatch(r"\d+(\.\d{1,2})?", s) else None


def parse_amount(text: str) -> tuple[str | None, str | None]:
    for line in text.splitlines():
        if not re.search(AMOUNT_KEYS, line, re.I):
            continue
        m = re.search(rf"({ISO})\s*([\d,]+(?:\.\d{{1,2}})?)", line)
        if m and _num(m.group(2)):
            return _num(m.group(2)), m.group(1)
        m = re.search(r"(₹|Rs\.?|€|£|₱|₦|KSh|MX\$|CA\$|A\$|S\$|\$|¥)\s*([\d,]+(?:\.\d{1,2})?)", line)
        if m and _num(m.group(2)):
            return _num(m.group(2)), SYMBOL_CCY.get(m.group(1).rstrip("."), SYMBOL_CCY.get(m.group(1)))
        m = re.search(rf"([\d,]+(?:\.\d{{1,2}})?)\s*({ISO})", line)
        if m and _num(m.group(1)):
            return _num(m.group(1)), m.group(2)
    return None, None


def parse_date(text: str) -> str | None:
    for line in text.splitlines():
        if not re.search(r"due|pay\s+by|last\s+date", line, re.I):
            continue
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", line)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", line)
        if m and m.group(2).lower() in MONTHS:
            return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
        m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", line)
        if m and m.group(1).lower() in MONTHS:
            return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
        m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", line)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            if mo > 12:
                d, mo = mo, d
            return f"{m.group(3)}-{mo:02d}-{d:02d}"
    return None


def parse_account(text: str) -> str | None:
    m = re.search(r"(account|acct|consumer|customer|student|patient|meter|tenant|ref(?:erence)?)\s*"
                  r"(?:no\.?|number|id|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{3,24})", text, re.I)
    return m.group(2) if m else None


def parse_category(text: str) -> str:
    t = text.lower()
    rules = [("school_fees", ("school", "tuition", "academy", "college", "term fee", "fees")),
             ("medical", ("hospital", "clinic", "medical", "patient", "pharmacy", "health")),
             ("electricity", ("electric", "power", "energy", "kwh", "light")),
             ("water", ("water", "sewer", "sanitation")),
             ("rent", ("rent", "lease", "tenant", "landlord", "apartment")),
             ("phone", ("mobile", "telecom", "phone", "airtime", "broadband"))]
    for cat, words in rules:
        if any(w in t for w in words):
            return cat
    return "other"


def parse_name(text: str) -> str | None:
    m = re.search(r"(?:name|bill\s+to|student|patient|tenant|customer)\s*(?:name)?\s*:\s*([A-Za-z][A-Za-z .'-]{2,40})",
                  text, re.I)
    return m.group(1).strip() if m else None


def parse_line_items(text: str) -> list[dict]:
    items = []
    for line in text.splitlines():
        if re.search(AMOUNT_KEYS, line, re.I) or re.search(r"due\s+date", line, re.I):
            continue
        m = re.match(r"\s*([A-Za-z][A-Za-z /&()'-]{2,40}?)\s*[:.]*\s+(?:[A-Z]{3}\s*|[^\w\s]{1,3}\s*)?([\d,]+\.\d{2})\s*$", line)
        if m:
            items.append({"label": m.group(1).strip(), "amount": m.group(2).replace(",", "")})
    return items


def regex_extract(text: str) -> dict:
    amount, currency = parse_amount(text)
    first = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 3), "")
    return {"biller_name": first[:80], "category": parse_category(text), "amount": amount, "currency": currency,
            "due_date": parse_date(text), "account_reference": parse_account(text), "customer_name": parse_name(text),
            "service_address": None, "city": None, "country": None, "line_items": parse_line_items(text),
            "looks_edited": False, "edit_signs": []}


# AI path ------------------------------------------------------------------------------------

PROMPT = """You read household bills (electricity, water, school fees, hospital or clinic invoices, rent, phone)
from any country. Return only a JSON object with these keys:
biller_name (string), category (one of electricity, water, school_fees, medical, rent, phone, other),
amount (the total amount due now, digits and a dot only, no separators), currency (ISO 4217 code; infer from symbols
and country), due_date (YYYY-MM-DD or null), account_reference (the customer, consumer, student, patient or account
number exactly as printed, or null), customer_name (or null), service_address (or null), city (or null),
country (ISO 3166 alpha-2 or null), line_items (array of {label, amount}), looks_edited (true only if you see clear
signs of digital editing such as mismatched fonts, misaligned digits, pasted blocks or blur around numbers),
edit_signs (array of short strings). Do not guess values that are not printed."""


def _clean(d: dict) -> dict:
    out = {k: d.get(k) for k in ("biller_name", "category", "amount", "currency", "due_date", "account_reference",
                                 "customer_name", "service_address", "city", "country", "line_items",
                                 "looks_edited", "edit_signs")}
    if out["amount"] is not None:
        out["amount"] = _num(str(out["amount"])) or None
    if out["currency"]:
        out["currency"] = str(out["currency"]).upper()[:3]
    if out["category"] not in CATEGORIES:
        out["category"] = "other"
    if out["due_date"]:
        try:
            dt.date.fromisoformat(str(out["due_date"]))
        except ValueError:
            out["due_date"] = None
    if out["country"]:
        out["country"] = str(out["country"]).upper()[:2]
    out["line_items"] = [i for i in (out["line_items"] or []) if isinstance(i, dict)][:20]
    out["edit_signs"] = [str(s)[:120] for s in (out["edit_signs"] or [])][:6]
    out["looks_edited"] = bool(out["looks_edited"])
    return out


async def extract(doc: Doc) -> dict:
    """Returns the fields plus 'source' and 'ocr' (the independent regex reading) for cross-checks."""
    ocr = regex_extract(doc.text) if doc.text else {}
    ai = None
    if doc.kind == "image" and doc.image:
        ai = await llm.vision_json(PROMPT, doc.image, doc.mime, kind="bill-vision")
    elif doc.text.strip():
        ai = await llm.chat_json(PROMPT, "Bill text:\n" + doc.text[:6000], kind="bill-text")
    if ai:
        fields = _clean(ai)
        source = "ai"
    else:
        fields = _clean(ocr) if ocr else _clean({})
        source = "ocr" if ocr else "none"
    fields["source"] = source
    fields["ocr"] = {k: ocr.get(k) for k in ("amount", "currency", "due_date", "account_reference")} if ocr else {}
    fields["ocr_chars"] = len(doc.text)
    return fields
