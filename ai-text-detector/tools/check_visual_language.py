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
   the only round thing allowed is a circle (50%), no blur/backdrop-filter/drop-shadow anywhere, no
   `background:` shorthand (it would reset the background layers the cut corners are painted with),
   the chamfer must be both declared and actually applied and its diagonal must be the same colour as
   the frame it carries round the corner, and both theme blocks must set their ground to #000000 /
   #ffffff.

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
# `background:` but never `background-color:` / `-image:` / `-size:` … — the negative lookbehind on a
# leading hyphen or word character is what tells the shorthand from the longhands the language uses.
BACKGROUND_SHORTHAND = re.compile(r"(?<![-\w])background\s*:")
GLASS = ("backdrop-filter", "blur(", "drop-shadow(", "--glass-sheen:", "--glass-blur:")


# A tone word names a *state* before it names a section: `warn`, `good`, `bad`, `free`, `lane`,
# `neutral` are the classes the tickets wear (`chip warn`, `chip lane`, `tag good`), and they are also
# the first word anyone reaches for when labelling a callout, a compartment or a row.
STATE_WORDS = {"warn", "good", "bad", "free", "lane", "neutral"}
BARE_CLASS = re.compile(r"^\.[a-z][a-z0-9-]*$")
# The properties that decide how big a box is. Colour is deliberately absent: a bare tone word that
# only tints is the state doing its job, and the ticket colours live on qualified selectors anyway.
BOX_PROPS = ("padding", "padding-top", "padding-bottom", "padding-left", "padding-right",
             "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
             "border", "border-top", "border-bottom",
             "width", "min-width", "max-width", "height", "min-height", "max-height",
             "font-size", "line-height")


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


def is_zero(value: str) -> bool:
    return value.split("!")[0].strip() in {"0", "0px", "0%"}


def matches_class(sel: str, name: str) -> bool:
    """Whether a selector can hit an element carrying `name`.

    `.chip` and `:where(.chip, .tag)` can; `.lane:not(.chip)` cannot. Reading a bare mention as an
    application is how the compartment rule written specifically to *avoid* the ticket was blamed for
    giving it padding — a guard that fires on the fix is worse than no guard, because the next person
    deletes the guard.
    """
    s = re.sub(r":not\([^()]*\)", "", sel)
    return bool(re.search(rf"\.{name}(?![\w-])", s))


# A `border` shorthand is width + style + colour and the colour is the only part the chamfer has to
# agree with, so the two grammar tokens are stripped before comparing. `color-mix(in srgb, …)` splits
# across spaces and goes back together as one string, which is why this works on a plain split.
BORDER_TOKEN = re.compile(r"^(?:\d+(?:\.\d+)?(?:px|rem|em|%)?|thin|medium|thick"
                          r"|solid|dashed|dotted|double|groove|ridge|inset|outset|hidden|none)$")


def frame_colour(d: dict[str, str]) -> str:
    """The colour half of whatever frame this block draws, "" when it draws none of its own.

    Walked in declaration order so the last of `border` / `border-color` wins, which is what the
    browser does with two frames in one block.
    """
    out = ""
    for name, value in d.items():
        if name not in ("border", "border-color"):
            continue
        parts = [p for p in value.replace("!important", "").split() if not BORDER_TOKEN.match(p)]
        out = " ".join(parts)
    return out.strip().lower()


