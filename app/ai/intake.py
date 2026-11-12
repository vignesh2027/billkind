"""Bill intake: read a photo or PDF, extract structured fields, collect authenticity signals.

Primary path: an open vision model (photos) or text model (PDF text) returns structured JSON.
Fallback path: Tesseract OCR or PDF text plus a regex parser. Both run when possible, so the
two readings can be cross-checked for the fraud model.
"""

from __future__ import annotations

import asyncio
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


# Upload limits. Only photos (JPEG, PNG, WebP) and PDFs are accepted; everything else is refused before parsing.
MAX_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 4
MAX_PIXELS = 40_000_000          # refuse decompression bombs long before Pillow's own limit
# A few kilobytes of PDF can inflate to megabytes of page content, and text extraction then runs for minutes. Real
# bills use a few hundred kilobytes at most across their pages.
MAX_PDF_CONTENT = 1_000_000
MAX_PDF_IMAGES = 16              # image objects looked at on a scanned page (only the largest is decoded)
MAX_TEXT_CHARS = 20_000          # text kept from a document for the reading, the hash and the AI
OCR_SIDE = 2400                  # OCR runs on a copy this size at most: enough for bill print, far cheaper
VISION_SIDE = 1024               # what the vision model sees (Groq free tier: about 2,000 input tokens per image)
STORE_SIDE = 1600                # the private copy kept for reviewers
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}


class DocumentError(ValueError):
    """The upload is not a photo or PDF we accept. The message is safe to show to the poster."""


@dataclass
class Doc:
    kind: str                      # image or pdf
    sha256: str
    text: str = ""                 # OCR or PDF text
    meta: dict = field(default_factory=dict)
    phash: str = ""
    image: bytes = b""             # downscaled JPEG for the vision model
    mime: str = "image/jpeg"
    stored: bytes = b""            # private copy to keep: re-encoded JPEG without EXIF/GPS, or a PDF without metadata


def sniff(data: bytes) -> str:
    """'image' or 'pdf' from the file's own bytes (never from the name or the browser's content type)."""
    if not data:
        raise DocumentError("That file is empty. Please choose a photo or PDF of the bill.")
    if len(data) > MAX_BYTES:
        raise DocumentError("Files up to 8 MB, please.")
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n") or \
            (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        raise DocumentError("HEIC photos cannot be read yet. Please choose JPG, or set the camera to Most Compatible.")
    raise DocumentError("Please upload a photo (JPG, PNG or WebP) or a PDF of the bill.")


def _ahash(img: Image.Image) -> str:
    small = img.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
    px = list(small.tobytes())
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
    small = img.convert("L")
    if max(small.size) > OCR_SIDE:
        small.thumbnail((OCR_SIDE, OCR_SIDE))
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    try:
        out = subprocess.run([exe, "stdin", "stdout", "--psm", "4"], input=buf.getvalue(),
                             capture_output=True, timeout=25)
        return out.stdout.decode("utf-8", "replace")
    except (subprocess.SubprocessError, OSError):
        return ""


def _jpeg(img: Image.Image, side: int, quality: int) -> bytes:
    """Re-encode without any metadata: Pillow writes no EXIF (so no GPS) unless it is passed in explicitly."""
    small = img.copy()
    small.thumbnail((side, side))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _open_image(data: bytes) -> tuple[Image.Image, dict]:
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt not in IMAGE_FORMATS:
            raise DocumentError("Please upload a photo (JPG, PNG or WebP) or a PDF of the bill.")
        if img.width * img.height > MAX_PIXELS:
            raise DocumentError("That photo is too large to read. Please send a smaller one.")
        exif = {}
        try:
            raw = img.getexif()
            exif = {ExifTags.TAGS.get(k, str(k)): str(v) for k, v in raw.items()}
        except Exception:
            pass
        img = ImageOps.exif_transpose(img).convert("RGB")
    except DocumentError:
        raise
    except Exception as exc:  # truncated or corrupt file, decompression bomb
        raise DocumentError("That file could not be opened. Please upload a photo (JPG or PNG) or a PDF.") from exc
    return img, exif


