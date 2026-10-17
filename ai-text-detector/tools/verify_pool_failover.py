#!/usr/bin/env python3
"""Live proof of the two things a user was promised and a unit test cannot show.

    python tools/verify_pool_failover.py            # full run (real provider calls)
    python tools/verify_pool_failover.py --no-discovery   # skip the screening run

1. **Auto-switch.** "If the model stops responding, switch to the second best based on the
   order of the user." Proved against a model that really does refuse -- the NVIDIA primary
   whose own /v1/models advertises it while every chat completion 404s -- and then against the
   health table that is supposed to stop paying for it a fourth time.

2. **Auto-discovery.** "After entering the key and testing it and saving, we bring his models,
   ranked, from all the unused ones." Proved by a real catalogue fetch on the caller's own
   credential and real 2-text screens through the shipped engine path, then by the rows those
   screens leave behind. A candidate that refuses the first text it is given is recorded as a
   refusal with its reason and the walk moves on to the next one, so a run where nothing is awake
   still proves the bookkeeping rather than depending on the provider's mood.

Both run against a throwaway COPY of the dev database, on a random port, with a throwaway user.
Nothing here writes to backend/data/detector.sqlite3, and no key is ever printed: the caller's
credential is copied as ciphertext from the shared row, so it stays encrypted end to end and
still decrypts because the copy inherits DETECTOR_SECRET.

Cost: four detections (three of them buying one fast 404 and one real answer), one key probe,
and -- unless --no-discovery -- at most `discovery.TRIAGE_CAP` (24) candidate calls, of which at
most the admin's `discovery_screen_limit` (4) go on to cost the full 2 texts.
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
PORT = int(os.environ.get("VERIFY_PORT", "8793"))
BASE = f"http://127.0.0.1:{PORT}"

# The endpoint whose free tier is being spent. Both models live on it, so one key pays for the
# whole failover walk.
PROVIDER = "nvidia"
PROVIDER_ID = 1
# moonshotai/kimi-k3 was retired by NVIDIA on 2026-10-15 and the gateway still says so in as many
# words: HTTP 410, "has reached its end of life on 2026-10-15T08:00:00Z and is no longer
# available", back in 0.6 s. A delisting is the one failure that cannot heal between checks, so
# it is the only honest fixture for "the model stopped responding".
#
# This replaced nvidia/nemotron-3-ultra-550b-a55b, which the first run of this verifier used and
# which turned out to be the wrong choice twice over. It answers now (a REVIEW_REQUIRED verdict
# in 25.3 s on 2026-10-17), and when it does fail it fails with 503 -- "busy", which by design
# can never mark a model `down` (backend/pool.py TRANSIENT_MARKERS: a provider's queue is not the
# model's death, or one spent OpenRouter account would demote its whole catalogue). So the
# reorder half of this script could not have passed with it, and asserting that it would have
# been testing the fixture's mood rather than the rule.
DEAD_MODEL = "moonshotai/kimi-k3"
# The measured, calibrated model the shipped default lane runs on. Slow -- it needed over 108 s
# on a congested pool on 2026-10-17, which is what exposed the failover-budget bug.
LIVE_MODEL = "z-ai/glm-5.3-flash"
# The workflow's own vocabulary (n8n/ai-only-workflow.json, "Assemble Verdict").
VERDICTS = ("AI_GENERATED", "HUMAN_LIKELY", "REVIEW_REQUIRED")
TEXT = ("Osaka is a city located in the Kansai region of Japan. It is the capital of Osaka "
        "Prefecture and the third-largest city in Japan, with a population of over 19 million "
        "people in its metropolitan area, and a major economic hub known for its port.")

# This host's console codec is cp1252, and a check label that carries a provider's own error
# text can hold anything. Reconfigure rather than lose the run to a UnicodeEncodeError halfway
# through four paid detections.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((bool(ok), f"{label}{': ' + detail if detail else ''}"))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)


class Api:
    """Cookie-jar HTTP, so the Flask session survives login exactly as the SPA's does."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method: str, path: str, body=None, timeout: float = 290.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, {"error": f"{type(exc).__name__}: {exc}"}
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, {"_raw": raw[:300].decode("utf-8", "replace")}


