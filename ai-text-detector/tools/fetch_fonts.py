#!/usr/bin/env python3
"""Fetch the self-hosted serif face into frontend/public/fonts/.

Why this exists: the app must render identically inside the Docker image and the
PyInstaller-packaged exe, both of which can be started with no network at all, so
a CDN <link> is not an option. The face is downloaded once, committed as a build
asset, and served by nginx / SpaSplit like any other file under public/.

Literata, chosen over the incumbents by measurement rather than taste: its latin
cut has an x-height of 0.507 em against Spectral's 0.450 and Newsreader's 0.440
(the numbers tools/check_font_metrics.py prints), so at the UI's 14.5px it is the
one of the three that a person can actually read, and the weight axis goes to 900
where Spectral stops at 700. One variable file replaces the five static cuts it
took before: 38,996 B instead of 113,864 B.

The file is SIL Open Font License 1.1 (OFL-Literata.txt is downloaded next to it).
Latin subset only: the UI is English, and the full cyrillic/greek/vietnamese
ranges would add hundreds of kilobytes to every install.

Re-runnable and idempotent. Prints what it wrote with byte counts and the axis it
proved. Usage:  python tools/fetch_fonts.py [--dry-run]
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "public" / "fonts"

# A modern UA makes the CSS API return woff2 (an old one returns ttf).
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# (css2 family spec, the weights the app asks for, the file stem, the OFL dir, the license name,
#  the wght axis the downloaded file must actually carry)
FAMILIES = [
    # The prose voice: the writing under examination, and nothing else.
    ("Literata", "wght@400;500;600;700", "Literata-var", "ofl/literata",
     "OFL-Literata.txt", (400, 900)),
    # The app's own voice: headings, labels, running text the interface writes.
    ("Space+Grotesk", "wght@400;500;600;700", "SpaceGrotesk-var", "ofl/spacegrotesk",
     "OFL-SpaceGrotesk.txt", (300, 700)),
    # The instrument voice: every number, code identifier and table cell.
    ("JetBrains+Mono", "wght@400;500;600;700", "JetBrainsMono-var", "ofl/jetbrainsmono",
     "OFL-JetBrainsMono.txt", (400, 800)),
]

CSS_URL = "https://fonts.googleapis.com/css2?family={spec}&display=swap"
LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/{path}/OFL.txt"

# Every @font-face Google emits, with the subset comment that precedes it.
BLOCK_RE = re.compile(r"/\*\s*([a-z\-]+)\s*\*/\s*@font-face\s*\{([^}]+)\}")
URL_RE = re.compile(r"url\((https://[^)]+\.woff2)\)")


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fail(msg: str) -> int:
    print(f"FAIL {msg}", file=sys.stderr)
    return 1


def check_variable(data: bytes, stem: str, want: tuple[int, int]) -> tuple[str, str]:
    """Prove the bytes are one font that spans the weights the stylesheet asks for.

    Returns (error, description); exactly one of the two is non-empty. A static
    instance has no fvar table, and if Google ever serves one under this request the
    @font-face range would be a lie: the browser would draw every weight from the 400
    cut and synthesise the rest. So the axis is read, not assumed.
    """
    font = TTFont(io.BytesIO(data))
    axes = {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes} if "fvar" in font else {}
    if "wght" not in axes:
        return (f"{stem}: no wght axis (axes={sorted(axes) or 'none'}) — Google served a static "
                f"instance, so @font-face must go back to one rule per weight", "")
    lo, hi = axes["wght"]
    if (lo, hi) != (float(want[0]), float(want[1])):
        return (f"{stem}: wght axis is {lo:g}-{hi:g}, not the {want[0]}-{want[1]} "
                f"the stylesheet claims", "")
    name = font["name"].getDebugName(4) or "?"
    return ("", f"wght {lo:g}-{hi:g}, named {name!r}, {len(font.getGlyphOrder())} glyphs")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    for spec, weights, stem, ofl_dir, license_name, axis in FAMILIES:
        css = get(CSS_URL.format(spec=f"{spec}:{weights}")).decode()
        latin = [URL_RE.search(body).group(1)
                 for subset, body in BLOCK_RE.findall(css)
                 if subset == "latin" and URL_RE.search(body)]
        uniq = sorted(set(latin))
        if len(uniq) != 1:
            return fail(f"{spec}: expected one latin woff2 for a variable family, "
                        f"Google listed {len(uniq) or 0} — the @font-face range no longer holds")
        if len({m.group(1) for m in re.finditer(r"font-weight:\s*(\d+)", css)}) != 4:
            return fail(f"{spec}: the CSS API did not answer all of {weights}")

        data = get(uniq[0])
        if data[:4] != b"wOF2":
            return fail(f"{stem}: not a woff2 (magic {data[:4]!r})")
        error, note = check_variable(data, stem, axis)
        if error:
            return fail(error)

        lic = get(LICENSE_URL.format(path=ofl_dir))
        if b"Open Font License" not in lic:
            return fail(f"{ofl_dir}: downloaded text is not the OFL")

        verb = "would write" if args.dry_run else "wrote"
        if not args.dry_run:
            (OUT / f"{stem}.woff2").write_bytes(data)
            (OUT / license_name).write_bytes(lic)
        print(f"{verb} {stem}.woff2  {len(data)} bytes  ({note})")
        print(f"{verb} {license_name}  {len(lic)} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
