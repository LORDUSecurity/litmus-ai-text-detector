#!/usr/bin/env python3
"""Fail loudly on a stylesheet the browser will not read the way the source says it should.

Two separate reasons this exists.

1. A silent breakage, mine, 2026-10-18: a `#` comment (Python syntax) was typed inside the page
   builder's CSS string. In CSS that is not a comment. At the top level a parser takes unparseable
   leading tokens as the *prelude* of whatever block follows, so the block became
   `# …text… :root { … }` — a selector that matches nothing — and the whole token set (--bg, --ink,
   --font-ui, --chamfer) vanished. Every string a content grep looks for was still in the file, and
   the page rendered in Times New Roman on a transparent body. So this parses, then asserts where a
   declaration lives, which is the thing a grep cannot see.

2. The design brief is a set of measurable claims — sharp corners with the top-right and bottom-right
   cut, absolute black / absolute white, no glass, blue only for interaction. A claim that lives in a
   commit message rots the moment someone edits a rule. Here it is a check: radius tokens stay 0,
   the only round thing allowed is a circle (50%), no blur/backdrop-filter/drop-shadow anywhere, the
   chamfer must be both declared and actually applied, and both theme blocks must set their ground to
   #000000 / #ffffff.

    python tools/check_visual_language.py            # app stylesheet, component styles, page CSS
    python tools/check_visual_language.py --verbose  # show what each surface contributes
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
PAGE_TOOL = ROOT / "tools" / "public_ranking_page.py"

# A corner may be square or a circle. Anything else is a rounding this language does not have.
# `var(--r-md)` is allowed inside a value because the tokens are asserted separately: a shorthand
# like `border-radius: 0 var(--r-md) var(--r-md) 0` is square exactly while --r-md is 0px, and the
# token check below is what makes that "while" a test rather than a hope.
RADIUS_PART = re.compile(r"^(?:0(?:px|%)?|50%|var\(--[a-z0-9-]+\))$")
RADIUS_DECL = re.compile(r"border-radius:\s*([^;}!]+)")
GLASS = ("backdrop-filter", "blur(", "drop-shadow(", "--glass-sheen:", "--glass-blur:")


def radius_is_sharp(value: str) -> bool:
    parts = [p for p in value.split() if p]
    return bool(parts) and all(RADIUS_PART.match(p) for p in parts)


def strip_comments(css: str) -> str:
    """Blank `/* … */` in place, so prose cannot be read as a declaration and line numbers hold."""
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.S)


def blocks(css: str) -> list[tuple[str, str]]:
    """Top-level (selector, body) pairs, comments removed, the way a parser splits a stylesheet.

    A nested block belongs to its parent's body, which is what makes a swallowed `:root` visible:
    the prelude recorded here is the junk, not `:root`.
    """
    s = strip_comments(css)
    out: list[tuple[str, str]] = []
    depth = sel_at = body_at = 0
    for i, ch in enumerate(s):
        if ch == "{":
            depth += 1
            if depth == 1:
                body_at = i + 1
        elif ch == "}" and depth:
            if depth == 1:
                out.append((s[sel_at:body_at - 1].strip(), s[body_at:i].strip()))
            depth -= 1
            if depth == 0:
                sel_at = i + 1
    return out


def decls(body: str) -> dict[str, str]:
    """The declarations of a block body, last write wins, values unquoted."""
    out: dict[str, str] = {}
    for decl in body.split(";"):
        if ":" in decl:
            name, _, value = decl.partition(":")
            out[name.strip()] = value.strip()
    return out


def style_guards(css: str, *, tokens: bool = False) -> list[str]:
    """Every reason this CSS might lie about what it paints. [] means it reads as it looks."""
    fails: list[str] = []
    # Everything is measured on the comment-blanked text: a sentence in a `/* … */` that mentions
    # `border-radius:50%` is documentation, not a declaration, and treating it as one turns this
    # tool into the thing it exists to catch. Line numbers are kept by blanking in place.
    s = strip_comments(css)

    # 1. The leak that started this: '#' is not a CSS comment, and the following block is eaten.
    for no, line in enumerate(css.splitlines(), 1):
        if re.match(r"^\s*#(\s|$)", line):
            fails.append(f"line {no}: a '#' line inside CSS — a parser folds it into the next "
                         f"selector and drops that block: {line.strip()[:70]!r}")

    # 2. An unbalanced brace silently re-parents every rule after it.
    if s.count("{") != s.count("}"):
        fails.append(f"unbalanced braces: {s.count('{')} '{{' vs {s.count('}')} '}}'")

    # 3. Nothing frosted, nothing glowing, nothing washed. A clip-path cuts a glow away anyway, so the
    #    ones that survive are the ones on boxes that were never cut — i.e. decoration. A `--glass-sheen`
    #    gradient counts here too: a 5% white wash down the top of a panel is the theme not being black.
    compact = s.replace(" ", "")
    for needle in GLASS:
        n = needle.replace(" ", "")
        if n in compact:
            fails.append(f"{needle!r} ×{compact.count(n)} — the language has no glass, and a "
                         "clip-path would cut it off regardless")

    # 4. Corners: square, or a circle. Radius *tokens* are the app's path, so they are checked too:
    #    `border-radius: var(--r-pill)` is only sharp while --r-pill is 0px.
    for m in RADIUS_DECL.finditer(s):
        value = m.group(1).strip()
        if not radius_is_sharp(value):
            line = s[:m.start()].count("\n") + 1
            fails.append(f"line {line}: border-radius: {value} — a literal corner that is not 0")
    for name_tok, value in re.findall(r"(--r-[a-z0-9-]+)\s*:\s*([^;\n]+)", s):
        if not radius_is_sharp(value.strip()):
            fails.append(f"{name_tok}: {value.strip()} — every radius token must resolve to 0")

    # 5. The chamfer must exist *and* be used. A declared-but-unused --chamfer means the cut shown in
    #    the design is not the cut being painted, which is the failure a screenshot review catches
    #    late and a grep never catches at all.
    if tokens:
        rules = blocks(css)
        declared = ""
        for sel, body in rules:
            if sel == ":root":
                declared = decls(body).get("--chamfer", "")
        if not declared:
            fails.append("no ':root' block whose --chamfer a parser can reach — if a stray token "
                         "swallowed this block, every var() in the page is undefined")
        elif not declared.startswith("polygon("):
            fails.append(f"--chamfer is {declared[:40]!r}, not a polygon")
        applied = [sel for sel, body in rules if "clip-path:var(--chamfer" in body.replace(" ", "")]
        if declared and not applied:
            fails.append("--chamfer is declared but no rule applies clip-path:var(--chamfer…)")

        # 6. Absolute dark, absolute white. These are the two grounds the brief names; a panel that
        #    drifted to #0a0a0a reads black in a screenshot and is not the shipped claim.
        dark = next((decls(b) for sel, b in rules if sel == ":root"), {})
        light = next((decls(b) for sel, b in rules
                      if "light" in sel and "--bg" in decls(b)), {})
        if dark.get("--bg", "").lower() not in {"#000000", "#000"}:
            fails.append(f"dark --bg is {dark.get('--bg', '(none)')!r}, not absolute black")
        if not light:
            fails.append("no light theme block that redefines --bg")
        elif light["--bg"].lower() != "#ffffff":
            fails.append(f"light --bg is {light['--bg']!r}, not absolute white")
        for theme, table in (("dark", dark), ("light", light)):
            scheme = table.get("color-scheme", "")
            if theme not in scheme:
                fails.append(f"{theme} block sets color-scheme:{scheme or '(unset)'}")
    return fails


def component_styles(text: str) -> list[tuple[str, list[str]]]:
    """The `styles:` literals of an Angular component, plus every way one is malformed.

    JavaScript ends a template literal at the first unescaped backtick, and so does the Angular
    compiler. That is how a design-pass comment quoting a token name in backticks closed the styles
    property early, produced "NG1002: incorrect number of arguments to @Component decorator" and
    left the whole app uncompilable — while a scanner that hunted for a *tidy* terminator read all
    110 lines of CSS and reported no problem. A reader more lenient than the language it reads is
    worse than no reader, so this stops exactly where the parser stops and then complains about what
    follows, because "not the end of the property" means the literal was closed by prose.
    """
    out: list[tuple[str, list[str]]] = []
    for m in re.finditer(r"styles:\s*\[?\s*`", text):
        start = m.end()
        i = start
        while i < len(text) and not (text[i] == "`" and text[i - 1] != "\\"):
            i += 1
        if i >= len(text):
            out.append(("", [f"a styles: literal opened at offset {start} never closes"]))
            continue
        css = text[start:i]
        tail = text[i + 1:].lstrip()
        problems: list[str] = []
        if not tail.startswith((",", "]", ";", "}")):
            line = text[:i].count("\n") + 1
            problems.append(
                f"line {line}: the styles: literal ends on a backtick followed by "
                f"{tail[:24]!r} — a backtick inside a CSS comment (or any prose) closes the "
                "template literal, which breaks the @Component decorator and the build")
        out.append((css, problems))
    return out


def surfaces(verbose: bool) -> list[tuple[str, str, bool, list[str]]]:
    """(name, css, tokens, extra problems) for everything that paints the app or the page."""
    out: list[tuple[str, str, bool, list[str]]] = []
    app = SRC / "styles.css"
    out.append(("frontend/src/styles.css", app.read_text(encoding="utf-8"), True, []))
    for path in sorted(SRC.rglob("*.ts")):
        rel = str(path.relative_to(SRC))
        for css, problems in component_styles(path.read_text(encoding="utf-8")):
            if css.strip() or problems:
                out.append((f"{rel} (component styles)", css, False, problems))
    # The published page carries its own copy of the tokens, because it has no bundler. Reading them
    # out of the builder's constant keeps the check honest even when dist/ is stale.
    page_src = PAGE_TOOL.read_text(encoding="utf-8")
    m = re.search(r'PAGE_CSS\s*=\s*"""(.*?)"""', page_src, re.S)
    name = "tools/public_ranking_page.py (PAGE_CSS)"
    out.append((name, m.group(1) if m else "/* the PAGE_CSS string could not be found */", True, []))
    if verbose:
        print(f"{len(out)} surface(s): "
              + ", ".join(f"{n} [{len(c.splitlines())} lines]" for n, c, _, _ in out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true", help="list the surfaces being checked")
    args = ap.parse_args()

    failures = 0
    for name, css, tokens, extra in surfaces(args.verbose):
        fails = style_guards(css, tokens=tokens) + extra
        failures += len(fails)
        mark = "ok  " if not fails else "FAIL"
        size = len(css.splitlines())
        print(f"  {mark} {name} ({size} lines)")
        for f in fails:
            print(f"        · {f}")
    print(f"\n{'PASS' if not failures else 'FAIL'}: {failures} problem(s) in the visual language")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
