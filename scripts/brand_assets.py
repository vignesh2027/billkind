"""Build the Billkind brand SVGs: the B monogram, the BillKind wordmark and the lockups.

The monogram is two overlapping rounded italic B shapes: a deep navy B behind and a blue to cyan B in front.
The wordmark is Manrope ExtraBold, slanted and outlined, so no font is needed to show it.

Not a runtime dependency. Needs shapely and fonttools (with brotli for the woff2 font):
    pip install shapely fonttools brotli
    python -m scripts.brand_assets
PNG renders (favicon.png, apple-touch-icon.png, logo-512.png, og-image.png, brand-card.png) are made from these
SVGs and docs/brand/card.html in a headless browser.
"""
from __future__ import annotations

import math
from pathlib import Path

from shapely import affinity
from shapely.geometry import MultiPolygon, Point, box
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "web" / "static"
BRAND = ROOT / "docs" / "brand"
FONT = STATIC / "fonts" / "manrope-latin.woff2"

NAVY = "#0B1E5B"
BLUE = "#1463FF"
SLANT = 15  # degrees


# ---------------------------------------------------------------- monogram geometry

def _stadium(x0, y0, x1, y1):
    """A rectangle whose right end is a half circle."""
    r = (y1 - y0) / 2
    return unary_union([box(x0, y0, x1 - r, y1), Point(x1 - r, y0 + r).buffer(r, 32)])


def _round(g, r):
    return g.buffer(-r, 16).buffer(r, 16)


def _b(x=0.0, y=0.0, s=1.0, counters=True):
    """Upright heavy B with two bowls (the lower one wider), a waist notch and slim counters. y grows down."""
    h, ht, wt, wb, t, cw, notch = 400 * s, 190 * s, 250 * s, 292 * s, 88 * s, 30 * s, 18 * s
    g = unary_union([_stadium(x, y, x + wt, y + ht + notch), _stadium(x, y + ht - notch, x + wb, y + h)])
    g = _round(g, 40 * s)
    if counters:
        y1 = y + (ht + notch) / 2
        y2 = y + (ht - notch + h) / 2
        c1 = _round(_stadium(x + t, y1 - cw / 2, x + wt - t * 1.2, y1 + cw / 2), cw / 2 - .5)
        c2 = _round(_stadium(x + t, y2 - cw / 2, x + wb - t * 1.1, y2 + cw / 2), cw / 2 - .5)
        g = g.difference(unary_union([c1, c2]))
    return g


def _slant(g, deg=SLANT, cy=200):
    k = math.tan(math.radians(deg))
    return affinity.affine_transform(g, [1, -k, 0, 1, cy * k, 0])


def _d(g, nd=1):
    polys = g.geoms if isinstance(g, MultiPolygon) else [g]
    out = []
    for p in polys:
        for ring in [p.exterior, *p.interiors]:
            cs = list(ring.coords)[:-1]
            out.append("M" + "L".join(f"{x:.{nd}f} {y:.{nd}f}" for x, y in cs) + "Z")
    return "".join(out)


def mark_geometry(size=512, pad=40):
    """Back (navy) and front (gradient) B polygons fitted into a size x size box."""
    back = _slant(_b(0, 0, 1.0, counters=False))
    front = _slant(_b(84, 74, .92))
    minx, miny, maxx, maxy = unary_union([back, front]).bounds
    sc = (size - 2 * pad) / max(maxx - minx, maxy - miny)
    ox = (size - (maxx - minx) * sc) / 2 - minx * sc
    oy = (size - (maxy - miny) * sc) / 2 - miny * sc
    fit = lambda g: affinity.affine_transform(g, [sc, 0, 0, sc, ox, oy])
    tol = .25 * size / 512
    return fit(back).simplify(tol), fit(front).simplify(tol)


def mark_svg(size=512, pad=40, reverse=False, uid="bk", shadow=True, title=True):
    back, front = mark_geometry(size, pad)
    fx0, fy0, fx1, fy1 = front.bounds
    navy = ('<stop offset="0" stop-color="#0E2FA3"/><stop offset="1" stop-color="#061763"/>' if not reverse
            else '<stop offset="0" stop-color="#FFFFFF"/><stop offset="1" stop-color="#CFE2FF"/>')
    sh = ""
    if shadow:
        sd = size / 512
        sh = (f'<g clip-path="url(#{uid}c)"><path d="{_d(affinity.translate(front, -7 * sd, 8 * sd))}" fill="#020B3A" '
              f'opacity="{.5 if not reverse else .32}" filter="url(#{uid}s)" fill-rule="evenodd"/></g>')
    back_fill = f"url(#{uid}n)"
    t = "<title>Billkind</title>" if title else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="Billkind">{t}'
        f'<defs><linearGradient id="{uid}n" x1="0" y1="0" x2="1" y2="1">{navy}</linearGradient>'
        f'<linearGradient id="{uid}g" x1="{fx0:.0f}" y1="{fy1:.0f}" x2="{fx1:.0f}" y2="{fy0:.0f}" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="#1747F5"/><stop offset=".5" stop-color="#1C8BFF"/><stop offset="1" stop-color="#2FD3F7"/></linearGradient>'
        f'<filter id="{uid}s" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="{10 * size / 512:.1f}"/></filter>'
        f'<clipPath id="{uid}c"><path d="{_d(back)}"/></clipPath></defs>'
        f'<path d="{_d(back)}" fill="{back_fill}"/>{sh}'
        f'<path d="{_d(front)}" fill="url(#{uid}g)" fill-rule="evenodd"/></svg>'
    )


