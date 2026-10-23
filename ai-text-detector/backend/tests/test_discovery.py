"""The screening walk, offline. Run from ai-text-detector/:
    python -m pytest backend/tests/test_discovery.py -q

No network, no engine, no provider budget: `fetch_catalogue` and `screen_one_text` are stubbed,
so what is under test is the *order the candidates are tried in and what each attempt costs*.
That is the part a live verifier cannot show honestly -- it spends a real key and only ever
sees whichever models the pool happened to answer for at that minute.

Why this file exists: on 2026-10-17 the live discovery job for nvidia listed 81 ids, screened
the first four in catalogue order (01-ai/yi-large, adept/fuyu-8b,
ai21labs/jamba-1.5-large-instruct, aisingapore/sea-lion-7b-instruct) and wrote `0/4 usable`.
The user was promised "we bring his models, ranked" and got nothing, because a provider's
catalogue is alphabetical and the delisted legacy models sort first.

No session is held open across `run_job`: the worker opens its own, and an outer write
transaction makes SQLite answer "database is locked" instead of testing anything.
"""
import os
import sqlite3
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-discovery-test-")
os.environ["DETECTOR_DB"] = os.path.join(_TMP, "disc.sqlite3")
os.environ["DETECTOR_SECRET"] = os.path.join(_TMP, "disc-secret.key")
os.environ["N8N_DB"] = os.path.join(_TMP, "no-such-n8n.sqlite")

import pytest  # noqa: E402

from backend import db, discovery  # noqa: E402

PROVIDER = "testprov"
BASE_URL = "https://integrate.api.nvidia.com/v1"


@pytest.fixture()
def provider_id():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    with db.session() as c:
        c.execute("INSERT INTO providers (name, base_url, free_tier, enabled) VALUES (?,?,1,1)",
                  (PROVIDER, BASE_URL))
        return c.execute("SELECT id FROM providers WHERE name = ?", (PROVIDER,)).fetchone()["id"]


def q(sql, args=()):
    """One short-lived read, so the assertions never sit on a lock the worker needs."""
    con = sqlite3.connect(db.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def _user() -> int:
    """A real user row, because the auto-pin only happens for a user the pool can belong to."""
    with db.session() as c:
        c.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                  ("pool-owner", "not-a-real-hash", "user"))
        return c.execute("SELECT id FROM users WHERE username = 'pool-owner'").fetchone()["id"]


def _stub_screen(monkeypatch, verdicts):
    """`screen_one_text` answers from `verdicts` per model id and records every call it was
    handed. An id missing from the map answers on both texts; a string value is a refusal; a
    two-element list answers the texts in order, which is the only way to write a model that
    tells the AI and the human screen text apart and one that has them backwards."""
    calls: list[tuple[str, str]] = []
    seen: dict[str, int] = {}

    def fake(url, base_url, model_id, key, text, timeout_s, hosts=None):
        calls.append((model_id, text[:20]))
        verdict = verdicts.get(model_id, 0.91)
        if isinstance(verdict, list):
            verdict = verdict[min(seen.get(model_id, 0), len(verdict) - 1)]
            seen[model_id] = seen.get(model_id, 0) + 1
        if isinstance(verdict, str):
            return {"ok": False, "error_code": verdict, "error_detail": "stubbed refusal",
                    "ai_probability": None, "confidence": None, "latency_ms": 900}
        return {"ok": True, "error_code": None, "error_detail": None,
                "ai_probability": verdict, "confidence": 0.8, "latency_ms": 1200}

    monkeypatch.setattr(discovery, "screen_one_text", fake)
    return calls


