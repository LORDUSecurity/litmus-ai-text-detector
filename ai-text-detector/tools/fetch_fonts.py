#!/usr/bin/env python3
"""Fetch the self-hosted serif faces into frontend/public/fonts/.

Why this exists: the app must render identically inside the Docker image and the
PyInstaller-packaged exe, both of which can be started with no network at all, so
a CDN <link> is not an option. The faces are downloaded once, committed as build
assets, and served by nginx / SpaSplit like any other file under public/.

Both families are SIL Open Font License 1.1 (see the OFL-*.txt files written next
to them). Latin subset only: the UI is English, and the full CJK/cyrillic ranges
would add hundreds of kilobytes to every install.

Re-runnable and idempotent. Prints a table of what it wrote with byte counts.
Usage:  python tools/fetch_fonts.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "public" / "fonts"

# A modern UA makes the CSS API return woff2 (an old one returns ttf).
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

FAMILIES = [
    # (css2 family spec, weights, ofl directory on google/fonts, license file name)
    ("Spectral", [400, 500, 600, 700], "ofl/spectral", "OFL-Spectral.txt"),
    # One weight only: Google serves the *same* latin woff2 for 600 and 700 in
    # Cormorant Garamond v21 (verified 2026-10-17, identical md5), so asking for
    # both would ship a duplicate file under a second @font-face rule. The guard
    # below fails the run if that ever changes shape.
    ("Cormorant+Garamond", [600], "ofl/cormorantgaramond", "OFL-CormorantGaramond.txt"),
]

CSS_URL = "https://fonts.googleapis.com/css2?family={spec}:wght@{weights}&display=swap"
LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/{path}/OFL.txt"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def subset_of(css: str, block_start: int) -> str:
    """Read the /* subset */ comment that precedes an @font-face block."""
    head = css[:block_start]
    m = list(re.finditer(r"/\*\s*([a-z\-]+)\s*\*/", head))
    return m[-1].group(1) if m else "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []
    written = 0
    seen_urls: dict[str, str] = {}

    for spec, weights, ofl_dir, license_name in FAMILIES:
        css = get(CSS_URL.format(spec=spec, weights=";".join(str(w) for w in weights))).decode()
        picked = 0
        for m in re.finditer(r"@font-face\s*\{", css):
            body = css[m.end(): css.index("}", m.end())]
            if subset_of(css, m.start()) != "latin":
                continue
            w = re.search(r"font-weight:\s*(\d+)", body)
            u = re.search(r"url\((https://[^)]+\.woff2)\)", body)
            fam = re.search(r"font-family:\s*'([^']+)'", body)
            if not (w and u and fam):
                continue
            if int(w.group(1)) not in weights:
                continue
            url = u.group(1)
            if url in seen_urls:
                print(f"FAIL {spec} weight {w.group(1)}: Google served the same woff2 as "
                      f"{seen_urls[url]} — two @font-face rules over one identical file. "
                      f"Drop the extra weight from FAMILIES.", file=sys.stderr)
                return 1
            seen_urls[url] = f"{spec} weight {w.group(1)}"
            name = f"{fam.group(1).replace(' ', '')}-{w.group(1)}.woff2"
            dest = OUT / name
            data = get(url)
            if not data[:4] == b"wOF2":
                print(f"FAIL {name}: not a woff2 (magic {data[:4]!r})", file=sys.stderr)
                return 1
            if args.dry_run:
                print(f"would write {dest.relative_to(ROOT)}  {len(data)} bytes")
            else:
                dest.write_bytes(data)
                print(f"wrote {dest.relative_to(ROOT)}  {len(data)} bytes")
            manifest.append(f"{fam.group(1)} {w.group(1)} {name} {len(data)}")
            written += 1
            picked += 1
        if picked != len(weights):
            print(f"FAIL {spec}: expected {len(weights)} latin faces, found {picked}",
                  file=sys.stderr)
            return 1

        lic = get(LICENSE_URL.format(path=ofl_dir))
        if b"Open Font License" not in lic:
            print(f"FAIL {ofl_dir}: downloaded text is not the OFL", file=sys.stderr)
            return 1
        (OUT / license_name).write_bytes(lic)
        print(f"wrote {license_name}  {len(lic)} bytes")

    print(f"\n{written} latin woff2 faces + {len(FAMILIES)} licenses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
