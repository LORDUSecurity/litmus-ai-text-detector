"""Auto-discovery: fetch a provider's model list, screen newcomers on 2 texts, rank them.

Triggered by a user proving their own key works (`POST /my/keys/<id>/verify`, or a
`POST /my/keys` that carried a model_id). Nothing polls on a timer: a poll would spend the
free-tier budget the user's own detections need, and would report a model as available on
the strength of a call nobody asked for.

The screen is deliberately the cheapest thing that can still be called a measurement. Two
texts, one labelled AI and one labelled HUMAN, both real captures from the corpus the rest
of the benchmark uses (`benchmark/corpus-probe2.json`), sent through the *shipped* engine
path -- same prompt, same parser, same lane contract -- so a screened score is comparable
with a captured one rather than being a second ruler. Two texts cannot rank a model, and
this module does not pretend otherwise: the run it writes carries `corpus='probe2'`, so
`/ranking` scores it at `n_clean = 2` and its `MEASURED_MIN_TEXTS = 24` tier puts it below
every real measurement. What the screen decides is narrower and useful: does this model
answer at all, and does it put the AI text above the human one.

The first of those two texts is also the triage. A candidate that cannot answer it is
recorded in `catalog_models` with the engine's own reason and costs one call, not two, and
the job moves on to the next id until it has `screen_limit` screens or has tried
`triage_cap` candidates -- whichever comes first. Both numbers exist because the catalogue
arrives in the provider's order, which is alphabetical, and the ids nobody has measured yet
are overwhelmingly the ones somebody retired: measured on 2026-10-17, a four-candidate job
on NVIDIA spent eight calls on `01-ai/yi-large`, `adept/fuyu-8b`,
`ai21labs/jamba-1.5-large-instruct` and `aisingapore/sea-lion-7b-instruct` and reported
`0/4 usable`, which is a job that did its arithmetic correctly and brought the user nothing.

Runs in a worker thread because one screen is two calls of up to three minutes each and no
HTTP request should be held open that long.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmark"
SCREEN_CORPUS_FILE = BENCH / "corpus-probe2.json"
SCREEN_CORPUS = "probe2"
SCREEN_TAG = "autoscreen"

# How many models one job may screen. A provider lists dozens (NVIDIA advertised 81 ids on
# 2026-10-17) and each screen is two real calls on the user's key, so the cap is what keeps
# "verify your key" from turning into an hour of spending. Admin-tunable via
# Settings -> discovery_screen_limit.
DEFAULT_SCREEN_LIMIT = 4

# How many candidates one job may *try* to get those screens. A provider's catalogue comes
# back in its own order, which for NVIDIA is alphabetical, and the ids an unmeasured user has
# never screened are overwhelmingly the legacy ones somebody delisted years ago: the 4-candidate
# job measured on 2026-10-17 tried 01-ai/yi-large, adept/fuyu-8b, ai21labs/jamba-1.5-large and
# aisingapore/sea-lion-7b and wrote `0/4 usable` -- eight real calls spent to bring the user
# nothing. Trying further is cheap because a refusal is the fast case (a 404 comes back in about
# a second), and it is the only way a live newcomer near the end of the list ever gets screened.
# The cap is what stops "nothing answers" from turning into 81 calls.
DEFAULT_TRIAGE_CAP = 24

# Days before an id that refused is worth refusing again. Without this the walk re-triages the
# same twenty-odd delisted models on every run and never reaches the tail of the catalogue;
# with it, a delisting that turns out to have been an outage is noticed a week later.
DEAD_RETRY_DAYS = 7
DEFAULT_SCREEN_TIMEOUT_MS = 180_000
DEFAULT_CATALOG_TIMEOUT_S = 25

# The timeout written onto a freshly discovered models row. This is the deployment's own lane
# ceiling (240 s, the value the shipped chain is nested inside: 240 lane -> 270 wait ->
# 285 gunicorn -> 330 nginx), not a measurement of the new model -- nobody has measured it,
# that is the point. The 90 s schema default would be worse than a guess: it is short enough
# that a congested free pool times out, and a timeout is then recorded as the model failing,
# which is how a working model gets marked down. Thresholds are left NULL on purpose, so the
# model is judged at the engine's own baked cuts rather than at a band copied from somewhere.
DISCOVERED_TIMEOUT_MS = 240_000

# Providers whose catalogue mixes paid and free models behind one endpoint. OpenRouter is
# the case that matters: only ids ending in `:free` are the zero-cost tier, and screening a
# paid id would bill the user's card for a model they never chose. Every other configured
# provider row carries free_tier, and the row is checked before this map is consulted.
FREE_SUFFIX_REQUIRED = {"openrouter": ":free"}


def load_screen_texts() -> list[dict]:
    """The 2-text screen corpus: [{id, label, text}].

    Raises rather than returning [] -- a screen with no texts would write a benchmark run
    with zero rows, and `/ranking` would then report a model as measured over nothing.
    """
    rows = json.loads(SCREEN_CORPUS_FILE.read_text(encoding="utf-8"))
    out = [{"id": str(r["id"]), "label": str(r["label"]), "text": str(r["text"])}
           for r in rows if r.get("text") and r.get("label")]
    if len(out) < 2:
        raise ValueError(f"{SCREEN_CORPUS_FILE.name} holds {len(out)} usable texts; the screen needs 2")
    if not any(r["label"] == "AI" for r in out) or not any(r["label"] == "HUMAN" for r in out):
        raise ValueError(f"{SCREEN_CORPUS_FILE.name} must carry one AI and one HUMAN text")
    return out


def fetch_catalogue(base_url: str, key: str, auth_header: str = "Authorization",
                    auth_prefix: str = "Bearer ", timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S):
    """GET <base_url>/models and return (model_ids, error).

    Never raises: a provider that 404s its catalogue (some gateways do) is a normal outcome
    of discovery and must land in the job's `error` field, not in a traceback the caller has
    to catch. The key is used and discarded, never logged or stored here.
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if key:
        req.add_header(auth_header or "Authorization", f"{auth_prefix or ''}{key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        return [], f"catalogue HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return [], f"catalogue unreachable: {exc}"
    if status != 200:
        return [], f"catalogue HTTP {status}"
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return [], "catalogue was not JSON"
    items = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return [], "catalogue had no data[] list"
    ids = []
    for it in items:
        mid = (it.get("id") if isinstance(it, dict) else None) or (it if isinstance(it, str) else None)
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())
    # De-duplicate, keep catalogue order: order is the provider's own and re-sorting it
    # would make the screen pick a different subset on each run.
    seen, out = set(), []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out, ""


