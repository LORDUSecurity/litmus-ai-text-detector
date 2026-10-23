#!/usr/bin/env python3
"""Prove the containerized stack actually serves a detection -- the checks #106 owed.

Everything before this run was static: `docker compose config` parses YAML, and the no-daemon
preflight reads Dockerfiles. Neither can see that a HEALTHCHECK aimed at an authenticated route
leaves the backend unhealthy forever, that the purger used to create n8n's SQLite as root and
crash-loop the engine, or that the shipped `n8n-import.sh` args get rewritten by Git Bash. Those
three only surface against a live daemon, so this drives the running stack and reports what it
actually got.

    python tools/verify_container_deploy.py [base_url]
    SKIP_DETECT=1 python tools/verify_container_deploy.py     # served bytes + catalogue, no lane call

Defaults to http://localhost:8080 (the published nginx origin) and talks to the API through it at
/api/*, so every answer below travelled nginx -> gunicorn -> n8n -> the provider lane -- except the
two detections, which are a purchase on the operator's key and are skipped when SKIP_DETECT is set. The texts come
from benchmark/corpus.json -- real HC3 rows with recorded labels -- because an authored sample would
only prove the pipeline accepts English, not that it separates the two classes it is calibrated on.
Nothing here prints a key: credentials are read from the repo .env and used, never echoed.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import re
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# `python tools/x.py` puts tools/ on sys.path already, but the check is written so that running it
# from the repo root (`python -m …`, or an IDE that changes cwd) still resolves the sibling module
# rather than failing at import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_visual_language as vis  # noqa: E402  # the same visual-language guard the build runs
import make_icon  # noqa: E402  # so the served favicon can be checked against the geometry that writes it
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
API = BASE + "/api"
# Name a lane to score the texts on it instead of the shipped default, e.g.
#   DETECT_MODEL=google/diffusiongemma-26b-a4b-it python tools/verify_container_deploy.py
# This exists because the deploy is not the only thing that can make a detection fail: on
# 2026-10-17 the shared NVIDIA pool could not answer a 320-char text on the shipped primary inside
# its own 180 s ceiling (raised to 240 s on 2026-10-17), twice, while the same containers answered
# another calibrated row in 1.8 s. Measured again on 2026-10-19: the same model answered in 11.6 s
# -- on OpenRouter -- while NVIDIA's chat endpoint never answered an 8-token probe inside 30 s, and
# its /v1/models answered in 0.2 s. Listed, not serving (see README).
# Overriding the lane proves nginx -> gunicorn -> n8n -> provider -> parse -> verdict -> phrases ->
# history end to end, and the class-separation check is then skipped rather than quietly re-labelled,
# because that lane's operating point is not the one the stack ships.
LANE = (os.environ.get("DETECT_MODEL") or "").strip() or None
# Which gateway to pay that lane with. The backend fills base_url from a stored `models` row, and a
# lane with no row -- any OpenRouter model, since this deploy pins its two lane rows on NVIDIA --
# used to keep the workflow's env default: the model id got posted to integrate.api.nvidia.com, the
# gateway answered the plain-text "not a valid model ID", and the override appeared to be the
# pipeline's failure rather than the pool's. This is the variable the shipped tool lacked on
# 2026-10-19, when the only gateway answering was OpenRouter.
#   DETECT_MODEL=inclusionai/ling-3.1-flash \
#   DETECT_BASE_URL=https://openrouter.ai/api/v1 python tools/verify_container_deploy.py
LANE_BASE_URL = (os.environ.get("DETECT_BASE_URL") or "").strip() or None
# Set SKIP_DETECT=1 to prove the deploy without asking a provider anything. The two texts below are
# real HC3 rows scored on the deployed lane on the operator's own key, which is a purchase, not a
# measurement -- so a rebuild that only needs to show the served bytes and the reclassified catalogue
# runs this way, and the lane legs are reported as skipped instead of quietly missing.
SKIP_DETECT = (os.environ.get("SKIP_DETECT") or "").strip().lower() in ("1", "true", "yes")
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)
    return bool(ok)


def call(url: str, payload: dict | None = None, opener=None, timeout: float = 240.0):
    """Return (status, parsed_json_or_text). Never raises -- a failed deploy reports, it does not trace.

    A `/detect` caller must override this default: the backend will wait `lane timeout_ms + 30 s`
    (270 s on the shipped primary), and a client that gives up first reports a working deploy as a
    failure. See the detect calls below.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    target = opener or urllib.request.build_opener()
    try:
        with target.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _jsonish(raw)
    except Exception as exc:  # noqa: BLE001
        return 0, {"transport_error": f"{type(exc).__name__}: {exc}"}
    return 200, _jsonish(raw)


