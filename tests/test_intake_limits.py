"""Bill intake stays fast on hostile files: inflating page content, huge embedded images, huge photos."""

import io
import time

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.ai import intake


def _pdf_with_content(content: bytes) -> bytes:
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = w._add_object(stream.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica")}))})})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_small_pdf_that_inflates_to_megabytes_is_refused_quickly():
    data = _pdf_with_content(b"BT /F1 12 Tf 10 10 Td (Hello world) Tj ET\n" * 50_000)  # about 2 MB of content
    assert len(data) < 20_000
    t0 = time.time()
    with pytest.raises(intake.DocumentError, match="could not be read"):
        intake.read_document(data)
    assert time.time() - t0 < 3


def test_ordinary_text_pdf_still_reads():
    data = _pdf_with_content(b"BT /F1 12 Tf 72 700 Td (Amount due USD 84.20 by 2026-12-01) Tj ET\n")
    doc = intake.read_document(data)
    assert "84.20" in doc.text


def _pdf_with_image(width: int, height: int, data: bytes, extra: tuple[int, int, bytes] | None = None) -> bytes:
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    xobjects = DictionaryObject()
    for i, (iw, ih, raw) in enumerate([(width, height, data)] + ([extra] if extra else [])):
        img = DecodedStreamObject()
        img.set_data(raw)
        img.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(iw), NameObject("/Height"): NumberObject(ih),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                    NameObject("/BitsPerComponent"): NumberObject(8)})
        xobjects[NameObject(f"/Im{i}")] = w._add_object(img.flate_encode())
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): xobjects})
    content = DecodedStreamObject()
    content.set_data(b"q 600 0 0 780 6 6 cm /Im0 Do Q")
    page[NameObject("/Contents")] = w._add_object(content)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_scanned_pdf_skips_oversized_images_without_decoding_them():
    huge = (8000, 8000, b"\x80" * (8000 * 8000))  # 64 MP, above the pixel cap, compresses to almost nothing
    data = _pdf_with_image(200, 100, b"\xff" * 20_000, extra=huge)
    t0 = time.time()
    doc = intake.read_document(data)
    assert time.time() - t0 < 5
    assert doc.image  # the small, allowed picture was used
    pic = intake._pdf_page_image(intake.PdfReader(io.BytesIO(data)))
    assert pic is not None and pic.size == (200, 100)


def test_ocr_runs_on_a_bounded_copy(monkeypatch):
    seen = {}

    def fake_run(cmd, input, capture_output, timeout):
        seen["size"] = Image.open(io.BytesIO(input)).size

        class R:
            stdout = b"text"
        return R()
    monkeypatch.setattr(intake.shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(intake.subprocess, "run", fake_run)
    assert intake._ocr(Image.new("RGB", (7900, 5000), "white")) == "text"
    assert max(seen["size"]) <= intake.OCR_SIDE
