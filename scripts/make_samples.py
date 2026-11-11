"""Generate the sample bill files in samples/ (fictional billers, fictional people)."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples"


def font(size: int, bold: bool = False):
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def bill_image(header: str, color: str, lines: list[tuple[str, str]], total: tuple[str, str], foot: str) -> Image.Image:
    img = Image.new("RGB", (1100, 1400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1100, 150], fill=color)
    d.text((60, 45), header, fill="white", font=font(52, True))
    y = 200
    for k, v in lines:
        d.text((60, y), k, fill="#333333", font=font(32))
        d.text((560, y), v, fill="#111111", font=font(32, True))
        y += 62
    y += 20
    d.line([60, y, 1040, y], fill="#999999", width=3)
    y += 30
    d.text((60, y), total[0], fill="#111111", font=font(42, True))
    d.text((560, y), total[1], fill="#111111", font=font(42, True))
    d.text((60, 1300), foot, fill="#666666", font=font(24))
    return img


def fresh_sample(seed: int | None = None) -> bytes:
    """A unique, realistic sample bill for testers (fictional school, fictional student)."""
    import random
    rnd = random.Random(seed)
    today = dt.date.today()
    due = (today + dt.timedelta(days=rnd.randint(3, 12))).isoformat()
    tuition = rnd.choice([2400, 2600, 2900, 3100])
    exam = rnd.choice([250, 300, 350])
    lib = rnd.choice([150, 200])
    total = tuition + exam + lib
    sid = f"SPS-{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}"
    name = rnd.choice(["Arjun", "Meena", "Ravi", "Lakshmi", "Farah", "Joseph"]) + " " + rnd.choice(
        ["Kumar", "Iyer", "Thomas", "Begum", "Raj", "Pillai"])
    img = bill_image("SUNRISE PUBLIC SCHOOL", "#1d4f91", [
        ("Fee receipt for", "Term 3 (Nov to Jan)"), ("Student name:", name), ("Student ID:", sid),
        ("Class:", f"Grade {rnd.randint(3, 9)} {rnd.choice('ABC')}"), ("City:", "Chennai, Tamil Nadu"),
        ("Tuition fee", f"INR {tuition:,}.00"), ("Exam fee", f"INR {exam:,}.00"), ("Library fee", f"INR {lib:,}.00"),
        ("Due date:", due)], ("Total due", f"INR {total:,}.00"),
        "Sunrise Public School, fictional sample for testing. Pay by the due date.")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# Drop-a-bill samples ------------------------------------------------------------------------------------
# Four realistic but clearly fictional bills from verified demo billers. fresh_bill() makes a new variant on every
# call (account number, amounts, due date, customer), so testers never trip the duplicate checks.

FIRST = ["Daniel", "Aisha", "Priya", "Tom", "Grace", "Luis", "Meena", "Omar", "Hannah", "Ravi", "Chloe", "Samuel"]
LAST = ["Ortiz", "Khan", "Raman", "Walker", "Mensah", "Garcia", "Iyer", "Haddad", "Clarke", "Nair", "Brooks", "Okafor"]


def _money(cur: str, v: float) -> str:
    return f"{cur} {v:,.2f}"


def _receipt(spec: dict) -> Image.Image:
    W, H = 1100, 1500
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    col = spec["color"]
    ink, muted, line = "#14181f", "#4b5563", "#cfd6e0"
    # Letterhead: a simple geometric mark, the biller name and contact line.
    d.rectangle([0, 0, W, 14], fill=col)
    mx, my = 60, 58
    d.rounded_rectangle([mx, my, mx + 84, my + 84], radius=18, fill=col)
    shape = spec["mark"]
    if shape == "bolt":
        d.polygon([(mx + 48, my + 12), (mx + 22, my + 48), (mx + 40, my + 48), (mx + 34, my + 74), (mx + 62, my + 36),
                   (mx + 44, my + 36)], fill="white")
    elif shape == "drop":
        d.ellipse([mx + 22, my + 32, mx + 62, my + 72], fill="white")
        d.polygon([(mx + 42, my + 12), (mx + 24, my + 46), (mx + 60, my + 46)], fill="white")
    elif shape == "book":
        d.polygon([(mx + 14, my + 34), (mx + 42, my + 20), (mx + 70, my + 34), (mx + 42, my + 48)], fill="white")
        d.rectangle([mx + 26, my + 44, mx + 58, my + 64], fill="white")
    else:
        d.rectangle([mx + 36, my + 18, mx + 48, my + 66], fill="white")
        d.rectangle([mx + 18, my + 36, mx + 66, my + 48], fill="white")
    d.text((172, 62), spec["biller"], fill=ink, font=font(40, True))
    d.text((174, 114), spec["tagline"], fill=muted, font=font(22))
    d.text((W - 60, 66), spec["doc_title"], fill=col, font=font(30, True), anchor="ra")
    d.text((W - 60, 110), "SAMPLE", fill="#9aa3b2", font=font(20, True), anchor="ra")
    y = 182
    d.line([60, y, W - 60, y], fill=line, width=2)
    # Customer and account block.
    y += 26
    for k, v in spec["fields"]:
        d.text((60, y), k, fill=muted, font=font(26))
        d.text((420, y), v, fill=ink, font=font(26, True))
        y += 46
    y += 18
    d.rounded_rectangle([60, y, W - 60, y + 58], radius=10, fill="#f3f5f9")
    d.text((84, y + 15), spec["section"], fill=ink, font=font(24, True))
    d.text((W - 84, y + 15), "Amount", fill=muted, font=font(24, True), anchor="ra")
    y += 80
    for label, amount in spec["items"]:
        d.text((84, y), label, fill=ink, font=font(26))
        d.text((W - 84, y), _money(spec["cur"], amount), fill=ink, font=font(26), anchor="ra")
        y += 50
    y += 6
    d.line([60, y, W - 60, y], fill=line, width=2)
    y += 26
    total = round(sum(a for _, a in spec["items"]), 2)
    d.rounded_rectangle([60, y, W - 60, y + 132], radius=14, outline=col, width=3)
    d.text((88, y + 22), spec["total_label"], fill=ink, font=font(34, True))
    d.text((W - 88, y + 22), _money(spec["cur"], total), fill=ink, font=font(34, True), anchor="ra")
    d.text((88, y + 80), "Due date:", fill=muted, font=font(26))
    d.text((W - 88, y + 80), spec["due"], fill=col, font=font(26, True), anchor="ra")
    y += 170
    for note in spec["notes"]:
        d.text((60, y), note, fill=muted, font=font(22))
        y += 34
    # Tear-off payment stub.
    sy = H - 250
    for x in range(60, W - 60, 22):
        d.line([x, sy, x + 11, sy], fill="#9aa3b2", width=2)
    d.text((60, sy + 28), "Payment stub. Return with your payment.", fill=muted, font=font(22))
    d.text((60, sy + 66), spec["stub_ref"], fill=ink, font=font(26, True))
    d.text((W - 60, sy + 66), _money(spec["cur"], total), fill=ink, font=font(26, True), anchor="ra")
    d.text((60, H - 70), f"{spec['biller']} is fictional. Sample bill for testing Billkind, not a real account.",
           fill="#8a93a3", font=font(20))
    return img


def sample_spec(kind: str, seed: int | None = None, today: dt.date | None = None) -> dict:
    import random
    rnd = random.Random(seed)
    today = today or dt.date.today()
    due = (today + dt.timedelta(days=rnd.randint(4, 12))).isoformat()
    who = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
    start = today.replace(day=1) - dt.timedelta(days=1)
    period = f"{start.replace(day=1):%d %b} to {start:%d %b %Y}"
    if kind == "electricity":
        kwh = rnd.randint(380, 720)
        energy = round(kwh * rnd.choice([0.112, 0.118, 0.124]), 2)
        acct = f"{rnd.randint(1000, 9999)} {rnd.randint(1000, 9999)} {rnd.randint(10, 99)}"
        return {"biller": "Lakeside Power & Light", "tagline": "Customer care 1-800-555-0142  ·  Detroit, MI",
                "doc_title": "ELECTRIC BILL", "color": "#0f6b4f", "mark": "bolt", "cur": "USD",
                "fields": [("Account number:", acct), ("Customer:", who),
                           ("Service address:", f"{rnd.randint(100, 2999)} Birch Street"), ("City:", "Detroit, MI"),
                           ("Service period:", period), ("Meter reading:", f"{kwh} kWh used")],
                "section": "Charges this period",
                "items": [("Energy charge", energy), ("Delivery charge", round(rnd.uniform(11, 16), 2)),
                          ("Customer charge", 7.50)],
                "total_label": "Amount due", "due": due,
                "notes": ["A late fee of 2% applies after the due date.", "Shutoff protection programs: call customer care."],
                "stub_ref": f"Account {acct}", "account": acct}
    if kind == "school":
        sid = f"SPS-{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}"
        return {"biller": "Sunrise Public School", "tagline": "12 Lake View Road, Chennai, Tamil Nadu",
                "doc_title": "FEE RECEIPT", "color": "#1d4f91", "mark": "book", "cur": "INR",
                "fields": [("Student ID:", sid), ("Student name:", who), ("Class:", f"Grade {rnd.randint(3, 9)} {rnd.choice('ABC')}"),
                           ("City:", "Chennai, Tamil Nadu"), ("Fee period:", "Term 3 (Nov to Jan)")],
                "section": "Fees for the term",
                "items": [("Tuition fee", float(rnd.choice([2400, 2600, 2900, 3100]))),
                          ("Exam fee", float(rnd.choice([250, 300, 350]))), ("Library fee", float(rnd.choice([150, 200])))],
                "total_label": "Total due", "due": due,
                "notes": ["Please pay by the due date to keep the seat for next term.",
                          "Fee concessions: ask the school office."],
                "stub_ref": f"Student ID {sid}", "account": sid}
    if kind == "water":
        acct = f"RW-{rnd.randint(100000, 999999)}"
        used = rnd.randint(14, 30)
        return {"biller": "Riverside Water", "tagline": "Water and sewerage services  ·  Leeds",
                "doc_title": "WATER BILL", "color": "#0b6fa4", "mark": "drop", "cur": "GBP",
                "fields": [("Account number:", acct), ("Customer:", who),
                           ("Service address:", f"{rnd.randint(2, 98)} Mill Lane"), ("City:", "Leeds"),
                           ("Service period:", period), ("Water used:", f"{used} cubic metres")],
                "section": "Charges this period",
                "items": [("Water supply", round(used * 1.62, 2)), ("Sewerage", round(used * 1.21, 2)),
                          ("Standing charge", 6.40)],
                "total_label": "Amount due", "due": due,
                "notes": ["Struggling to pay? Ask about a payment plan.", "Report a leak any time, day or night."],
                "stub_ref": f"Account {acct}", "account": acct}
    if kind == "clinic":
        pid = f"CP-{rnd.randint(10000, 99999)}"
        visit = (today - dt.timedelta(days=rnd.randint(8, 20))).strftime("%d %b %Y")
        return {"biller": "Coastline Pediatrics", "tagline": "Family clinic  ·  Tampa, FL",
                "doc_title": "PATIENT BILL", "color": "#7a2e8a", "mark": "cross", "cur": "USD",
                "fields": [("Patient ID:", pid), ("Patient:", who), ("Guardian:", f"{rnd.choice(FIRST)} {who.split()[1]}"),
                           ("City:", "Tampa, FL"), ("Visit date:", visit)],
                "section": "Services",
                "items": [("Office visit", float(rnd.choice([65, 75, 85]))), ("Lab test", float(rnd.choice([18, 24, 32]))),
                          ("Insurance adjustment", -float(rnd.choice([20, 25, 30])))],
                "total_label": "Patient balance due", "due": due,
                "notes": ["Payment plans and financial assistance are available.", "Questions about this bill: 813-555-0177."],
                "stub_ref": f"Patient ID {pid}", "account": pid}
    raise ValueError(f"unknown sample kind {kind}")


def fresh_bill(kind: str, seed: int | None = None) -> bytes:
    img = _receipt(sample_spec(kind, seed))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


DROP_FILES = {"electricity": "electricity-bill-detroit.jpg", "school": "school-fee-receipt-chennai.jpg",
              "water": "water-bill-leeds.jpg", "clinic": "clinic-bill-tampa.jpg"}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    today = dt.date.today()
    due = (today + dt.timedelta(days=6)).isoformat()
    img = bill_image("SUNRISE PUBLIC SCHOOL", "#1d4f91", [
        ("Fee receipt for", "Term 3 (Nov to Jan)"), ("Student name:", "Priya Raman"), ("Student ID:", "SPS-2291-0457"),
        ("Class:", "Grade 6 B"), ("City:", "Chennai, Tamil Nadu"), ("Tuition fee", "INR 2,900.00"),
        ("Exam fee", "INR 350.00"), ("Library fee", "INR 200.00"), ("Due date:", due)],
        ("Total due", "INR 3,450.00"), "Sunrise Public School, fictional sample for testing. Pay by the due date.")
    img.save(OUT / "school-fees-chennai.jpg", quality=90)

    due2 = (today + dt.timedelta(days=4)).isoformat()
    img = bill_image("LAKESIDE POWER & LIGHT", "#0f6b4f", [
        ("Account number:", "4455 6677 88"), ("Customer:", "Daniel Ortiz"), ("Service address:", "1180 Birch Street"),
        ("City:", "Detroit, MI"), ("Usage this period", "612 kWh"), ("Energy charge", "USD 71.40"),
        ("Delivery charge", "USD 12.80"), ("Due date:", due2)],
        ("Amount due", "USD 84.20"), "Lakeside Power & Light, fictional sample for testing.")
    img.save(OUT / "power-bill-detroit.jpg", quality=90)

    img = bill_image("ST. CLARE COMMUNITY CLINIC", "#7a1f3d", [
        ("Patient ID:", "PT-55821"), ("Patient:", "Maria Santos"), ("City:", "Quezon City"),
        ("Consultation", "PHP 1,200.00"), ("Laboratory", "PHP 800.00"), ("Due date:", due)],
        ("Total due", "PHP 9,800.00"), "St. Clare Community Clinic, fictional sample. EDITED COPY for fraud testing.")
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.1 (Macintosh)"
    img.save(buf, format="JPEG", quality=90, exif=exif.tobytes())
    (OUT / "tampered-clinic-bill.jpg").write_bytes(buf.getvalue())
    for kind, name in DROP_FILES.items():
        (OUT / name).write_bytes(fresh_bill(kind, seed=2026))
    print("samples written to", OUT)


if __name__ == "__main__":
    main()
