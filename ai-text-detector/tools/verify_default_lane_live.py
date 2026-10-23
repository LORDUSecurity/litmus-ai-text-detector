#!/usr/bin/env python3
"""Live proof that the promoted default lane really serves a detection.

#119 replaced a primary that 404s on every completion. The seed tests prove what a fresh
install writes; this proves what a running app on THIS host does with it -- boot re-seeds a
copy of the real dev DB from the repo `.env`, the old default loses its lane, and one
document is judged end-to-end by the promoted model on its own measured cut. Real providers,
one request's worth of budget.

It also re-proves the phrase evidence leg on a model that can answer: nemotron's captures
were empty because the model never returned, so quotes have never been demonstrated on the
deployed default until now.

    python tools/verify_default_lane_live.py            # needs n8n up on :5678
    VERIFY_DEFAULT_PORT=8796 python tools/verify_default_lane_live.py

Runs against a throwaway copy of the dev DB; the real one is not written. DETECTOR_SECRET is
inherited, not invented, so the copy's stored encrypted keys still decrypt.
"""
from __future__ import annotations

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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REAL_DB = ROOT / "backend" / "data" / "detector.sqlite3"
PORT = int(os.environ.get("VERIFY_DEFAULT_PORT", "8795"))
BASE = f"http://127.0.0.1:{PORT}"
DETECT_TIMEOUT = float(os.environ.get("VERIFY_DETECT_TIMEOUT", "220"))
VOCAB = ("AI_GENERATED", "HUMAN_LIKELY", "REVIEW_REQUIRED")

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))


class Api:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.op.open(req, timeout=DETECT_TIMEOUT) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:                                  # noqa: BLE001
                return e.code, {}


def serving() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), 0.4):
            return True
    except OSError:
        return False