def wait_for_server(proc: subprocess.Popen, timeout: float = 60.0) -> bool:
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


def pool_models(body: dict) -> list[str]:
    return [p["model"] for p in (body or {}).get("pool", [])]


def main() -> int:
    do_discovery = "--no-discovery" not in sys.argv
    if not REAL_DB.is_file():
        print(f"no dev database at {REAL_DB} — nothing to copy")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="aitd-pool-verify-"))
    db_copy = tmp / "verify.sqlite3"
    shutil.copyfile(REAL_DB, db_copy)

    c = sqlite3.connect(db_copy)
    c.row_factory = sqlite3.Row
    shared = c.execute("SELECT key_cipher, key_hint FROM api_keys WHERE provider_id = ? "
                       "ORDER BY id LIMIT 1", (PROVIDER_ID,)).fetchone()
    c.close()
    if shared is None:
        print("the copy has no shared provider key — cannot give the throwaway user one")
        return 2
    print(f"copy of {REAL_DB.name} at {db_copy}")

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "DETECTOR_SECRET": str(SECRET),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8"})
    log = open(tmp / "server.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([sys.executable, "-m", "backend.serve_local"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    api = Api()
    try:
        if not wait_for_server(proc):
            print("server never listened; log follows")
            log.flush()
            print((tmp / "server.log").read_text(encoding="utf-8", errors="replace")[-3000:])
            return 2

        # --------------------------------------------------------------- boot
        st, body = api.call("GET", "/api/healthz")
        check(st == 200 and body.get("ok") is True, "server answers /healthz", f"http {st}")

        # ------------------------------------------------- a user, and no key yet
        user = f"poolverify_{int(time.time())}"
        pw = "verify-" + os.urandom(8).hex()
        st, body = api.call("POST", "/api/auth/register", {"username": user, "password": pw})
        check(st == 201, "a visitor can register", f"http {st}")
        st, body = api.call("POST", "/api/auth/login", {"username": user, "password": pw})
        check(st == 200 and (body.get("user") or {}).get("role") == "user",
              "the throwaway account is a plain user, not an admin",
              (body.get("user") or {}).get("role", "?"))

        # /gateway-health is behind the session guard (only /healthz and / are public), so the
        # engine check has to come after login -- before it, a perfectly healthy engine reads as
        # a 401 and the run stops for a reason that has nothing to do with the engine.
        st, body = api.call("GET", "/api/gateway-health")
        engine_ok = st == 200 and body.get("ok") is True
        check(engine_ok, "the detection engine is reachable",
              "" if engine_ok else f"http {st} {json.dumps(body)[:200]}")
        if not engine_ok:
            print("everything below needs a live engine; start n8n and re-run")
            return finish(tmp)

        st, body = api.call("POST", "/api/my/discover", {"provider_id": PROVIDER_ID})
        check(st == 409 and body.get("error_code") == "NO_USER_KEY",
              "discovery is refused without the caller's own key",
              f"http {st} {body.get('error_code')}")

        # The caller's credential, copied as ciphertext: it never appears in this process as
        # plaintext, and it decrypts server-side because the copy inherited DETECTOR_SECRET.
        st, body = api.call("GET", "/api/auth/me")
        uid = (body.get("user") or {}).get("id")
        c = sqlite3.connect(db_copy)
        c.execute("INSERT INTO user_api_keys (user_id, provider_id, label, key_cipher, key_hint,"
                  " verified) VALUES (?, ?, 'my key', ?, ?, 0)",
                  (uid, PROVIDER_ID, shared["key_cipher"], shared["key_hint"]))
        c.commit()
        c.close()
        st, keys = api.call("GET", "/api/my/keys")
        key_id = keys[0]["id"] if isinstance(keys, list) and keys else None
        check(key_id is not None and keys[0].get("provider_name") == PROVIDER,
              "the stored key is listed for its owner, hint only",
              f"id={key_id} hint={keys[0].get('key_hint') if keys else '?'}")

        # ------------------------------------------------- the user's own order
        st, body = api.call("GET", "/api/my/pool")
        check(st == 200 and body.get("pool") == [] and bool(body.get("note")),
              "a fresh pool is empty and says why the order matters",
              (body.get("note") or "")[:60])
        for m in (LIVE_MODEL, DEAD_MODEL):
            st, body = api.call("POST", "/api/my/pool", {"provider": PROVIDER, "model": m})
            check(st == 201, f"'{m}' added to the using pool", f"http {st}")
        # The user ranks the DEAD model first: this is the drag they performed, and it is the
        # ranking that must survive everything below.
        st, body = api.call("POST", "/api/my/pool/order",
                            {"order": [{"provider": PROVIDER, "model": DEAD_MODEL},
                                       {"provider": PROVIDER, "model": LIVE_MODEL}]})
        ranks = {p["model"]: p["your_rank"] for p in body.get("pool", [])}
        check(st == 200 and ranks == {DEAD_MODEL: 1, LIVE_MODEL: 2},
              "the drag writes the user's rank, dead model first", str(ranks))
        check(pool_models(body) == [DEAD_MODEL, LIVE_MODEL],
              "with everything healthy the served order IS the user's order",
              str(pool_models(body)))

        # ------------------------------------------------------- auto-switch #1
        print(f"-> detection 1: pool top is the model that refuses ({DEAD_MODEL})")
        t0 = time.time()
        st, body = api.call("POST", "/api/detect", {"text": TEXT})
        secs = time.time() - t0
        sw = body.get("auto_switch")
        check(st == 200 and body.get("classification") in VERDICTS,
              "detection 1 still returns a real verdict",
              f"{body.get('classification')} in {secs:.1f}s")
        check(bool(sw) and sw.get("from") == DEAD_MODEL and sw.get("to") == LIVE_MODEL,
              "the verdict names the switch, from the model that refused to the one that answered",
              json.dumps({k: sw.get(k) for k in ("from", "to", "reason")}) if sw else "no auto_switch")
        check(body.get("lane_source") == "failover",
              "the body says the pool's order was overridden by a failover",
              str(body.get("lane_source")))
        att = (sw or {}).get("attempts") or []
        check(bool(att) and att[0].get("ok") is True and att[0].get("to") == LIVE_MODEL,
              "the attempt list records what the second model did",
              json.dumps(att[0]) if att else "no attempts")
        check(isinstance(body.get("ai_probability"), float),
              "the switched-to model really scored the text",
              str(body.get("ai_probability")))
        legs = ((body.get("evidence") or {}).get("models")) or []
        prim = next((l for l in legs if l.get("lane") == "primary"), {})
        check(prim.get("model") == LIVE_MODEL,
              "the evidence panel attributes the verdict to the model that answered",
              str(prim.get("model")))

        st, body = api.call("GET", "/api/my/pool")
        dead = next(p for p in body["pool"] if p["model"] == DEAD_MODEL)
        check(dead["health"] == "degraded" and dead["demoted"] is False and dead["position"] == 1,
              "ONE refusal does not reorder the pool (a blip is not a delisting)",
              f"health={dead['health']} failures={dead['consecutive_failures']} "
              f"position={dead['position']} your_rank={dead['your_rank']}")
        check(dead["consecutive_failures"] == 1 and bool(dead["last_error"]),
              "the refusal is counted and its reason kept",
              f"{dead['consecutive_failures']} x {dead['last_error'][:60]}")

        # ------------------------------------------- two more refusals -> marked down
        for n in (2, 3):
            print(f"-> detection {n}: same pool, same dead top")
            st, body = api.call("POST", "/api/detect", {"text": TEXT})
            check(st == 200 and body.get("classification") in VERDICTS,
                  f"detection {n} still answers", str(body.get("classification")))
        st, body = api.call("GET", "/api/my/pool")
        rows = {p["model"]: p for p in body["pool"]}
        dead, live = rows[DEAD_MODEL], rows[LIVE_MODEL]
        check(dead["health"] == "down", "three consecutive refusals mark the model down",
              f"failures={dead['consecutive_failures']} last={dead['last_error'][:50]}")
        check(dead["your_rank"] == 1 and dead["position"] == 2 and dead["demoted"] is True,
              "the auto-rearrange moves the served order and keeps the user's rank",
              f"your_rank={dead['your_rank']} served={dead['position']} demoted={dead['demoted']}")
        check(pool_models(body) == [LIVE_MODEL, DEAD_MODEL],
              "the served order now starts with the model that answers", str(pool_models(body)))

        # ------------------------------------------- detection 4: nothing wasted
        print(f"-> detection 4: the dead model is 'down', so it must not be called at all")
        t0 = time.time()
        st, body = api.call("POST", "/api/detect", {"text": TEXT})
        secs = time.time() - t0
        check(st == 200 and body.get("classification") in VERDICTS,
              "detection 4 answers", f"{body.get('classification')} in {secs:.1f}s")
        check(body.get("lane_source") == "user-pool" and "auto_switch" not in body
              and "failover" not in body,
              "no failover was needed: the pool's own reordering picked the live model first",
              f"lane_source={body.get('lane_source')} auto_switch={'auto_switch' in body}")
        legs = ((body.get("evidence") or {}).get("models")) or []
        prim = next((l for l in legs if l.get("lane") == "primary"), {})
        check(prim.get("model") == LIVE_MODEL and prim.get("ok") is True,
              "one call, to the live model, and it answered", str(prim.get("model")))
        det_id = body.get("backend_detection_id")
        st, row = api.call("GET", f"/api/detections/{det_id}")
        check(st == 200 and row.get("primary_model") == LIVE_MODEL,
              "the history row names the model that answered, not the one the user ranked first",
              str(row.get("primary_model")))

        st, body = api.call("GET", "/api/ranking")
        prow = next((p for p in body.get("pool", []) if p["model"] == DEAD_MODEL), None)
        check(prow is not None and prow["health"] == "down",
              "the ranking page carries the same health the pool does",
              json.dumps({k: prow.get(k) for k in ("health", "your_rank", "position")}
                         if prow else None))

        # ------------------------------------------------------- auto-discovery
        if not do_discovery:
            print("--no-discovery: skipping the screening run")
            return finish(tmp)

        # No `timeout_seconds`: this is the request the SPA makes, so it is proved against the
        # ceiling the product actually gives a probe. An explicit number here would re-test the
        # old 60 s false negative instead of the fix for it.
        st, body = api.call("POST", f"/api/my/keys/{key_id}/verify",
                            {"model_id": LIVE_MODEL})
        check(st == 200 and body.get("ok") is True,
              "the stored key verifies live against the provider",
              f"{body.get('provider')}/{body.get('model')} latency={body.get('latency_ms')}ms")
        disc = body.get("discovery") or {}
        check(disc.get("queued") is True and disc.get("job_id"),
              "saving a verified key queues discovery on its own",
              f"job {disc.get('job_id')} ({disc.get('reason')})")
        if not disc.get("queued"):
            print("no job queued; the checks below have nothing to watch")
            return finish(tmp)

        job_id = disc["job_id"]
        # The catalogue row count can never prove a discovery run: every id NVIDIA serves is
        # already recorded, and `run_job` writes rows only for the ids it touches, so the table
        # grows by however many BRAND-NEW ids happened to be tried -- 96 -> 96 on the 2026-10-17
        # run. What the screening pass must leave behind is a refusal note carrying its reason for
        # each candidate that did not answer, and that is a clock-free count of one note prefix.
        c = sqlite3.connect(db_copy)
        runs_before = c.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0]
        models_before = c.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        triage_before = c.execute(
            "SELECT COUNT(*) FROM catalog_models WHERE provider = ? AND probe_note LIKE 'triage %'",
            (PROVIDER,)).fetchone()[0]
        c.close()

        t0, job, stages = time.time(), {}, []
        while time.time() - t0 < 1500:
            st, body = api.call("GET", "/api/my/discover", timeout=30)
            job = next((j for j in body.get("jobs", []) if j["id"] == job_id), {})
            if job.get("stage") not in stages:
                stages.append(job.get("stage"))
                print(f"   [{time.time() - t0:6.1f}s] {job.get('status')}: {job.get('stage')}")
            if job.get("status") in ("done", "failed"):
                break
            time.sleep(4)
        secs = time.time() - t0
        check(job.get("status") == "done", "the discovery job ran to completion",
              f"{job.get('status')} in {secs:.0f}s ({job.get('error') or 'no error'})")
        check((job.get("n_listed") or 0) > 0,
              "a real catalogue fetch came back with models to choose from",
              f"n_listed={job.get('n_listed')}")
        # A screen is written only for a candidate that ANSWERS the text it is given, so "the job
        # must screen something" was testing the provider's inventory rather than our walk -- and
        # the walk is the thing that changed. The job's own contract is that it TRIED candidates
        # from the catalogue it fetched, or said out loud there were none left to try.
        screened = job.get("n_screened") or 0
        walked = re.search(r"(\d+) candidates tried", job.get("stage") or "")
        tried = int(walked.group(1)) if walked else screened
        refusals = max(0, tried - screened)
        check(bool(walked) or screened > 0 or "nothing new to screen" in (job.get("stage") or ""),
              "the job walked candidates out of the catalogue it fetched, or said there were none",
              f"tried={tried} screened={screened} stage={job.get('stage')!r}")
        st, body = api.call("GET", "/api/my/discover", timeout=30)
        check(body.get("screen_texts") == "probe2" and "probe tier" in (body.get("note") or ""),
              "the status says out loud that a screen is 2 texts and only a probe tier",
              (body.get("note") or "")[:70])

        c = sqlite3.connect(db_copy)
        c.row_factory = sqlite3.Row
        runs_after = c.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0]
        probe_runs = [dict(r) for r in c.execute(
            "SELECT model, corpus, tag, n_rows FROM benchmark_runs "
            "WHERE corpus = 'probe2' ORDER BY id DESC LIMIT 12")]
        models_after = c.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        triage_rows = [dict(r) for r in c.execute(
            """SELECT model_id, probe_note FROM catalog_models
               WHERE provider = ? AND probe_note LIKE 'triage %' ORDER BY id DESC LIMIT 24""",
            (PROVIDER,))]
        unscored_duds = [r["model_id"] for r in c.execute(
            """SELECT cm.model_id FROM catalog_models cm
               WHERE cm.provider = ? AND cm.probe_note LIKE 'triage %'
                 AND NOT EXISTS (SELECT 1 FROM benchmark_runs br WHERE br.model = cm.model_id)""",
            (PROVIDER,))]
        c.close()

        # Every candidate that refused its first text now carries a note saying so, so the number
        # of such rows is a lower bound on this job's refusals -- a row that was already known
        # keeps its id and only changes note. This is the half the old `cat_after > cat_before`
        # check could not see: the catalogue was already complete, so it never grew.
        check(len(triage_rows) >= refusals,
              "each candidate that refused the triage text was recorded as a refusal",
              f"'triage …' notes for {PROVIDER}: {triage_before} -> {len(triage_rows)}, "
              f"refused={refusals} (tried {tried}, screened {screened})")
        check(not triage_rows or all(re.search(r"triage 0/1 texts answered: \S+", r["probe_note"])
                                     for r in triage_rows),
              "a refusal names why it refused, so a delisting is not mistaken for a bad afternoon",
              "; ".join(f"{r['model_id']} -> {r['probe_note'][:56]}" for r in triage_rows[:2]))

        if screened:
            check(runs_after > runs_before and all(r["corpus"] == "probe2" for r in probe_runs),
                  "the screens left real benchmark_runs rows, attributed to the probe corpus",
                  f"{runs_before} -> {runs_after}; e.g. "
                  + ", ".join(f"{r['model']}({r['n_rows']})" for r in probe_runs[:3]))
            check(bool(probe_runs) and all(r["n_rows"] == 2 for r in probe_runs),
                  "each screened model was judged on exactly the 2 texts the user approved",
                  str([(r["model"], r["n_rows"]) for r in probe_runs[:4]]))
            check(models_after >= models_before,
                  "screened models got a row so they can be ranked",
                  f"{models_before} -> {models_after}")
        else:
            # The other half of the same rule, and the one a live run only reaches when the
            # provider is asleep. A walk that got nothing back must leave nothing rankable:
            # backend/tests/test_discovery.py covers this offline against a stub, and the
            # 2026-10-17 run showed it is not obvious -- four refusals, four score rows written.
            check(runs_after == runs_before,
                  "a model that refused never becomes a score",
                  f"benchmark_runs stayed at {runs_after} with n_screened=0, {refusals} refusals")
            check(models_after == models_before,
                  "a refused model gets no models row, so it cannot be pinned",
                  f"models stayed at {models_after}")

        st, body = api.call("GET", "/api/ranking")
        ranked = body.get("ranked") or []
        screened_ids = [r["model"] for r in probe_runs]
        in_ranking = [r for r in ranked if r["model"] in screened_ids]
        measured = [r for r in ranked if (r.get("n_rows") or 0) >= 24]
        if screened:
            check(bool(in_ranking), "a screened model now appears on the ranking page",
                  ", ".join(f"#{r['rank']} {r['model']} n_rows={r['n_rows']}" for r in in_ranking[:3]))
            check(bool(measured) and all(r["rank"] > measured[0]["rank"] for r in in_ranking),
                  "a 2-text screen lands in the probe tier and cannot outrank a measured capture",
                  (f"best measured #{measured[0]['rank']} ({measured[0]['model']}, "
                   f"n_rows={measured[0]['n_rows']})" if measured else "no measured row at all")
                  + " vs screened " + ", ".join(f"#{r['rank']}" for r in in_ranking[:3]))
        else:
            # Nothing answered, so there is no new row to place -- and the honest live expectation
            # becomes the negative one: the refusals must not have become ranking entries.
            print("   no model answered this run, so the probe-tier placement has nothing to "
                  f"rank; {len(unscored_duds)} refusals recorded without a score instead")
            check(not (set(unscored_duds) & {r["model"] for r in ranked}),
                  "a recorded refusal stays off the ranking page",
                  f"{len(unscored_duds)} unscored refusals, none among {len(ranked)} ranked models")

        st, body = api.call("GET", "/api/my/pool")
        after = body.get("pool", [])
        auto = [p for p in after if p["source"] == "auto"]
        placed = [p for p in after if p["source"] == "user"]
        healthy_placed = [p for p in placed if p["health"] != "down"]
        overtakes = [p["model"] for p in auto
                     if any(p["position"] < h["position"] for h in healthy_placed)]
        check(not overtakes,
              "a discovered model is appended behind the healthy ones the user placed by hand",
              f"placed={[(p['model'], p['position']) for p in placed]} "
              f"auto={[(p['model'], p['position']) for p in auto]}"
              + (f" — overtook: {overtakes}" if overtakes else ""))
        ranks = {p["model"]: p["your_rank"] for p in after}
        check(ranks.get(DEAD_MODEL) == 1 and ranks.get(LIVE_MODEL) == 2,
              "discovery did not touch the ranking the user set", str(ranks))
        check(not (set(unscored_duds) & {p["model"] for p in after}),
              "a model that refused triage was not added to the using pool",
              f"pool now {[(p['model'], p['source']) for p in after]}")
        return finish(tmp)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        # The temp dir holds the only record of what the engine actually answered, so it stays
        # when something failed -- including when the failure was an exception in this script.
        if not [m for ok, m in results if not ok] and sys.exc_info()[0] is None:
            shutil.rmtree(tmp, ignore_errors=True)


def finish(tmp) -> int:
    """Print the tally; the caller's `finally` does the cleanup."""
    bad = [m for ok, m in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    if bad:
        print("failed:")
        for m in bad:
            print(f"  - {m}")
        logf = tmp / "server.log"
        if logf.is_file():
            print(f"\n--- last 40 lines of {logf} ---")
            print("\n".join(
                logf.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
