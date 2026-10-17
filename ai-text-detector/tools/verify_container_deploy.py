#!/usr/bin/env python3
"""Prove the containerized stack actually serves a detection -- the checks #106 owed.

Everything before this run was static: `docker compose config` parses YAML, and the no-daemon
preflight reads Dockerfiles. Neither can see that a HEALTHCHECK aimed at an authenticated route
leaves the backend unhealthy forever, that the purger used to create n8n's SQLite as root and
crash-loop the engine, or that the shipped `n8n-import.sh` args get rewritten by Git Bash. Those
three only surface against a live daemon, so this drives the running stack and reports what it
actually got.

    python tools/verify_container_deploy.py [base_url]

Defaults to http://localhost:8080 (the published nginx origin) and talks to the API through it at
/api/*, so every answer below travelled nginx -> gunicorn -> n8n -> the provider lane. The texts come
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
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
API = BASE + "/api"
# Name a lane to score the texts on it instead of the shipped default, e.g.
#   DETECT_MODEL=google/diffusiongemma-26b-a4b-it python tools/verify_container_deploy.py
# This exists because the deploy is not the only thing that can make a detection fail: on
# 2026-10-17 the shared NVIDIA pool could not answer a 320-char text on the shipped primary inside
# its own 180 s ceiling (raised to 240 s on 2026-10-17), twice, while the same containers answered
# another calibrated row in 1.8 s.
# Overriding the lane proves nginx -> gunicorn -> n8n -> provider -> parse -> verdict -> phrases ->
# history end to end, and the class-separation check is then skipped rather than quietly re-labelled,
# because that lane's operating point is not the one the stack ships.
LANE = (os.environ.get("DETECT_MODEL") or "").strip() or None
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

    # 7-9: two real detections through the whole path.
    human, ai = corpus_pair()
    if not (human and ai):
        check(False, "corpus pair available", "benchmark/corpus.json has no labeled human+AI row")
    if LANE:
        print(f"  (lane override: scoring on {LANE}; the shipped default is not being exercised)")
    verdicts = {}
    for name, row in (("human", human), ("AI", ai)):
        if not row:
            continue
        t0 = time.time()
        body = {"text": row["text"]}
        if LANE:
            body["lane_config"] = {"primary": {"model": LANE}}
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
