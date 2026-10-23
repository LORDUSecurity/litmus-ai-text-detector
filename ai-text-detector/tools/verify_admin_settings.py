#!/usr/bin/env python3
"""Live proof that an admin can point the app at a detection engine it can actually reach.

Unit tests show what the code says; this shows what a running server answers, on a throwaway
copy of the real dev database, with a real n8n on 127.0.0.1:5678. It answers the question the
packaged .exe leaves open (#116): the field that breaks detection is the same field that fixes
it, so it has to be readable, writable, validated, and self-describing when it is wrong.

    python tools/verify_admin_settings.py

Nothing here touches backend/data/detector.sqlite3: the file is copied to a temp path first, and
the admin password of the *copy* is replaced with a random one that is never printed. The one
network cost is a single detection through the saved target; skip it with --no-detect.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_DB = ROOT / "backend" / "data" / "detector.sqlite3"
SECRET = ROOT / "backend" / "data" / "backend-secret.key"
PORT = int(os.environ.get("VERIFY_PORT", "8791"))
BASE = f"http://127.0.0.1:{PORT}"
GOOD_ENGINE = "http://localhost:5678"
DEAD_ENGINE = "http://127.0.0.1:1"
WEBHOOK = "/webhook/ai-detect-ai"
# GOOD_ENGINE is spelled `localhost`, not `127.0.0.1`, because that is the value the packaged app
# actually ships. On this host it also matters: `netsh int ipv4 show dynamicport tcp` reports the
# ephemeral range starting at 1024, so 5678 is a port Windows will hand out to an outbound socket,
# and a client whose local port equals the server's port gets WSAECONNREFUSED (10061) on loopback —
# a failed connect leaves that port free, so Windows picks it again and the refusal sticks across
# retries. A real install can hit the same thing under pool load, which is exactly the case the
# ENGINE_UNREACHABLE label below exists to make visible.

# The guides this checks writes are the ones the Ranking page shows every user, so they are real
# sign-up links, not filler.
GUIDES = [
    {"provider": "nvidia", "url": "https://build.nvidia.com/", "steps": "sign up -> Get API key -> paste below"},
    {"provider": "openrouter", "url": "https://openrouter.ai/settings/keys", "steps": "sign up with a free account -> Keys -> Create"},
]

# The workflow's own vocabulary (n8n/ai-only-workflow.json, "Assemble Verdict"). Anything else is
# either a transport failure or an error the backend produced before the engine ran.
VERDICTS = ("AI_GENERATED", "HUMAN_LIKELY", "REVIEW_REQUIRED")

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{': ' + detail if detail else ''}"))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)


def phrase_summary(body: dict) -> tuple[int, int, str]:
    """(phrase count, lane count, first phrase flattened) out of a /detect body.

    The workflow may emit each phrase as an object ({"direction", "quote", "strength"}) or as a
    bare string, depending on how the lane answered, and a quote is verbatim from the input so it
    carries that text's own newlines. Printing the raw repr buries the span inside a dict dump and
    the newline makes the run look like it stopped — so name the direction and strength, then
    flatten. Zero phrases on a *decided* verdict is the signal the band re-validation tracks.
    """
    lanes = (body.get("evidence") or {}).get("models") or []
    out: list[str] = []
    for lane in lanes:
        for p in (lane.get("result") or {}).get("phrases") or []:
            if isinstance(p, dict):
                tag = str(p.get("direction") or "?")
                if p.get("strength") not in (None, ""):
                    tag += f" {p['strength']}"
                out.append(f"[{tag}] {p.get('quote') or p.get('text') or ''}")
            else:
                out.append(str(p))
    return len(out), len(lanes), (re.sub(r"\s+", " ", out[0]).strip()[:110] if out else "")


class Api:
    """Cookie-jar HTTP so the Flask session cookie survives login, exactly as the SPA does."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers: dict = {}

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=280) as resp:
                raw = resp.read()
                status = resp.status
                self.headers = dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            self.headers = dict(exc.headers)
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, {"_raw": raw[:300].decode("utf-8", "replace")}


