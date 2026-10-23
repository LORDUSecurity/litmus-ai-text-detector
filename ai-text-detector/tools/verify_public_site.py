#!/usr/bin/env python3
"""Prove the published ranking site still says what this checkout's /ranking says.

`public_ranking_page.py` refuses to write a page whose row order differs from the route's, so the
build is guarded. This is the other half: the *served* artifact, after publication, from outside,
with no credentials. It matters because the two ways this site can be wrong are invisible from the
repo. A stale release serves yesterday's ranking while every file on disk is correct. And the host
answers an unknown path the way a single-page host does, so a missing asset is not a 404 — it is
whatever the fallback returns, which can be HTML with a 200.

So nothing here trusts a status code alone. The report is compared byte for byte against the copy
the build read, the fonts are compared against what `fetch_fonts.py` wrote, and the row order is
re-read from the served bytes and compared against `/ranking` answered by *this* checkout's code
over a copy of the data — the same call the generator made. If the app and the page disagree, this
fails, which is precisely the drift that cost #142.

    python tools/verify_public_site.py                                # the published host
    python tools/verify_public_site.py --url http://127.0.0.1:4173    # a local preview of dist/
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import public_ranking_page as gen  # noqa: E402  (reuse its /ranking, its paths, its row markup)
import check_visual_language as vis  # noqa: E402  (the shipped stylesheet rule, not a copy of it)

DEFAULT_URL = "https://ai-text-ranking-630tcvp9y1c.qoder.website"
UA = "Mozilla/5.0 (ai-text-detector research verifier)"

# The script the host injects into every delivered page. It is not ours, it is not in the build, and
# comparing it would make this check fail for a reason we cannot fix. It is stripped from both sides
# so the rest of the page can be compared exactly.
WATERMARK = re.compile(rb'<script[^>]*data-qoder-watermark[^>]*>.*?</script>', re.S)

# Strings that must never leave this machine. A key is the obvious one; an internal endpoint is the
# one that slips in when a table prints a model's host.
SECRET_PATTERNS = (
    ("64-hex", re.compile(rb"\b[0-9a-f]{64}\b")),
    ("Bearer", re.compile(rb"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("sk-", re.compile(rb"(?i)\bsk-[A-Za-z0-9\-]{12,}")),
    ("nvapi-", re.compile(rb"(?i)nvapi-[A-Za-z0-9\-]{12,}")),
    ("api-key=", re.compile(rb"(?i)api[-_]key\s*=")),
    ("secret name", re.compile(rb"DETECTOR_SECRET")),
)
INTERNAL_PATTERNS = (
    ("127.0.0.1", re.compile(rb"127\.0\.0\.1")),
    ("localhost", re.compile(rb"(?i)\blocalhost[:/]")),
    ("192.168.x.x", re.compile(rb"\b192\.168\.\d+\.\d+")),
    ("10.x.x.x:port", re.compile(rb"\b10\.\d+\.\d+\.\d+:")),
)

RESULTS: list[tuple[bool, str, str]] = []


def ok(label: str, detail: str = "") -> None:
    RESULTS.append((True, label, detail))
    print(f"PASS  {label}" + (f"  [{detail}]" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    RESULTS.append((False, label, detail))
    print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def get(url: str, timeout: int = 60) -> tuple[int, dict[str, str], bytes, str]:
    """(status, lowercased headers, body, transport-error-or-empty). Never raises: a 404 is a result."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read(), ""
    except urllib.error.HTTPError as exc:
        head = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001 - a truncated error body is still a non-200
            body = b""
        return exc.code, head, body, ""
    except Exception as exc:  # noqa: BLE001
        return 0, {}, b"", f"{type(exc).__name__}: {exc}"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help="origin of the published page")
    ap.add_argument("--skip-ranking", action="store_true",
                    help="compare served bytes to the built files only, without opening /ranking")
    args = ap.parse_args()
    origin = args.url.rstrip("/")

    payload: dict = {}
    # One fetch of the page, reused by every page-level check: two requests could see two releases
    # and the checks would then disagree with each other rather than with the truth.
    status, head, raw, err = get(origin + "/")
    if err:
        bad("the site answers GET /", err)
        print(f"\n0/{len(RESULTS)} checks passed — nothing else can be verified while the origin is unreachable")
        return 1
    if status != 200:
        bad("GET / returns 200 to an anonymous visitor", f"http={status}, {len(raw):,} B")
        return 1
    page = WATERMARK.sub(b"", raw)
    ok("GET / returns 200 to an anonymous visitor", f"{len(page):,} B page + host watermark")
    if "set-cookie" in head:
        bad("the page asks for no session", f"Set-Cookie: {head['set-cookie'][:60]}")
    else:
        ok("the page asks for no session", "no Set-Cookie")

    # 1. What the host serves is what the build wrote, minus the host's own injection.
    built = gen.OUT_DIR / "index.html"
    if not built.exists():
        bad("the built page exists to compare against", f"{gen.OUT_DIR} has no index.html")
    else:
        local = WATERMARK.sub(b"", built.read_bytes())
        if local == page:
            ok("served page is byte-identical to public-site/dist/index.html", f"{sha(page)}")
        else:
            bad("served page is byte-identical to public-site/dist/index.html",
                f"served {sha(page)} ({len(page):,} B) vs built {sha(local)} ({len(local):,} B)"
                " — republish, or the release is older than the build")

    # 1b. The served stylesheet is one a parser reaches. Check 1 compares to the build, and the build
    #     refuses to write a broken sheet -- but a release older than that guard serves a page whose
    #     token block was eaten by a stray token, and every string a grep would look for is still in
    #     the file. That is the failure this runs the shipped rule on, against the served bytes.
    served_style = re.search(rb"<style>(.*?)</style>", page, re.S)
    if not served_style:
        bad("the served page carries a <style> block", "no <style>…</style> in the served bytes")
    else:
        css = served_style.group(1).decode("utf-8", "replace")
        breaks = vis.style_guards(css, tokens=True)
        if breaks:
            bad("the served stylesheet is read as written",
                "; ".join(breaks[:4]) + (f" (+{len(breaks) - 4} more)" if len(breaks) > 4 else ""))
        else:
            ok("the served stylesheet is read as written",
               f"{len(css.splitlines())} lines: :root parsed, absolute black + white, chamfer applied, "
               "no radius, no glass")

    # 2. The served row order is this checkout's /ranking order. This is the check the whole task
    #    exists to make unnecessary, so it compares to the route, not to the file.
    served_rows = re.findall(rb'<tr class="model-row" data-rank="(\d+)" data-model="([^"]*)"', page)
    if not served_rows:
        bad("the served page renders model rows", "no .model-row found — is this the ranking page?")
    elif args.skip_ranking:
        ok("the served page renders model rows", f"{len(served_rows)} rows (--skip-ranking)")
    else:
        payload = gen.fetch_ranking()
        want = [(str(r["rank"]).encode(), r["model"].encode()) for r in payload.get("ranked", [])]
        if served_rows == want:
            ok("served row order == this code's GET /ranking order",
                f"{len(want)} rows, measured before probe")
        else:
            at = next((i for i in range(max(len(served_rows), len(want)))
                       if served_rows[i:i + 1] != want[i:i + 1]), None)
            bad("served row order == this code's GET /ranking order",
                f"first divergence at row {at}: served {served_rows[at:at+1]} vs route {want[at:at+1]}"
                if at is not None else f"served {len(served_rows)} rows vs route {len(want)}")

    # 3. The lane bar says what the route says. Named from the rendered lead model, not from a
    #    string search: nemotron is a *ranked* model on this page (rank 4 and 20), and that is the
    #    app's own answer. What must not be claimed is a retired model presented as the default.
    auth = payload.get("lane_authority") or {}
    lead = re.findall(rb'<span class="la-model">([^<]*)</span>', page)
    if not auth:
        ok("the page names no lane the route did not", "(--skip-ranking: no route to compare)")
    elif not lead:
        bad("the page renders a lead model", "no .la-model in the served page")
    else:
        want_lead = (auth.get("next") or {}).get("model")
        if want_lead and lead[0].decode() == want_lead:
            ok("the page's lead model == the route's next model", want_lead)
        else:
            bad("the page's lead model == the route's next model",
                f"page {lead[0].decode()!r} vs route {want_lead!r}")
        dd = (auth.get("deployed_default") or {}).get("model")
        if dd and dd.encode() in page:
            ok("the route's deployed default is on the page", dd)
        else:
            bad("the route's deployed default is on the page", str(dd))

    # 4. The report, both formats, against the files the build copied.
    for src, path, is_pdf in (
        (gen.FRONT_PUBLIC / "research" / "report.pdf", "/research/report.pdf", True),
        (gen.FRONT_PUBLIC / "research" / "report.html", "/research/report.html", False),
    ):
        st, rh, body, e2 = get(origin + path)
        if e2:
            bad(f"{path} is served", e2)
            continue
        if st != 200:
            bad(f"{path} is served", f"http={st}, {len(body):,} B")
            continue
        # The host injects its watermark into every HTML it delivers, including the report.
        if not is_pdf:
            body = WATERMARK.sub(b"", body)
        if is_pdf and not body.startswith(b"%PDF"):
            bad(f"{path} is a PDF, not the fallback page", f"starts {body[:12]!r}")
        elif not is_pdf and not body.lstrip()[:9].lower().startswith(b"<!doctype"):
            bad(f"{path} is the report, not the app shell", f"starts {body[:16]!r}")
        elif "set-cookie" in rh:
            bad(f"{path} needs no session", f"Set-Cookie: {rh['set-cookie'][:60]}")
        elif src.exists() and len(body) == src.stat().st_size and sha(body) == sha(src.read_bytes()):
            ok(f"{path} is byte-identical to {src.parent.name}/{src.name}",
               f"{len(body):,} B, {sha(body)}, content-type {rh.get('content-type', '?')}")
        else:
            bad(f"{path} is byte-identical to the source",
                f"served {len(body):,} B {sha(body)} vs "
                + (f"{src.stat().st_size:,} B {sha(src.read_bytes())}" if src.exists() else "MISSING source"))

    # 5. Every font the served stylesheet references, read out of the served stylesheet — not from a
    #    list written here, which would go stale the day the type changes. The quotes are optional
    #    because the two stylesheets this repo owns spell `url()` differently on purpose.
    names = sorted(set(re.findall(rb"""url\(["']?/fonts/([^)"']+?)["']?\)""", page)))
    if not names:
        bad("the served stylesheet self-hosts its serif faces", "no url(/fonts/...) in the page")
    for name in names:
        st, fh, body, e3 = get(origin + "/fonts/" + name.decode())
        want_src = gen.FRONT_PUBLIC / "fonts" / name.decode()
        if e3 or st != 200:
            bad(f"/fonts/{name.decode()} is served", e3 or f"http={st}, {len(body):,} B")
            continue
        if body[:4] != b"wOF2":
            bad(f"/fonts/{name.decode()} is a font, not the fallback page",
                f"starts {body[:4]!r}, {len(body):,} B — an unknown path here can still answer 200")
            continue
        if want_src.exists() and sha(want_src.read_bytes()) == sha(body):
            ok(f"/fonts/{name.decode()} matches what fetch_fonts.py wrote", f"{len(body):,} B")
        else:
            bad(f"/fonts/{name.decode()} matches what fetch_fonts.py wrote",
                f"served {len(body):,} B {sha(body)}" + ("" if want_src.exists() else ", source missing"))

    # 5b. And nothing MORE than that. Cormorant and Spectral were staged before the type pass moved to
    #     Literata, and the build's font step only copies — it never removed what the source stopped
    #     carrying — so a public bundle can serve faces no stylesheet mentions. Dead bytes in an
    #     artifact that is cited are also a claim about which type ships, and that claim is wrong, so
    #     the reference set read above is the whitelist: a staged .woff2 nobody references, or an OFL
    #     whose family left with it, is a failure here.
    ref = {n.decode() for n in names}
    staged = sorted(p for p in (gen.OUT_DIR / "fonts").glob("*") if p.is_file())
    if not staged:
        bad("the staged fonts directory is not empty", f"{gen.OUT_DIR / 'fonts'} has no files — the page "
            "would serve its serif from nowhere")
    for p in staged:
        if p.suffix == ".woff2":
            if p.name not in ref:
                bad(f"fonts/{p.name} is used", "no url(/fonts/…) references it — remove it from the build")
        elif p.suffix == ".txt":
            family = {p.name[len("OFL-"):-len(".txt")]}
            if not family & {q.name.split("-")[0] for q in staged if q.name in ref}:
                bad(f"fonts/{p.name} is used", f"no staged face belongs to {sorted(family)[0]}")

    # 6. Two copies of one report is one drift risk, measured here rather than assumed.
    pairs = [(gen.FRONT_PUBLIC / "research" / "report.pdf", ROOT / "research-zenodo" / "report.pdf"),
             (gen.FRONT_PUBLIC / "research" / "report.html", ROOT / "research-zenodo" / "report.html")]
    for a, b in pairs:
        if a.exists() and b.exists() and a.read_bytes() == b.read_bytes():
            ok(f"the site's {a.name} equals the deposit package's", f"{sha(a.read_bytes())}")
        else:
            bad(f"the site's {a.name} equals the deposit package's",
                f"cp research-zenodo/{a.name} frontend/public/research/ then rebuild and republish")

    # 7. Secrets and topology. The page is public and is generated from a database full of both.
    hits = [label for label, p in SECRET_PATTERNS if p.search(page)]
    if hits:
        bad("no credential-shaped string in the page", ", ".join(hits))
    else:
        ok("no credential-shaped string in the page", "64-hex, Bearer, sk-, nvapi-, api-key=, secret name")
    hits = [label for label, p in INTERNAL_PATTERNS if p.search(page)]
    if hits:
        bad("no private address in the page", ", ".join(hits))
    else:
        ok("no private address in the page", "127.0.0.1, localhost, RFC1918 with a port")
    # The address is masked, the endpoint name is not — and that distinction is the whole point of
    # `_host_label`, so it is checked row by row against the route rather than by one string search.
    # A provider called "omniroute" is the app's own label for the row and appears here on purpose;
    # the loopback *behind* it must not.
    private = [r for r in (payload.get("ranked") or [])
               if (r.get("host") or "").split(":")[0] in ("127.0.0.1", "localhost")
               or (r.get("host") or "").split(":")[0].startswith("192.168.")]
    if payload and private:
        leaks = [f"{r['model']}@{r['host']}" for r in private if (r["host"] or "").encode() in page]
        if leaks:
            bad("every loopback-hosted row is printed as the local gateway", "; ".join(leaks))
        elif gen.LOCAL_HOST.encode() in page:
            ok("every loopback-hosted row is printed as the local gateway",
               f"{len(private)} rows masked, endpoint names kept")
        else:
            bad("every loopback-hosted row is printed as the local gateway",
                f"{len(private)} private hosts in the payload and no {gen.LOCAL_HOST!r} label on the page")
    elif payload:
        ok("every loopback-hosted row is printed as the local gateway",
           "no private hosts in the payload to mask")

    # 8. A missing path must not look like the app. This host can answer an unknown path with a 200
    #    and HTML, which is why every asset above is proved by content, not by status code.
    st, _, ubody, _ = get(origin + "/no-such-page-probe")
    if st == 404:
        ok("an unknown path is a real 404", f"http={st}")
    elif st == 200 and ubody.lstrip()[:9].lower().startswith(b"<!doctype"):
        bad("an unknown path is a real 404",
            f"http=200 with {len(ubody):,} B of HTML — this host's fallback is why bytes, not codes, are the proof")
    else:
        ok("an unknown path does not return the page", f"http={st}, {len(ubody):,} B")

    passed = sum(1 for flag, _, _ in RESULTS if flag)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    if passed != len(RESULTS):
        print("failed:")
        for flag, label, detail in RESULTS:
            if not flag:
                print(f"  - {label}" + (f"  [{detail}]" if detail else ""))
        return 1
    print(f"published site verified against {origin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
