#!/usr/bin/env python3
"""Build the public research page from the shipped /ranking — then it can be published.

The page has one job: show the model ranking and each model's measured performance, in the order
the application itself follows. That is why every number here is read through `GET /ranking`
rather than re-derived from the benchmark tables. A generator that re-implemented the sort would
produce a second answer to the same question, and #142 is exactly what two answers cost: the page
went on naming a deployed default that no request ever reached, because the client had its own
idea of what the lane was. Reading the route means the page and the app cannot disagree — the
route even supplies the lane authority, which `/ranking` builds from the same functions `/detect`
walks.

It runs against a copy of the dev database, never the original: the ranking needs a session, and
the cheapest honest session is a throwaway account with no pool and no keys — which is also the
state a first-time visitor to this page is in.

    python tools/public_ranking_page.py --dump      # print the payload, write nothing
    python tools/public_ranking_page.py             # write public-site/dist/ for publication

The publishable directory is `public-site/dist/`: `index.html`, the report at `/research/report.pdf`
and `/research/report.html`, the self-hosted serif under `/fonts/`, and the two favicons. The page's
tokens are not typed here — `app_css_head()` lifts `@font-face`, `:root` and the light block straight
out of `frontend/src/styles.css`, so the published page cannot show a palette the app no longer has.
`dist/` is a separate
output directory rather than the project root because the packager refuses a root `webDirectory`, and
because a site that publishes the whole repo would publish the backend too.

Publishing is a separate, deliberate step (`sites-hosting`); this tool only produces the files.
"""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ is a package of siblings, not a package
import check_visual_language as vis  # noqa: E402  (the stylesheet rule the page has to satisfy)
REAL_DB = ROOT / "backend" / "data" / "detector.sqlite3"
SECRET = ROOT / "backend" / "data" / "backend-secret.key"
SITE_DIR = ROOT / "public-site"
FRONT_PUBLIC = ROOT / "frontend" / "public"
OUT_DIR = SITE_DIR / "dist"

# Mirrors backend/app.py's MEASURED_MIN_TEXTS, and used only in a tooltip that quotes it. The
# tiering itself comes from the route's own `provisional` flag, so a changed constant here can
# make a caption stale but can never make the page sort differently from the app.
MEASURED_LABEL = "24 texts"

# Everything below renders; nothing here re-sorts. The one exception is the split between the
# two tables, which slices on the `provisional` flag the route already set — the route puts the
# full captures first, so the slice preserves the payload order rather than creating a new one.


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _pct(value: object) -> str:
    return "—" if value is None else f"{round(float(value) * 100.0, 1):g}%"


def _health_chip(state: str) -> str:
    tone = {"ok": "good", "degraded": "warn", "down": "bad"}.get(state, "neutral")
    return f'<span class="chip {tone}">{_esc(state)}</span>'


LOCAL_HOST = "local gateway (measuring machine)"


def _host_label(r: dict) -> str:
    """A public page must not print a loopback address.

    Two of the measured models were captured through a gateway running on this machine, and
    `/ranking` reports that host truthfully as 127.0.0.1 — correct for the app, noise on a public
    page, and a leak of private topology. The row keeps its endpoint name and its place in the
    order; only the address is replaced, by a label that says what it actually was.
    """
    host = (r.get("host") or "").strip()
    if not host:
        return ""
    bare = host.split(":")[0]
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        if bare in ("localhost",) or bare.endswith((".local", ".internal", ".localhost")):
            return LOCAL_HOST
        return host
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
        return LOCAL_HOST
    return host


def _row(r: dict) -> str:
    band = r.get("band") or {}
    endpoint = r.get("provider_name") or r.get("provider") or "—"
    host = _host_label(r)
    search = f"{r.get('model', '')} {endpoint} {host} {r.get('corpus', '')}".lower()
    marks = []
    if r.get("lane"):
        marks.append(f'<span class="chip lane">{_esc(r["lane"])}</span>')
    if r.get("free_tier"):
        marks.append('<span class="chip free">free tier</span>')
    if r.get("provisional"):
        marks.append(
            f'<span class="chip warn" title="Fewer texts than {MEASURED_LABEL}: a sample, not a result">'
            f"sample · {_esc(r.get('n_clean'))} texts</span>"
        )
    cells = [
        f'<td class="num rank">{_esc(r.get("rank"))}</td>',
        f'<td class="model"><span class="mname">{_esc(r.get("model"))}</span>'
        f'<span class="mtag">{_esc(r.get("tag"))} · {_esc(r.get("corpus"))} {" ".join(marks)}</span></td>',
        f'<td class="ep"><span class="epname">{_esc(endpoint)}</span>'
        f'<span class="ephost">{_esc(host)}</span></td>',
        f'<td class="num">{_esc(r.get("n_clean"))}<span class="of">of {_esc(r.get("n_rows"))}</span></td>',
        f'<td class="num good-t">{_esc(r.get("correct"))}</td>',
        f'<td class="num {"bad-t" if r.get("wrong") else ""}">{_esc(r.get("wrong"))}</td>',
        f'<td class="num dim">{_esc(r.get("review"))}</td>',
        f'<td class="num strong">{_pct(r.get("accuracy"))}</td>',
        f'<td class="num">{_pct(r.get("coverage"))}</td>',
        f'<td class="band">≥{_esc(band.get("ai"))} · ≤{_esc(band.get("human"))} · g{_esc(band.get("gate"))}</td>',
        f'<td class="health">{_health_chip(r.get("health") or "unknown")}</td>',
    ]
    return (
        f'<tr class="model-row" data-rank="{_esc(r.get("rank"))}" '
        f'data-model="{_esc(r.get("model"))}" data-search="{_esc(search)}">' + "".join(cells) + "</tr>"
    )