def is_free_candidate(provider_name: str, model_id: str, free_tier: bool) -> bool:
    suffix = FREE_SUFFIX_REQUIRED.get((provider_name or "").lower())
    if suffix:
        return model_id.endswith(suffix)
    return bool(free_tier)


def needs_screen(conn, provider_name: str, model_id: str) -> bool:
    """True when this model is worth spending a call on.

    A row in `models` with `measured = 1` is a full capture -- re-screening it on 2 texts
    would write a *smaller* measurement over a larger one for the same model id, which is
    the one thing `/ranking`'s "keep the fullest run" rule cannot undo. A row with
    `measured = 0` is either a seeded default or an earlier screen, and both are fair to
    refresh, because the point of discovery is noticing that a model started or stopped
    answering.

    `usable` never needs re-proving: it is the state a user can act on, and it only ever
    changes for the worse when a model is delisted -- which the pool's own health tracking
    catches on the next real detection. A `dead` row is skipped for DEAD_RETRY_DAYS and then
    retried, because a delisting and a bad afternoon look identical from one refusal, and the
    walk has to be able to reach the ids behind it.
    """
    row = conn.execute(
        """SELECT m.measured FROM models m JOIN providers p ON p.id = m.provider_id
           WHERE m.model_id = ? AND p.name = ?""", (model_id, provider_name)).fetchone()
    if row is not None and int(row["measured"]):
        return False
    cat = conn.execute(
        """SELECT probe_status,
                  probed_at > datetime('now', ?) AS is_recent
           FROM catalog_models WHERE provider = ? AND model_id = ?""",
        (f"-{DEAD_RETRY_DAYS} days", provider_name, model_id)).fetchone()
    if cat is None:
        return True
    if cat["probe_status"] == "usable":
        return False
    return not (cat["probe_status"] == "dead" and int(cat["is_recent"]))


def engine_url(conn) -> str:
    n8n = db.get_setting(conn, "n8n_url", "http://localhost:5678") or "http://localhost:5678"
    path = db.get_setting(conn, "webhook_path", "/webhook/ai-detect-ai") or "/webhook/ai-detect-ai"
    return n8n.rstrip("/") + path


