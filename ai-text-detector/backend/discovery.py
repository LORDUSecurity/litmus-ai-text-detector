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

What a non-answer is *blamed on* is the other half of this module, and it is not one rule but
three, because the three causes have three cures:

* `dead` -- the model refused (404 function-id, 410 end-of-life, `no_choices`). Nothing to
  wait for, so it is written off for `DEAD_RETRY_DAYS` and gets no `models` row: an id that
  will not answer is not something to pin a lane to.
* `slow` / `busy` -- the lane did not finish in time, or the provider's queue said no. That is
  a property of a pool at a minute, not of a model, so the text is asked again before anything
  is recorded, the row still gets a `models` entry so the user can pick it, and the next job
  tries it again rather than skipping it for a week.
* `engine_down` -- the engine itself did not answer, so no candidate was ever asked. The job
  stops and says so; recording that against twenty-four models is how a stopped n8n retires a
  whole catalogue.

Runs in a worker thread because one screen is two calls of up to three minutes each and no
HTTP request should be held open that long.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db, engine, pool

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

# How many extra times one text is asked after it times out. A timed-out lane says nothing
# about the model: the same NVIDIA account serves one model in 1.4 s and another in 269 s, and
# the gateway 504s at ~302 s whatever the client asks for. So the first answer to "did not
# answer in time" is to ask again, in the same job, before writing anything off -- and only a
# *timeout* is re-asked, because a 404 is deterministic and paying for it twice buys the same
# refusal.
DEFAULT_TIMEOUT_RETRIES = 1

# An answer that arrives this quickly is worth a decision now, while the user is looking at the
# page -- which is what `DECISION_WINDOW_S` bounds. Measured against the lane ceilings this
# deployment ships (240 s) and the pool regimes in docs/free-model-research.md, 45 s is the
# line between "this answers" and "this answers eventually".
FAST_ANSWER_MS = 45_000

# How long the user gets to say yes or no to a model that answered only after a timeout. Short on
# purpose: the offer is worth making while he is still on the page that ran the discovery, and a
# decision that never arrives must not become a silent pin or a silent veto. When the window
# closes the row stops being offered, stays pickable, and the next job is allowed to ask it again
# (`needs_screen`) -- so an unread offer costs nothing but reverts to "we do not know yet".
DECISION_WINDOW_S = 600

# What a timeout is made of, in the words the engine and the client actually return. Kept
# separate from the busy markers because the two cure differently: a busy pool needs waiting,
# a timeout may also need a longer lane ceiling.
TIMEOUT_MARKERS = ("engine_timeout", "timed out", "timeout", "etimedout", "504", "gateway time")

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


def _engine_listening(url: str, probe_s: float = 2.0) -> bool:
    """Is the engine's own port still answering? Imported late because app.py imports this
    module -- and the answer is app.py's measurement, not a second opinion: on this host a
    connect to a dead local port hangs for the whole client timeout instead of refusing
    (measured 2026-10-16), so `timed out` in a client exception cannot by itself say whether
    the lane was slow or the engine is gone. Same rule, same two-second handshake, one
    implementation."""
    from .app import _engine_listening as probe
    return probe(url, probe_s)