def _run(provider_id, monkeypatch, ids, screen_limit, triage_cap=None, user_id=None):
    kwargs = {"screen_limit": screen_limit}
    if triage_cap is not None:
        kwargs["triage_cap"] = triage_cap
    monkeypatch.setattr(discovery, "fetch_catalogue",
                        lambda base_url, key, *a, **k: (list(ids), ""))
    with db.session() as c:
        job_id = db.discovery_job(c, user_id, PROVIDER)
    return discovery.run_job(job_id, user_id, provider_id, PROVIDER, BASE_URL,
                             "sk-not-a-real-key", "Authorization", "Bearer ", **kwargs)


def test_a_model_that_never_answers_costs_one_call_and_the_walk_moves_on(provider_id, monkeypatch):
    """The first screen text doubles as triage: a candidate that cannot answer it has already
    told us it is no use, so buying it a second text spends the user's budget on a 404. The
    walk keeps going past it, so the `screen_limit` screens are models that answered rather
    than the four oldest ids in the provider's alphabetical list."""
    ids = ["01-ai/yi-large", "adept/fuyu-8b", "z-ai/live-one", "z-ai/live-two", "z-ai/live-three"]
    verdicts = {"01-ai/yi-large": "http_404", "adept/fuyu-8b": "no_choices"}
    calls = _stub_screen(monkeypatch, verdicts)
    summary = _run(provider_id, monkeypatch, ids, screen_limit=2)

    assert [m for m, _ in calls] == ["01-ai/yi-large", "adept/fuyu-8b",
                                     "z-ai/live-one", "z-ai/live-one",
                                     "z-ai/live-two", "z-ai/live-two"], calls
    assert summary["n_screened"] == 2 and summary["n_usable"] == 2, summary
    assert summary["n_tried"] == 4, summary


def test_a_refused_candidate_is_recorded_without_a_score(provider_id, monkeypatch):
    """`catalog_models` is where "this id does not answer" belongs. Writing a benchmark run for
    it would put a row in `/ranking` that looks like a measurement of a model that never
    answered, and a models row would make it pinnable. Both stay away from a dud; the note
    keeps the reason the engine actually gave."""
    _stub_screen(monkeypatch, {"old/dead": "http_410", "new/live": 0.88})
    _run(provider_id, monkeypatch, ["old/dead", "new/live"], screen_limit=1)

    rows = q("SELECT probe_status, probe_note FROM catalog_models "
             "WHERE provider = ? AND model_id = ?", (PROVIDER, "old/dead"))
    assert rows and rows[0]["probe_status"] == "dead", rows
    assert "http_410" in rows[0]["probe_note"], rows[0]
    assert q("SELECT COUNT(*) n FROM benchmark_runs WHERE model = 'old/dead'")[0]["n"] == 0
    assert q("SELECT COUNT(*) n FROM models WHERE model_id = 'old/dead'")[0]["n"] == 0
    assert q("SELECT COUNT(*) n FROM models WHERE model_id = 'new/live'")[0]["n"] == 1


def test_the_walk_stops_at_the_cap_when_nothing_answers(provider_id, monkeypatch):
    """An 81-id catalogue where nothing answers must not turn "verify your key" into 81 calls.
    The cap bounds the trying; `screen_limit` still bounds the buying."""
    ids = [f"legacy/{i}" for i in range(40)]
    verdicts = {m: "http_404" for m in ids}
    calls = _stub_screen(monkeypatch, verdicts)
    summary = _run(provider_id, monkeypatch, ids, screen_limit=2, triage_cap=6)

    assert len(calls) == 6, calls
    assert summary["n_tried"] == 6 and summary["n_screened"] == 0, summary
    assert q("SELECT COUNT(*) n FROM benchmark_runs")[0]["n"] == 0