def _jsonish(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw[:200]


def admin_opener():
    """A cookie-jar opener logged in as the deploy's admin, from .env -- credentials never printed."""
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    st, body = call(API + "/auth/login", {
        "username": env.get("ADMIN_USERNAME", "admin"),
        # seed_admin() falls back to DEPLOY.md's documented default when the .env line is
        # blank, which is the shipped state -- so the probe mirrors that fallback rather than
        # sending an empty password and reporting BAD_CREDENTIALS as if it were a deploy bug.
        "password": env.get("ADMIN_PASSWORD") or "changeme-admin"}, opener=op)
    return op, st, body, env


def corpus_pair():
    """One real human and one real AI text from the labeled benchmark corpus."""
    rows = json.loads((ROOT / "benchmark" / "corpus.json").read_text(encoding="utf-8"))
    human = next((r for r in rows if str(r.get("label", "")).lower().startswith("h")
                  and len(r.get("text", "")) > 300), None)
    ai = next((r for r in rows if str(r.get("label", "")).lower().startswith("ai")
               and len(r.get("text", "")) > 300), None)
    return human, ai


def verdict_of(res) -> str | None:
    """The answer field of a detection. It is `classification`, not `verdict`.

    The API's own vocabulary is AI_GENERATED / HUMAN_LIKELY / REVIEW_REQUIRED in `classification`
    (what the DB column is called, and what verify_admin_settings.py already asserts). A probe that
    reads `verdict` sees None on a detection that answered in 1.6 s, which is how this tool reported
    two FAILs against a stack that was working.
    """
    if not isinstance(res, dict):
        return None
    return res.get("classification") or res.get("verdict")


def main() -> int:
    print(f"target {BASE}  (API at {API})  lane={LANE or 'shipped default'}\n")

    # 1-2: the anonymous surface. /healthz must answer; the operator status must not.
    st, body = call(API + "/healthz")
    check(st == 200 and body == {"ok": True}, "nginx proxies a liveness route with no session",
          f"{st} {body}")
    st, body = call(API + "/gateway-health")
    check(st == 401, "…and the detailed status stays behind a login",
          f"{st} {body if isinstance(body, dict) else str(body)[:60]}")

    # 2b: which deployment this is, as the SPA itself learns it. One built bundle serves both apps and
    # asks /auth/me before it renders: "accounts" shows the sign-in gate, "local" opens straight into
    # Detect. So the container's login page is a property of this answer, and the packaged exe's
    # absence of one is proved by its own tool (tools/verify_exe_bundle.py). Read anonymously, because
    # a host that reported "local" here would be handing an admin session to whoever reaches the port.
    st, me = call(API + "/auth/me")
    me = me if isinstance(me, dict) else {}
    check(st == 200 and me.get("auth_mode") == "accounts" and me.get("user") is None,
          "…and the hosted build reports the account mode, with nobody signed in",
          f"{st} auth_mode={me.get('auth_mode')!r} user={me.get('user')!r}")

    # 3: the SPA itself is served and is the current build.
    # call() truncates non-JSON bodies, and the <script> tag can sit past the cut, so the SPA
    # bundle name is read from the raw bytes instead.
    try:
        with urllib.request.urlopen(BASE + "/", timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        text = ""
        print(f"  (SPA fetch failed: {type(exc).__name__}: {exc})")
    bundle = re.findall(r"(main-[A-Z0-9]{8}\.js)", text)
    dist = ROOT / "frontend" / "dist" / "frontend" / "browser"
    want = sorted(f.name for f in dist.glob("main-*.js"))[:1] if dist.is_dir() else []
    check(bool(bundle) and (not want or bundle[0] == want[0]),
          "nginx serves the SPA, and the bundle the repo built",
          f"served={bundle[:1]} dist={want}")

    # 3b-3c: the citation the page links must be served by this deploy. An unknown path here does NOT
    # 404 -- the SPA fallback answers 200 with index.html -- so the only honest test is byte equality
    # with the copy the repo holds, not a status code.
    def fetch_raw(path: str) -> tuple[bytes, str]:
        try:
            with urllib.request.urlopen(BASE + path, timeout=30) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001
            return b"", f"{type(exc).__name__}: {exc}"

    served_html, html_ctype = fetch_raw("/research/report.html")
    repo_html = (ROOT / "research-zenodo" / "report.html")
    same_html = repo_html.is_file() and served_html == repo_html.read_bytes()
    check(same_html, "…and the technical report is served by the site, byte for byte",
          f"{len(served_html)} B ctype={html_ctype.split(';')[0]!r} "
          f"part_ii={b'Part II' in served_html}")
    served_pdf, pdf_ctype = fetch_raw("/research/report.pdf")
    check(served_pdf.startswith(b"%PDF") and "pdf" in pdf_ctype.lower(),
          "…and the printable copy downloads as a PDF, not as HTML",
          f"{len(served_pdf)} B ctype={pdf_ctype.split(';')[0]!r}")

    # 3d: the self-hosted serif faces. Their absence is quieter than the report's -- the page still
    # renders, just in whatever serif the visitor's OS has, and nothing anywhere errors -- and nginx's
    # SPA fallback answers /fonts/anything with index.html and a 200. So: the woff2 magic, then byte
    # equality with the staged file. The Content-Type is printed, not gated: nginx's own mime table
    # decides it, and a browser takes a font it can parse.
    face = "Literata-var.woff2"
    served_face, face_ctype = fetch_raw(f"/fonts/{face}")
    dist_face = dist / "fonts" / face
    check(served_face[:4] == b"wOF2" and dist_face.is_file()
          and served_face == dist_face.read_bytes(),
          "…and the self-hosted serif face is served, not the SPA fallback",
          f"{len(served_face)} B ctype={face_ctype.split(';')[0]!r} "
          f"identical_to_dist={dist_face.is_file() and served_face == dist_face.read_bytes()}")

    # 3d-2: the favicon, which is the newest static asset and the one nobody looks at in a diff.
    # Same failure shape as the face: a stale image keeps serving the previous mark and nothing
    # anywhere complains. Byte equality against dist proves the deploy is current; byte equality
    # against tools/make_icon.py's geometry proves the deploy carries the mark the repo actually
    # specifies, which is the one that could drift (an .ico hand-edited once, a .svg never rebuilt).
    for icon in ("favicon.svg", "favicon.ico"):
        served_icon, _ = fetch_raw(f"/{icon}")
        dist_icon = dist / icon
        sys.path.insert(0, str(ROOT / "tools"))
        import make_icon
        want_icon = (make_icon.svg_text(make_icon.GEOMETRY).encode() if icon.endswith(".svg")
                     else make_icon.ico_bytes(make_icon.GEOMETRY))
        check(served_icon == want_icon and dist_icon.is_file()
              and served_icon == dist_icon.read_bytes(),
              f"…and the {icon} it serves is the mark the geometry says",
              f"{len(served_icon)} B "
              f"identical_to_dist={dist_icon.is_file() and served_icon == dist_icon.read_bytes()} "
              f"identical_to_generator={served_icon == want_icon}")

    # 3e: the stylesheet, which is where a design pass actually lands. The application builder folds
    # every component's `styles:` into the global styles-*.css, so a chamfer/colour/radius change moves
    # ZERO bytes in main-*.js and check 3 would have called a stale deploy current. Same reasoning as
    # the exe bundle check's needle-in-both-halves fix: prove the sheet the container serves is the one
    # dist built, then prove a parser reads it as written -- nginx's SPA fallback answers an unknown
    # path with index.html + 200, and a stray top-level `#` line silently drops the `:root` block that
    # follows it, so neither a status code nor a grep of the file is evidence.
    sheet = re.findall(r"(styles-[A-Z0-9]{8}\.css)", text)
    want_sheet = sorted(f.name for f in dist.glob("styles-*.css"))[:1] if dist.is_dir() else []
    served_css, css_ctype = fetch_raw("/" + sheet[0]) if sheet else (b"", "")
    dist_css = dist / want_sheet[0] if want_sheet else None
    same_css = bool(dist_css and dist_css.is_file() and served_css == dist_css.read_bytes())
    check(bool(sheet) and same_css, "…and the stylesheet it serves is the one dist built",
          f"served={sheet[:1]} dist={want_sheet} {len(served_css)} B "
          f"ctype={css_ctype.split(';')[0]!r} identical_to_dist={same_css}")
    if served_css:
        breaks = vis.style_guards(served_css.decode("utf-8", "replace"), tokens=True)
        check(not breaks, "…and that stylesheet is read as it was written",
              f"{len(served_css):,} B: " + ("; ".join(breaks) if breaks else
                                            ":root parsed, absolute black + white, "
                                            "chamfer applied, no radius, no glass"))

    # 4-6: an authenticated operator view of the engine.
    op, st, who, _env = admin_opener()
    # /auth/login wraps the account: {"user": {"username", "role", ...}}. Reading `role` off the
    # body itself made this check fail on a deploy where the sign-in had plainly succeeded.
    user = who.get("user") if isinstance(who, dict) else None
    check(st == 200 and isinstance(user, dict) and user.get("role") == "admin",
          "the deployed admin can sign in", f"{st} {str(user)[:90]}")

    st, gh = call(API + "/gateway-health", opener=op)
    gh = gh if isinstance(gh, dict) else {}
    check(st == 200, "signed-in gateway status answers", f"{st}")
    defaults = gh.get("defaults") or {}
    check(defaults.get("primary_model") == "z-ai/glm-5.3-flash",
          "the containerised backend seeded the promoted primary",
          f"defaults={defaults}")
    check(gh.get("purge_enabled") is True,
          "the purger attached to n8n's own SQLite (no root-owned file, no crash loop)",
          f"purge_enabled={gh.get('purge_enabled')} error={str(gh.get('purge_error'))[:80]}")

    st, ns = call(API + "/n8n/status", opener=op)
    ns = ns if isinstance(ns, dict) else {}
    check(bool(ns.get("webhook_registered")),
          "the detection workflow is published and its webhook is registered",
          f"{st} webhook_registered={ns.get('webhook_registered')} url={str(ns.get('url'))[:60]}")

    # 6c-6e: the rule a deployed install learns once, applied to the rows the previous build wrote.
    # These two checks are why this pair lives here and not in a unit test: the hosted database is the
    # operator's real one, 96 rows of it stamped with the old vocabulary by the import that shipped
    # before the timeout/refusal split existed. `timeout` is a lane that ran out of time, which the
    # current rule calls `slow` -- and a legacy word is invisible to `/my/pool/options`, whose picker
    # reads only `models` rows. So an install that was told the new rule once must show (a) no old
    # words left in the census and (b) at least one asleep model listed, pickable and unmeasured.
    # Neither costs a provider call, and neither is provable from the source tree.
    sys.path.insert(0, str(ROOT))  # `python tools/x.py` puts tools/ on the path, not the repo root
    from backend import discovery as disc  # the shipped map, not a copy of it
    st, cat = call(API + "/benchmark/catalog", opener=op)
    cat = cat if isinstance(cat, dict) else {}
    hist: dict[str, int] = {}
    for by_status in cat.values():
        if isinstance(by_status, dict):
            for status, models in by_status.items():
                hist[status] = hist.get(status, 0) + len(models or [])
    stale = sorted(set(hist) & set(disc.LEGACY_PROBE_STATUS))
    check(st == 200 and bool(hist) and not stale,
          "…and the catalogue census holds no word from the old vocabulary",
          f"{st} " + " ".join(f"{k}={v}" for k, v in sorted(hist.items()))
          + (f" LEGACY_LEFT={stale}" if stale else ""))

    st, opts = call(API + "/my/pool/options", opener=op)
    opts = opts if isinstance(opts, dict) else {}
    entries = opts.get("options") or []
    check(st == 200 and bool(entries),
          "…and the picker lists what the deploy actually lets a user add",
          f"{st} entries={len(entries)} window_s={opts.get('window_s')} "
          f"fast_ms={opts.get('fast_ms')}" if entries else
          f"{st} — no JSON answer either way: {str(opts)[:120]}")

    asleep = [e for e in entries
              if e.get("measured") is False and e.get("probe_status") in ("slow", "busy")]
    check(bool(asleep) and all(e.get("timeout_ms") == disc.DISCOVERED_TIMEOUT_MS for e in asleep)
          and all(e.get("probe_status") not in disc.LEGACY_PROBE_STATUS for e in asleep),
          "…and a model that only ever timed out is pickable, unmeasured, at the deployment's own ceiling",
          f"{len(asleep)} asleep rows, e.g. "
          + ", ".join(f"{e['provider']}/{e['model']}@{e['timeout_ms']}ms" for e in asleep[:3]))

    # 7-9: two real detections through the whole path.
    human, ai = corpus_pair()
    if not (human and ai):
        check(False, "corpus pair available", "benchmark/corpus.json has no labeled human+AI row")
    if LANE:
        print(f"  (lane override: scoring on {LANE}"
              + (f" at {LANE_BASE_URL}" if LANE_BASE_URL else "")
              + "; the shipped default is not being exercised)")
    verdicts = {}
    if SKIP_DETECT:
        # Every leg below pays for itself: two texts on the deployed lane, on the operator's key.
        # A deploy can be proven current without that (bundle + sheet + report + census + picker are
        # all free), so the default for a rebuild is to run those and stop here until the operator
        # says the lane may be asked.
        print("  (detect legs skipped: SKIP_DETECT=1 -- nothing was asked of a provider, so the "
              "nginx -> gunicorn -> n8n -> lane path is NOT proven by this run)")
    else:
        for name, row in (("human", human), ("AI", ai)):
            if not row:
                continue
            t0 = time.time()
            body = {"text": row["text"]}
            if LANE:
                body["lane_config"] = ({"primary": {"model": LANE, "base_url": LANE_BASE_URL}}
                                       if LANE_BASE_URL else {"primary": {"model": LANE}})
            # 360 s: above the whole server-side chain (270 s backend wait, 285 s gunicorn, 330 s nginx),
            # so a slow answer is reported as the server's verdict, never as this client giving up.
            st, res = call(API + "/detect", body, opener=op, timeout=360)
            res = res if isinstance(res, dict) else {}
            verdict = verdict_of(res)
            ok = st == 200 and bool(verdict)
            verdicts[name] = res if ok else {}
            cfg = (res.get("config_used") or {}) if ok else {}
            legs = (res.get("evidence") or {}).get("models") or [] if ok else []
            phrases = sum(len((((l.get("result") or {}).get("phrases")) or [])) for l in legs)
            check(bool(ok), f"a labeled {name} text is scored through nginx ({row['id']})",
                  f"{st} {time.time() - t0:.1f}s verdict={verdict} "
                  f"p={res.get('ai_probability') if ok else None} "
                  f"key_source={(cfg.get('primary') or {}).get('key_source')} "
                  f"lane={(cfg.get('primary') or {}).get('model')} "
                  f"secondary={ (cfg.get('secondary') or {}).get('enabled') } "
                  f"evidence_legs={len(legs)} phrases={phrases}"
                  # A 502 from this stack is a specific thing, and the backend already names it
                  # (ENGINE_TIMEOUT vs ENGINE_UNREACHABLE vs a pool error). Without it the line reads
                  # "no verdict", which hides whether the deploy is wired and the lane is merely slow.
                  + ("" if ok else f" error={res.get('error_code')} {str(res.get('error'))[:110]}"))

    if verdicts.get("human") and verdicts.get("AI"):
        # The shipped headline feature is phrase-level evidence: the page paints quotes green/red by
        # strength, so a deployment that returns a number but no quotes is not serving the product.
        ai_phrases = sum(len((((l.get("result") or {}).get("phrases")) or []))
                         for l in ((verdicts["AI"].get("evidence") or {}).get("models") or []))
        check(ai_phrases > 0, "the scored AI text carries phrase evidence to highlight",
              f"quotes={ai_phrases}")
        if LANE:
            print(f"  (class-separation not asserted: {LANE} is a calibrated row, not the shipped "
                  f"operating point -- human={verdict_of(verdicts['human'])} "
                  f"ai={verdict_of(verdicts['AI'])})")
        else:
            check(verdict_of(verdicts["human"]) != "AI_GENERATED"
                  and verdict_of(verdicts["AI"]) != "HUMAN_LIKELY",
                  "the deployed lane separates the two classes it was calibrated on",
                  f"human={verdict_of(verdicts['human'])} ai={verdict_of(verdicts['AI'])}")

    print(f"\n{sum(1 for ok, _ in RESULTS if ok)}/{len(RESULTS)} checks passed")
    failed = [label for ok, label in RESULTS if not ok]
    if failed:
        print("failed: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