def screen_one_text(url: str, base_url: str, model_id: str, key: str, text: str,
                    timeout_s: float) -> dict:
    """One detection through the real engine, aimed at one candidate model.

    The tie-break lane is switched off explicitly: the screen measures this model, and a
    second lane would both double the cost and let a different model's answer be recorded
    as this one's. No `tuning` block is sent, so an unknown model is judged at the
    workflow's baked defaults -- borrowing another model's calibrated cuts would be
    inventing a measurement.
    """
    payload = {
        "text": text,
        "lane_config": {
            "primary": {"base_url": base_url, "model": model_id},
            "secondary": {"enabled": False},
        },
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("x-primary-api-key", key)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error_code": "ENGINE_UNREACHABLE", "error_detail": str(exc)[:300],
                "ai_probability": None, "confidence": None,
                "latency_ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error_code": f"http_{status}", "error_detail": "engine answer was not JSON",
                "ai_probability": None, "confidence": None, "latency_ms": ms}
    if not isinstance(body, dict):
        return {"ok": False, "error_code": f"http_{status}", "error_detail": "engine answer was not an object",
                "ai_probability": None, "confidence": None, "latency_ms": ms}
    legs = ((body.get("evidence") or {}).get("models")) or []
    leg = next((l for l in legs if isinstance(l, dict) and l.get("lane") == "primary"), None)
    p = body.get("ai_probability")
    conf = body.get("confidence")
    # The verdict has to come from THIS model's own leg. A body that answers while its
    # primary leg failed means something else decided, and recording that probability as
    # the candidate's would credit a model that never answered.
    ok = bool(leg and leg.get("ok")) and isinstance(p, (int, float))
    return {"ok": ok,
            "error_code": None if ok else (leg or {}).get("error_code") or body.get("error_code") or f"http_{status}",
            "error_detail": None if ok else ((leg or {}).get("error_detail") or body.get("error")) or "",
            "ai_probability": float(p) if isinstance(p, (int, float)) else None,
            "confidence": float(conf) if isinstance(conf, (int, float)) else None,
            "latency_ms": ms}


def store_screen(conn, provider_name: str, model_id: str, texts: list[dict],
                 results: list[dict], user_id: int | None, base_url: str,
                 provider_id: int) -> dict:
    """Write the screen as a benchmark run + (if it answered) a models row.

    The run is keyed UNIQUE(tag, model, corpus) so re-screening the same model replaces its
    2-text score instead of accumulating rows that `/ranking` would then have to choose
    between. `source_file` names the live path rather than a capture file, because there is
    no capture file: attribution for a discovered model lives in params_json, and
    `benchmark/runs-manifest.log` covers offline captures only.
    """
    answered = [r for r in results if r["ok"] and r["ai_probability"] is not None]
    usable = len(answered) == len(texts)
    separated = None
    if usable:
        by_label = {t["label"]: r["ai_probability"] for t, r in zip(texts, results)}
        separated = by_label.get("AI", 0.0) > by_label.get("HUMAN", 1.0)
    latency = sum(r["latency_ms"] for r in results)
    note_bits = [f"screen {len(answered)}/{len(texts)} texts answered"]
    if separated is False:
        note_bits.append("did NOT separate the AI text from the human one")
    note = "; ".join(note_bits)

    conn.execute(
        """INSERT INTO benchmark_runs (tag, provider, model, corpus, prompt, params_json,
                                       n_rows, source_file)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(tag, model, corpus) DO UPDATE SET
             provider = excluded.provider, prompt = excluded.prompt,
             params_json = excluded.params_json, n_rows = excluded.n_rows,
             source_file = excluded.source_file, imported_at = datetime('now')""",
        (SCREEN_TAG, provider_name, model_id, SCREEN_CORPUS, "direct",
         json.dumps({"screen": True, "texts": len(texts), "answered": len(answered),
                     "separated": separated, "base_url": base_url,
                     "discovered_by_user_id": user_id,
                     "latency_ms": latency,
                     "note": "auto-discovery 2-text screen; probe tier only, not a calibration"}),
         len(results), "live-discovery"),
    )
    run_id = conn.execute(
        "SELECT id FROM benchmark_runs WHERE tag = ? AND model = ? AND corpus = ?",
        (SCREEN_TAG, model_id, SCREEN_CORPUS)).fetchone()["id"]
    conn.execute("DELETE FROM benchmark_results WHERE run_id = ?", (run_id,))
    for t, r in zip(texts, results):
        conn.execute(
            """INSERT INTO benchmark_results (run_id, text_id, label, ok, error_code,
                                              ai_probability, confidence)
               VALUES (?,?,?,?,?,?,?)""",
            (run_id, t["id"], t["label"], 1 if r["ok"] else 0, r.get("error_code"),
             r["ai_probability"], r["confidence"]),
        )
    conn.execute(
        """INSERT INTO catalog_models (provider, model_id, listed, probe_status, probe_note, latency_ms)
           VALUES (?,?,1,?,?,?)
           ON CONFLICT(provider, model_id) DO UPDATE SET
             listed = 1, probe_status = excluded.probe_status,
             probe_note = excluded.probe_note, latency_ms = excluded.latency_ms,
             probed_at = datetime('now')""",
        (provider_name, model_id, "usable" if usable else "dead", note, latency or None),
    )
    if answered:
        # A models row is what makes the model usable at all: /ranking reads the endpoint
        # and the caller's verified-key availability from it, and /detect reads its band.
        # measured stays 0 and lane stays NULL -- a 2-text screen is not a calibration and
        # must never claim a lane. Thresholds stay NULL too, so the engine judges this model
        # at its own baked cuts instead of at a band borrowed from another model.
        conn.execute(
            """INSERT INTO models (provider_id, model_id, display_name, role, lane, timeout_ms,
                                   notes, enabled, measured)
               VALUES (?,?,?,?,NULL,?,?,1,0)
               ON CONFLICT(provider_id, model_id) DO UPDATE SET
                 notes = excluded.notes, updated_at = datetime('now')""",
            (provider_id, model_id, model_id, "either", DISCOVERED_TIMEOUT_MS,
             f"auto-discovered {time.strftime('%Y-%m-%d')}: {note}. Screen only -- 2 texts, "
             f"scored in the ranking's probe tier at the engine's default cuts (no confidence "
             f"gate), so it is evidence the model answers, not a calibration."),
        )
    return {"model": model_id, "usable": usable, "separated": separated,
            "answered": len(answered), "texts": len(texts), "note": note,
            "latency_ms": latency, "run_id": run_id}