def test_stop_walking_past_the_refusals_and_the_same_catalogue_yields_nothing(provider_id,
                                                                              monkeypatch):
    """The 2026-10-17 result reproduced on purpose.

    Cap the walk at the screen count -- which is what "screen the first four" was -- and the
    catalogue that has two live models in it yields `0 usable`, because the four candidates it
    is willing to touch are the four oldest ids. The walking half of this is
    `test_a_model_that_never_answers_costs_one_call_and_the_walk_moves_on`, on the same fixture
    and the same stub: the only difference between the two outcomes is the triage cap.
    """
    ids = ["01-ai/yi-large", "adept/fuyu-8b", "z-ai/live-one", "z-ai/live-two"]
    calls = _stub_screen(monkeypatch, {"01-ai/yi-large": "http_404", "adept/fuyu-8b": "no_choices"})
    stuck = _run(provider_id, monkeypatch, ids, screen_limit=2, triage_cap=2)

    assert [m for m, _ in calls] == ["01-ai/yi-large", "adept/fuyu-8b"], calls
    assert stuck["n_tried"] == 2, stuck
    assert stuck["n_screened"] == 0 and stuck["n_usable"] == 0, stuck
    assert q("SELECT COUNT(*) n FROM benchmark_runs")[0]["n"] == 0


def test_a_refusal_is_retried_on_a_week_not_on_every_run(provider_id):
    """Without this the walk spends its whole cap re-triaging the same twenty-odd delisted ids
    and never reaches the tail of the catalogue. With it a delisting that was really an outage
    is still noticed, seven days later."""
    with db.session() as c:
        for model, age, status in (("fresh/dead", "-1 day", "dead"),
                                   ("stale/dead", "-30 days", "dead"),
                                   ("known/usable", "-30 days", "usable"),
                                   ("never/seen", None, None)):
            if age is None:
                continue
            c.execute("""INSERT INTO catalog_models (provider, model_id, listed, probe_status, probed_at)
                         VALUES (?,?,1,?, datetime('now', ?))""",
                      (PROVIDER, model, status, age))
    with db.session() as c:
        got = {m: discovery.needs_screen(c, PROVIDER, m)
               for m in ("fresh/dead", "stale/dead", "known/usable", "never/seen")}
    assert got == {"fresh/dead": False, "stale/dead": True,
                   "known/usable": False, "never/seen": True}, got


def test_a_candidate_that_answers_once_still_completes_its_two_text_screen(provider_id, monkeypatch):
    """Triage must not quietly become a one-text screen: a model that answers text 1 is still
    judged on both texts the user approved, and a half-answered screen stays recorded as such
    (usable = 0) rather than being rewritten as a success."""
    calls: list[tuple[str, str]] = []

    def first_text_only(url, base_url, model_id, key, text, timeout_s, hosts=None):
        calls.append((model_id, text[:20]))
        answered = len(calls) == 1
        return {"ok": answered, "error_code": None if answered else "http_503",
                "error_detail": None if answered else "the pool is busy",
                "ai_probability": 0.9 if answered else None,
                "confidence": 0.8 if answered else None, "latency_ms": 1000}

    monkeypatch.setattr(discovery, "screen_one_text", first_text_only)
    summary = _run(provider_id, monkeypatch, ["half/live"], screen_limit=2)

    assert len(calls) == 2, calls
    assert summary["n_usable"] == 0, summary
    runs = q("SELECT n_rows FROM benchmark_runs WHERE model = 'half/live'")
    assert runs and runs[0]["n_rows"] == 2, runs


def test_a_screened_model_that_tells_the_two_texts_apart_is_added_to_the_pool(provider_id,
                                                                             monkeypatch):
    """The useful half of the auto-pin, stated so the guard below cannot be read as "never
    pin". A model that answers both texts AND puts the AI one above the human one has passed
    the only test discovery runs, so it goes into the pool behind whatever the user placed."""
    uid = _user()
    _stub_screen(monkeypatch, {"z-ai/separating": [0.90, 0.10]})
    summary = _run(provider_id, monkeypatch, ["z-ai/separating"], screen_limit=1, user_id=uid)

    row = summary["screened"][0]
    assert row["usable"] and row["separated"] is True, row
    assert q("SELECT model_id, source FROM user_model_pool WHERE user_id = ?", (uid,)) == [
        {"model_id": "z-ai/separating", "source": "auto"}], q(
        "SELECT model_id, source FROM user_model_pool WHERE user_id = ?", (uid,))