def _clean_pdf(reader: PdfReader) -> bytes:
    """A copy with the pages only: no document info, no XMP metadata, no attachments or scripts."""
    from pypdf import PdfWriter
    w = PdfWriter()
    for page in reader.pages:
        w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _pdf_page_image(reader: PdfReader) -> Image.Image | None:
    """For scanned PDFs without a text layer: the largest picture on the first page.

    Sizes are read from each image's dictionary first; only the single largest allowed image is decoded.
    """
    page = reader.pages[0]
    try:
        xobjects = page["/Resources"].get_object()["/XObject"].get_object()
    except Exception:
        return None
    best_name, best_px = None, 0
    for name in list(xobjects.keys())[:MAX_PDF_IMAGES]:
        try:
            obj = xobjects[name].get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            px = int(obj.get("/Width", 0)) * int(obj.get("/Height", 0))
        except Exception:
            continue
        if 0 < px <= MAX_PIXELS and px > best_px:
            best_name, best_px = name, px
    if best_name is None:
        return None
    try:
        pil = page.images[str(best_name)].image
    except Exception:
        return None
    if pil is None or pil.width * pil.height > MAX_PIXELS:
        return None
    return pil.convert("RGB")


def _pdf_text(reader: PdfReader) -> str:
    """Page text, refusing PDFs whose page content would take far longer to read than any real bill."""
    total = 0
    for page in reader.pages:
        try:
            contents = page.get_contents()
            total += len(contents.get_data()) if contents is not None else 0
        except Exception as exc:
            raise DocumentError("That PDF could not be read. Please try a photo of the bill instead.") from exc
        if total > MAX_PDF_CONTENT:
            raise DocumentError("That PDF could not be read. Please try a photo of the bill instead.")
    return "\n".join((p.extract_text() or "") for p in reader.pages)[:MAX_TEXT_CHARS]


def read_document(data: bytes, filename: str = "", content_type: str = "") -> Doc:
    kind = sniff(data)
    sha = hashlib.sha256(data).hexdigest()
    if kind == "pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
        except Exception as exc:
            raise DocumentError("That PDF could not be opened. Please try a photo of the bill instead.") from exc
        if reader.is_encrypted:
            raise DocumentError("That PDF is password protected. Please upload an unlocked copy or a photo.")
        if pages == 0:
            raise DocumentError("That PDF has no pages.")
        if pages > MAX_PDF_PAGES:
            raise DocumentError(f"Bills up to {MAX_PDF_PAGES} pages, please. Upload just the page with the amount due.")
        text = _pdf_text(reader)
        info = reader.metadata or {}
        meta = {"producer": str(info.get("/Producer", "") or ""), "creator": str(info.get("/Creator", "") or ""),
                "created": str(info.get("/CreationDate", "") or ""), "modified": str(info.get("/ModDate", "") or ""),
                "pages": pages}
        doc = Doc(kind="pdf", sha256=sha, text=text, meta=meta, phash=hashlib.md5(text.encode()).hexdigest()[:16],
                  stored=_clean_pdf(reader))
        if len(text.strip()) < 40:  # a scan saved as PDF: read its picture instead
            pic = _pdf_page_image(reader)
            if pic is not None:
                doc.text = _ocr(pic)[:MAX_TEXT_CHARS]
                doc.image = _jpeg(pic, VISION_SIDE, 82)
                doc.phash = _ahash(pic)
        return doc
    img, exif = _open_image(data)
    meta = {"software": exif.get("Software", ""), "camera": exif.get("Model", ""),
            "datetime": exif.get("DateTime", ""), "width": img.width, "height": img.height,
            "had_gps": "GPSInfo" in exif}
    return Doc(kind="image", sha256=sha, text=_ocr(img)[:MAX_TEXT_CHARS], meta=meta, phash=_ahash(img),
               image=_jpeg(img, VISION_SIDE, 82), stored=_jpeg(img, STORE_SIDE, 85))


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
    # Labels that are always followed by the number come first, so "Customer care 1-800..." never wins over
    # "Account number: ...".
    strong = (r"(?:account|acct|consumer|customer|student|patient|meter|tenant)\s*(?:no\.?|number|id|#)|"
              r"ref(?:erence)?\s*(?:no\.?|number|#)")
    for labels in (strong, r"account|acct|consumer|customer|student|patient|meter|tenant|ref(?:erence)?"):
        for m in re.finditer(rf"\b(?:{labels})\s*[:#]?\s*([A-Z0-9][A-Z0-9\- /]{{3,24}})", text, re.I):
            value = m.group(1).strip()
            if any(ch.isdigit() for ch in value):  # skip labels such as "Student name"
                return value
    return None


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


