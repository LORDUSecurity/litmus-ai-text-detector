#!/usr/bin/env python3
"""Measure the committed self-hosted faces: x-height, cap height, weight axis, bytes.

Why this exists: "pick a better font" is only an opinion until the numbers are on the table.
The face is chosen for the size the UI actually sets it at, and the one number that decides
whether small serif text reads is the x-height as a fraction of the em. This tool prints it
for every woff2 under frontend/public/fonts/ by walking the outlines of "x" and "H" — the
metrics tables (OS/2.sxHeight) are often absent or wrong in subsetted webfonts, so they are
not trusted here.

The floor is the guard, not the choice: 0.48 em is where a serif stops being legible at the
UI's 14.5px. Literata's latin cut measures 0.507, which is why it is committed; Spectral
(0.450) and Newsreader (0.440) sat under the floor and were dropped.

Usage:  python tools/check_font_metrics.py [--floor 0.48]
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "frontend" / "public" / "fonts"


def bbox_of(font: TTFont, name: str) -> tuple[float, float, float, float]:
    """Outline bounds of one glyph, from the drawing pen rather than the header tables."""
    pen = RecordingPen()
    font.getGlyphSet()[name].draw(pen)
    pts = [pt for op, args in pen.value
           if op in ("moveTo", "lineTo", "qCurveTo", "curveTo") for pt in args]
    if not pts:
        raise SystemExit(f"glyph {name!r} has no outline — is this a text font?")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=float, default=0.48,
                    help="minimum x-height as a fraction of the em")
    args = ap.parse_args()

    files = sorted(FONTS.glob("*.woff2"))
    if not files:
        print(f"FAIL no woff2 under {FONTS.relative_to(ROOT)} — run tools/fetch_fonts.py",
              file=sys.stderr)
        return 1

    rows, bad = [], []
    for path in files:
        font = TTFont(io.BytesIO(path.read_bytes()))
        upm = font["head"].unitsPerEm
        _, _, x0, x1 = bbox_of(font, "x")
        _, _, c0, c1 = bbox_of(font, "H")
        xh, cap = (x1 - x0) / upm, (c1 - c0) / upm
        axes = {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes} if "fvar" in font else {}
        wght = axes.get("wght")
        desc = (f"{wght[0]:g}-{wght[1]:g}" if wght else "static")
        stem = path.name[: path.name.index("-")] if "-" in path.name else path.stem
        family = font["name"].getDebugName(1) or "?"
        rows.append((path.name, len(path.read_bytes()), family, stem, xh, cap, xh / cap, desc, len(font.getGlyphOrder())))
        if xh < args.floor:
            bad.append(f"{path.name}: x-height {xh:.3f} em is under the {args.floor:.2f} floor")

    print(f"{'file':<26}{'bytes':>8}  {'family':<18}{'x-height':>9}{'cap':>7}{'x/cap':>7}  {'wght':<10}{'glyphs':>7}")
    for name, size, family, _stem, xh, cap, ratio, desc, glyphs in rows:
        print(f"{name:<26}{size:>8,}  {family:<18}{xh:>9.3f}{cap:>7.3f}{ratio:>7.2f}  {desc:<10}{glyphs:>7}")

    if bad:
        for line in bad:
            print(f"FAIL {line}", file=sys.stderr)
        return 1
    print(f"PASS: {len(rows)} face(s), every x-height >= {args.floor:.2f} em")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