def wait_for_server(proc: subprocess.Popen, timeout: float = 45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> int:
    do_detect = "--no-detect" not in sys.argv
    if not REAL_DB.is_file():
        print(f"no dev database at {REAL_DB} — nothing to copy")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="aitd-admin-verify-"))
    db_copy = tmp / "verify.sqlite3"
    shutil.copyfile(REAL_DB, db_copy)

    # Replace the admin credential on the COPY only, so this script never needs to know the real
    # password and can still exercise the admin-gated routes.
    from backend import db as _db  # after sys.path is set up

    admin_pw = "verify-" + os.urandom(8).hex()
    c = sqlite3.connect(db_copy)
    c.row_factory = sqlite3.Row
    col = next((r["name"] for r in c.execute("PRAGMA table_info(users)")
                if "password" in r["name"] or "hash" in r["name"]), None)
    admins = c.execute("SELECT id, username FROM users WHERE role = 'admin' ORDER BY id").fetchall()
    if not col or not admins:
        print("copy has no admin row or no password column — cannot verify")
        return 2
    c.execute(f"UPDATE users SET {col} = ? WHERE id = ?", (_db.hash_password(admin_pw), admins[0]["id"]))
    c.commit()
    stored_before = dict(c.execute("SELECT key, value FROM settings").fetchall())
    admin_id, admin_name = int(admins[0]["id"]), admins[0]["username"]
    c.close()
    print(f"copy of {REAL_DB.name}: admin '{admin_name}', "
          f"{len(stored_before)} settings rows, password replaced on the copy only")

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "DETECTOR_SECRET": str(SECRET),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                # Stated, not inherited. `serve_local` now defaults to single-owner local mode (no
                # sign-in page), in which every request is answered as the owner: the registration
                # below would be refused 409 and the two 403 gate checks would see 200 -- the same
                # server quietly answering a different question, which is how a stand-in model once
                # made ten "is nemotron reachable" rounds all mean glm. This verifier is about the
                # admin/plain-user split, so it asks for the accounts mode by name.
                "LOCAL_SINGLE_USER": "0"})
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "backend.serve_local"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    keep_dir = False
    try:
        if not wait_for_server(proc):
            log.flush()
            print("server did not come up — last lines of its own output:")
            print((tmp / "server.log").read_text(encoding="utf-8", errors="replace")[-2500:])
            print("kept for inspection:", tmp)
            proc.kill()
            log.close()
            keep_dir = True
            return 2

        anon = Api()
        st, cfg = anon.call("GET", "/api/config")
        print(f"note   anonymous GET /config -> {st} (the guard decides whether the site is "
              f"sign-in-first; the Ranking page is a logged-in view)")

        # --- the gate itself -----------------------------------------------------------------
        stranger = Api()
        # Sign-up is addressed by e-mail now, so the stranger the gate is tested against is
        # created the way the page creates one.
        who = f"verify{int(time.time()) % 100000}"
        st, _ = stranger.call("POST", "/api/auth/register",
                              {"name": f"Verifier {who}", "email": f"{who}@invalid.example",
                               "password": "verifypass1", "confirm": "verifypass1"})
        st403, body403 = stranger.call("GET", "/api/settings")
        check(st403 == 403, "a plain user cannot read /settings",
              f"{st403} {str(body403)[:60]}")
        st403b, _ = stranger.call("POST", "/api/settings", {"n8n_url": GOOD_ENGINE})
        check(st403b == 403, "a plain user cannot write /settings", f"{st403b}")

        admin = Api()
        st, _ = admin.call("POST", "/api/auth/login", {"username": admin_name, "password": admin_pw})
        check(st == 200, "admin session established on the copy", f"{st}")

        st, before = admin.call("GET", "/api/settings")
        check(st == 200 and "n8n_url" in before, "GET /settings returns the engine fields",
              f"n8n_url={before.get('n8n_url')!r} webhook_path={before.get('webhook_path')!r}")

        # --- write-time validation, live ------------------------------------------------------
        for bad in ("localhost:5678", "http://", "not a url"):
            st, body = admin.call("POST", "/api/settings", {"n8n_url": bad})
            if st != 400 or body.get("field") != "n8n_url":
                check(False, f"POST /settings refuses {bad!r}", f"{st} {str(body)[:80]}")
                break
        else:
            check(True, "POST /settings refuses a target the forwarder cannot use",
                  "400 + field=n8n_url for 3 forms")

        st, body = admin.call("POST", "/api/settings", {"webhook_path": "webhook/ai-detect-ai"})
        check(st == 400 and body.get("field") == "webhook_path",
              "a path without a leading slash is refused", f"{st} {str(body)[:60]}")

        st, _ = admin.call("GET", "/api/settings")
        after_reject = dict(admin.call("GET", "/api/settings")[1])
        check(after_reject.get("n8n_url") == before.get("n8n_url"),
              "a rejected write left the stored engine URL alone",
              f"{after_reject.get('n8n_url')!r}")

        # --- the write that should work -------------------------------------------------------
        st, written = admin.call("POST", "/api/settings",
                                 {"n8n_url": GOOD_ENGINE, "webhook_path": WEBHOOK,
                                  "api_key_guides": GUIDES})
        check(st == 200 and written.get("ok") is True, "engine target + guides saved", f"{st}")

        st, rt = admin.call("GET", "/api/settings")
        check(isinstance(rt.get("api_key_guides"), list) and len(rt.get("api_key_guides", [])) == len(GUIDES),
              "GET /settings hands back the same shape the form sent",
              f"{type(rt.get('api_key_guides'))} n={len(rt.get('api_key_guides') or [])}")
        check(rt.get("n8n_url") == GOOD_ENGINE, "the saved engine URL round-trips", str(rt.get("n8n_url")))

        st, cfg2 = stranger.call("GET", "/api/config")
        check(st == 200 and isinstance(cfg2.get("api_key_guides"), list)
              and any(g.get("provider") == "openrouter" for g in (cfg2.get("api_key_guides") or [])),
              "the new guides reach a signed-in plain user's Ranking page",
              f"{st} guides={len(cfg2.get('api_key_guides') or [])}")

        # --- does the status endpoint follow the setting? --------------------------------------
        st, s1 = admin.call("GET", "/api/n8n/status")
        check(st == 200 and s1.get("n8n_url") == GOOD_ENGINE,
              "/n8n/status reports the stored target, not a build-time constant", str(s1.get("n8n_url")))
        check(s1.get("reachable") is True, f"that target answers /healthz",
              f"healthz={s1.get('healthz')} published={s1.get('published')} "
              f"webhook={s1.get('webhook_registered')}")

        # --- the failure an operator can now read ----------------------------------------------
        st, _ = admin.call("POST", "/api/settings", {"n8n_url": DEAD_ENGINE})
        st2, err = admin.call("POST", "/api/detect",
                             {"text": "This sentence exists only to be sent to the configured engine."})
        check(st2 == 502 and err.get("error_code") == "ENGINE_UNREACHABLE",
              "an unreachable engine is labelled, not scored",
              f"{st2} error_code={err.get('error_code')} classification={err.get('classification')} "
              f"hdr={ {k: admin.headers.get(k) for k in ('Content-Type', 'Location', 'Server')} } "
              f"body={str(err)[:160]}")
        check(DEAD_ENGINE in str(err.get("engine_url")) + str(err.get("error")),
              "the response names the URL to fix", str(err.get("engine_url")) + str(err.get("error"))[:80])

        st, _ = admin.call("POST", "/api/settings", {"n8n_url": GOOD_ENGINE})
        st2, s3 = admin.call("GET", "/api/n8n/status")
        print(f"       after restore: /n8n/status {st2} reachable={s3.get('reachable')} "
              f"healthz={s3.get('healthz')} err={str(s3.get('healthz_error') or '')[:70]}")

        if do_detect:
            # The decisive leg: a verdict produced through the target that was just saved. n8n on
            # this host is known to stall its listener under pool load (a slow-pool webhook ties
            # up a worker), and a stalled listener answers a new connect with WinError 10061 —
            # indistinguishable, from here, from a missing engine. So retry the same request a few
            # times before calling the target bad; the first attempt's body is kept for the report.
            last: tuple[int, dict] = (0, {})
            for attempt in range(1, 4):
                st, d = admin.call("POST", "/api/detect", {"text": (
                    "The switch converged on the ingress ring and the protection path stayed clear "
                    "for the whole window, which is what the design intended when the ring was built."
                )})
                last = (st, d)
                if d.get("classification") in VERDICTS:
                    break
                print(f"       attempt {attempt}: {st} {d.get('error_code') or d.get('classification')} "
                      f"detail={str(d.get('detail') or d.get('error') or '')[:110]} — retrying in 20s")
                time.sleep(20)
            st, d = last
            cs = d.get("component_scores") or {}
            check(st == 200 and d.get("classification") in VERDICTS,
                  "a real detection runs through the saved engine target",
                  f"{st} {d.get('classification')} p={d.get('ai_probability')} "
                  f"conf={d.get('confidence')} lane={cs.get('deciding_lane')} method={d.get('method')} "
                  f"{str(d.get('error') or d.get('error_code') or '')[:80]}")
            nph, nlane, first = phrase_summary(d)
            print(f"       evidence phrases: {nph} across {nlane} lane(s)"
                  + (f", first={first!r}" if nph else ""))
        else:
            print("SKIP  live detection (--no-detect)")

        # Restore whatever the install had, on the copy only, and prove the read is stable.
        final = admin.call("GET", "/api/settings")[1]
        print(f"\ncopy's final engine target = {final.get('n8n_url')!r}, "
              f"guides = {len(final.get('api_key_guides') or [])}, "
              f"real DB untouched ({REAL_DB})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if not keep_dir:
            shutil.rmtree(tmp, ignore_errors=True)

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for f in failed:
            print("  FAILED:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