def _table(rows: list[dict], *, caption: str, blurb: str) -> str:
    head = (
        '<tr><th>#</th><th>Model</th><th>Endpoint</th><th title="clean texts scored">Texts</th>'
        "<th>Right</th><th>Wrong</th><th title=\"sent to the tie-break, not decided\">Deferred</th>"
        "<th>Accuracy</th><th title=\"share of texts the model decided itself\">Coverage</th>"
        "<th title=\"this model's own saved decision rule\">Band (AI · human · gate)</th><th>Health</th></tr>"
    )
    body = "\n".join(_row(r) for r in rows)
    return (
        f'<section class="panel table-card">\n<h2>{_esc(caption)}</h2>\n'
        f'<p class="sub">{blurb}</p>\n<div class="tablewrap"><table>{head}<tbody>{body}</tbody></table></div>\n'
        "</section>"
    )


def _authority_strip(la: dict | None) -> str:
    """Rendered only from what /ranking reports. If the key is missing the block says nothing —
    a page that invents a lane beside the real one is the #142 bug, in public."""
    if not la:
        return (
            '<section class="panel authority missing"><p class="hint">This payload carries no '
            "lane authority, so this page makes no claim about which model answers a run.</p></section>"
        )
    dd, tb = la.get("deployed_default") or {}, la.get("deployed_tie_break") or {}
    nxt, fail = la.get("next"), la.get("failover") or []
    if nxt:
        from_pool = nxt.get("source") == "user-pool"
        src_label = "first in that visitor’s own using pool" if from_pool else "the admin’s deployed default"
        lead = (
            f'<span class="la-model">{_esc(nxt.get("model"))}</span>'
            f'<span class="la-src">{_esc(nxt.get("provider"))} · {src_label}</span>'
        )
        # Three honest cases, and they must not be collapsed into one sentence: a pool with nothing
        # healthy behind it, a pool with models behind it, and no pool at all — which is what an
        # anonymous visitor to this page is.
        if fail:
            behind = "  →  ".join(_esc(f.get("model")) for f in fail)
            order_line = f'<p class="la-order">If it stops answering, in that pool’s order: <span class="la-fail">{behind}</span></p>'
        elif from_pool:
            order_line = ('<p class="la-order">If it stops answering, nothing else in that pool is '
                          'healthy enough to take the run.</p>')
        else:
            order_line = ('<p class="la-order">If it stops answering, there is no pool behind it to rescue '
                          'the run — a rescue model only exists once you put one into your own using pool.</p>')
    else:
        lead = '<span class="la-model">No primary model is enabled</span>'
        order_line = ""
    return f"""<section class="panel authority">
  <p class="la-lead">A run with nothing chosen is scored by</p>
  <p class="la-row">{lead}</p>
  {order_line}
  <p class="la-chips">
    <span class="chip lane">deployed default · {_esc(dd.get("model") or "none set")}</span>
    <span class="chip neutral">tie-break lane · {_esc((tb or {}).get("model") or "off")}</span>
  </p>
  <p class="hint">{_esc(la.get("note") or "")}</p>
</section>"""