def classify_attempt(r: dict) -> str:
    """'answered' | 'slow' | 'busy' | 'engine_down' | 'refused' for one screen attempt.

    The five-way split is the whole difference between a model being measured and a model being
    written off, so it is decided from the engine's own words rather than from which text was
    sent: `ENGINE_UNREACHABLE` means the candidate was never asked, timeout markers mean it was
    asked and took too long, and `pool.TRANSIENT_MARKERS` (429, 503, quota, overloaded) means the
    provider's queue said no. Everything else is the model itself refusing, which is the only
    non-answer that justifies `DEAD_RETRY_DAYS`.
    """
    if r.get("ok"):
        return "answered"
    blob = f"{r.get('error_code') or ''} {r.get('error_detail') or ''}".lower()
    if "engine_unreachable" in blob:
        return "engine_down"
    if any(m in blob for m in TIMEOUT_MARKERS):
        return "slow"
    if any(m in blob for m in pool.TRANSIENT_MARKERS):
        return "busy"
    return "refused"


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

    `slow` and `busy` are NOT skipped: a lane that did not finish in time says nothing about the
    model, and the only honest re-read is to ask it again on the next run. `pending` waits for
    the user's accept-or-refuse instead of spending another call -- but only while that offer is
    still live, because an offer he never saw must not freeze the row forever; once the window
    closes the next job asks again. `declined` is a decision he already made and is terminal."""
    row = conn.execute(
        """SELECT m.measured FROM models m JOIN providers p ON p.id = m.provider_id
           WHERE m.model_id = ? AND p.name = ?""", (model_id, provider_name)).fetchone()
    if row is not None and int(row["measured"]):
        return False
    cat = conn.execute(
        """SELECT probe_status,
                  probed_at > datetime('now', ?) AS is_recent,
                  probed_at > datetime('now', ?) AS offer_live
           FROM catalog_models WHERE provider = ? AND model_id = ?""",
        (f"-{DEAD_RETRY_DAYS} days", f"-{DECISION_WINDOW_S} seconds",
         provider_name, model_id)).fetchone()
    if cat is None:
        return True
    status = cat["probe_status"]
    if status == "pending":
        return not int(cat["offer_live"])
    if status in ("usable", "declined"):
        return False
    return not (status == "dead" and int(cat["is_recent"]))


def engine_url(conn) -> str:
    """The webhook address of the workflow engine.

    Only the hosted deploy needs it. A packaged install scores inside this process, so on that
    path `screen_one_text` neither asks for an address nor opens a socket to :5678 -- and a screen
    that had to start n8n to answer would report every catalogue as gone on an install that has
    no reason to own n8n at all."""
    n8n = db.get_setting(conn, "n8n_url", "http://localhost:5678") or "http://localhost:5678"
    path = db.get_setting(conn, "webhook_path", "/webhook/ai-detect-ai") or "/webhook/ai-detect-ai"
    return n8n.rstrip("/") + path


def detect_hosts(conn, base_url: str, user_id: int | None) -> tuple[str, ...]:
    """The endpoint allowlist to hand the in-process engine for one screen.

    `engine.normalize()` re-checks every lane it dials, so a list narrower than the one `/detect`
    uses would reject the very gateway this job was asked to verify -- the pair-of-rulers trap
    `app.allowed_hosts()` documents. The endpoint being screened is added explicitly, because a
    key under verification may not be in `api_keys` yet: the flow that starts this job is the user
    proving a key works, not a proof bought after it was saved.

    Imported late because app.py imports this module, and read off this `conn` because a worker
    thread has no request context to borrow."""
    from . import app

    hosts = app.allowed_hosts(conn) + app.key_hosts(conn, user_id)
    host = app.host_of(base_url)
    return hosts + ((host,) if host else ())


def _no_answer(error_code, error_detail, ms: int) -> dict:
    return {"ok": False, "error_code": error_code,
            "error_detail": (error_detail or "")[:300],
            "ai_probability": None, "confidence": None, "latency_ms": ms}


def _read_engine_body(status, body, ms: int) -> dict:
    """Read a detection response the way a screen must: from the candidate's own leg.

    Shared by both transports -- the workflow's Respond node and `engine.detect()` answer the same
    shape, which is what `tools/engine_parity.py` exists to guarantee -- so a model cannot be
    measured one way on a container and another way on an exe."""
    if not isinstance(body, dict):
        return _no_answer(f"http_{status}", "engine answer was not an object", ms)
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


def screen_one_text(url: str, base_url: str, model_id: str, key: str, text: str,
                    timeout_s: float, hosts: tuple[str, ...] | None = None) -> dict:
    """One detection through the shipped engine, aimed at one candidate model.

    The tie-break lane is switched off explicitly: the screen measures this model, and a
    second lane would both double the cost and let a different model's answer be recorded
    as this one's. No `tuning` block is sent, so an unknown model is judged at the
    workflow's baked defaults -- borrowing another model's calibrated cuts would be
    inventing a measurement.

    Two transports, one contract: a single-owner install asks the in-process engine, a hosted
    deploy POSTs to the workflow's webhook. `url` is used only by the second and `hosts` only by
    the first, so the local path needs no engine process and no allowlist read from a settings
    row it may not have.
    """
    payload = {
        "text": text,
        "lane_config": {
            "primary": {"base_url": base_url, "model": model_id},
            "secondary": {"enabled": False},
        },
    }
    if engine.native_enabled():
        t0 = time.time()
        status, body = engine.detect(payload, headers=({"x-primary-api-key": key} if key else {}),
                                     timeout=timeout_s,
                                     allowed_hosts=(tuple(hosts) if hosts is not None else None))
        return _read_engine_body(status, body, int((time.time() - t0) * 1000))

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
        ms = int((time.time() - t0) * 1000)
        # The client's own words cannot tell a slow engine from a missing one -- both come back
        # as `timed out`, and on this host a connect to a dead port hangs instead of refusing --
        # so the same one-handshake discriminator /detect uses decides which code to record.
        # Guessing wrong here would spend the retry budget on a port nothing answers, or freeze a
        # live lane for a week as "unreachable". Same rule as app.forward(), copied deliberately
        # rather than imported: a *read* timeout proves the connection was accepted, so it names
        # the phase without asking; anything else needs the handshake.
        why = str(exc).lower()
        timed_out = "read operation timed out" in why or _engine_listening(url)
        return _no_answer("ENGINE_TIMEOUT" if timed_out else "ENGINE_UNREACHABLE",
                          f"engine at {url}: {exc}", ms)
    ms = int((time.time() - t0) * 1000)
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return _no_answer(f"http_{status}", "engine answer was not JSON", ms)
    return _read_engine_body(status, body, ms)


def screen_text(url: str, base_url: str, model_id: str, key: str, text: str,
                timeout_s: float, retries: int = DEFAULT_TIMEOUT_RETRIES,
                hosts: tuple[str, ...] | None = None) -> dict:
    """One screen text, asked again while the only answer was "too slow".

    Retrying is the cheapest evidence there is that a candidate is alive: a refusal is
    deterministic and costs the same words twice, so `refused`, `busy` and `engine_down` are
    returned after one call, and only `slow` buys another. The extra call is bounded by the same
    per-text ceiling the first one had, which is why the cap is one and not three -- on the
    shipped 240 s lane a job could otherwise spend twenty minutes learning that a pool is busy.

    `attempts` rides along on the returned dict: `run_job` uses it to tell a model that answered
    on its own merits from one that answered only after being asked twice, and those two get
    different treatment (the second one is offered to the user, not pinned into his lane).
    """
    result = screen_one_text(url, base_url, model_id, key, text, timeout_s, hosts=hosts)
    attempts = 1
    while classify_attempt(result) == "slow" and attempts <= max(0, int(retries)):
        result = screen_one_text(url, base_url, model_id, key, text, timeout_s, hosts=hosts)
        attempts += 1
    result["attempts"] = attempts
    return result


def ensure_models_row(conn, provider_id: int, model_id: str, note: str) -> None:
    """Give a model that proved it exists a `models` row, so the user can pick it.

    The row is what makes a model usable at all: `/ranking` reads the endpoint and the caller's
    key availability from it, `/detect` reads its band, and the pool picker lists it. `measured`
    stays 0 and `lane` stays NULL -- a 2-text screen, let alone a timeout, is not a calibration
    and must never claim a lane. Thresholds stay NULL too, so the engine judges this model at its
    own baked cuts instead of at a band borrowed from another model. `timeout_ms` is the
    deployment's lane ceiling (see DISCOVERED_TIMEOUT_MS), which for a model already recorded as
    slow is the difference between "it gets a fair call" and "it is timed out by a 90 s default
    nobody chose for it"."""
    conn.execute(
        """INSERT INTO models (provider_id, model_id, display_name, role, lane, timeout_ms,
                               notes, enabled, measured)
           VALUES (?,?,?,?,NULL,?,?,1,0)
           ON CONFLICT(provider_id, model_id) DO UPDATE SET
             notes = excluded.notes, updated_at = datetime('now')""",
        (provider_id, model_id, model_id, "either", DISCOVERED_TIMEOUT_MS, note),
    )