def store_dud(conn, provider_name: str, model_id: str, result: dict,
              user_id: int | None) -> dict:
    """Record a candidate that never answered the text it was given.

    Deliberately neither a `benchmark_runs` row nor a `models` row. A model that refused has no
    score to display, and `/ranking` would show a 2-text run whose rows all failed as if it were
    a measurement; a `models` row would make it pinnable, which is how a delisted id ends up at
    the top of somebody's pool. What a refusal does deserve is its reason, kept here in the
    caller's own words: `http_410` (delisted), `no_choices` (advertised, not served) and
    `http_503` (the pool is busy) are three different answers to "should I put my text on this
    model", and the first two are what `DEAD_RETRY_DAYS` is for.
    """
    note = f"triage 0/1 texts answered: {result.get('error_code') or 'refused'}"
    detail = (result.get("error_detail") or "").strip()
    if detail:
        note = f"{note} -- {detail[:160]}"
    conn.execute(
        """INSERT INTO catalog_models (provider, model_id, listed, probe_status, probe_note, latency_ms)
           VALUES (?,?,1,'dead',?,?)
           ON CONFLICT(provider, model_id) DO UPDATE SET
             listed = 1, probe_status = 'dead', probe_note = excluded.probe_note,
             latency_ms = excluded.latency_ms, probed_at = datetime('now')""",
        (provider_name, model_id, note[:400], result.get("latency_ms")),
    )
    return {"model": model_id, "usable": False, "refused": True, "note": note,
            "error_code": result.get("error_code"), "latency_ms": result.get("latency_ms"),
            "seen_by_user_id": user_id}