def _dials(rows: list[dict]) -> str:
    measured = [r for r in rows if not r.get("provisional")]
    if not measured:
        return ""
    top = measured[0]
    acc, cov = top.get("accuracy"), top.get("coverage")
    if acc is None and cov is None:
        return ""
    dec = [
        f'<span class="stat"><b>{_esc(top.get("decided"))}</b> decided</span>',
        f'<span class="stat"><b class="{"bad-t" if top.get("wrong") else "good-t"}">{_esc(top.get("wrong"))}</b> wrong</span>',
        f'<span class="stat"><b>{_esc(top.get("review"))}</b> deferred</span>',
        f'<span class="stat"><b>{_esc(top.get("n_clean"))}</b> texts</span>',
    ]
    def dial(label: str, value: object, tone: str) -> str:
        pct = 0 if value is None else round(float(value) * 100.0, 1)
        return (
            f'<div class="dial" style="--pct:{pct}" data-tone="{tone}">'
            f'<div class="dial-in"><span class="dial-v">{_pct(value)}</span>'
            f'<span class="dial-l">{label}</span></div></div>'
        )
    return f"""<section class="panel hero">
  <p class="eyebrow">Best measurement · rank 1 of {len(measured)} full captures</p>
  <p class="hero-model">{_esc(top.get("model"))}</p>
  <p class="sub">{_esc(top.get("tag"))} · {_esc(top.get("corpus"))} · judged at its own band
   ≥{_esc((top.get("band") or {}).get("ai"))} / ≤{_esc((top.get("band") or {}).get("human"))}</p>
  <div class="dials">
    {dial("accuracy on decided texts", acc, "good")}
    {dial("coverage — decided without help", cov, "accent")}
  </div>
  <p class="stats">{" ".join(dec)}</p>
  <p class="hint">Best here means <em>fewest wrong on this corpus at this model’s own saved band</em>.
   It is not the model that answers your runs — the bar above names that one, and a model can be the
   best judge on a corpus and still be unreachable today (health: {_esc(top.get("health") or "unknown")}).</p>
</section>"""


STYLES = ROOT / "frontend" / "src" / "styles.css"


def _block(css: str, opener: str) -> str:
    """The brace-balanced block whose selector matches `opener`, verbatim, or ''."""
    m = re.search(opener, css, re.M)
    if not m:
        return ""
    depth, i = 0, css.index("{", m.end() - 1)
    for j in range(i, len(css)):
        depth += css[j] == "{"
        if css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[m.start(): j + 1]
    return ""


def app_css_head() -> str:
    """The app's @font-face rules and both token blocks, lifted out of frontend/src/styles.css.

    This used to be a hand-copied duplicate with a comment asking whoever changed the palette to
    remember to change it here as well, which is the drift a generator exists to refuse: the page
    and the application cannot now disagree about what --accent or --font-ui means, because there
    is one source for both. It fails loudly rather than emitting a page with no tokens if the
    stylesheet is ever restructured so a block cannot be found.
    """
    css = STYLES.read_text(encoding="utf-8")
    parts = re.findall(r"@font-face\s*\{[^}]*\}", css)
    parts.append(_block(css, r"^:root\s*\{"))
    parts.append(_block(css, r"^html\.light,\s*:root\[data-theme=\"light\"\]\s*\{"))
    missing = [what for what, got in zip(("@font-face", ":root", "light :root"),
                                         (parts[:len(parts) - 2], parts[-2], parts[-1])) if not got]
    if missing:
        raise SystemExit(f"{STYLES.relative_to(ROOT)} no longer has {' and '.join(missing)} "
                         f"in the shape the page copies — update app_css_head(), do not publish "
                         f"an unstyled ranking page")
    return "\n\n".join(p for p in parts if p) + "\n"