# The words a build from before the timeout/refusal split stamped into `probe_status`, read in the
# vocabulary `classify_attempt` produces. The spelling changed; the facts did not. `timeout` is a
# lane that ran out of time and `rate_limited` is somebody else's queue, which is what the current
# rule calls `slow` and `busy`; `not_found`, `forbidden`, `http_400` and `http_500` are the
# classes this code still calls a refusal, so they become the frozen `dead`. `answered` is decided
# from the row's note instead -- see `_legacy_outcome`. This adds no judgement to the shipped one;
# it applies it to rows written before the shipped rule existed.
LEGACY_PROBE_STATUS = {
    "timeout": "slow",
    "rate_limited": "busy",
    "not_found": "dead",
    "forbidden": "dead",
    "http_400": "dead",
    "http_500": "dead",
}
PROBE_VOCAB_VERSION = 2


def _legacy_outcome(status: str, note: str) -> str | None:
    """Read one older build's row under the current rule, or None if the word is already current.

    `answered` needs the row's own note because the word is weaker than it sounds: the import that
    wrote it only knew the probe returned HTTP 200. Measured in an installed database on
    2026-10-20, those 200s split -- some notes are `p=0.98 finish=stop`, which is a judgement and
    therefore `usable`, and some are a safety label, a `finish=length` ramble or a table of
    markup, which is the class the live path calls `no_choices` and writes off. Reading the note
    is the only way to tell them apart without spending a call to re-ask.
    """
    if status == "answered":
        return "usable" if re.search(r"\bp=\d", note or "") else "dead"
    return LEGACY_PROBE_STATUS.get(status)


