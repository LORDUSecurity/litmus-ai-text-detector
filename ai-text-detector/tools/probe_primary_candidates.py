#!/usr/bin/env python3
"""Which model can actually take over the default primary lane (#119)?

The shipped default primary is `nvidia/nemotron-3-ultra-550b-a55b`. It is still advertised by
`GET /v1/models` but every call fails (2026-10-16: `no_choices`; earlier: HTTP 404 "Function id
... is not found"), so a fresh install answers with the tie-break lane alone — a weaker detector
that also reports on a different scale. The replacement has to earn two separate things, and this
checks both:

  * it is **servable right now** through the real app path, not just listed in a catalogue, and
  * it is **measured**, with a band fitted on captured data.

Ranking facts are read from the app's own `/benchmark/models` summary (the same numbers the
Ranking page shows) rather than recomputed here, so this script and the panel cannot disagree.

    python tools/probe_primary_candidates.py                    # every NVIDIA/OpenRouter row
    python tools/probe_primary_candidates.py --texts z-ai/glm-5.3,inclusionai/ling-3.1-flash

Runs against a throwaway copy of the dev database (the admin password on the *copy* is replaced
with a random one, never printed), so nothing here writes to backend/data/detector.sqlite3. Each
candidate costs one real detection with the tie-break lane switched off.

The `answers` column is about transport, not quality, and it gets one ceiling to be honest about.
On 2026-10-16 the first version of this probe reported `NO` for `z-ai/glm-5.3` and every other
slow lane, and the reason turned out to be a label rather than a dead model: `forward()` caught
read timeouts and refused connects in one branch, so a candidate that was merely thinking took the
"n8n unreachable" path. With the split in place (ENGINE_TIMEOUT vs ENGINE_UNREACHABLE) the two
still need a ceiling wide enough to tell apart, so the *copy's* `models.timeout_ms` is raised and
this script's own read timeout raised with it. Shipped rows sit at 90 s (a 120 s client ceiling,
`timeout_ms + 30`), which is below the ~269 s glm-5.3 takes on this host — so a `NO` here means
"did not answer at all, or answered with an error", never "too slow for today's setting". How slow
is separately visible: the backend records the real latency, so read History, not this column.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
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
PORT = int(os.environ.get("PROBE_PORT", "8792"))
BASE = f"http://127.0.0.1:{PORT}"

# Written into the throwaway copy only. backend/app.py gives the request
# `timeout_ms / 1000 + 30` seconds, so 300000 is a 330 s ceiling: above glm-5.3's ~269 s and above
# the ~302 s at which the gateway 504s gemma-4-31b-it, so a candidate can no longer hide behind the
# label "did not answer in time". 300000 is also the highest the backend will accept at all — the
# tuning validator refuses anything over it (measured: a 330000 probe run cost 0 provider requests
# because every leg died as LANE_CONFIG_INVALID before the forward). Real deployments keep their own
# 90 s; see the docstring.
PROBE_LANE_TIMEOUT_MS = int(os.environ.get("PROBE_LANE_TIMEOUT_MS", "300000"))
# This script's own read timeout, one leg past the backend ceiling above.
PROBE_CLIENT_S = float(os.environ.get("PROBE_CLIENT_S", "400"))

# Two texts with opposite labels, phrased so neither leans on a length shortcut. The point is not
# to score them — one pass cannot establish accuracy — it is to prove the lane answers with a
# usable verdict rather than a transport error.
AI_TEXT = ("The company reported revenue growth of twelve per cent, attributed the result to two "
           "acquired facilities, and said hiring would continue through the autumn across all "
           "regional offices. Management expects the margin to hold at its current level.")
HUMAN_TEXT = ("I walked the ring road twice before the bus came. The shelter glass was cracked in "
              "a way nobody had bothered to fix, and the timetable under it had last been updated "
              "in a year I can still name.")


class Api:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=PROBE_CLIENT_S) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"_raw": raw[:200].decode("utf-8", "replace")}
        except urllib.error.URLError as exc:
            # Reachable only if this script's own ceiling fires first — a leg still going after
            # the backend gave up. Returned as a body the table already understands, so one hung
            # candidate cannot kill the run mid-print.
            return 0, {"error_code": "PROBE_CLIENT_TIMEOUT",
                       "classification": "DETECTION_UNAVAILABLE", "detail": str(exc)}


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", help="comma-separated model ids to try (default: every measured portable row)")
    args = ap.parse_args()

    if not REAL_DB.is_file():
        print(f"no dev database at {REAL_DB}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="aitd-primary-probe-"))
    db_copy = tmp / "probe.sqlite3"
    shutil.copyfile(REAL_DB, db_copy)

    from backend import db as _db

    pw = "probe-" + os.urandom(8).hex()
    c = sqlite3.connect(db_copy)
    c.row_factory = sqlite3.Row
    col = next((r["name"] for r in c.execute("PRAGMA table_info(users)")
                if "password" in r["name"] or "hash" in r["name"]), None)
    admin = c.execute("SELECT id, username FROM users WHERE role = 'admin' ORDER BY id").fetchone()
    if not col or not admin:
        print("copy has no admin row")
        return 2
    c.execute(f"UPDATE users SET {col} = ? WHERE id = ?", (_db.hash_password(pw), admin["id"]))
    # Widened on the copy only, for the reason in the docstring: at the shipped 90 s the backend
    # stops waiting at 120 s, and that is shorter than some live candidates need to answer. Without
    # this the table cannot tell "the model is dead" from "the model is slow".
    c.execute("UPDATE models SET timeout_ms = ?", (PROBE_LANE_TIMEOUT_MS,))
    c.commit()

    # Portable = a provider whose credentials a fresh install can carry: the NVIDIA and
    # OpenRouter keys seeded from .env. OmniRoute's kiro lanes score best but answer through a
    # local gateway on this machine only, so they cannot be the shipped default.
    portable = c.execute(
        """SELECT m.id, m.model_id, p.name AS provider, m.ai_prob_threshold, m.human_prob_threshold,
                  m.ai_conf_threshold, m.measured, p.base_url
             FROM models m JOIN providers p ON p.id = m.provider_id
            WHERE p.name IN ('nvidia','openrouter') AND m.enabled = 1
            ORDER BY m.measured DESC, p.name, m.model_id"""
    ).fetchall()
    c.close()

    wanted = {s.strip() for s in args.texts.split(",")} if args.texts else None
    rows = [r for r in portable if (wanted is None or r["model_id"] in wanted)]
    measured = [r for r in rows if r["measured"] and r["ai_prob_threshold"] is not None]
    if wanted is None:
        rows = measured
    print(f"{len(rows)} candidate row(s) on providers a fresh install can serve "
          f"({', '.join(sorted({r['provider'] for r in rows}))})")

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "DETECTOR_SECRET": str(SECRET),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                # Not left to `serve_local`, which now defaults this to 1. The admin login below
                # would still answer 200 in single-owner mode -- the guard would have answered as
                # the owner either way -- so a lapse here fails silently instead of loudly. Pinning
                # it keeps this probe on the same code path a hosted deploy takes.
                "LOCAL_SINGLE_USER": "0"})
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "backend.serve_local"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_for_server(proc):
            log.close()
            print("server did not come up:", (tmp / "server.log").read_text(errors="replace")[-1500:])
            proc.kill()
            return 2

        api = Api()
        st, _ = api.call("POST", "/api/auth/login", {"username": admin["username"], "password": pw})
        if st != 200:
            print("admin login failed", st)
            return 2

        # The app's own ranking numbers, so the table below is the panel's table.
        st, models = api.call("GET", "/api/benchmark/models")
        by_model: dict[str, dict] = {}
        if st == 200:
            for row in models if isinstance(models, list) else models.get("models", []):
                key = f"{row.get('provider')}/{row.get('model')}"
                if (row.get("n_scored") or 0) > 0:
                    by_model[key] = row

        print(f"\n{'model':<44} {'answers':<9} {'slowest':<9} {'legs':<34} measured accuracy")
        print(f"(ceiling for this run: lane timeout_ms set to {PROBE_LANE_TIMEOUT_MS} on the DB copy "
              f"= {PROBE_LANE_TIMEOUT_MS / 1000 + 30:.0f} s; this script waits {PROBE_CLIENT_S:.0f} s. "
              f"'NO' = no verdict at all: dead model, refused, gateway error, or slower than that "
              f"ceiling — the code in brackets says which, and 'slowest' is the real wait.)")
        printable = rows if wanted else measured
        for r in printable:
            out: list[str] = []
            leg_seconds: list[float] = []
            reason = ""
            for label, text in (("AI", AI_TEXT), ("HUMAN", HUMAN_TEXT)):
                t0 = time.time()
                st, d = api.call("POST", "/api/detect", {
                    "text": text,
                    "lane_config": {"primary": {"model": r["model_id"]},
                                    "secondary": {"enabled": False}},
                })
                leg_seconds.append(time.time() - t0)
                cls = d.get("classification") or d.get("error_code") or "?"
                out.append(f"{label}={cls.replace('_GENERATED', '').replace('_LIKELY', '')}")
                if d.get("error_code"):
                    out[-1] += f"({d['error_code']})"
                    # A rejection that costs 0 s never reached a provider, so the message is the
                    # whole diagnosis; without it a validation refusal is indistinguishable from a
                    # dead model in the table above.
                    reason = reason or f"{d.get('error')} {d.get('hint') or ''}".strip()
                elif d.get("warnings"):
                    pass
            served = not any("DETECTION_UNAVAILABLE" in x for x in out)
            stat = by_model.get(f"{r['provider']}/{r['model_id']}") or {}
            acc = (f"{stat.get('correct')}/{stat.get('n_scored')} correct, "
                   f"{stat.get('wrong')} wrong, {stat.get('deferred')} deferred "
                   f"on {stat.get('corpus', '?')}"
                   if stat else "no scored run in the DB")
            band = f"{r['ai_prob_threshold']}/{r['human_prob_threshold']}"
            print(f"{r['model_id'][:43]:<44} {'yes' if served else 'NO':<9} "
                  f"{f'{max(leg_seconds):.0f}s' if leg_seconds else '-':<9} "
                  f"{' '.join(out):<34} {acc} band={band} "
                  f"conf={r['ai_conf_threshold']}")
            if reason:
                print(f"{'':<44} reason: {reason[:200]}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