# A plain string, not an f-string: CSS is full of braces, and `.format()` would read every one of
# them as a field. Interpolation happens by concatenation in `write_site`.
PAGE_CSS = app_css_head() + """
/* Everything below is this page's own layout; the tokens above are the application's, copied by
   app_css_head() so the two surfaces cannot drift. tools/check_visual_language.py reads both. */
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:400 14.5px/1.6 var(--font-ui);color:var(--ink);background-color:var(--bg);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
/* One interaction vocabulary, as in the app: a gold ring drawn inside the box (a clip-path cuts
   an outside ring away), and a gold response only on something a person can act on. */
a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--touch);outline-offset:var(--focus-offset)}
.wrap{max-width:1180px;margin:0 auto;padding:clamp(14px,3vw,30px) clamp(12px,3vw,26px) 60px}
.topbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand{display:flex;flex-direction:column;gap:2px}
.eyebrow{margin:0;font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-dim)}
h1{margin:0;font-family:var(--font-display);font-weight:600;font-size:clamp(22px,3.4vw,34px);line-height:1.15}
.actions{display:flex;gap:8px;flex-wrap:wrap}
/* One control vocabulary, as in the app: a plain button is a square, and the ONE control that
   commits something — the call to action — carries the corner cut, with the frame's rule painted
   along the diagonals because a clip-path takes the corner line away with the corner. Hover lifts
   the box and steps the plate itself darker; a brightness() on top of that step fought it on the
   white ground, where --accent-2 is the deeper gold. */
a.btn,button.btn{display:inline-flex;align-items:center;gap:7px;padding:9px 16px;
  border:1px solid var(--edge);background-color:var(--panel);color:var(--ink);font:inherit;font-size:13.5px;
  text-decoration:none;cursor:pointer;
  transition:transform .16s ease,border-color .16s ease,background-color .16s ease,color .16s ease}
a.btn:hover,button.btn:hover{transform:translateY(-1px);border-color:var(--touch);
  background-color:color-mix(in srgb,var(--touch) 10%,var(--panel))}
a.btn.cta{background-color:var(--grad-cta);color:var(--on-grad);border-color:var(--accent-deep);
  font-weight:600;clip-path:var(--chamfer);--cut-ink:var(--accent-deep);
  background-image:linear-gradient(to top right,transparent calc(50% - 1px),var(--cut-ink) calc(50% - 1px),var(--cut-ink) 50%,transparent 50%),linear-gradient(to bottom right,transparent calc(50% - 1px),var(--cut-ink) calc(50% - 1px),var(--cut-ink) 50%,transparent 50%);
  background-size:var(--cut-edges-size);background-position:var(--cut-edges-at);
  background-origin:border-box;background-repeat:no-repeat}
a.btn.cta:hover{background-color:color-mix(in srgb,var(--accent-fill) 84%,#000000);color:var(--on-grad)}
/* The app's corner signature: a clip-path takes the top-right and bottom-right corner off every
   panel, and the frame's own rule is painted along the two diagonals, because a clipped box has a
   hole where its corner line used to be. The gradients are written here rather than lifted as a
   --cut-edges token: a custom property resolves its own var() calls where it is DECLARED, so a token
   at :root would paint every corner in :root's ink and this page's .authority strip — which re-points
   --cut-ink — would keep a grey diagonal through its heavier frame. The geometry tokens do come from
   the lifted :root, so the cut stays the same size as the app's. No box-shadow: the box is clipped,
   and a clip-path throws away everything painted outside the edge. */
.panel{border:1px solid var(--line);background-color:var(--panel);
  padding:clamp(16px,2.2vw,24px);margin:0 0 16px;clip-path:var(--chamfer);
  background-image:linear-gradient(to top right,transparent calc(50% - 1px),var(--cut-ink) calc(50% - 1px),var(--cut-ink) 50%,transparent 50%),linear-gradient(to bottom right,transparent calc(50% - 1px),var(--cut-ink) calc(50% - 1px),var(--cut-ink) 50%,transparent 50%);
  background-size:var(--cut-edges-size);background-position:var(--cut-edges-at);
  background-origin:border-box;background-repeat:no-repeat}
.panel h2{margin:0 0 4px;font-family:var(--font-display);font-weight:600;font-size:20px}
.sub{margin:0 0 12px;color:var(--ink-dim);font-size:14px}
.hint{margin:10px 0 0;color:var(--ink-dim);font-size:12.5px;line-height:1.5}
.hero{background-color:var(--panel)}
.hero-model{font-family:var(--font-display);font-weight:600;font-size:clamp(24px,4vw,38px);margin:2px 0 6px;word-break:break-word;line-height:1.15}
.hero .hint em{font-style:italic;color:var(--ink)}
.dials{display:flex;gap:clamp(14px,4vw,32px);flex-wrap:wrap;margin:16px 0 6px}
/* A meter is a circle, not a rounded corner, so it keeps its shape -- but it loses the accent
   bloom it used to cast, and an untinted meter is neutral rather than the interaction gold. */
.dial{position:relative;width:min(150px,38vw);aspect-ratio:1;border-radius:50%;--pct:0;
  background-image:conic-gradient(var(--tone,var(--neutral)) calc(var(--pct)*1%),var(--ring-track) 0)}
.dial[data-tone=good]{--tone:var(--good)}
.dial[data-tone=accent]{--tone:var(--neutral)}
.dial::after{content:"";position:absolute;inset:11px;border-radius:50%;background-color:var(--panel-solid);
  border:1px solid var(--line)}
.dial-in{position:absolute;inset:0;display:grid;place-content:center;gap:2px;text-align:center;z-index:1}
.dial-v{font-family:var(--mono);font-size:clamp(19px,3.6vw,26px);font-weight:600}
.dial-l{font-size:11px;color:var(--ink-dim);max-width:8.5ch;line-height:1.3}
.stats{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--ink-dim);margin:8px 0 0}
.stats b{font-family:var(--mono);color:var(--ink);font-size:15px}
/* Which lane is authoritative is a statement about the deployment, not a control, so the strip
   is neutral: the gold now belongs to the buttons that do something. */
.authority{border-color:var(--line-strong);--cut-ink:var(--line-strong);background-color:var(--panel)}
.la-lead{margin:0;font-size:12.5px;letter-spacing:.02em;color:var(--ink-dim);text-transform:uppercase;font-family:var(--mono)}
.la-row{margin:6px 0 2px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.la-model{font-family:var(--mono);font-size:clamp(15px,2.4vw,19px);font-weight:600;color:var(--ink)}
.la-src{font-size:13px;color:var(--ink-dim)}
.la-order{margin:4px 0 0;font-size:13.5px;color:var(--ink-dim)}
.la-fail{font-family:var(--mono);font-size:12.5px;color:var(--ink)}
.la-chips{margin:10px 0 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
/* One label primitive, the same box the application draws its tickets with: a fact about the row it
   sits on, in two or three words, squared (a chamfer on a box this short is a missing corner, and a
   ticket is not a surface). The height is owned by the cap alone — no vertical padding, because 4px
   of it put the box past its own min-height and made the arithmetic, not the token, the thing that
   decided how tall a ticket was. One height, one tracking, one horizontal padding. Only the states
   that mean something about the model (good / warn / bad) and the free-tier mark take a colour. */
.chip{display:inline-flex;align-items:center;gap:var(--s-1);min-height:1.375rem;
  padding:0 var(--s-2);font-family:var(--mono);font-size:var(--fs-micro);font-weight:600;
  line-height:var(--lh-tight);letter-spacing:var(--tr-chip);
  border:1px solid var(--line);background-color:var(--panel-2);color:var(--ink-dim);white-space:nowrap}
.chip.good{border-color:color-mix(in srgb,var(--good) 45%,transparent);background-color:var(--good-a16);color:var(--good)}
.chip.warn{border-color:color-mix(in srgb,var(--warn) 45%,transparent);background-color:var(--warn-a16);color:var(--warn)}
.chip.bad{border-color:color-mix(in srgb,var(--bad) 45%,transparent);background-color:var(--bad-a16);color:var(--bad)}
.chip.free{border-color:color-mix(in srgb,var(--accent) 55%,transparent);background-color:var(--accent-a16);color:var(--accent)}
.chip.neutral{color:var(--neutral)}
.chip.lane{border-color:var(--line-strong);color:var(--ink)}
.filter{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
/* The search field keeps its corners: a single-line input scrolls its own text to the right, and a
   notch on that edge is exactly where the caret lands on a full line. */
.filter input{flex:1;min-width:220px;padding:10px 16px;border:1px solid var(--edge);
  background-color:var(--field-bg);color:var(--ink);font:inherit;font-size:14px}
.filter .count{font-family:var(--mono);font-size:12.5px;color:var(--ink-dim)}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:0;background-color:var(--panel-2);font-family:var(--mono);font-size:11px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-dim);z-index:2}
/* Hovering a row is the one place the interaction gold appears in the table: it answers the pointer. */
tbody tr:hover{background-color:color-mix(in srgb,var(--touch) 8%,transparent)}
td.num{font-family:var(--mono);text-align:right}
td.num .of{display:block;font-size:10.5px;color:var(--ink-dim)}
td.strong{font-weight:700}
td.dim,.dim{color:var(--ink-dim)}
td.rank{color:var(--ink);font-weight:700}
td.model{white-space:normal;min-width:190px}
.mname{display:block;font-family:var(--mono);font-size:13px;font-weight:600;word-break:break-all}
.mtag{display:block;color:var(--ink-dim);font-size:11.5px;white-space:normal;margin-top:3px}
td.band{font-family:var(--mono);font-size:11.5px;color:var(--ink-dim);white-space:normal;min-width:120px}
.epname{display:block;font-size:12.5px}
.ephost{display:block;font-family:var(--mono);font-size:10.5px;color:var(--ink-dim)}
.good-t{color:var(--good)}
.bad-t{color:var(--bad)}
tr.hidden{display:none}
.pdfview{position:fixed;inset:0;z-index:60;display:flex;flex-direction:column;gap:8px;padding:10px;
  background-color:color-mix(in srgb,var(--bg) 88%,transparent)}
.pdfview[hidden]{display:none}
.pv-bar{display:flex;gap:8px;align-items:center;justify-content:flex-end}
.pv-title{margin-right:auto;font-family:var(--mono);font-size:12.5px;color:var(--ink-dim)}
/* The viewer is a document surface, so it stays a plain rectangle: a notch in its corner would
   cut into the PDF's own page, which is content rather than chrome. */
#pv-frame{flex:1;width:100%;border:1px solid var(--line-strong);background-color:#fff;min-height:60vh}
footer{margin-top:22px;color:var(--ink-dim);font-size:12.5px;line-height:1.6}
footer code{font-family:var(--mono);font-size:12px;color:var(--ink);word-break:break-all}
.prov{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

# Enhancements only: every number is already in the HTML, so the page still reads with JS off.
# Nothing here sorts. The filter hides rows; the order is fixed in the markup by the generator.
PAGE_JS = """
(function () {
  var root = document.documentElement;
  var saved = null;
  try { saved = localStorage.getItem('aidetect-theme'); } catch (e) {}
  if (saved === 'light' || saved === 'dark') { root.setAttribute('data-theme', saved); }
  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      toggle.textContent = next === 'light' ? 'Dark mode' : 'Light mode';
      try { localStorage.setItem('aidetect-theme', next); } catch (e) {}
    });
    var cur = root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    toggle.textContent = cur === 'light' ? 'Dark mode' : 'Light mode';
  }
  var field = document.getElementById('filter');
  var count = document.getElementById('visible-count');
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr.model-row'));
  function apply() {
    if (!field || !count) { return; }
    var q = field.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (tr) {
      var hit = !q || tr.getAttribute('data-search').indexOf(q) !== -1;
      tr.classList.toggle('hidden', !hit);
      if (hit) { shown++; }
    });
    count.textContent = shown + ' of ' + rows.length + ' models' + (q ? ' matching “' + field.value.trim() + '”' : '');
  }
  if (field) { field.addEventListener('input', apply); apply(); }

  /* Read the PDF in place. The host serves the bytes as application/octet-stream — verified live,
     476,570 B with %PDF- magic (2026-10-20) — so a browser navigates to it as a download instead of
     a page. Re-typing the same bytes as a Blob is what makes them render, and it was proven here: an
     iframe on that object URL reports document.contentType "application/pdf". The raw link is kept
     as the fallback (and as the Download button), so nothing is lost if the fetch fails. */
  var cta = document.getElementById('pdf-cta');
  var view = document.getElementById('pdfview');
  var frame = document.getElementById('pv-frame');
  var closeBtn = document.getElementById('pv-close');
  var lastObjectUrl = null;
  function shut() {
    if (!view) { return; }
    view.hidden = true;
    if (frame) { frame.src = 'about:blank'; }
    if (lastObjectUrl) { URL.revokeObjectURL(lastObjectUrl); lastObjectUrl = null; }
  }
  function open(ev) {
    if (ev && ev.metaKey === true) { return; }
    if (!view || !frame || typeof fetch !== 'function') { return; }
    if (ev) { ev.preventDefault(); }
    view.hidden = false;
    frame.src = 'about:blank';
    fetch('/research/report.pdf').then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.arrayBuffer();
    }).then(function (buf) {
      lastObjectUrl = URL.createObjectURL(new Blob([buf], { type: 'application/pdf' }));
      frame.src = lastObjectUrl;
    }).catch(function () { window.location.href = '/research/report.pdf'; });
  }
  if (cta) { cta.addEventListener('click', open); }
  if (closeBtn) { closeBtn.addEventListener('click', shut); }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && view && !view.hidden) { shut(); }
  });
})();
"""


def _footer(payload: dict, rows: list[dict], measured: int, probes: int, generated: str) -> str:
    corpora = sorted({r.get("corpus", "") for r in rows if r.get("corpus")})
    return f"""<footer class="panel">
  <p><b>Where these numbers come from.</b> Every cell is the deployed application's own
  <code>GET /ranking</code> answer — the same route the app's Ranking page reads — scored per model at
  the decision band saved on that model, over {measured} full captures and {probes} short probes of
  {MEASURED_LABEL} or more/fewer texts. The page is generated from that response, so it cannot
  disagree with the app: nothing here is re-sorted, re-scored or averaged on the way in.</p>
  <p><b>What a rank means.</b> Fewest wrong first, then most decided, then accuracy, then coverage —
  a model that never errs outranks one that guesses right more often. Deferred texts were sent to the
  tie-break lane and are not counted as correct or wrong. Accuracy is over decided texts only, so
  read it together with coverage.</p>
  <p><b>What Health means.</b> It is availability, not accuracy: the application measures it from real
  detections of that model, so it reads <em>unknown</em> until one runs and can turn
  <em>degraded</em> or <em>down</em> when a gateway stops answering — which a published benchmark cannot
  show, because a model can hold a perfect score and still have been delisted by its provider.</p>
  <p><b>Corpora:</b> {_esc(", ".join(corpora))}</p>
  <div class="prov">
    <span class="chip neutral">generated {_esc(generated)}</span>
    <span class="chip neutral">{len(rows)} ranked models</span>
    <span class="chip neutral">python tools/public_ranking_page.py</span>
  </div>
  <p class="hint">{_esc(payload.get("note") or "")}</p>