def _recovered_note(legacy: str, status: str) -> str:
    why = {
        "slow": "the lane ran out of time, which is a property of the pool at that minute",
        "busy": "the provider's queue said no, which is a property of the key's window",
        "usable": "its own note records a probability, so the id judged the text it was given",
    }[status]
    return (f"read back from the older build's `{legacy}` on "
            f"{time.strftime('%Y-%m-%d')}: {why}. Recorded as `{status}`, and given the "
            f"models row that state has always implied -- pickable, but unmeasured: no text "
            f"was scored, so it has no rank and no calibration.")


def migrate_probe_vocabulary(conn) -> dict:
    """Give an existing install the rows the old build never wrote, once, without a call.

    The picker lists `models` rows, because a `models` row is what makes a model usable: `/detect`
    reads its band, `/ranking` reads its endpoint. A build that treated a timeout as a verdict
    wrote no such row, so every model that merely ran out of time is not "unmeasured but
    pickable" in that install -- it is invisible, on the page whose whole job is saying which
    models answer. Reading the legacy status back under the current rule restores exactly what
    the same rule would have written on the day it was probed, and spends nothing to do it.

    Two things it deliberately does not do. It does not touch a `models` row that already exists:
    its notes are that row's own history and `enabled = 0` is somebody's choice to take a lane out
    of service. And it does not backfill a refusal -- a delisted id with a row is a confuser at the
    top of someone's pool, which is the failure the guard in `run_job` exists to prevent.

    Like `apply_lane_defaults`, this is a one-shot: the version key is written on the first boot
    that runs it, so a status the user or a later job changes is never overwritten again.
    """
    if db.get_setting(conn, "probe_vocab_version") is not None:
        return {"reclassified": 0, "rows_added": 0}
    rows = conn.execute(
        """SELECT cm.provider AS prov, cm.model_id AS mid, cm.probe_status AS st,
                  cm.probe_note AS note, p.id AS pid
           FROM catalog_models cm JOIN providers p ON p.name = cm.provider""").fetchall()
    reclassified = added = 0
    for r in rows:
        want = _legacy_outcome(r["st"], r["note"])
        if want is None:
            continue
        conn.execute("UPDATE catalog_models SET probe_status = ? "
                     "WHERE provider = ? AND model_id = ?", (want, r["prov"], r["mid"]))
        reclassified += 1
        if want == "dead":
            continue
        if conn.execute("SELECT 1 FROM models WHERE provider_id = ? AND model_id = ?",
                        (r["pid"], r["mid"])).fetchone():
            continue
        ensure_models_row(conn, r["pid"], r["mid"], _recovered_note(r["st"], want))
        added += 1
    db.set_setting(conn, "probe_vocab_version", str(PROBE_VOCAB_VERSION))
    return {"reclassified": reclassified, "rows_added": added}