def parse_labelled(text: str, labels: str) -> str | None:
    """The value printed after a label such as "Service address:" on the same line."""
    m = re.search(rf"\b(?:{labels})\b\s*[:\-]?\s+(.{{3,60}}?)\s*$", text, re.I | re.M)
    return m.group(1).strip() if m else None


def parse_period(text: str) -> str | None:
    return parse_labelled(text, r"service\s+period|billing\s+period|bill\s+period|period|fee\s+receipt\s+for|"
                                r"term|visit\s+date|date\s+of\s+service")


def regex_extract(text: str) -> dict:
    amount, currency = parse_amount(text)
    first = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 3), "")
    return {"biller_name": first[:80], "category": parse_category(text), "amount": amount, "currency": currency,
            "due_date": parse_date(text), "account_reference": parse_account(text), "customer_name": parse_name(text),
            "service_address": parse_labelled(text, r"service\s+address|supply\s+address|property"),
            "service_period": parse_period(text), "city": None, "country": None,
            "line_items": parse_line_items(text), "looks_edited": False, "edit_signs": [], "confidence": {}}


# AI path ------------------------------------------------------------------------------------

PROMPT = """You read household bills (electricity, water, school fees, hospital or clinic invoices, rent, phone)
from any country. Return only a JSON object with these keys:
biller_name (string), category (one of electricity, water, school_fees, medical, rent, phone, other),
amount (the total amount due now, digits and a dot only, no separators), currency (ISO 4217 code; infer from symbols
and country), due_date (YYYY-MM-DD or null), account_reference (the customer, consumer, student, patient or account
number exactly as printed, or null), customer_name (or null), service_address (or null), service_period (the billing
period, school term or visit date as printed, or null), city (or null),
country (ISO 3166 alpha-2 or null), line_items (array of {label, amount}), looks_edited (true only if you see clear
signs of digital editing such as mismatched fonts, misaligned digits, pasted blocks or blur around numbers),
edit_signs (array of short strings), confidence (an object giving, for biller_name, amount, currency, due_date,
category and account_reference, a number from 0 to 1 for how clearly that value is printed and read).
Do not guess values that are not printed."""

CONFIDENCE_KEYS = ("biller_name", "amount", "currency", "due_date", "category", "account_reference")


def _clean(d: dict) -> dict:
    out = {k: d.get(k) for k in ("biller_name", "category", "amount", "currency", "due_date", "account_reference",
                                 "customer_name", "service_address", "service_period", "city", "country",
                                 "line_items", "looks_edited", "edit_signs", "confidence")}
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
    for k in ("biller_name", "account_reference", "customer_name", "service_address", "service_period", "city"):
        if out[k] is not None:
            out[k] = str(out[k]).strip()[:120] or None
    conf = out["confidence"] if isinstance(out["confidence"], dict) else {}
    clean_conf = {}
    for k in CONFIDENCE_KEYS:
        try:
            clean_conf[k] = max(0.0, min(1.0, float(conf[k])))
        except (KeyError, TypeError, ValueError):
            continue
    out["confidence"] = clean_conf
    return out


async def extract(doc: Doc) -> dict:
    """Returns the fields plus 'source' and 'ocr' (the independent regex reading) for cross-checks."""
    ocr = regex_extract(doc.text) if doc.text else {}
    ai, reader = None, ""
    # One model call at a time, to stay inside the free tier's tokens per minute: the vision model reads the
    # picture; if it is unavailable or rate limited, a text model reads the OCR or PDF text; if that also fails,
    # the regex reading is used.
    if doc.image:
        ai = await llm.vision_json(PROMPT, doc.image, doc.mime, kind="bill-vision")
        reader = "vision" if ai else ""
    if ai is None and doc.text.strip():
        ai = await llm.chat_json(PROMPT, "Bill text:\n" + doc.text[:6000], kind="bill-text")
        reader = "text" if ai else ""
    if ai:
        fields = _clean(ai)
        source = "ai"
    else:
        fields = _clean(ocr) if ocr else _clean({})
        source = "ocr" if ocr else "none"
    fields["source"] = source
    fields["reader"] = reader or source
    fields["ocr"] = {k: ocr.get(k) for k in ("amount", "currency", "due_date", "account_reference")} if ocr else {}
    fields["ocr_chars"] = len(doc.text)
    return fields
