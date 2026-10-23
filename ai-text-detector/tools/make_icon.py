#!/usr/bin/env python3
"""Write the Litmus mark into frontend/public/ as favicon.svg + favicon.ico, and into the
installer as the Windows app icon.

Why a generator instead of three committed binaries: the mark is the same object in every file,
and the header tile in styles.css is drawn from the same numbers. Hand-editing one and not the
other is how a tab icon ends up showing last year's logo, so all three come out of the one
GEOMETRY below and this script is re-runnable.

The shape is the app's corner language, not a decoration: a filled tile with the top-right and
bottom-right corners cut and the left corners square (--chamfer in styles.css), holding three
lines of prose at three different widths. It is a thumbnail of a marked-up paragraph, which is
what the Detect view actually shows. Gold tile, black ink: #000000 on #ffd400 is 14.67:1, the
same pair tools/check_palette_contrast.py gates for a filled accent control, and it holds on
any browser chrome or Explorer background because the tile paints its own ground.

Usage:  python tools/make_icon.py [--check]
        --check  compare what is on disk against what the geometry says, write nothing
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "public"
INSTALLER = ROOT / "installer"

# side, notch, and the prose bars as (x, y, w) in a `side` x `side` box. Bar height is the stroke
# weight; y is the bar centre, so a size change keeps the rhythm rather than drifting the baseline.
GEOMETRY = {
    "side": 64,
    "notch": 12,
    "tile": "#ffd400",
    "ink": "#000000",
    "bars": [(12, 20, 29), (12, 32, 37), (12, 44, 21)],
    "bar_h": 6,
}


def tile_points(g: dict, k: int = 1) -> list[tuple[int, int]]:
    s, n = g["side"] * k, g["notch"] * k
    return [(0, 0), (s - n, 0), (s, n), (s, s - n), (s - n, s), (0, s)]


def svg_text(g: dict) -> str:
    pts = " ".join(f"{x},{y}" for x, y in tile_points(g))
    r = g["bar_h"] / 2
    bars = "\n".join(
        f'  <rect x="{x}" y="{y - r:g}" width="{w}" height="{g["bar_h"]}" rx="{r:g}" fill="{g["ink"]}"/>'
        for x, y, w in g["bars"]
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {g["side"]} {g["side"]}" '
        f'role="img" aria-label="Litmus">\n'
        f'  <title>Litmus</title>\n'
        f'  <polygon points="{pts}" fill="{g["tile"]}"/>\n'
        f"{bars}\n"
        f"</svg>\n"
    )


def ico_bytes(g: dict, sizes=(48, 32, 16)) -> bytes:
    import io

    from PIL import Image, ImageDraw

    k = 8  # supersample, so the 16px cut and the 6-unit bars survive the downscale
    s = g["side"] * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    rgb = lambda h: tuple(int(h.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)) + (255,)
    dr.polygon(tile_points(g, k), fill=rgb(g["tile"]))
    for x, y, w in g["bars"]:
        h = g["bar_h"]
        dr.rounded_rectangle(
            [x * k, (y - h / 2) * k, (x + w) * k, (y + h / 2) * k],
            radius=h / 2 * k,
            fill=rgb(g["ink"]),
        )
    # The tile must fill the canvas. This is not paranoia: the first version of this function
    # drew the polygon in unscaled units while the bars went in at k, and the favicon that came
    # out was a 12%-of-the-frame corner stamp that only a rendered preview would reveal.
    # Two 12-unit notches off a 64-unit square is 96.5% coverage, so anything else is a bug.
    opaque = img.getchannel("A").histogram()[255]
    if not 0.94 * s * s <= opaque <= s * s:
        raise SystemExit(f"the tile covers {opaque / (s * s):.1%} of the {s}px canvas — expected "
                         f"~96.5%; the geometry and the supersample factor have drifted apart")
    buf = io.BytesIO()
    # One container, several frames: a 16px tab strip picks the 16px frame out of it instead of
    # scaling the 48px one itself, which is where a chamfered corner normally turns to mush.
    img.save(buf, format="ICO", sizes=[(n, n) for n in sizes])
    return buf.getvalue()


# The Windows exe / installer icon needs the large frames a tab strip never asks for: Explorer
# draws the desktop shortcut at 256 and the taskbar at 32, and a container that only holds 48/32/16
# gets upscaled into a blurry tile at exactly the size where the identity is supposed to read.
APP_ICON_SIZES = (256, 64, 48, 32, 16)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify the files on disk, write nothing")
    args = ap.parse_args()

    want = {
        OUT / "favicon.svg": svg_text(GEOMETRY).encode(),
        OUT / "favicon.ico": ico_bytes(GEOMETRY),
        INSTALLER / "app-icon.ico": ico_bytes(GEOMETRY, APP_ICON_SIZES),
    }

    if args.check:
        bad = []
        for path, blob in want.items():
            got = path.read_bytes() if path.is_file() else None
            same = got == blob
            print(f"{'ok  ' if same else 'DIFF'} {path.relative_to(ROOT)}  on-disk={len(got or b'')}B "
                  f"generated={len(blob)}B sha={hashlib.sha256(blob).hexdigest()[:12]}")
            if not same:
                bad.append(path.name)
        if bad:
            print(f"FAIL: {', '.join(bad)} does not match tools/make_icon.py", file=sys.stderr)
            return 1
        print(f"PASS: all three icons are what the geometry says "
              f"({len(want[OUT / 'favicon.svg'])}B svg, {len(want[OUT / 'favicon.ico'])}B ico, "
              f"{len(want[INSTALLER / 'app-icon.ico'])}B app icon)")
        return 0

    for path, blob in want.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        print(f"wrote {path.relative_to(ROOT)}  {len(blob)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
