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
    print("samples written to", OUT)


if __name__ == "__main__":
    main()
