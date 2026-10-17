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


def _stub_screen(monkeypatch, verdicts):
    """`screen_one_text` answers from `verdicts` per model id and records every call it was
    handed. An id missing from the map answers on both texts; a string value is a refusal."""
    calls: list[tuple[str, str]] = []

    def fake(url, base_url, model_id, key, text, timeout_s):
        calls.append((model_id, text[:20]))
        verdict = verdicts.get(model_id, 0.91)
        if isinstance(verdict, str):
            return {"ok": False, "error_code": verdict, "error_detail": "stubbed refusal",
                    "ai_probability": None, "confidence": None, "latency_ms": 900}
        return {"ok": True, "error_code": None, "error_detail": None,
                "ai_probability": verdict, "confidence": 0.8, "latency_ms": 1200}

    monkeypatch.setattr(discovery, "screen_one_text", fake)
    return calls


def _run(provider_id, monkeypatch, ids, screen_limit, triage_cap=None):
    kwargs = {"screen_limit": screen_limit}
    if triage_cap is not None:
        kwargs["triage_cap"] = triage_cap
    monkeypatch.setattr(discovery, "fetch_catalogue",
                        lambda base_url, key, *a, **k: (list(ids), ""))
    with db.session() as c:
        job_id = db.discovery_job(c, None, PROVIDER)
    return discovery.run_job(job_id, None, provider_id, PROVIDER, BASE_URL,
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

    def first_text_only(url, base_url, model_id, key, text, timeout_s):
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