def store_screen(conn, provider_name: str, model_id: str, texts: list[dict],
                 results: list[dict], user_id: int | None, base_url: str,
                 provider_id: int, timed_out_once: bool = False) -> dict:
    """Write the screen as a benchmark run + (if it answered) a models row.

    The run is keyed UNIQUE(tag, model, corpus) so re-screening the same model replaces its
    2-text score instead of accumulating rows that `/ranking` would then have to choose
    between. `source_file` names the live path rather than a capture file, because there is
    no capture file: attribution for a discovered model lives in params_json, and
    `benchmark/runs-manifest.log` covers offline captures only.

    A model that answered only after a retry (`timed_out_once`) is recorded as `pending`, not
    `usable`: the evidence says it is alive on a good minute, which is a decision the user should
    make with his own eyes on the latency rather than something the job silently pins into his
    lane. A half-answered screen keeps the same honesty and takes the class of the answer it did
    not get -- a lane that timed out is `slow`, not `dead`.
    """
    answered = [r for r in results if r["ok"] and r["ai_probability"] is not None]
    usable = len(answered) == len(texts)
    separated = None
    if usable:
        by_label = {t["label"]: r["ai_probability"] for t, r in zip(texts, results)}
        separated = by_label.get("AI", 0.0) > by_label.get("HUMAN", 1.0)
    latency = sum(r["latency_ms"] for r in results)
    # How long the model took when it DID answer, which is the number the accept-or-refuse offer
    # is judged on: the summed cost of the job includes the attempt that timed out, and a model
    # that spent 180 s failing then 3 s replying is a fast model, not a slow one.
    answer_ms = max((r["latency_ms"] for r in answered), default=0)
    note_bits = [f"screen {len(answered)}/{len(texts)} texts answered"]
    if timed_out_once:
        note_bits.append("only after a retry: the first attempt timed out")
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
    # What a half-answered screen is blamed on is the class of the text it did NOT get: a lane
    # that timed out is `slow`, not `dead`.
    failed = next((r for r in results if not r["ok"]), None)
    _catalog_upsert(conn, provider_name, model_id,
                    ("pending" if timed_out_once else "usable") if usable
                    else (classify_attempt(failed) if failed else "slow"),
                    note, (answer_ms or latency) or None)
    if answered:
        ensure_models_row(
            conn, provider_id, model_id,
            f"auto-discovered {time.strftime('%Y-%m-%d')}: {note}. Screen only -- 2 texts, "
            f"scored in the ranking's probe tier at the engine's default cuts (no confidence "
            f"gate), so it is evidence the model answers, not a calibration.")
    return {"model": model_id, "usable": usable, "separated": separated,
            "answered": len(answered), "texts": len(texts), "note": note,
            "latency_ms": latency, "answer_ms": answer_ms,
            "timed_out_once": timed_out_once,
            "pending_decision": bool(usable and timed_out_once),
            "fast": bool(answered) and answer_ms <= FAST_ANSWER_MS,
            "run_id": run_id}


def _catalog_upsert(conn, provider_name: str, model_id: str, status: str,
                    note: str, latency_ms: int | None) -> None:
    """One row per (provider, id): what the last probe concluded, in its own words, and how long
    it took. `probe_status` is the vocabulary `/my/pool/options` and the Providers page read, so
    the five classes `classify_attempt` produces are exactly the five states a user can act on."""
    conn.execute(
        """INSERT INTO catalog_models (provider, model_id, listed, probe_status, probe_note, latency_ms)
           VALUES (?,?,1,?,?,?)
           ON CONFLICT(provider, model_id) DO UPDATE SET
             listed = 1, probe_status = excluded.probe_status,
             probe_note = excluded.probe_note, latency_ms = excluded.latency_ms,
             probed_at = datetime('now')""",
        (provider_name, model_id, status, note[:400], latency_ms),
    )