def test_a_model_cannot_tell_ai_from_human_is_never_pinned_into_the_lane(provider_id, monkeypatch):
    """The miss this guards: a `usable` screen only proves the id answers, and discovery writes
    the stronger fact into the very same row -- `separated` -- without reading it before
    pinning. Measured on 2026-10-20, `meta/llama-3.2-11b-vision-instruct` was auto-pinned to
    pool position 1 on a screen whose note reads "did NOT separate the AI text from the human
    one"; because the pool outranks the deployed default it then answered every Detect, and
    scored a freshly generated AI poem as HUMAN_LIKELY at 0.0 % with confidence 1.0. On the
    calibrated lane the identical text scored 0.82 AI_GENERATED, and on the full corpus the
    pinned model was wrong on 32 of 48 texts.

    So the row stays recorded (it is evidence the model answers, and `/ranking` shows it) and
    the pool is not touched: a model known to fail the one A/B test discovery has is not the
    model that decides what the user's texts are."""
    uid = _user()
    _stub_screen(monkeypatch, {"meta/inverted": [0.05, 0.95]})
    summary = _run(provider_id, monkeypatch, ["meta/inverted"], screen_limit=1, user_id=uid)

    row = summary["screened"][0]
    assert row["usable"] and row["separated"] is False, row
    assert q("SELECT COUNT(*) n FROM models WHERE model_id = 'meta/inverted'")[0]["n"] == 1
    assert q("SELECT COUNT(*) n FROM user_model_pool WHERE user_id = ?", (uid,))[0]["n"] == 0
    assert "added_to_pool" not in row, row


# ---------------------------------------------------------------- timeouts are not verdicts
#
# A screen that times out has learned something about the pool, not about the model: measured
# on this deployment, the same NVIDIA account serves one model in 1.4 s and another in 269 s,
# and the gateway itself 504s at ~302 s no matter what the client asks for. Treating that as
# "this id does not work" writes a model off on a property of somebody else's queue, freezes it
# for a week, and -- because the row that makes a model pickable is the one a dud does not
# write -- takes the choice away from the person whose key it is.


def test_a_timed_out_text_is_retried_before_the_model_is_written_off(provider_id, monkeypatch):
    """The retry comes first, every time. One refusal on a congested free pool is a coin flip
    about that minute, and the cheapest evidence that a candidate is alive is asking it again."""
    calls = _stub_screen(monkeypatch, {"slow/recovers": ["ENGINE_TIMEOUT", 0.90, 0.10]})
    summary = _run(provider_id, monkeypatch, ["slow/recovers"], screen_limit=1)

    assert [m for m, _ in calls] == ["slow/recovers"] * 3, calls
    row = summary["screened"][0]
    assert row["usable"] is True and row["timed_out_once"] is True, row
    assert summary["n_usable"] == 1, summary


def test_a_model_that_only_answered_after_a_timeout_is_offered_not_pinned(provider_id,
                                                                         monkeypatch):
    """Answering on the second try is evidence the id is alive, not evidence it is a good judge:
    the user decides whether it joins his pool, and the offer is what he decides on. Silent
    pinning is what #198 removed for a non-separating model, and a recovery on a retry is no
    stronger than the screen that got there on the first attempt."""
    uid = _user()
    _stub_screen(monkeypatch, {"slow/recovers": ["ENGINE_TIMEOUT", 0.90, 0.10]})
    summary = _run(provider_id, monkeypatch, ["slow/recovers"], screen_limit=1, user_id=uid)

    row = summary["screened"][0]
    assert row["pending_decision"] is True and "added_to_pool" not in row, row
    assert q("SELECT COUNT(*) n FROM user_model_pool WHERE user_id = ?", (uid,))[0]["n"] == 0
    cat = q("SELECT probe_status FROM catalog_models WHERE provider = ? AND model_id = ?",
            (PROVIDER, "slow/recovers"))
    assert cat[0]["probe_status"] == "pending", cat
    assert q("SELECT COUNT(*) n FROM models WHERE model_id = 'slow/recovers'")[0]["n"] == 1