def vertical_padding(d: dict[str, str]) -> list[str]:
    """Every non-zero vertical padding a block asks for, shorthand and longhand alike.

    The ticket primitive is allowed one horizontal padding and no vertical one, so that its height is
    decided by `min-height` alone. Any vertical padding here puts the box past its own cap and makes
    arithmetic — not the token — the thing that decides how tall a label is.
    """
    out: list[str] = []
    short = d.get("padding")
    if short:
        parts = [p for p in short.split() if p]
        if parts[-1].startswith("!"):
            parts = parts[:-1]
        # 1-2 values: the first is the vertical pair. 3-4: the first and third. Both cases are `parts[::2]`.
        if any(not is_zero(v) for v in parts[::2]):
            out.append(f"padding: {short}")
    for name in ("padding-top", "padding-bottom"):
        if d.get(name) and not is_zero(d[name]):
            out.append(f"{name}: {d[name]}")
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

    # 5. No `background:` shorthand anywhere. The chamfer's corner rule is painted on the surface's
    #    own background layers (--cut-edges as a background-image, positioned by background-size/-
    #    position/-origin/-repeat), and the shorthand resets all four of those to their initial
    #    values — so `background: var(--panel)` on a card that was just cut leaves the box with two
    #    clipped corners and no line along them. It is a one-line edit that renders correctly in a
    #    screenshot at a glance and cannot be found by looking for a missing declaration, which is
    #    why the whole stylesheet is longhand and this is the rule that keeps it that way.
    for m in BACKGROUND_SHORTHAND.finditer(s):
        line = s[:m.start()].count("\n") + 1
        fails.append(f"line {line}: `background:` shorthand — it resets background-image and takes "
                     "the chamfer's painted corner back off; use background-color / background-image")

    # 6. A cut surface has to paint its own corner. Two ways this goes wrong, both invisible in a
    #    screenshot at normal zoom: the `background:` shorthand (checked above) takes the painted
    #    diagonal off, and a diagonal token declared at :root carries :root's ink to every surface, so
    #    a card whose frame turns gold keeps a grey corner — measured in Chrome 2026-10-21, which is
    #    why --cut-edges was deleted. So: nothing may reference the dead token, and every selector that
    #    applies a chamfer must read var(--cut-ink) in a background-image of its own.
    if "--cut-edges)" in compact:
        fails.append("var(--cut-edges) — that token was removed because a custom property resolves its "
                     "var() calls where it is DECLARED: at :root it would paint every corner in :root's "
                     "ink. Spell the two gradients out in the rule that cuts.")
    cut: dict[str, bool] = {}
    for sel, body in blocks(css):
        d = decls(body)
        paints = "var(--cut-ink)" in d.get("background-image", "")
        if "clip-path:var(--chamfer" in body.replace(" ", ""):
            cut[sel] = cut.get(sel, False) or paints
        elif paints:
            cut.setdefault(sel, True)
    for sel, painted in cut.items():
        if not painted:
            fails.append(f"{sel[:60]!r} applies a chamfer but never paints its diagonal with "
                         "var(--cut-ink) — the cut corner will read as a gap in the frame")

    # 7. A chamfer's diagonal is the frame carried around the corner, so the two must be ONE colour.
    #    Measured 2026-10-21: every surface that re-points --cut-ink also re-points its own border, and
    #    each pair uses the identical expression — `border-color: color-mix(in srgb, var(--warn) 55%,
    #    transparent)` beside `--cut-ink:` the same, `border: 1px solid var(--edge)` beside
    #    `--cut-ink: var(--edge)`. A block that sets the diagonal to something else renders a corner in
    #    a different hue from the two rules it joins: a seam at exactly the place the signature is
    #    supposed to read as one continuous edge, and invisible unless you sample the pixels along the
    #    45 degrees. Only compared when ONE block declares both, because a surface may inherit its ink
    #    from :root and its frame from another rule — and a comparison across two rules would have to
    #    guess which cascade order the browser applied.
    for sel, body in blocks(css):
        d = decls(body)
        ink = d.get("--cut-ink")
        if not ink:
            continue
        edge = frame_colour(d)
        if edge and edge != ink.lower():
            fails.append(f"{sel[:52]!r} paints the chamfer in {ink!r} but its frame is {edge!r} — "
                         "the corner will read as two different lines meeting at a seam")

    # 8. A bare tone word may not own a box. Two collisions, both measured on the served bundle on
    #    2026-10-21, neither findable by looking at the element that went wrong: the Advanced panel that
    #    holds a provider lane was written `.lane { padding: var(--s-3) var(--s-4) }` and put 14px of
    #    compartment padding — and its chamfer — on the 22px `deployed · primary` ticket, and the callout
    #    rule `.notice, .warn { padding …; font-size … }` made a `probe` ticket 36.9px tall, because
    #    `warn` is also the state class the tickets wear. One page ended up with five ticket heights and
    #    the two rules that caused it never mentioned a ticket. So: a selector whose only class is a state
    #    word is a state, and states tint. Qualifying it — `.lane:not(.chip)`, `.notice.warn` — is the fix,
    #    and passes: the qualification says which of the two objects the rule means.
    for sel, body in blocks(css):
        d = decls(body)
        for part in (p.strip() for p in sel.split(",")):
            if not (BARE_CLASS.match(part) and part[1:] in STATE_WORDS):
                continue
            for name, value in d.items():
                if name in BOX_PROPS:
                    fails.append(f"{part} is a ticket state word and a section word at once — this box "
                                 f"declaration ({name}: {value[:40]}) lands on every `chip {part[1:]}`, "
                                 f"`tag {part[1:]}`. Qualify the selector (`.{part[1:]}:not(.chip)`, "
                                 f"or put it on the section class)")

    # 9. The chamfer must exist *and* be used. A declared-but-unused --chamfer means the cut shown in
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

        # 10. Absolute dark, absolute white. These are the two grounds the brief names; a panel that
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

        # 11. One ticket, one height, owned by one declaration. The label primitive has no vertical
        #     padding and a `min-height` cap, so a ticket is as tall as the cap and a bigger ticket says
        #     so by raising the cap (`.verdict-pill` in a row header). With 4px of vertical padding the
        #     text plus padding measured 21.88px against a 19.94px cap — the cap bound nothing, the
        #     height was arithmetic, and `--fs-micro` at a fluid root size could not be relied on. This
        #     is the check on the fix, so it must outlive the comment that explains it.
        chips = [(sel, decls(body)) for sel, body in rules if matches_class(sel, "chip")]
        for sel, d in chips:
            for bad in vertical_padding(d):
                fails.append(f"{sel[:52]!r} gives a ticket vertical padding ({bad}) — the height stops "
                             "being the min-height cap and becomes padding arithmetic")
        if chips and not any("min-height" in d for _, d in chips):
            fails.append("no rule matching .chip declares min-height — a ticket's height has no owner, "
                         "so it is set by line-height plus padding on whatever line it lands on")
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
    # The published page has no bundler, so it carries the tokens itself — but it does not *type*
    # them any more: `app_css_head()` lifts @font-face, :root and the light block out of the app
    # stylesheet at import time. A regex over the builder's source can only ever see the literal
    # tail, so it would report a page with no ':root' while the page it generates is fine. The
    # value is read off the imported module instead, because a second copy of the lift rule here
    # is the two-answers failure this repo already paid for once (#142).
    out.append(("tools/public_ranking_page.py (PAGE_CSS)", page_css(), True, []))
    if verbose:
        print(f"{len(out)} surface(s): "
              + ", ".join(f"{n} [{len(c.splitlines())} lines]" for n, c, _, _ in out))
    return out


def page_css() -> str:
    """The CSS the generated page will actually ship, read from the generator itself."""
    sys.path.insert(0, str(PAGE_TOOL.parent))
    try:
        import public_ranking_page  # noqa: PLC0415  (the sibling-import path must be set up first)
    except SystemExit as exc:  # app_css_head() refuses to build a page whose tokens are missing
        return f"/* the generator could not be imported: {exc} */"
    return public_ranking_page.PAGE_CSS


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