def store_no_answer(conn, provider_name: str, model_id: str, result: dict,
                    user_id: int | None, provider_id: int, status: str) -> dict:
    """Record a candidate that did not answer because the pool would not finish in time.

    `slow` and `busy` get the opposite treatment from a refusal, and the reason is that a
    timeout is not a fact about the model: the same gateway serves one model in 1.4 s and
    another in 269 s, and a 429 is a property of the key's window. So the row is written off for
    nobody -- it gets a `models` entry, which is what lets its owner pick it and gives it the
    deployment's own lane ceiling rather than a 90 s default, and the next job tries it again
    instead of freezing it for `DEAD_RETRY_DAYS`. What it does NOT get is a benchmark run: an
    unanswered text has no score, and `/ranking` would show a failed row as a measurement of a
    model that never said anything.
    """
    attempts = result.get("attempts") or 1
    note = (f"triage 0/1 texts answered: {result.get('error_code') or 'no answer'}"
            + (f" after {attempts} attempts" if attempts > 1 else ""))
    detail = (result.get("error_detail") or "").strip()
    if detail:
        note = f"{note} -- {detail[:160]}"
    _catalog_upsert(conn, provider_name, model_id, status, note, result.get("latency_ms"))
    ensure_models_row(
        conn, provider_id, model_id,
        f"auto-discovered {time.strftime('%Y-%m-%d')}: {note}. "
        f"{'Timed out' if status == 'slow' else 'The provider was busy'} at the screen's own "
        f"ceiling, which says nothing about whether this model judges well. Pickable, but "
        f"unmeasured: no text was scored, so it has no rank and no calibration.")
    return {"model": model_id, "usable": False, "answered": False, "status": status,
            "attempts": attempts, "note": note, "error_code": result.get("error_code"),
            "latency_ms": result.get("latency_ms"), "seen_by_user_id": user_id}