</footer>"""


def write_site(payload: dict) -> list[Path]:
    rows = payload.get("ranked") or []
    if not rows:
        raise SystemExit("/ranking returned no rows — nothing to publish")
    measured = [r for r in rows if not r.get("provisional")]
    probes = [r for r in rows if r.get("provisional")]
    # The route already puts full captures first, so slicing by the flag it set keeps two tables out
    # of one payload without inventing a second order. Asserted, because if that ever stops being
    # true, publishing a table whose order differs from the app's is exactly the #142 failure.
    if [r["model"] for r in measured + probes] != [r["model"] for r in rows]:
        raise SystemExit("/ranking stopped ordering measured rows before probes; refusing to split the tables")

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "research").mkdir(exist_ok=True)
    (OUT_DIR / "fonts").mkdir(exist_ok=True)

    copied: list[Path] = []
    for src, dst in [
        (FRONT_PUBLIC / "research" / "report.pdf", OUT_DIR / "research" / "report.pdf"),
        (FRONT_PUBLIC / "research" / "report.html", OUT_DIR / "research" / "report.html"),
        (FRONT_PUBLIC / "favicon.svg", OUT_DIR / "favicon.svg"),
        (FRONT_PUBLIC / "favicon.ico", OUT_DIR / "favicon.ico"),
    ]:
        if not src.exists():
            raise SystemExit(f"missing asset to publish: {src}")
        shutil.copy2(src, dst)
        copied.append(dst)
    keep = set()
    for font in sorted((FRONT_PUBLIC / "fonts").glob("*")):
        if font.suffix in (".woff2", ".txt"):
            shutil.copy2(font, OUT_DIR / "fonts" / font.name)
            copied.append(OUT_DIR / "fonts" / font.name)
            keep.add(font.name)
    # Then drop what the source no longer carries. The copy above is additive, so when the type pass
    # moved from Cormorant + Spectral to Literata the old faces stayed in `dist/fonts` and went out
    # on the public release: 113,900 B of .woff2 nobody references, 122,679 B counting their two
    # licences, in an artifact people cite — it claimed three families where the app ships one.
    # `dist` is this function's own staging directory, so the authoritative set is the source's, and
    # each removal is printed rather than silent: a file a human put in `dist` by hand would show up
    # here as a deletion, which is the thing to notice, not to hide.
    for stale in sorted((OUT_DIR / "fonts").iterdir()):
        if stale.is_file() and stale.suffix in (".woff2", ".txt") and stale.name not in keep:
            stale.unlink()
            print(f"  pruned fonts/{stale.name} — the app no longer ships that face")

    pdf_bytes = (OUT_DIR / "research" / "report.pdf").stat().st_size
    tables = []
    if measured:
        tables.append(_table(
            measured,
            caption=f"Full captures ({len(measured)})",
            blurb="Scored over " + MEASURED_LABEL + " or more real texts. This is the ranking the "
                  "application’s own Ranking page reads from the same route, and the list a visitor "
                  "picks their using pool from — which model answers a run is named above, because the "
                  "pool, not this table, decides that.",
        ))
    if probes:
        tables.append(_table(
            probes,
            caption=f"Short probes ({len(probes)})",
            blurb=f"Under {MEASURED_LABEL} of real texts. Each is a sample, not a result — a lucky 2-text "
                  "run cannot outrank an honest 48-text one, which is why these sit below the full captures "
                  "however few errors they show.",
        ))

    page = (
        "<!doctype html>\n<html lang=\"en\" data-theme=\"dark\">\n<head>\n"
        "<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Litmus — measured model ranking of AI text detectors</title>\n"
        '<meta name="description" content="Every free and paid model the detector has measured, ranked '
        "by errors first, in the order the application actually follows. Includes the technical report as PDF.\">\n"
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        '<link rel="alternate icon" href="/favicon.ico">\n<style>' + PAGE_CSS + "</style>\n</head>\n<body>\n"
        '<div class="wrap">\n<div class="topbar"><div class="brand">'
        "<p class=\"eyebrow\">Litmus · open research</p>\n"
        "<h1>Which model judges text, measured</h1>\n</div>\n"
        '<div class="actions">\n'
        '<a class="btn cta" id="pdf-cta" href="/research/report.pdf">Read the report (PDF)</a>\n'
        '<a class="btn" href="/research/report.html">Report in the browser</a>\n'
        '<button class="btn" id="theme-toggle" type="button">Light mode</button>\n'
        "</div></div>\n"
        + _authority_strip(payload.get("lane_authority"))
        + _dials(rows)
        + '<section class="panel"><p class="eyebrow">Filter</p>'
        '<div class="filter" style="margin:8px 0 0"><label class="sub" for="filter" style="margin:0">Search by model, endpoint or corpus</label>'
        '<input id="filter" type="search" placeholder="e.g. glm, NVIDIA, hc3-48" autocomplete="off">'
        '<span class="count" id="visible-count"></span></div></section>\n'
        + "".join(tables)
        + "\n"
        + _footer(payload, rows, len(measured), len(probes), generated)
        + "\n</div>\n"
        + f"""<div class="pdfview" id="pdfview" hidden role="dialog" aria-modal="true" aria-label="Technical report">
  <div class="pv-bar"><span class="pv-title">report.pdf · {pdf_bytes:,} bytes, published unchanged</span>
  <a class="btn" id="pv-download" href="/research/report.pdf" download>Download instead</a>
  <button class="btn" id="pv-close" type="button">Close</button></div>
  <iframe id="pv-frame" title="Technical report PDF"></iframe>