def run_job(job_id: int, user_id: int | None, provider_id: int, provider_name: str,
            base_url: str, key: str, auth_header: str, auth_prefix: str,
            screen_limit: int = DEFAULT_SCREEN_LIMIT,
            screen_timeout_ms: int = DEFAULT_SCREEN_TIMEOUT_MS,
            catalog_timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
            triage_cap: int = DEFAULT_TRIAGE_CAP,
            add_to_pool: bool = True) -> dict:
    """Execute one discovery job to completion. Returns the summary written to the row."""
    with db.session() as conn:
        db.discovery_update(conn, job_id, status="running", stage="fetching catalogue",
                            started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    ids, err = fetch_catalogue(base_url, key, auth_header, auth_prefix, catalog_timeout_s)
    summary = {"job_id": job_id, "provider": provider_name, "n_listed": len(ids),
               "n_screened": 0, "n_usable": 0, "n_tried": 0,
               "screened": [], "refused": [], "error": err}
    if err:
        with db.session() as conn:
            db.discovery_update(conn, job_id, status="failed", stage="catalogue",
                                n_listed=0, error=err[:400],
                                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return summary

    cap = max(0, int(screen_limit))
    walk_cap = max(cap, int(triage_cap))
    with db.session() as conn:
        free_tier = bool(conn.execute("SELECT free_tier FROM providers WHERE id = ?",
                                     (provider_id,)).fetchone()["free_tier"])
        url = engine_url(conn)
        candidates = [m for m in ids
                      if is_free_candidate(provider_name, m, free_tier)
                      and needs_screen(conn, provider_name, m)]
        db.discovery_update(conn, job_id,
                            stage=f"{len(candidates)} to try, up to {walk_cap} triaged "
                                  f"for {cap} screens",
                            n_listed=len(ids))
    if not candidates:
        with db.session() as conn:
            db.discovery_update(conn, job_id, status="done", n_listed=len(ids),
                                stage="nothing new to screen",
                                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return summary

    try:
        texts = load_screen_texts()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        with db.session() as conn:
            db.discovery_update(conn, job_id, status="failed", stage="screen corpus",
                                n_listed=len(ids), error=str(exc)[:400],
                                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        summary["error"] = str(exc)[:400]
        return summary

    timeout_s = max(5.0, float(screen_timeout_ms) / 1000.0)
    for model_id in candidates:
        if len(summary["screened"]) >= cap or summary["n_tried"] >= walk_cap:
            break
        with db.session() as conn:
            db.discovery_update(
                conn, job_id,
                stage=f"triage {summary['n_tried'] + 1}/{walk_cap}, "
                      f"screen {len(summary['screened'])}/{cap}: {model_id}",
                n_screened=len(summary["screened"]))
        summary["n_tried"] += 1
        # The first text is the triage and the first text of the screen at once: an id that
        # cannot answer it has already answered the only question that matters, and a second
        # text would be bought for a model we know does not work.
        first = screen_one_text(url, base_url, model_id, key, texts[0]["text"], timeout_s)
        if not first["ok"]:
            with db.session() as conn:
                row = store_dud(conn, provider_name, model_id, first, user_id)
            summary["refused"].append(row)
            continue
        results = [first, screen_one_text(url, base_url, model_id, key,
                                         texts[1]["text"], timeout_s)]
        with db.session() as conn:
            row = store_screen(conn, provider_name, model_id, texts, results, user_id,
                               base_url, provider_id)
            if row["usable"] and add_to_pool and user_id is not None:
                # Appended with source='auto', so it lands after everything the user placed
                # and can never overtake a rank they set by hand (db.pool_add's ON CONFLICT
                # DO NOTHING keeps an existing row where it is).
                db.pool_add(conn, user_id, provider_name, model_id, source="auto")
                row["added_to_pool"] = True
            db.discovery_update(conn, job_id, n_screened=len(summary["screened"]) + 1)
        summary["screened"].append(row)
        summary["n_usable"] += 1 if row["usable"] else 0

    summary["n_screened"] = len(summary["screened"])
    with db.session() as conn:
        db.discovery_update(conn, job_id, status="done", n_listed=len(ids),
                            n_screened=summary["n_screened"], n_usable=summary["n_usable"],
                            stage=f"{summary['n_usable']}/{summary['n_screened']} usable of "
                                  f"{summary['n_tried']} candidates tried",
                            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    return summary


class DiscoveryWorker:
    """One job at a time, in a daemon thread.

    Serial on purpose: the screens spend the *user's* free-tier budget through one engine,
    and parallel jobs would trip the provider's rate limit and then record the resulting 429
    as a property of the model. `queue.Queue` rather than a shared list so enqueue and
    dequeue need no lock of their own.
    """

    def __init__(self, idle_s: float = 0.5):
        self.q: queue.Queue = queue.Queue()
        self.idle_s = idle_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_summary: dict | None = None

    @property
    def running(self) -> bool:
        """Is the worker thread alive in THIS process?

        The entry points (wsgi, the packaged launcher, `python -m backend.app`) start it; a
        Flask test client does not. `enqueue_discovery` refuses to queue work nobody will
        pick up, which also means a test that verifies a key cannot silently launch a thread
        that makes real provider calls against a database the next test deletes.
        """
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="model-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.q.put(None)  # wake a blocked get()

    def enqueue(self, job: dict) -> None:
        self.q.put(job)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.q.get(timeout=self.idle_s)
            except queue.Empty:
                continue
            if job is None:
                continue
            try:
                self.last_summary = run_job(**job)
            except Exception as exc:  # a discovery bug must not kill the thread or the app
                try:
                    with db.session() as conn:
                        db.discovery_update(conn, job.get("job_id", 0), status="failed",
                                            stage="worker", error=f"{type(exc).__name__}: {exc}"[:400],
                                            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
                except Exception:
                    pass

    def pending(self) -> int:
        return self.q.qsize()


worker = DiscoveryWorker()