def store_dud(conn, provider_name: str, model_id: str, result: dict,
              user_id: int | None) -> dict:
    """Record a candidate that refused the text it was given.

    Deliberately neither a `benchmark_runs` row nor a `models` row. A model that refused has no
    score to display, and `/ranking` would show a 2-text run whose rows all failed as if it were
    a measurement; a `models` row would make it pinnable, which is how a delisted id ends up at
    the top of somebody's pool. What a refusal does deserve is its reason, kept here in the
    caller's own words: `http_410` (delisted), `no_choices` (advertised, not served) and
    `http_404` (a function id that no longer exists) are the three that justify writing an id off
    -- and they are the only ones that reach this function, because a timeout or a 429 is
    somebody else's queue and goes to `store_no_answer` instead.
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
            add_to_pool: bool = True,
            timeout_retries: int = DEFAULT_TIMEOUT_RETRIES) -> dict:
    """Execute one discovery job to completion. Returns the summary written to the row."""
    with db.session() as conn:
        db.discovery_update(conn, job_id, status="running", stage="fetching catalogue",
                            started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    ids, err = fetch_catalogue(base_url, key, auth_header, auth_prefix, catalog_timeout_s)
    summary = {"job_id": job_id, "provider": provider_name, "n_listed": len(ids),
               "n_screened": 0, "n_usable": 0, "n_tried": 0,
               "screened": [], "refused": [], "deferred": [], "error": err}
    if err:
        with db.session() as conn:
            db.discovery_update(conn, job_id, status="failed", stage="catalogue",
                                n_listed=0, error=err[:400],
                                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return summary

    cap = max(0, int(screen_limit))
    walk_cap = max(cap, int(triage_cap))
    native = engine.native_enabled()
    with db.session() as conn:
        free_tier = bool(conn.execute("SELECT free_tier FROM providers WHERE id = ?",
                                     (provider_id,)).fetchone()["free_tier"])
        # An in-process screen has no address to dial, so `url` stays empty and nothing in this
        # job reads `n8n_url`. `hosts` is the allowlist the engine will re-check the candidate's
        # endpoint against, computed once per job from the same rows /detect uses.
        url = "" if native else engine_url(conn)
        hosts = detect_hosts(conn, base_url, user_id) if native else None
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
        # text would be bought for a model we know does not work. It is asked through
        # `screen_text`, so a lane that ran out of time is asked again before any of the three
        # outcomes below can be recorded against the model.
        first = screen_text(url, base_url, model_id, key, texts[0]["text"], timeout_s,
                            timeout_retries, hosts)
        if not first["ok"]:
            kind = classify_attempt(first)
            if kind == "engine_down":
                # Nobody answered, so no candidate was ever asked and nothing was learned about
                # any of them. Writing this off as `dead` is how one stopped n8n retires a whole
                # catalogue for a week; `slow` would be gentler but still wrong, because a retry
                # spends two more minutes on a port where nothing listens. Stop and say what is
                # actually broken.
                why = (first.get("error_detail") or first.get("error_code") or "no answer")
                # Name the thing that is broken in the words this deployment can act on. On the
                # hosted path that is an address somebody has to bring back up; on an in-process
                # one there is no address, and a message ending in `at ` would send the reader
                # looking for a server that was never part of the install.
                msg = (f"detection engine at {url} is unreachable: {why}" if url
                       else f"detection engine did not answer: {why}")[:400]
                summary["error"] = msg
                with db.session() as conn:
                    db.discovery_update(conn, job_id, status="failed", stage="engine",
                                        n_listed=len(ids), n_screened=len(summary["screened"]),
                                        n_usable=summary["n_usable"], error=msg,
                                        finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
                return summary
            with db.session() as conn:
                if kind in ("slow", "busy"):
                    row = store_no_answer(conn, provider_name, model_id, first, user_id,
                                          provider_id, kind)
                else:
                    row = store_dud(conn, provider_name, model_id, first, user_id)
            (summary["deferred"] if kind in ("slow", "busy")
             else summary["refused"]).append(row)
            continue
        results = [first, screen_text(url, base_url, model_id, key, texts[1]["text"],
                                      timeout_s, timeout_retries, hosts)]
        # A screen that only got its answer on the second ask is evidence the id is alive on a
        # good minute -- which is a thing the user should decide about, watching the latency,
        # not a thing to silently pin into his lane.
        timed_out_once = any((r.get("attempts") or 1) > 1 for r in results)
        with db.session() as conn:
            row = store_screen(conn, provider_name, model_id, texts, results, user_id,
                               base_url, provider_id, timed_out_once=timed_out_once)
            if (row["usable"] and row.get("separated") is not False and not timed_out_once
                    and add_to_pool and user_id is not None):
                # Appended with source='auto', so it lands after everything the user placed
                # and can never overtake a rank they set by hand (db.pool_add's ON CONFLICT
                # DO NOTHING keeps an existing row where it is).
                #
                # `separated` is the guard, because `usable` is not what a pool entry is for.
                # Answering proves the id is alive; the pool decides which model judges the
                # user's texts, and a model that scored the AI screen text BELOW the human one
                # has already failed the only A/B discovery performs. Measured on 2026-10-20,
                # a non-separating vision model was pinned to position 1 and then answered
                # every Detect: a freshly generated AI poem came back HUMAN_LIKELY at 0.0 %
                # with confidence 1.0, where the calibrated lane scored the same bytes 0.82
                # AI_GENERATED, and the model's own record is wrong on 32 of 48 texts. The
                # screen row and the models row are still written above -- this only declines
                # to make a known confuser the lane of record.
                #
                # `timed_out_once` is the second half of the same rule: an id that answered only
                # after being asked twice is alive on a good minute, which is a decision the user
                # makes from the offer (`store_screen` records it as `pending`), not a fact the
                # job pins on his behalf.
                db.pool_add(conn, user_id, provider_name, model_id, source="auto")
                row["added_to_pool"] = True
            db.discovery_update(conn, job_id, n_screened=len(summary["screened"]) + 1)
        summary["screened"].append(row)
        summary["n_usable"] += 1 if row["usable"] else 0

    summary["n_screened"] = len(summary["screened"])
    deferred = len(summary["deferred"])
    with db.session() as conn:
        db.discovery_update(conn, job_id, status="done", n_listed=len(ids),
                            n_screened=summary["n_screened"], n_usable=summary["n_usable"],
                            stage=f"{summary['n_usable']}/{summary['n_screened']} usable of "
                                  f"{summary['n_tried']} candidates tried"
                                  + (f" ({deferred} still pickable: the pool timed out, "
                                     f"not the model)" if deferred else ""),
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
