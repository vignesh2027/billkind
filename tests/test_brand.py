"""The landing page, the brand assets and the AR model files."""

from __future__ import annotations

import re
import struct
import zipfile
from pathlib import Path

import httpx

from .conftest import BASE

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"


def test_landing_shows_the_flow_and_the_ar_viewer():
    html = httpx.get(BASE + "/").text
    assert "Cover a stranger's bill and see it stamped" in html and "Kindness, with a receipt" in html
    assert "Your money goes to the biller, never to the person who posted." in html
    assert "Got a bill? Drop it here" in html and "Three steps. One PAID stamp." in html
    # The biller-not-person rule is said once in the hero, not three times.
    hero = html.split('aria-labelledby="hero-title"', 1)[1].split("</section>", 1)[0]
    assert hero.lower().count("never") == 1
    for counter in ("k-paid", "k-countries", "k-sponsors", "k-live"):
        assert f'id="{counter}"' in html
    for step in ("Drop a bill", "A stranger pays with PayPal", "The biller gets paid"):
        assert step in html
    assert "Your giving agent pays by them." in html
    # AR: the vendored model-viewer is loaded lazily and the button only lives in model-viewer's AR slot.
    assert "/static/vendor/model-viewer-4.0.0/model-viewer.min.js" in html
    assert 'slot="ar-button"' in html and "See it on your desk" in html
    assert "'ar-modes': 'webxr scene-viewer quick-look'" in html
    assert "/static/ar/bill.glb" in html and "/static/ar/bill.usdz" in html
    # The old Sponsor node with a heart is gone, and the new header carries the wordmark.
    assert "ui('heart')" not in html and "icon-heart" not in html
    assert "/static/wordmark.svg" in html
    assert re.search(r'<meta property="og:image" content="http[^"]+/static/og-image.png">', html)


def test_text_has_no_em_dashes_or_arrows():
    pages = [httpx.get(BASE + "/").text, (STATIC / "hero3d.js").read_text(), (STATIC / "billscene.js").read_text()]
    for text in pages:
        assert "\u2014" not in text
        assert "->" not in re.sub(r"<!--|-->", "", text)


def test_ar_model_files_are_served_with_their_types():
    glb = httpx.get(BASE + "/static/ar/bill.glb")
    assert glb.status_code == 200 and glb.headers["content-type"].startswith("model/gltf-binary")
    magic, version, length = struct.unpack("<4sII", glb.content[:12])
    assert magic == b"glTF" and version == 2 and length == len(glb.content) < 1_000_000
    usdz = httpx.get(BASE + "/static/ar/bill.usdz")
    assert usdz.status_code == 200 and usdz.headers["content-type"].startswith("model/vnd.usdz+zip")
    poster = httpx.get(BASE + "/static/ar/bill-poster.webp")
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/webp"
    names = zipfile.ZipFile(STATIC / "ar" / "bill.usdz").namelist()
    assert names[0].endswith(".usda") and any(n.endswith(".png") for n in names)


def test_brand_assets_exist_and_are_not_registered_marks():
    for name in ("logo.svg", "logo-reverse.svg", "favicon.svg", "favicon.png", "apple-touch-icon.png",
                 "logo-512.png", "wordmark.svg", "wordmark-reverse.svg", "og-image.png"):
        assert (STATIC / name).stat().st_size > 0, name
        assert httpx.get(f"{BASE}/static/{name}").status_code == 200
    brand = STATIC.parent.parent.parent / "docs" / "brand"
    assert (brand / "brand-card.png").exists()
    for svg in ("logo.svg", "wordmark.svg"):
        text = (STATIC / svg).read_text()
        assert "\u00ae" not in text and "&reg;" not in text


def test_fonts_are_self_hosted_with_their_licences():
    css = (STATIC / "app.css").read_text()
    html = httpx.get(BASE + "/").text
    fonts = STATIC / "fonts"
    for name in ("inter-latin", "inter-latin-ext", "bricolage-grotesque-latin", "bricolage-grotesque-latin-ext", "manrope-latin"):
        assert f"/static/fonts/{name}.woff2" in css
        assert (fonts / f"{name}.woff2").read_bytes()[:4] == b"wOF2"
    for family in ("Inter", "BricolageGrotesque"):
        assert "SIL Open Font License" in (fonts / f"OFL-{family}.txt").read_text()
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in css
    assert '<link rel="preload" href="/static/fonts/inter-latin.woff2"' in html
    assert ".stat-value {" in css and "tabular-nums" in css.split(".stat-value {", 1)[1].split("}", 1)[0]
