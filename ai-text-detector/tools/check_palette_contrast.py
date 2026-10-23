#!/usr/bin/env python3
"""Measure the shipped palette the way a screen does, from styles.css's own tokens.

Why this exists: the absolute-dark / absolute-white pass (2026-10-18) removed the royal
blooms that every documented ratio was measured against. A comment saying "#2b45b8 is
7.74:1" is only true of the backdrop it was read on, and a theme change silently invalidates
all of them. This tool re-reads the token blocks and re-computes the pairs the UI actually
draws, so a palette edit that loses a stop fails here instead of in a screenshot review.

The gold pass (2026-10-19) is why the table matters this much: a yellow that is gloriously
legal on absolute black is illegal on absolute white (#ffd400 measures 1.43:1 there), so the
accent cannot be one colour across themes and every pair has to be re-read per theme.
--royal became --touch when the interaction hue stopped being royal blue, and the single-hue
pass (2026-10-21) made the response gold too: --touch is now an alias of --accent-2, and
--accent-fill / --on-accent-fill separate the plate a label sits on from the text gold, which is
what stops the brand mark going brown on white. Token aliases are followed the way a browser
follows them.

Usage:
    python tools/check_palette_contrast.py             # the pair table, both themes
    python tools/check_palette_contrast.py --probe "#ffd400:#000000"   # ad hoc ratio
    python tools/check_palette_contrast.py --verbose   # show the resolved tokens

Floors: 4.5:1 for body text, 3.0:1 for large text (>=24px / 19px bold) and for non-text
graphics (borders, icons, focus rings, meters). Exit 1 if any pair is under its floor.
"""
from __future__ import annotations

import argparse
import re
import sys
import pathlib

CSS = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src" / "styles.css"

TEXT = 4.5
LARGE = 3.0
UI = 3.0


# ---------------------------------------------------------------- colour maths

def parse_color(spec: str):
    """Return (r, g, b, a) in 0..1 (rgb) / 0..1 (alpha) for a CSS colour literal."""
    spec = spec.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", spec)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (1.0,)
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,?\s*([\d.]*)\s*\)", spec)
    if m:
        r, g, b = (float(m.group(i)) / 255 for i in (1, 2, 3))
        a = float(m.group(4)) if m.group(4) else 1.0
        return (r, g, b, a)
    raise ValueError(f"unsupported colour: {spec!r}")


def over(fg, bg):
    """Composite an alpha colour onto an opaque background (sRGB, no gamma)."""
    a = fg[3]
    if a >= 1.0:
        return fg[:3] + (1.0,)
    return tuple(a * fg[i] + (1 - a) * bg[i] for i in range(3)) + (1.0,)


def mix(c_a, pct, c_b):
    """color-mix(in srgb, A pct%, B) -- sRGB channel interpolation."""
    t = pct / 100.0
    return tuple(t * c_a[i] + (1 - t) * c_b[i] for i in range(3)) + (1.0,)