def main() -> int:
    if not REAL_DB.is_file():
        print(f"no dev DB at {REAL_DB}")
        return 2
    if serving():
        print(f"port {PORT} is already serving -- not this run's backend. "
              f"Use VERIFY_DEFAULT_PORT=<free port>.")
        return 2

    from backend import db as _db, seed as _seed

    want_primary = _seed.DEFAULT_LANES["primary"][0]
    want_secondary = _seed.DEFAULT_LANES["secondary"][0]
    band = _seed.LANE_BANDS.get(want_primary)
    if not band:
        print(f"{want_primary} is the default primary but has no measured band overlay")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="aitd-default-lane-"))
    db_copy = tmp / "run.sqlite3"
    shutil.copyfile(REAL_DB, db_copy)

    corpus = json.loads((ROOT / "benchmark" / "corpus.json").read_text(encoding="utf-8"))
    texts = corpus if isinstance(corpus, list) else corpus.get("texts") or list(corpus)
    sample = next((t for t in texts if isinstance(t, dict) and (t.get("text") or t.get("content"))), None)
    text = (sample.get("text") or sample.get("content"))[:1200] if sample else None
    if not text:
        print("could not pull a real text out of benchmark/corpus.json")
        shutil.rmtree(tmp, ignore_errors=True)
        return 2

    admin_pw = "verify-" + os.urandom(8).hex()
    c = sqlite3.connect(db_copy)
    c.row_factory = sqlite3.Row
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
    col = next((k for k in ("password_hash", "password") if k in cols), None)
    admins = c.execute("SELECT id, username FROM users WHERE role = 'admin' ORDER BY id").fetchall()
    if not col or not admins:
        c.close()
        shutil.rmtree(tmp, ignore_errors=True)
        return 2
    c.execute(f"UPDATE users SET {col} = ? WHERE id = ?", (_db.hash_password(admin_pw), admins[0]["id"]))
    c.commit()
    admin_name = admins[0]["username"]
    c.close()

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "BACKEND_HOST": "127.0.0.1",
                "BACKEND_PORT": str(PORT), "AITD_NO_BROWSER": "1",
                "PYTHONIOENCODING": "utf-8",
                # Asked for by name, not left to `serve_local`'s new default of 1. In single-owner
                # mode every request is answered as the owner whether or not the login below worked,
                # so the "admin session on the copy" check would stop proving anything about the
                # session -- and the admin-only /models + /detect path this tool traces is the hosted
                # one. The packaged mode has its own proof: tools/verify_exe_bundle.py.
                "LOCAL_SINGLE_USER": "0"})
    log = open(tmp / "backend.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "backend.serve_local"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0, up = time.time(), False
        while time.time() - t0 < 90:
            if proc.poll() is not None:
                break
            if serving():
                up = True
                break
            time.sleep(1.0)
        if not up:
            check(False, "backend booted on the copy", f"exited {proc.returncode}; see {tmp/'backend.log'}")
            return 1

        # 1. what the boot actually seeded into the copy (upgrade path, real .env)
        con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        lanes = {r["lane"]: dict(r) for r in con.execute(
            "SELECT lane, model_id, ai_prob_threshold, human_prob_threshold, timeout_ms, role "
            "FROM models WHERE lane IS NOT NULL")}
        check(set(lanes) == {"primary", "secondary"}, "exactly one row per lane after boot",
              json.dumps({k: v["model_id"] for k, v in lanes.items()}))
        prim = lanes.get("primary") or {}
        check(prim.get("model_id") == want_primary, "boot promoted the measured default primary",
              f"{prim.get('model_id')}")
        check(abs((prim.get("ai_prob_threshold") or -1) - band["AI_PROBABILITY_THRESHOLD"]) < 1e-9
              and abs((prim.get("human_prob_threshold") or -1)
                      - band["HUMAN_PROBABILITY_THRESHOLD"]) < 1e-9,
              "…on its own band, not the previous primary's",
              f"{prim.get('ai_prob_threshold')}/{prim.get('human_prob_threshold')} "
              f"measured {band['AI_PROBABILITY_THRESHOLD']}/{band['HUMAN_PROBABILITY_THRESHOLD']}")
        demoted = con.execute("SELECT lane, ai_prob_threshold FROM models "
                             "WHERE model_id LIKE '%nemotron-3-ultra%' AND lane IS NOT NULL").fetchall()
        check(not demoted, "the dead model no longer claims a lane",
              f"{len(demoted)} stale lane row(s)")
        con.close()

        # 2. and the API reports the same thing the DB holds
        api = Api()
        st, _ = api.call("POST", "/api/auth/login",
                         {"username": admin_name, "password": admin_pw})
        check(st == 200, "admin session on the copy", f"{st}")
        st, models = api.call("GET", "/api/models")
        rows = models if isinstance(models, list) else models.get("models") or models.get("rows") or []
        pinn = [m for m in rows if m.get("lane") == "primary"]
        check(st == 200 and len(pinn) == 1 and pinn[0]["model_id"] == want_primary,
              "/api/models serves one primary and it is the promoted one",
              f"{st} {[m.get('model_id') for m in pinn]}")

        # 3. one real document, no lane overrides: the default has to carry it alone
        st, body = api.call("POST", "/api/detect", {"text": text})
        cfg = (body or {}).get("config_used") or {}
        lane = str(json.dumps(cfg, default=str))
        verdict = (body or {}).get("classification") or (body or {}).get("verdict")
        tuning = (body or {}).get("tuning_applied") or (body or {}).get("effective_tuning") or {}
        phrases = ((body or {}).get("evidence") or {}).get("models") or []
        quotes = sum(len(((m.get("result") or {}).get("phrases")) or []) for m in phrases if isinstance(m, dict))
        check(st == 200 and verdict in VOCAB, "the default lane answered a real document",
              f"{st} {verdict} {str(body)[:110]}")
        check(want_primary in lane, "…and it was the promoted model, not a fallback",
              lane[:160])
        check(not tuning or abs(float(tuning.get("ai_prob_threshold", -1))
                                - band["AI_PROBABILITY_THRESHOLD"]) < 1e-9,
              "the cut that judged it is the model's own",
              json.dumps({k: tuning[k] for k in list(tuning)[:4]}, default=str))
        check(quotes > 0, "phrase evidence comes back on the deployed default",
              f"{quotes} quote(s) over {len(phrases)} leg(s)  secondary="
              f"{lanes.get('secondary', {}).get('model_id')}")
        # The operator's chosen shipping state is "one lane, tie-break off", so the proof has
        # to say the lane stayed off -- not just that the DB has a disabled row. `evidence.models`
        # is one entry per leg that actually ran, and config_used.secondary.enabled is the
        # engine's own statement about the lane, so together they are the behavioural check.
        sec_cfg = cfg.get("secondary") or {}
        warns = " ".join(str(w) for w in ((body or {}).get("warnings") or []))
        check(sec_cfg.get("enabled") is False and len(phrases) == 1,
              "…and the tie-break lane stayed off (no second call bought)",
              f"secondary.enabled={sec_cfg.get('enabled')!r} legs={len(phrases)} "
              f"disable_warning={'disabled' in warns.lower()}")
        return 0 if not [1 for ok, _ in results if not ok] else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        log.close()
        for _ in range(8):
            if not serving():
                break
            time.sleep(1.0)
        else:
            check(False, "the run left the port free", f"still held by :{PORT}")
        failed = [label for ok, label in results if not ok]
        print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
        for f in failed:
            print("  FAILED:", f)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