def test_a_model_that_only_times_out_stays_pickable_and_is_tried_again(provider_id, monkeypatch):
    """No answer, so no score: a benchmark run would be an invented measurement and `/ranking`
    would show it. But the model itself is not retired -- the row that lets the user pick it is
    written, with the deployment's own lane ceiling rather than a guess about the model -- and
    the walk tries it again on the next run instead of freezing it for a week."""
    calls = _stub_screen(monkeypatch, {"slow/forever": "ENGINE_TIMEOUT"})
    summary = _run(provider_id, monkeypatch, ["slow/forever"], screen_limit=1)

    assert len(calls) == discovery.DEFAULT_TIMEOUT_RETRIES + 1, calls
    cat = q("SELECT probe_status, probe_note FROM catalog_models "
            "WHERE provider = ? AND model_id = ?", (PROVIDER, "slow/forever"))
    assert cat[0]["probe_status"] == "slow", cat
    assert "ENGINE_TIMEOUT" in cat[0]["probe_note"], cat
    assert q("SELECT COUNT(*) n FROM models WHERE model_id = 'slow/forever'")[0]["n"] == 1
    assert q("SELECT COUNT(*) n FROM benchmark_runs WHERE model = 'slow/forever'")[0]["n"] == 0
    assert summary["n_screened"] == 0 and summary["n_usable"] == 0, summary
    with db.session() as c:
        assert discovery.needs_screen(c, PROVIDER, "slow/forever") is True


def test_a_busy_pool_is_not_frozen_like_a_delisting(provider_id, monkeypatch):
    """429 and 503 are the provider's queue, and the shipped pool already refuses to call them
    a model's death (pool.TRANSIENT_MARKERS). Discovery must use the same vocabulary or a key
    that hit its daily cap writes off every id it touched for a week."""
    for model, code in (("pool/rate", "http_429"), ("pool/capacity", "http_503")):
        _stub_screen(monkeypatch, {model: code})
        _run(provider_id, monkeypatch, [model], screen_limit=1)
        cat = q("SELECT probe_status FROM catalog_models WHERE provider = ? AND model_id = ?",
                (PROVIDER, model))
        assert cat[0]["probe_status"] == "busy", (model, cat)
        assert q("SELECT COUNT(*) n FROM models WHERE model_id = ?", (model,))[0]["n"] == 1
        with db.session() as c:
            assert discovery.needs_screen(c, PROVIDER, model) is True, model


def test_an_unreachable_engine_is_not_recorded_against_the_candidate(provider_id, monkeypatch):
    """Nothing was asked, so nothing was learned about any model. Before this, a stopped n8n
    made every candidate in the catalogue look delisted for a week -- and the retry the test
    above adds would have spent two more minutes of socket hang per candidate on a port where
    nothing answers. The job says what is wrong and stops."""
    calls = _stub_screen(monkeypatch, {"any/model": "ENGINE_UNREACHABLE"})
    summary = _run(provider_id, monkeypatch, ["any/model", "other/model"], screen_limit=1)

    assert [m for m, _ in calls] == ["any/model"], calls
    assert q("SELECT COUNT(*) n FROM catalog_models")[0]["n"] == 0
    assert q("SELECT COUNT(*) n FROM models WHERE model_id = 'any/model'")[0]["n"] == 0
    assert "unreachable" in (summary["error"] or "").lower(), summary
    assert summary["refused"] == [], summary