def rel_lum(rgb):
    def chan(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    l1, l2 = rel_lum(fg[:3]), rel_lum(bg[:3])
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------- theme reading

def declarations(block: str):
    """Key/value pairs from one token block, with comments removed first.

    Prose in this stylesheet names tokens the way CSS declares them ("mixed from the BRIGHT
    yellow, not from --accent: a 6% wash..."), and `--accent:` inside a comment otherwise reads
    as a declaration whose value runs until the next real semicolon -- which is the whole rest of
    the comment. Strip before parsing.
    """
    stripped = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", stripped)}


def theme_tokens(text: str, verbose: bool = False):
    """Pull the two token blocks out of the stylesheet."""
    blocks = {}
    root = re.search(r":root\s*\{(.*?)\n\}", text, re.S)
    if not root:
        raise SystemExit("no :root block in " + CSS.name)
    blocks["dark"] = declarations(root.group(1))
    light = re.search(r"html\.light[^{]*\{(.*?)\n\}", text, re.S)
    if not light:
        raise SystemExit("no html.light block in " + CSS.name)
    # Light inherits: start from dark, overlay the light declarations.
    merged = dict(blocks["dark"])
    merged.update(declarations(light.group(1)))
    blocks["light"] = merged
    if verbose:
        for name, toks in blocks.items():
            print(f"[{name}] " + " ".join(f"{k}={v.strip()}" for k, v in sorted(toks.items())
                                          if k in {"--bg", "--panel", "--panel-2", "--field-bg",
                                                   "--ink", "--ink-dim", "--accent", "--touch"}))
    return blocks


def resolve(toks, spec, base, depth=0):
    """A spec is a literal colour, a token name, or ('mix', A, pct, B) / ('over', A, B).

    A token may be declared as an alias of another token -- `--touch: var(--accent-2)` --
    so one lookup is not always enough. Follow the chain; a cycle is a stylesheet bug, not
    something to silently recurse into.
    """
    if depth > 8:
        raise SystemExit(f"token chain too deep (likely a cycle) at {spec!r}")
    if isinstance(spec, tuple):
        if spec[0] == "mix":
            _, a, pct, b = spec
            return mix(resolve(toks, a, base, depth), pct, resolve(toks, b, base, depth))
        if spec[0] == "over":
            _, a, b = spec
            return over(resolve(toks, a, base, depth), resolve(toks, b, base, depth))
        raise ValueError(spec[0])
    spec = spec.strip()
    if spec.startswith("--"):
        if spec not in toks:
            raise SystemExit(f"token {spec} is not defined in the stylesheet")
        spec = toks[spec].strip()
    alias = re.fullmatch(r"var\(\s*(--[\w-]+)\s*\)", spec)
    if alias:
        return resolve(toks, alias.group(1), base, depth + 1)
    if spec.startswith("linear-gradient") or spec == "none":
        raise SystemExit(f"{spec!r} is a paint, not a colour -- give its stop")
    return parse_color(spec)


# ---------------------------------------------------------------- the pair table
# (label, floor, foreground, background) -- background may be a stack, given as
# ("over", wash, surface) so a translucent fill is read on what it actually sits on.

def pairs():
    return [
        # body copy on the three surfaces a paragraph can land on
        ("ink on bg", TEXT, "--ink", "--bg"),
        ("ink on panel", TEXT, "--ink", "--panel"),
        ("ink on panel-2", TEXT, "--ink", "--panel-2"),
        ("ink on field-bg", TEXT, "--ink", "--field-bg"),
        ("ink-dim on panel", TEXT, "--ink-dim", "--panel"),
        ("ink-dim on panel-2", TEXT, "--ink-dim", "--panel-2"),
        ("ink-dim on bg", TEXT, "--ink-dim", "--bg"),
        # the interaction accent, as text: links, active labels, the in-use lane chip. On the gold
        # identity this is the pair that decides everything: a bright yellow is 14.67:1 on black and
        # 1.43:1 on white, so the light theme's --accent is the same hue driven down until it is legal.
        ("accent as text on bg", TEXT, "--accent", "--bg"),
        ("accent as text on panel", TEXT, "--accent", "--panel"),
        ("accent-2 as text on panel", TEXT, "--accent-2", ("over", "--accent-a16", "--panel")),
        ("accent-2 as text on bg", TEXT, "--accent-2", "--bg"),
        # button fills: the label colour against the filled control. Fills read --accent-fill, which
        # is the BRIGHT plate in both themes — a filled surface carries its own ink, so it never has to
        # be driven down for contrast, and that is what stops the brand mark going brown on white.
        ("on-accent-fill on accent-fill", TEXT, "--on-accent-fill", "--accent-fill"),
        # the plate gets darkened by color-mix with black when it is hovered and again when it is
        # pressed; the label has to survive all three states, not just the resting one.
        ("on-accent-fill on the hover plate", TEXT, "--on-accent-fill",
         ("mix", "--accent-fill", 84, "#000000")),
        ("on-accent-fill on the pressed plate", TEXT, "--on-accent-fill",
         ("mix", "--accent-fill", 72, "#000000")),
        # semantics, both flat and on their own 10-16% wash
        ("good on panel", TEXT, "--good", "--panel"),
        ("good on its wash", TEXT, "--good", ("over", "--good-a16", "--panel")),
        ("bad on panel", TEXT, "--bad", "--panel"),
        ("bad on its wash", TEXT, "--bad", ("over", "--bad-a16", "--panel")),
        ("warn on panel", TEXT, "--warn", "--panel"),
        ("warn on its wash", TEXT, "--warn", ("over", "--warn-a16", "--panel")),
        ("neutral on panel", TEXT, "--neutral", "--panel"),
        # the free-tier ticket: the identity gold read on its own wash, which is the only place
        # --accent is both the text and the tint under it.
        ("accent as chip text on its wash", TEXT, "--accent", ("over", "--accent-a16", "--panel")),
        # non-text: the control boundary (must clear 3:1 to be findable), the hover/selection
        # response colour, the focus ring, the meters. The card grouping edge is deliberately quieter
        # than a control, so it is checked at 1.2:1 -- visible, not normative.
        ("control edge vs bg", UI, ("over", "--edge", "--bg"), "--bg"),
        ("panel edge vs bg", 1.2, ("over", "--line", "--bg"), "--bg"),
        ("touch as hover edge", UI, "--touch", "--bg"),
        ("touch as focus ring on panel", UI, "--touch", "--panel"),
        ("touch as drop rule on panel-2", UI, "--touch", "--panel-2"),
        # A clip-path cuts an outside outline away, so the ring is drawn INSIDE (--focus-offset: -3px)
        # and on a filled control it paints over the fill: the ring colour has to separate from the
        # surface it lands on, which is why a filled control takes its own label colour as its ring.
        ("ring inside a filled accent", UI, "--on-accent-fill", "--accent-fill"),
        ("ring inside the hover plate", UI, "--on-accent-fill", ("mix", "--accent-fill", 84, "#000000")),
        ("accent as meter on panel", UI, "--accent", "--panel"),
        ("good as meter on panel", UI, "--good", "--panel"),
        ("bad as meter on panel", UI, "--bad", "--panel"),
        # The chamfer's diagonals. check_visual_language.py now requires a block's --cut-ink to be the
        # colour of its own frame, so measuring the frame measures the cut corner: these rows cover the
        # notch on every surface that re-points its ink. A 55% state mix is a grouping device, not a
        # control boundary, so it sits at the --line stance (1.2) rather than the --edge one (3.0) --
        # and the arithmetic is honest because `color-mix(A 55%, transparent)` composited on a panel is
        # the same colour as A mixed 55% into that panel.
        ("warn rule and its diagonals on panel", 1.2, ("mix", "--warn", 55, "--panel"), "--panel"),
        ("bad rule and its diagonals on panel", 1.2, ("mix", "--bad", 55, "--panel"), "--panel"),
        ("touch rule and its diagonals on panel", 1.2, ("mix", "--touch", 40, "--panel"), "--panel"),
        # The filled plate's edge, and therefore the notch on button.primary and .brand-mark. The gold
        # fill is 1.43:1 on a white page, so the frame is the ONLY thing that makes the control
        # findable there -- which is why it is checked against the ground at the control floor, not as
        # a grouping hairline.
        ("the plate's own edge vs bg", UI, "--accent-deep", "--bg"),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true", help="print the resolved tokens")
    ap.add_argument("--probe", metavar="FG:BG", help="ratio of two literal colours or tokens")
    args = ap.parse_args()

    if args.probe:
        fg, bg = args.probe.split(":", 1)
        blocks = theme_tokens(text if (text := CSS.read_text(encoding="utf-8")) else "", False)
        toks = blocks["dark"]

        def one(spec):
            c = resolve(toks, spec, None)
            return over(c, resolve(toks, "--bg", None)) if c[3] < 1 else c

        f, b = one(fg), one(bg)
        r = ratio(f, b)
        print(f"{fg} on {bg} = {r:.2f}:1 (floor {TEXT} text / {UI} graphics)")
        return 0 if r >= UI else 1

    text = CSS.read_text(encoding="utf-8")
    blocks = theme_tokens(text, args.verbose)
    failures = 0
    for theme, toks in blocks.items():
        print(f"\n=== {theme} ({CSS.name}) ===")
        for label, floor, fg, bg in pairs():
            base = resolve(toks, "--bg", None)
            f, b = resolve(toks, fg, None), resolve(toks, bg, None)
            # A translucent token is a fill over the page, not a colour of its own: a panel's
            # alpha must be composited before it can be read, or the ratio is fiction.
            if f[3] < 1.0:
                f = over(f, base)
            if b[3] < 1.0:
                b = over(b, base)
            r = ratio(f, b)
            ok = r >= floor
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {label:34s} {r:5.2f}:1  floor {floor}")
    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} pair(s) under floor")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