# ---------------------------------------------------------------- wordmark

def wordmark_paths():
    """Outlines of 'Bill' and 'Kind' in Manrope 800, slanted. Returns (bill_d, kind_d, bounds, cap_height) in font units."""
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    font = instantiateVariableFont(TTFont(str(FONT)), {"wght": 800})
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    cap = font["OS/2"].sCapHeight or 1440
    k = math.tan(math.radians(12))
    track = -30
    x = 0
    parts = {"Bill": "", "Kind": ""}
    bp = BoundsPen(gs)
    for word in ("Bill", "Kind"):
        for ch in word:
            name = cmap[ord(ch)]
            pen = SVGPathPen(gs)
            # flip y (font units grow up) and slant: x' = x + y*k, y' = cap - y
            m = (1, 0, k, -1, x, cap)
            gs[name].draw(TransformPen(pen, m))
            gs[name].draw(TransformPen(bp, m))
            parts[word] += pen.getCommands()
            x += hmtx[name][0] + track
        if word == "Bill":
            x += 10
    return parts["Bill"], parts["Kind"], bp.bounds, cap


def lockup_svg(reverse=False, uid="bkw"):
    """Horizontal lockup: monogram then the BillKind wordmark. Height 120 units."""
    bill, kind, (bx0, by0, bx1, by1), cap = wordmark_paths()
    w = bx1
    H = 120
    mark = mark_svg(512, 18, reverse, uid=uid + "m", title=False)
    inner = mark[mark.index(">", mark.index("<svg")) + 1: mark.rindex("</svg>")].replace("<title>Billkind</title>", "")
    ms = H / 512
    tscale = 62 / cap  # cap height 62 units
    tx = H + 10
    ty = (H - 62) / 2 - 1
    width = tx + w * tscale + 18
    bill_fill = NAVY if not reverse else "#FFFFFF"
    kind_grad = ('<stop offset="0" stop-color="#1463FF"/><stop offset="1" stop-color="#0B4FE0"/>' if not reverse
                 else '<stop offset="0" stop-color="#7FD8F7"/><stop offset="1" stop-color="#4DB2FF"/>')
    sw = 52  # extra weight, font units
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {H}" width="{width:.0f}" height="{H}" role="img" aria-label="BillKind">'
        f'<title>Billkind</title><defs><linearGradient id="{uid}k" x1="0" y1="0" x2="1" y2="1">{kind_grad}</linearGradient></defs>'
        f'<g transform="scale({ms:.5f})">{inner}</g>'
        f'<g transform="translate({tx:.2f} {ty:.2f}) scale({tscale:.5f})" stroke-linejoin="round" stroke-width="{sw}">'
        f'<path d="{bill}" fill="{bill_fill}" stroke="{bill_fill}"/>'
        f'<path d="{kind}" fill="url(#{uid}k)" stroke="url(#{uid}k)"/></g></svg>'
    )


def wordmark_only_svg(reverse=False, uid="bkt"):
    bill, kind, (x0, y0, x1, y1), cap = wordmark_paths()
    pad = 40
    bill_fill = NAVY if not reverse else "#FFFFFF"
    kind_grad = ('<stop offset="0" stop-color="#1463FF"/><stop offset="1" stop-color="#0B4FE0"/>' if not reverse
                 else '<stop offset="0" stop-color="#7FD8F7"/><stop offset="1" stop-color="#4DB2FF"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0 - pad:.0f} {y0 - pad:.0f} {x1 - x0 + 2 * pad:.0f} {y1 - y0 + 2 * pad:.0f}" role="img" aria-label="BillKind">'
        f'<title>Billkind</title><defs><linearGradient id="{uid}k" x1="0" y1="0" x2="1" y2="1">{kind_grad}</linearGradient></defs>'
        f'<g stroke-linejoin="round" stroke-width="52"><path d="{bill}" fill="{bill_fill}" stroke="{bill_fill}"/>'
        f'<path d="{kind}" fill="url(#{uid}k)" stroke="url(#{uid}k)"/></g></svg>'
    )


def main():
    BRAND.mkdir(parents=True, exist_ok=True)
    files = {
        "logo.svg": mark_svg(512, 40),
        "logo-reverse.svg": mark_svg(512, 40, reverse=True, uid="bkr"),
        "favicon.svg": mark_svg(64, 2, uid="bkf", shadow=False),
        "wordmark.svg": lockup_svg(),
        "wordmark-reverse.svg": lockup_svg(reverse=True, uid="bkv"),
        "wordmark-text.svg": wordmark_only_svg(),
        "wordmark-text-reverse.svg": wordmark_only_svg(reverse=True, uid="bku"),
    }
    for name, svg in files.items():
        for d in (STATIC, BRAND):
            (d / name).write_text(svg + "\n")
        print(name, len(svg), "bytes")


if __name__ == "__main__":
    main()