def test_a_read_timeout_on_the_engine_is_named_as_a_timeout(provider_id, monkeypatch):
    """The classifier can only tell slow from gone if the client says which one happened, and
    urllib's words for both are `timed out`: on this host a connect to a port where nothing
    answers hangs for the whole ceiling instead of refusing (measured 2026-10-16). Discovery
    therefore discriminates the same way /detect does -- one bare handshake after the failure
    -- instead of stamping every slow lane `ENGINE_UNREACHABLE` and letting it be read as gone."""
    import urllib.error

    def raising(*a, **k):
        raise urllib.error.URLError(OSError("timed out"))

    monkeypatch.setattr(discovery.urllib.request, "urlopen", raising)
    monkeypatch.setattr(discovery, "_engine_listening", lambda url, probe_s=2.0: True)
    got = discovery.screen_one_text("http://127.0.0.1:5678/webhook/x", BASE_URL,
                                    "m/any", "k", "some text", 5.0)
    assert got["error_code"] == "ENGINE_TIMEOUT", got

    monkeypatch.setattr(discovery, "_engine_listening", lambda url, probe_s=2.0: False)
    gone = discovery.screen_one_text("http://127.0.0.1:5678/webhook/x", BASE_URL,
                                     "m/any", "k", "some text", 5.0)
    assert gone["error_code"] == "ENGINE_UNREACHABLE", gone


# ------------------------------------------------- the engine a local install already has
#
# A packaged exe answers /detect inside its own process -- it holds the model rows, the decrypted
# keys and the text. Discovery used to POST to the webhook anyway, so on the one install the user
# is most likely to run, "verify my key" opened a socket to :5678, got nothing, and recorded
# `ENGINE_UNREACHABLE` for every candidate in the catalogue: a screen that reported the whole
# provider pool dead on a machine that had just scored a paragraph by itself.

def test_a_local_screen_asks_the_engine_in_this_process(provider_id, monkeypatch):
    seen = {}

    def fake_detect(payload, headers=None, timeout=None, allowed_hosts=None, **kw):
        seen.update(payload=payload, headers=headers, timeout=timeout, hosts=allowed_hosts)
        return 200, {"ai_probability": 0.82, "confidence": 0.9,
                     "evidence": {"models": [{"lane": "primary", "ok": True}]}}

    def no_socket(*a, **k):
        raise AssertionError("a local screen must not open a socket to an engine it already has")

    monkeypatch.setenv("DETECTION_ENGINE", "python")
    monkeypatch.setattr(discovery.engine, "detect", fake_detect)
    monkeypatch.setattr(discovery.urllib.request, "urlopen", no_socket)

    got = discovery.screen_one_text("", BASE_URL, "m/any", "secret-key", "some text", 30.0,
                                   hosts=("integrate.api.nvidia.com",))

    assert got["ok"] and got["ai_probability"] == 0.82, got
    # the same lane contract the hosted screen sends: measure this model, at its own cuts
    assert seen["payload"]["lane_config"] == {"primary": {"base_url": BASE_URL, "model": "m/any"},
                                             "secondary": {"enabled": False}}
    assert "tuning" not in seen["payload"]
    assert seen["headers"] == {"x-primary-api-key": "secret-key"}
    assert seen["timeout"] == 30.0
    assert seen["hosts"] == ("integrate.api.nvidia.com",)