</div>
"""
        + "<script>" + PAGE_JS + "</script>\n</body>\n</html>\n"
    )

    out = OUT_DIR / "index.html"
    out.write_text(page, encoding="utf-8")

    # Self-check: the published row order, read back out of the file, is the route's order.
    rendered = re.findall(r'<tr class="model-row" data-rank="(\d+)" data-model="([^"]*)"', out.read_text(encoding="utf-8"))
    want = [(str(r["rank"]), r["model"]) for r in rows]
    if rendered != want:
        raise SystemExit(
            f"rendered order != /ranking order ({len(rendered)} rows read back vs {len(want)} in the payload)"
        )
    # …and the stylesheet read back out of the file is one a parser will actually apply. Written
    # second because the order check names a different failure; both refuse to publish a page that
    # is not what the source says it is.
    html_text = out.read_text(encoding="utf-8")
    style = re.search(r"<style>(.*?)</style>", html_text, re.S)
    if not style:
        raise SystemExit("no <style> block in the written page")
    breaks = vis.style_guards(style.group(1), tokens=True)
    if breaks:
        raise SystemExit("the page's own stylesheet would not be read as written:\n  - "
                         + "\n  - ".join(breaks))
    print(f"wrote {out} ({out.stat().st_size:,} B) — {len(rows)} rows in /ranking order, verified "
          f"(stylesheet parsed: {len(style.group(1).splitlines())} lines)")
    for dst in copied:
        print(f"  {dst.relative_to(SITE_DIR)} ({dst.stat().st_size:,} B)")
    return [out, *copied]


def fetch_ranking() -> dict:
    """The live answer of GET /ranking, from this working copy's code over a copy of the data."""
    if not REAL_DB.exists():
        raise SystemExit(f"no database to read at {REAL_DB}")
    tmp = Path(tempfile.mkdtemp(prefix="ranking-page-")) / "copy.sqlite3"
    shutil.copy2(REAL_DB, tmp)
    os.environ["DETECTOR_DB"] = str(tmp)
    # Inherited, not replaced: /ranking marks which endpoints have a working key behind them, and
    # that only decrypts with the key the rows were written under.
    os.environ["DETECTOR_SECRET"] = str(SECRET)
    sys.path.insert(0, str(ROOT))

    from backend.app import create_app  # noqa: E402  (after env, before db import binds)

    app = create_app()
    client = app.test_client()
    # A throwaway session: registering writes only to the copy, and the ranking a visitor sees must
    # not depend on anyone's private pool. Sign-up is addressed by e-mail now (name, address,
    # password twice), so this posts what the shipped form posts -- the same contract a real
    # visitor walks, rather than a field name the page no longer sends.
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    pw = "unused-for-ranking"
    r = client.post("/auth/register", json={"name": f"Page generator {stamp}",
                                            "email": f"pagegen{stamp}@invalid.example",
                                            "password": pw, "confirm": pw})
    if r.status_code not in (200, 201):
        raise SystemExit(f"could not open a session on the copied DB: {r.status_code} {r.get_json()}")
    r = client.get("/ranking")
    if r.status_code != 200:
        raise SystemExit(f"GET /ranking answered {r.status_code}: {r.get_json()}")
    payload = r.get_json()
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", action="store_true", help="print the /ranking payload and exit")
    args = ap.parse_args()

    payload = fetch_ranking()
    if args.dump:
        rows = payload.get("ranked", [])
        print(f"ranked rows: {len(rows)}")
        print("keys:", json.dumps(sorted(rows[0].keys())) if rows else "(none)")
        print("lane_authority:", json.dumps(payload.get("lane_authority"), indent=1))
        print("note:", payload.get("note"))
        print("first three:")
        print(json.dumps(rows[:3], indent=1))
        prov = sum(1 for r in rows if r.get("provisional"))
        lanes = [(r["model"], r["lane"]) for r in rows if r.get("lane")]
        print(f"provisional {prov} / measured {len(rows) - prov}; deployed lanes: {lanes}")
        return 0

    write_site(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