def test_a_local_screen_blames_the_candidate_not_the_engine(provider_id, monkeypatch):
    """The candidate's own leg is the only thing a screen may read, and an in-process answer that
    carries no `ENGINE_*` code must not be able to stop the job as `engine_down`: the engine is
    this process, so a leg that failed is a provider that failed, and the walk has to keep going
    and tell that model's id apart from the twenty-three behind it."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    monkeypatch.setattr(discovery.engine, "detect", lambda *a, **k: (200, {
        "classification": "DETECTION_UNAVAILABLE", "ai_probability": None, "confidence": None,
        "evidence": {"models": [{"lane": "primary", "ok": False, "model": "m/any",
                                 "error_code": "unreachable", "error_detail": "connection refused"}]},
    }))
    got = discovery.screen_one_text("", BASE_URL, "m/any", "k", "some text", 30.0, hosts=())
    assert not got["ok"] and got["error_code"] == "unreachable", got
    assert discovery.classify_attempt(got) != "engine_down", got

    monkeypatch.setattr(discovery.engine, "detect", lambda *a, **k: (200, {
        "classification": "AI_GENERATED", "ai_probability": 0.9, "confidence": 0.9,
        "evidence": {"models": [{"lane": "secondary", "ok": True, "model": "m/other"}]},
    }))
    other = discovery.screen_one_text("", BASE_URL, "m/any", "k", "some text", 30.0, hosts=())
    assert not other["ok"], other


def test_the_local_allowlist_names_the_endpoint_being_screened(provider_id):
    """A key under verification may not be saved yet, and an owner's own gateway is not in the
    deployment's list, so the host this job dials has to be in the list it hands the engine.
    Without it the screen answers `LANE_CONFIG_INVALID` about the gateway the user just typed in,
    and the engine's re-check -- which exists to stop a lane naming a host nobody approved -- reads
    as a model refusal."""
    with db.session() as c:
        hosts = discovery.detect_hosts(c, "https://gateway.example.internal:8000/v1", None)
    assert "gateway.example.internal" in hosts, hosts
    # the deployment's own list still applies: widening for one screen is not a bypass
    assert "integrate.api.nvidia.com" in hosts, hosts


# ------------------------------------------------- the vocabulary an older build wrote
#
# A catalogue row is the only place an install remembers why a model has no rank -- and the rows
# written before the timeout/refusal split use words this code never produces (`timeout`,
# `rate_limited`, `answered`, `not_found`). The new picker lists `models` rows, and the old build
# wrote no `models` row for a timeout, so in an existing install the models that merely ran out of
# time are absent from every surface: not "unmeasured but pickable", simply invisible. This
# migration re-reads those rows under the shipped rule and gives back what the old build should
# have written, without spending a call on anybody.

def _legacy(model_id: str, status: str, note: str | None = None) -> None:
    """One catalogue row exactly as the older build left it: its own word for the outcome, and no
    `models` row -- which is the defect, not a detail of the fixture."""
    with db.session() as c:
        c.execute("""INSERT INTO catalog_models (provider, model_id, listed, probe_status,
                                                probe_note, latency_ms)
                     VALUES (?,?,1,?,?,?)""",
                  (PROVIDER, model_id, status,
                   note if note is not None else f"triage 0/1 texts answered: {status}", 1200))


def test_a_legacy_timeout_row_becomes_slow_and_pickable(provider_id):
    _legacy("asleep/timeout", "timeout")
    _legacy("asleep/busy", "rate_limited")
    _legacy("asleep/answered", "answered", "p=0.98 finish=stop")
    with db.session() as c:
        discovery.migrate_probe_vocabulary(c)

    for model, want in (("asleep/timeout", "slow"), ("asleep/busy", "busy"),
                        ("asleep/answered", "usable")):
        cat = q("SELECT probe_status s, probe_note n FROM catalog_models "
                "WHERE provider = ? AND model_id = ?", (PROVIDER, model))
        assert cat[0]["s"] == want, (model, cat)
        rows = q("SELECT * FROM models WHERE provider_id = ? AND model_id = ?",
                 (provider_id, model))
        assert len(rows) == 1, (model, rows)
        assert rows[0]["enabled"] == 1, model
        # What it did not get is as important as what it did: no measurement, no lane, and the
        # deployment's own ceiling rather than the 90 s default that made it look dead.
        assert rows[0]["measured"] == 0, model
        assert rows[0]["lane"] is None, model
        assert rows[0]["timeout_ms"] == discovery.DISCOVERED_TIMEOUT_MS, model
        assert want in (rows[0]["notes"] or ""), (model, rows[0]["notes"])


def test_a_legacy_refusal_stays_written_off_and_earns_no_row(provider_id):
    """`not_found` is the old build's word for the id being gone, which is exactly the class the
    shipped rule freezes. Backfilling those would put delisted ids at the top of someone's pool,
    and re-asking them would spend a call on a 404 that answers the same way twice."""
    _legacy("gone/not_found", "not_found")
    _legacy("gone/bad_request", "http_400")
    with db.session() as c:
        discovery.migrate_probe_vocabulary(c)
    for model in ("gone/not_found", "gone/bad_request"):
        assert q("SELECT probe_status s FROM catalog_models WHERE model_id = ?",
                 (model,))[0]["s"] == "dead", model
        assert q("SELECT COUNT(*) n FROM models WHERE model_id = ?", (model,))[0]["n"] == 0
    with db.session() as c:
        for model in ("gone/not_found", "gone/bad_request"):
            assert discovery.needs_screen(c, PROVIDER, model) is False, model


def test_a_legacy_answer_without_a_probability_is_a_refusal_not_a_result(provider_id):
    """`answered` was the old import's word for "HTTP 200", and 200 is not the same fact as "this
    model judged the text". Measured in the installed database on 2026-10-20: of the ids the old
    build called `answered`, some carry `p=0.98 finish=stop` in their own note and some carry a
    safety label, a truncated `finish=length` ramble or a table of markup -- nothing the parser
    can read. The live path names that class `no_choices` and writes it off; backfilling it as
    `usable` would hand the user a card for a model that has never produced a score, and freeze it
    from ever being re-asked."""
    _legacy("answered/score", "answered", "p=0.3 finish=stop")
    _legacy("answered/empty", "answered", "no string content finish=length")
    _legacy("answered/safety", "answered", "User Safety: safe finish=stop")
    with db.session() as c:
        discovery.migrate_probe_vocabulary(c)
    assert q("SELECT probe_status s FROM catalog_models WHERE model_id = 'answered/score'",
             )[0]["s"] == "usable"
    for model in ("answered/empty", "answered/safety"):
        assert q("SELECT probe_status s FROM catalog_models WHERE model_id = ?",
                 (model,))[0]["s"] == "dead", model
        assert q("SELECT COUNT(*) n FROM models WHERE model_id = ?", (model,))[0]["n"] == 0


def test_the_migration_runs_once_and_never_outvotes_a_later_decision(provider_id):
    _legacy("asleep/timeout", "timeout")
    with db.session() as c:
        assert discovery.migrate_probe_vocabulary(c)["rows_added"] == 1
        # The user says no to it afterwards -- a `declined` row is his decision, not a stale word.
        c.execute("UPDATE catalog_models SET probe_status = 'declined' WHERE model_id = ?",
                  ("asleep/timeout",))
    with db.session() as c:
        assert discovery.migrate_probe_vocabulary(c)["reclassified"] == 0
    assert q("SELECT probe_status s FROM catalog_models WHERE model_id = ?",
             ("asleep/timeout",))[0]["s"] == "declined"


def test_a_models_row_the_admin_disabled_is_not_switched_back_on(provider_id):
    """A `models` row with `enabled = 0` is somebody's choice to take that lane out of service.
    The migration's job is to create the rows the old build forgot, not to reopen a row that was
    closed on purpose -- so it must leave `enabled` exactly where the admin put it."""
    _legacy("asleep/timeout", "timeout")
    with db.session() as c:
        c.execute("INSERT INTO models (provider_id, model_id, display_name, role, enabled, measured) "
                  "VALUES (?,?,'x','either',0,0)", (provider_id, "asleep/timeout"))
        discovery.migrate_probe_vocabulary(c)
    row = q("SELECT enabled, measured FROM models WHERE model_id = ?", ("asleep/timeout",))[0]
    assert row["enabled"] == 0, row
