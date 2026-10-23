"""The using-pool, model health, auto-discovery and failover. Run from ai-text-detector/:
    python -m pytest backend/tests/test_pool.py -q

Same throwaway-DB / stubbed-network discipline as test_auth.py: no real provider call, no
live n8n database, no printed key. Every "model answered" here is a stubbed return value,
so what is under test is the bookkeeping -- who is marked down, who is served next, what a
screen writes -- and not a network nobody can replay.
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-pool-test-")
os.environ["DETECTOR_DB"] = os.path.join(_TMP, "pool.sqlite3")
os.environ["DETECTOR_SECRET"] = os.path.join(_TMP, "pool-secret.key")
os.environ["N8N_DB"] = os.path.join(_TMP, "no-such-n8n.sqlite")
os.environ["ADMIN_USERNAME"] = "root"
os.environ["ADMIN_PASSWORD"] = "rootpass123"

import pytest  # noqa: E402

from backend import db, discovery, pool  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.tests._session import arm_csrf, fresh_throttle  # noqa: E402

TEXT = "This is a long enough sample of text for the detector to accept it."
NVIDIA = "https://integrate.api.nvidia.com/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


@pytest.fixture()
def app():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    a = create_app()
    a.config["TESTING"] = True
    a.config["AUTH_FORCE"] = True
    return fresh_throttle(a)


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield arm_csrf(c)


@pytest.fixture()
def conn(app):
    with db.session() as c:
        yield c


def _register(client, handle, password="userpass123"):
    """Sign up the way the shipped form does -- name, email, password typed twice.

    `handle` still names the account, because an email of `handle@example.org` derives the
    legacy username `handle`, which is what these tests read back.
    """
    body = {"name": f"Pool {handle.title()}", "email": f"{handle}@example.org",
            "password": password, "confirm": password}
    r = client.post("/auth/register", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["user"]


def _admin(client):
    # `root` is the env-seeded admin and has no email, so it signs in by its legacy handle.
    r = client.post("/auth/login", json={"identifier": "root", "password": "rootpass123"})
    assert r.status_code == 200, r.get_json()


def _seed_providers(client):
    """nvidia + openrouter, both in the default allowed_llm_hosts."""
    _admin(client)
    nv = client.post("/providers", json={"name": "nvidia", "base_url": NVIDIA}).get_json()["id"]
    orr = client.post("/providers", json={"name": "openrouter", "base_url": OPENROUTER}).get_json()["id"]
    client.post("/models", json={"provider_id": nv, "model_id": "deployed/primary", "lane": "primary"})
    return nv, orr


# --------------------------------------------------------------- classify / health

def test_classify_separates_busy_from_gone():
    assert pool.classify(True, None) == "ok"
    # A rate limit is the provider's queue, not the model's death.
    assert pool.classify(False, "429") == "transient"
    assert pool.classify(False, "rate_limit_exceeded") == "transient"
    assert pool.classify(False, None, "upstream is overloaded") == "transient"
    # A lane that ran out of time is the same kind of "not the model's fault".
    assert pool.classify(False, "ENGINE_TIMEOUT") == "transient"
    assert pool.classify(False, "http_504") == "transient"
    assert pool.classify(False, None, "read operation timed out") == "transient"
    # A delisting (NVIDIA's 410 for kimi-k3) and a 404 function-id are both "gone".
    assert pool.classify(False, "410") == "dead"
    assert pool.classify(False, "model_not_found") == "dead"
    assert pool.classify(False, "json_parse_failed") == "dead"


def test_three_consecutive_failures_mark_a_model_down_and_one_success_clears_it(conn):
    db.health_note(conn, "nvidia", "m/a", True)
    assert db.health_get(conn, "nvidia", "m/a")["state"] == "ok"
    assert pool.note_attempt(conn, "nvidia", "m/a", False, "404") == "degraded"
    assert pool.note_attempt(conn, "nvidia", "m/a", False, "404") == "degraded"
    assert pool.note_attempt(conn, "nvidia", "m/a", False, "404") == "down"
    # Health is "is it answering now", not a lifetime record: one answer restores the rank.
    assert pool.note_attempt(conn, "nvidia", "m/a", True) == "ok"
    row = db.health_get(conn, "nvidia", "m/a")
    assert row["consecutive_failures"] == 0
    assert row["last_error"] == ""


def test_a_busy_streak_never_reaches_down(conn):
    # OpenRouter's free cap is per account, so a spent key 429s on EVERY model at once.
    # Demoting the whole catalogue would empty the pool for a reason that is not the models'.
    for _ in range(10):
        state = pool.note_attempt(conn, "openrouter", "m/free:free", False, "429")
    assert state == "degraded"
    assert db.health_get(conn, "openrouter", "m/free:free")["consecutive_failures"] == 10


def test_a_timeout_streak_never_reaches_down(conn):
    """The other half of the busy rule, and the one that used to bite: a lane that ran out of
    time is a pool at a minute, not a model that stopped existing. Measured on this deployment,
    the same NVIDIA account serves one model in 1.4 s and another in 269 s, and the gateway 504s
    at ~302 s whatever the client asks for -- so counting three slow detections as three dead
    ones demoted the model, re-ranked the user's pool, and took it out of the failover chain for
    a reason that has nothing to do with it. It still counts (a lane that is always too slow is
    not usable) but it can never reach `down`."""
    for code, detail in (("ENGINE_TIMEOUT", None), ("http_504", None),
                         ("timeout", None), (None, "read operation timed out")):
        state = pool.note_attempt(conn, "nvidia", "slow/model", False, code, detail)
        assert state == "degraded", (code, detail, state)
    assert db.health_get(conn, "nvidia", "slow/model")["consecutive_failures"] == 4


def test_health_is_keyed_per_endpoint_not_per_model_id(conn):
    pool.note_attempt(conn, "nvidia", "shared/id", False, "404")
    pool.note_attempt(conn, "nvidia", "shared/id", False, "404")
    pool.note_attempt(conn, "nvidia", "shared/id", False, "404")
    assert db.health_get(conn, "nvidia", "shared/id")["state"] == "down"
    # The same id served from another endpoint is a different fate.
    assert db.health_get(conn, "openrouter", "shared/id") is None


# ------------------------------------------------------------------- order / ranks

def _pool_rows(conn, user_id, specs):
    for prov, mid in specs:
        db.pool_add(conn, user_id, prov, mid)


def test_effective_order_sinks_only_down_models_and_keeps_the_users_ranking(conn):
    _pool_rows(conn, 1, [("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c"), ("nvidia", "d")])
    db.health_note(conn, "nvidia", "b", False)
    db.health_note(conn, "nvidia", "b", False)
    db.health_note(conn, "nvidia", "b", False)   # down
    db.health_note(conn, "nvidia", "c", False)   # degraded, one failure only
    rows = pool.effective_order(db.pool_list(conn, 1))
    got = [r["model_id"] for r in rows]
    # 'b' is down so it goes last; 'c' is merely degraded and does NOT move -- one 504 on a
    # congested free pool is not a delisting, and demoting on a blip would make the order
    # jitter between two identical requests.
    assert got == ["a", "c", "d", "b"]


def test_readding_a_model_never_moves_it(conn):
    db.pool_add(conn, 1, "nvidia", "a")
    db.pool_add(conn, 1, "nvidia", "b")
    db.pool_add(conn, 1, "nvidia", "a")  # already there
    rows = db.pool_list(conn, 1)
    assert [(r["model_id"], r["position"]) for r in rows] == [("a", 0), ("b", 1)]


def test_pool_set_positions_writes_the_drag_and_keeps_unnamed_rows(conn):
    _pool_rows(conn, 1, [("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c")])
    db.pool_set_positions(conn, 1, [("nvidia", "c"), ("nvidia", "a")])
    got = [(r["model_id"], r["position"], r["source"]) for r in db.pool_list(conn, 1)]
    # 'b' was not named by the drag: it is appended, not deleted.
    assert got == [("c", 0, "user"), ("a", 1, "user"), ("b", 2, "user")]


def test_next_candidates_skips_tried_and_down(conn):
    _pool_rows(conn, 1, [("nvidia", "a"), ("nvidia", "b"), ("nvidia", "c")])
    for _ in range(3):
        db.health_note(conn, "nvidia", "c", False)
    got = pool.next_candidates(conn, 1, {"a"})
    assert got == [{"provider": "nvidia", "model": "b"}]


def test_pool_view_reports_a_demotion_as_one(conn):
    _pool_rows(conn, 1, [("nvidia", "a"), ("nvidia", "b")])
    for _ in range(3):
        db.health_note(conn, "nvidia", "a", False)
    view = pool.pool_view(conn, 1)
    assert [v["model"] for v in view] == ["b", "a"]
    assert view[1]["your_rank"] == 1 and view[1]["position"] == 2 and view[1]["demoted"] is True
    assert view[0]["demoted"] is False


# ------------------------------------------------------------------ note_detection

def _body(primary_ok, code=None, secondary_skipped=True):
    legs = [{"lane": "primary", "model": "m/one", "ok": primary_ok, "skipped": False,
             "error_code": code}]
    if not secondary_skipped:
        legs.append({"lane": "secondary", "model": "m/two", "ok": True, "skipped": False})
    return {"evidence": {"models": legs},
            "config_used": {"primary": {"base_url": NVIDIA},
                            "secondary": {"base_url": NVIDIA}}}


def test_note_detection_records_the_lane_that_failed(conn):
    def provider_of(base_url):
        return "nvidia" if "nvidia.com" in (base_url or "") else ""
    failed = pool.note_detection(conn, _body(False, "404"), provider_of)
    assert failed == [{"lane": "primary", "model": "m/one", "provider": "nvidia",
                       "kind": "dead", "error_code": "404"}]
    assert db.health_get(conn, "nvidia", "m/one")["consecutive_failures"] == 1


def test_note_detection_ignores_a_lane_that_was_never_called(conn):
    def provider_of(base_url):
        return "nvidia"
    body = _body(True)
    body["evidence"]["models"].append(
        {"lane": "secondary", "model": "m/two", "ok": False, "skipped": True,
         "error_code": "not_called"})
    assert pool.note_detection(conn, body, provider_of) == []
    # The tie-break lane only runs on a gray-zone text. Counting "not called" would demote a
    # perfectly healthy secondary for never being asked.
    assert db.health_get(conn, "nvidia", "m/two") is None


def test_note_detection_gives_an_unknown_host_no_health_row(conn):
    body = _body(False, "404")
    body["config_used"]["primary"]["base_url"] = "https://evil.example/v1"
    failed = pool.note_detection(conn, body, lambda u: "")
    assert failed[0]["provider"] == ""
    assert conn.execute("SELECT COUNT(*) AS n FROM model_health").fetchone()["n"] == 0


def test_note_detection_counts_the_tie_break_legs_that_never_reached_evidence(conn):
    """A walk dials N models and reports one lane, so the refusals in the middle are visible only in
    `secondary_walk.attempts` -- and a model that 404s three times unseen stays at the top of the
    pool. The leg that ran last is BOTH the final attempt and the answer's secondary lane, so it has
    to be counted once."""
    def provider_of(base_url):
        return "nvidia"
    body = _body(True, code="404", secondary_skipped=False)
    body["evidence"]["models"][1].update({"ok": False, "error_code": "404"})
    body["secondary_walk"] = {"from": "m/dead1", "switched_to": None, "attempts": [
        {"model": "m/dead1", "base_url": NVIDIA, "ok": False, "error_code": "404"},
        {"model": "m/dead2", "base_url": NVIDIA, "ok": False, "error_code": "404"},
        {"model": "m/two", "base_url": NVIDIA, "ok": False, "error_code": "404"}]}
    failed = pool.note_detection(conn, body, provider_of)
    assert [f["model"] for f in failed] == ["m/two", "m/dead1", "m/dead2"]
    assert db.health_get(conn, "nvidia", "m/dead1")["consecutive_failures"] == 1
    assert db.health_get(conn, "nvidia", "m/dead2")["consecutive_failures"] == 1
    assert db.health_get(conn, "nvidia", "m/two")["consecutive_failures"] == 1


# ------------------------------------------------------------------- the routes

def test_pool_routes_require_a_session(client):
    assert client.get("/my/pool").status_code == 401
    assert client.post("/my/pool", json={"provider": "nvidia", "model": "a"}).status_code == 401
    assert client.get("/my/discover").status_code == 401


def test_a_user_adds_reorders_and_removes_their_own_pool(client):
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    for m in ("m/a", "m/b", "m/c"):
        r = client.post("/my/pool", json={"provider": "nvidia", "model": m})
        assert r.status_code == 201, r.get_json()
    assert [p["model"] for p in r.get_json()["pool"]] == ["m/a", "m/b", "m/c"]

    r = client.post("/my/pool/order", json={"order": [
        {"provider": "nvidia", "model": "m/c"}, {"provider": "nvidia", "model": "m/a"}]})
    assert r.status_code == 200, r.get_json()
    assert [p["model"] for p in r.get_json()["pool"]] == ["m/c", "m/a", "m/b"]

    r = client.delete("/my/pool", json={"provider": "nvidia", "model": "m/a"})
    assert r.status_code == 200
    assert [p["model"] for p in r.get_json()["pool"]] == ["m/c", "m/b"]
    assert client.delete("/my/pool", json={"provider": "nvidia", "model": "m/a"}).status_code == 404


def test_a_pool_entry_for_an_unknown_or_disallowed_endpoint_is_refused(client):
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    assert client.post("/my/pool", json={"provider": "nope", "model": "m/a"}).status_code == 400
    r = client.post("/my/pool", json={"provider": "nvidia", "model": "bad model id!"})
    assert r.status_code == 400
    assert client.post("/my/pool", json={"provider": "nvidia"}).status_code == 400


def test_one_users_pool_is_not_another_users(client):
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    client.post("/my/pool", json={"provider": "nvidia", "model": "m/alice"})
    client.post("/auth/logout")
    _register(client, "bob")
    client.post("/auth/login", json={"username": "bob", "password": "userpass123"})
    assert client.get("/my/pool").get_json()["pool"] == []


def test_discover_needs_the_callers_own_key(client):
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    r = client.post("/my/discover", json={"provider_id": 1})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "NO_USER_KEY"


def test_discover_says_when_the_worker_is_not_running(client):
    """A queued job nobody will pick up must not be reported as queued."""
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    with db.session() as c:
        pid = c.execute("SELECT id FROM providers WHERE name = 'nvidia'").fetchone()["id"]
        db.add_user_key(c, alice_id(c, "alice"), pid, "nv-key-not-a-secret-1234567890")
    r = client.post("/my/discover", json={"provider_id": pid})
    assert r.status_code == 503
    assert r.get_json()["error_code"] == "DISCOVERY_DISABLED"
    assert r.get_json()["queued"] is False


def alice_id(conn, username):
    return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]


def test_ranking_exposes_pool_membership_and_health(client):
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    client.post("/my/pool", json={"provider": "nvidia", "model": "deployed/primary"})
    body = client.get("/ranking").get_json()
    assert "pool" in body
    row = next((r for r in body["ranked"] if r["model"] == "deployed/primary"), None)
    if row is not None:  # only ranked once a benchmark run exists; membership is the claim
        assert row["in_pool"] is True and row["your_rank"] == 1
    assert [p["model"] for p in body["pool"]] == ["deployed/primary"]


# ------------------------------------------------------------------ /detect failover
#
# These drive the real route with a stubbed transport rather than calling the guards directly,
# because a wrong failover decision costs the user a full extra detection (up to 270 s on the
# shipped lane ceiling) and the guard has to be measured against the bodies `forward()`
# actually returns -- not against a hand-written 200 that never happens.

DEAD_ENGINE = {
    "error": "n8n unreachable at http://localhost:5678/webhook/ai-detect-ai",
    "error_code": "ENGINE_UNREACHABLE", "method": "ENGINE_UNREACHABLE",
    "classification": "DETECTION_UNAVAILABLE", "review_required": False,
}
SLOW_ENGINE = {
    "error": "n8n accepted the connection but did not answer within 120 s",
    "error_code": "ENGINE_TIMEOUT", "method": "ENGINE_TIMEOUT",
    "classification": "DETECTION_UNAVAILABLE", "review_required": False,
}


def _model_refused(model):
    """What the engine returns when the *model* will not answer: NVIDIA's 404 function-id for a
    model its own /v1/models advertises. A real body, copied from the deployed primary's failure
    on 2026-10-16 -- this is the case failover exists for."""
    return {
        "classification": "DETECTION_UNAVAILABLE", "method": "ALL_MODELS_FAILED",
        "review_required": False, "ai_probability": None,
        "evidence": {"models": [{"lane": "primary", "model": model, "ok": False,
                                 "error_code": "http_404",
                                 "error_detail": "Function id ... is not found"}]},
    }


def _answered(model, p=0.93):
    return {
        "classification": "AI_GENERATED", "ai_probability": p, "confidence": 0.9,
        "method": "SINGLE_MODEL_PRIMARY_AI", "review_required": False,
        "evidence": {"models": [{"lane": "primary", "model": model, "ok": True}]},
        "config_used": {"primary": {"model": model}},
    }


def _stub_forward_seq(monkeypatch, answers, timeouts=None):
    """forward() answers from `answers` in call order (the last one repeats); returns the list
    of payloads it was handed, so a test can assert on how many models were bought.

    Pass `timeouts=[]` to also see the seconds each call was allowed. That number is the
    failover budget, and it is a property of the request, not of whichever model happens to sit
    at the top of the pool -- which is the thing test_a_fast_refusal_... below pins down."""
    import backend.app as app_mod
    calls: list = []

    def fake(url, payload, headers, timeout):
        calls.append(payload)
        if timeouts is not None:
            timeouts.append(timeout)
        status, body = answers[min(len(calls) - 1, len(answers) - 1)]
        return status, body, 50.0

    monkeypatch.setattr(app_mod, "forward", fake)
    return calls


def _pool(client, models, rows=None):
    """alice, signed in, with a using-pool in exactly this order.

    `rows` writes extra `models` rows before the pool is built: {"m/spare": {"timeout_ms":
    240000}}. A pool member with no row is called at the deployed lane's ceiling, so any test
    about how long a call gets needs the rows to say so."""
    nv, _orr = _seed_providers(client)
    for model_id, fields in (rows or {}).items():
        r = client.post("/models", json={"provider_id": nv, "model_id": model_id, **fields})
        assert r.status_code in (200, 201), r.get_json()
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    for m in models:
        r = client.post("/my/pool", json={"provider": "nvidia", "model": m})
        assert r.status_code == 201, r.get_json()


def _models_called(calls):
    return [c["lane_config"]["primary"]["model"] for c in calls]


def test_a_dead_engine_does_not_buy_two_more_calls_from_other_models(client, monkeypatch):
    """The engine never answered, so no model can be blamed and none can rescue it: a second
    detection against m/spare would spend up to the whole remaining budget to reproduce the
    same 502. On Windows a dead local port hangs rather than refusing, so "reproduce it twice
    more" is measured in minutes, not milliseconds."""
    _pool(client, ["m/dead", "m/spare"])
    calls = _stub_forward_seq(monkeypatch, [(502, DEAD_ENGINE)])
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 502
    body = r.get_json()
    assert _models_called(calls) == ["m/dead"], _models_called(calls)
    assert "auto_switch" not in body
    assert "failover" not in body
    assert body["error_code"] == "ENGINE_UNREACHABLE"
    assert body["lane_source"] == "user-pool"


def test_an_engine_timeout_is_not_retried_against_a_second_model(client, monkeypatch):
    """A ceiling that was too short for one model is too short for the next one on the same
    congested pool; the hint already names the right knob (the lane's timeout_ms)."""
    _pool(client, ["m/dead", "m/spare"])
    calls = _stub_forward_seq(monkeypatch, [(502, SLOW_ENGINE)])
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 502
    assert _models_called(calls) == ["m/dead"], _models_called(calls)
    assert r.get_json()["error_code"] == "ENGINE_TIMEOUT"


def test_a_model_that_refused_still_switches_to_the_next_in_the_users_order(client, monkeypatch):
    """The guard must not over-correct: a model-level non-answer is exactly what the pool's order
    is for, and the switch has to be named out loud in the body."""
    _pool(client, ["m/dead", "m/spare"])
    calls = _stub_forward_seq(monkeypatch,
                              [(200, _model_refused("m/dead")), (200, _answered("m/spare"))])
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert _models_called(calls) == ["m/dead", "m/spare"]
    assert body["classification"] == "AI_GENERATED"
    assert body["lane_source"] == "failover"
    assert body["auto_switch"]["from"] == "m/dead"
    assert body["auto_switch"]["to"] == "m/spare"
    assert body["auto_switch"]["attempts"][0]["ok"] is True
    assert any("m/spare" in w for w in body["warnings"])


def test_a_candidate_that_hit_a_dead_engine_is_not_reported_as_answering(client, monkeypatch):
    """The other half of the same conflation: the candidate's own call died on the engine, so it
    did not answer, and the walk must stop there rather than buy a third model from a pool that
    has already proved the engine is gone."""
    _pool(client, ["m/dead", "m/spare", "m/third"])
    calls = _stub_forward_seq(monkeypatch,
                              [(200, _model_refused("m/dead")), (502, DEAD_ENGINE)])
    r = client.post("/detect", json={"text": TEXT})
    body = r.get_json()
    assert _models_called(calls) == ["m/dead", "m/spare"], _models_called(calls)
    assert body["classification"] == "DETECTION_UNAVAILABLE"
    assert "auto_switch" not in body, body.get("auto_switch")
    assert body["lane_source"] != "failover"
    assert body["failover"]["switched_to"] is None
    assert body["failover"]["attempts"][0]["ok"] is False
    assert body["failover"]["attempts"][0]["error_code"] == "ENGINE_UNREACHABLE"


def test_a_fast_refusal_leaves_the_replacement_its_own_full_lane_call(client, monkeypatch):
    """The switch is only worth making if the model it switches TO can still finish.

    Live, 2026-10-17: a pool of [nvidia/nemotron-3-ultra-550b-a55b (row timeout 90 s),
    z-ai/glm-5.3-flash (240 s)] refused in 0.7 s and failover DID run -- and the replacement was
    handed 109 s, because the request's whole deadline was computed from the FIRST model's
    ceiling. glm needed longer than that on a congested pool, so the user waited the full 120 s
    and got no verdict from either model. "Auto switching if the model stops responding" that
    starves the responder is not the feature anyone asked for.

    The budget therefore belongs to the request, and the tier above this (270 s wait -> 285 s
    gunicorn -> 330 s nginx) is what bounds it -- not whichever row happens to be on top.
    """
    timeouts: list = []
    _pool(client, ["m/dead", "m/spare"], rows={"m/spare": {"timeout_ms": 240000}})
    calls = _stub_forward_seq(monkeypatch,
                              [(200, _model_refused("m/dead")), (200, _answered("m/spare"))],
                              timeouts=timeouts)
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    assert _models_called(calls) == ["m/dead", "m/spare"], _models_called(calls)
    # m/dead has no row, so it is called at the deployed lane's ceiling; m/spare's own row says
    # 240 s, and 0.7 s of that request was spent learning that m/dead will not answer.
    assert timeouts[0] == pytest.approx(120.0), timeouts
    assert timeouts[1] > 200.0, (
        f"the replacement got {timeouts[1]:.0f}s of a {timeouts[0]:.0f}s request -- it was "
        f"left with the first model's leftovers instead of its own lane ceiling")
    # Bounded by the request budget, not by the candidate's own ceiling: the outer tiers are
    # derived from one lane at the shipped ceiling, so a two-attempt request must fit under them.
    from backend.app import REQUEST_BUDGET_S
    assert timeouts[1] <= REQUEST_BUDGET_S + 1.0, timeouts


def test_a_key_probe_is_given_the_ceiling_the_model_itself_has(client, monkeypatch):
    """"Test my key" must not call a working credential broken because the model is thorough.

    Live, 2026-10-17: POST /my/keys/<id>/verify for z-ai/glm-5.3-flash was given 60 s and the
    congested NVIDIA pool needed more, so the run reported the key as unusable -- and since a
    verified key is the ONLY thing that queues auto-discovery, the "enter it, test it, save it,
    get your models ranked" flow stopped dead at step one. A probe ceiling is a ceiling, not a
    wait: a healthy gateway still answers "reply with the single word: ok" in a couple of seconds
    and returns exactly as fast as it always did.

    Asserted through the route, not by calling the helper, because the helper reads the request's
    own connection -- and the thing that broke was the number wired into the probe, not the
    arithmetic that produces it.
    """
    import backend.app as app_mod

    nv, _orr = _seed_providers(client)
    for model_id, ms in (("slow/lane", 240000), ("quick/lane", 4000)):
        assert client.post("/models", json={"provider_id": nv, "model_id": model_id,
                                            "timeout_ms": ms}).status_code == 201
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    kid = client.post("/my/keys", json={"provider_id": nv, "api_key": "nvapi-testkey-ABC",
                                        "label": "test"}).get_json()["id"]

    asked: list = []

    def stub_probe(prov, model_id, key, timeout_s):
        asked.append(timeout_s)
        # Refuses on purpose. `enqueue_discovery` only runs for a probe that said ok, and it is a
        # closure inside create_app, so a test cannot stub it from here -- the only way to keep a
        # real catalogue fetch and four screen calls out of a unit run is to never verify. The
        # number under test is the one handed TO the probe, and that is recorded either way.
        return {"ok": False, "error_code": "STUBBED", "latency_ms": 12}

    monkeypatch.setattr(app_mod, "_probe_with_key", stub_probe)

    client.post(f"/my/keys/{kid}/verify", json={"model_id": "slow/lane"})
    assert asked[-1] > 200.0, f"a 240 s lane was probed with only {asked[-1]:.0f}s"
    # A model whose own ceiling is short still gets the floor: 4 s of network patience is not a
    # timeout anyone would trust, and this is the case the old 45 s default already handled.
    client.post(f"/my/keys/{kid}/verify", json={"model_id": "quick/lane"})
    assert asked[-1] == 45.0, asked
    # A model with no row at all keeps that same floor -- nothing gets slower to fail.
    client.post(f"/my/keys/{kid}/verify", json={"model_id": "brand/new"})
    assert asked[-1] == 45.0, asked
    # An explicit number in the request still wins: that is how the admin's own tooling, and this
    # repo's verifiers, ask for a fast failure.
    client.post(f"/my/keys/{kid}/verify", json={"model_id": "slow/lane", "timeout_seconds": 7})
    assert asked[-1] == 7.0, asked


# ------------------------------------- the tie-break lane walks the pool below the primary
#
# "The 2nd lane starts just after the 1st model that answered, and keeps going on a non-response --
# like the logic for 1 lane." `backend/engine.py: run_secondary` holds the walk, because a second
# opinion is bought INSIDE one detection and re-posting /detect would bill the primary a second time
# for a leg the user already paid for. This section is the other half of it: which models the backend
# offers, in whose order, and what the stored row and the health table say once a leg has answered.
#
# `DETECTION_ENGINE=python` selects the in-process path, and `engine.run_lane` is the only seam:
# normalize, the walk and the ensemble then run for real against a table of recorded-shaped gateway
# bodies, keyed by MODEL because three tie-break attempts are three different models, not one lane.

REFUSAL = {"error": {"code": 404, "message": "Function id pool/x is not found"}}


def _answer(prob, conf=0.9):
    return {"choices": [{"message": {"content": json.dumps(
        {"ai_probability": prob, "confidence": conf, "signals": [], "phrases": []})},
        "finish_reason": "stop"}]}


def _pool_native(client, models, rows=None, tie="deployed/tie", providers=None, extra_keys=()):
    """alice, her pool in exactly this order, a credential, and an enabled tie-break row.

    The tie row is what turns the second lane ON (round 25c ships it off), and the key is what the
    in-process engine insists on before it dials anything. `tie=None` leaves the lane switched off.

    `providers` maps a model id to a different provider slug -- a pool that spans gateways is the
    normal case for a free tier -- and `extra_keys` is [("openrouter", "sk-or-...")] stored for her
    by provider NAME, because a leg at the second gateway has to be paid for at the second gateway
    and the caller of this helper does not have the row id before the call that creates it.
    """
    nv, orr = _seed_providers(client)
    by_name = {"nvidia": nv, "openrouter": orr}
    for model_id, fields in (rows or {}).items():
        assert client.post("/models", json={"provider_id": nv, "model_id": model_id,
                                            **fields}).status_code in (200, 201)
    if tie:
        assert client.post("/models", json={"provider_id": nv, "model_id": tie,
                                            "lane": "secondary"}).status_code in (200, 201)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    for which, key in ([("nvidia", "nvapi-testkey-ABC")] + list(extra_keys)):
        assert client.post("/my/keys", json={"provider_id": by_name[which], "api_key": key,
                                             "label": f"test {which}"}).status_code == 201
    for m in models:
        assert client.post("/my/pool", json={"provider": (providers or {}).get(m, "nvidia"),
                                             "model": m}).status_code == 201
    return nv, orr


def _native_lanes(monkeypatch, answers):
    """`engine.run_lane` answered from `answers` by model id, recording every leg it dialled."""
    import backend.engine as engine_mod
    calls: list = []

    def run(cfg, lane, text, deadline=None):
        model = cfg[lane + "_model"]
        calls.append((lane, model))
        return engine_mod.parse_lane(cfg, lane, answers.get(model, REFUSAL))

    monkeypatch.setattr(engine_mod, "run_lane", run)
    return calls


def test_the_tie_break_lane_starts_below_the_primary_and_keeps_going_down(client, monkeypatch):
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["pool/top", "pool/second", "pool/third", "pool/fourth"],
                 rows={"pool/second": {"secondary_decision_threshold": 0.6}})
    calls = _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9),
                                       "pool/fourth": _answer(0.95, 0.8)})
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert calls == [("primary", "pool/top"), ("secondary", "pool/second"),
                     ("secondary", "pool/third"), ("secondary", "pool/fourth")]
    assert body["method"] == "TIE_BREAK_AI" and body["classification"] == "AI_GENERATED"
    walk = body["secondary_walk"]
    assert walk["from"] == "pool/second" and walk["switched_to"] == "pool/fourth"
    # The model whose cut this leg was read on keeps its own number, whatever the deployed row says.
    assert walk["attempts"][0]["decision_threshold"] == 0.6
    # The deployed tie row is not in the pool, and a pool that can answer for itself does not ask it.
    assert "deployed/tie" not in [m for _lane, m in calls]
    assert body["config_used"]["secondary"]["model"] == "pool/fourth"


def test_the_stored_row_names_the_tie_break_model_that_answered(client, monkeypatch):
    """History is where a verdict is re-read months later: a row that says `deployed/tie` decided a
    text that `pool/third` actually scored is the unattributable number this app keeps removing."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["pool/top", "pool/second", "pool/third"])
    _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9), "pool/third": _answer(0.95, 0.8)})
    body = client.post("/detect", json={"text": TEXT}).get_json()
    row = client.get(f"/detections/{body['backend_detection_id']}").get_json()
    assert row["primary_model"] == "pool/top"
    assert row["secondary_model"] == "pool/third"


def test_every_tie_break_leg_that_refused_is_counted_against_its_own_model(client, monkeypatch):
    """`evidence.models` carries only the LAST leg of a walk, so without the walk record the two
    models that 404ed in the middle would never be marked -- and would sit at the top of the pool
    being dialled again on the next gray-zone text."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["pool/top", "pool/second", "pool/third"])
    _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9)})
    body = client.post("/detect", json={"text": TEXT}).get_json()
    assert body["classification"] == "REVIEW_REQUIRED"
    view = {p["model"]: p for p in client.get("/my/pool").get_json()["pool"]}
    assert view["pool/second"]["consecutive_failures"] == 1
    assert view["pool/third"]["consecutive_failures"] == 1, "the last leg is counted once"


def test_a_pool_with_nothing_below_the_primary_leaves_the_deployed_tie_row_in_charge(client,
                                                                                     monkeypatch):
    """The walk is a pool property. One member is a pool, and the lane it cannot supply has to come
    from the row that owns it rather than from nobody."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["pool/top"])
    calls = _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9),
                                        "deployed/tie": _answer(0.95, 0.8)})
    r = client.post("/detect", json={"text": TEXT})
    assert calls == [("primary", "pool/top"), ("secondary", "deployed/tie")]
    assert "secondary_walk" not in r.get_json()
    assert r.get_json()["method"] == "TIE_BREAK_AI"


def test_a_second_lane_switched_off_is_not_walked_back_on(client, monkeypatch):
    """The owner's rule says which model answers when the lane IS asked. No pool entry can turn a
    switched-off lane on -- that is a row edit, and round 25c is why."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["pool/top", "pool/second"], tie=None)
    calls = _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9),
                                        "pool/second": _answer(0.95, 0.8)})
    body = client.post("/detect", json={"text": TEXT}).get_json()
    assert [m for lane, m in calls if lane == "secondary"] == []
    assert body["method"] == "SINGLE_MODEL_PRIMARY_UNCERTAIN"


def test_a_tie_break_the_caller_named_is_never_rewritten(client, monkeypatch):
    """An explicit lane is a choice for THIS detection and outranks the pool order, exactly as the
    primary does -- otherwise "use this model" would silently drag a second model in with it.

    Both legs name their `base_url` because neither model has a row: with no row the app has no
    gateway to fill in, and a lane aimed at a host it cannot name gets no credential -- the engine
    then refuses the whole request with PROVIDER_KEY_MISSING before any leg runs, which would test
    the key resolver instead of the precedence."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["pool/top", "pool/second"])
    calls = _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9),
                                        "named/tie": _answer(0.95, 0.8)})
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "pool/top", "base_url": NVIDIA},
        "secondary": {"model": "named/tie", "base_url": NVIDIA}}})
    assert r.status_code == 200, r.get_json()
    assert [m for lane, m in calls if lane == "secondary"] == ["named/tie"]


def test_a_failover_candidate_gets_the_tie_break_below_it_not_the_one_it_just_replaced(client,
                                                                                        monkeypatch):
    """The two walks are one rule read twice, so the tie-break has to move down WITH the primary
    rather than stay pinned to the model that died.

    Left as it was, the retry dials the lane resolved for the dead model: `dead/2`, which is now the
    primary itself, so the collision guard switched the second opinion off and a user who lost their
    top model to a 404 also lost the corroborating call -- on the very texts where it settles the
    answer. Read from the payloads rather than from legs, because walking is the native engine's
    ability and this is about which lane each attempt is ASKED to run.
    """
    _pool_native(client, ["dead/1", "dead/2", "pool/spare", "pool/fourth"])
    calls = _stub_forward_seq(monkeypatch, [(200, _model_refused("dead/1")),
                                            (200, _answered("dead/2"))])
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    assert [c["lane_config"]["primary"]["model"] for c in calls] == ["dead/1", "dead/2"]
    assert [c["lane_config"]["secondary"]["model"] for c in calls] == ["dead/2", "pool/spare"], \
        "each attempt's second lane is the pool entry below the model that is its primary"
    assert r.get_json()["auto_switch"]["to"] == "dead/2"


def test_each_leg_of_a_mixed_provider_pool_is_paid_for_with_its_own_key(client, monkeypatch):
    """A key belongs to a provider and a provider is its endpoint, so a walk down a pool that spans
    gateways has to carry a credential per leg. Handing the NVIDIA token to the OpenRouter leg would
    come back 401 -- and `classify` files a 401 against the MODEL, so a healthy model would sit frozen
    for a week over a mistake the request made."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _pool_native(client, ["nv/a", "or/b", "nv/c"],
                 providers={"or/b": "openrouter"},
                 extra_keys=[("openrouter", "sk-or-v1-testkey-XYZ")])
    import backend.engine as engine_mod
    legs: list = []

    def run(cfg, lane, text, deadline=None):
        model = cfg[lane + "_model"]
        legs.append((lane, model, cfg[lane + "_api_key"], cfg[lane + "_base_url"]))
        answers = {"nv/a": _answer(0.5, 0.9), "nv/c": _answer(0.95, 0.8)}
        return engine_mod.parse_lane(cfg, lane, answers.get(model, REFUSAL))

    monkeypatch.setattr(engine_mod, "run_lane", run)
    body = client.post("/detect", json={"text": TEXT}).get_json()
    assert body["review_required"] is False, body
    assert [(lane, m) for lane, m, _k, _b in legs] == [("primary", "nv/a"),
                                                       ("secondary", "or/b"),
                                                       ("secondary", "nv/c")]
    tie = [leg for leg in legs if leg[0] == "secondary"]
    assert [k for _l, _m, k, _b in tie] == ["sk-or-v1-testkey-XYZ", "nvapi-testkey-ABC"], \
        "the OpenRouter leg answers to OpenRouter and the NVIDIA leg to NVIDIA"
    assert [b for _l, _m, _k, b in tie] == [OPENROUTER, NVIDIA]
    assert body["secondary_walk"]["switched_to"] == "nv/c"


def test_a_pool_entry_this_deploy_may_not_dial_is_skipped_rather_than_poisoning_the_request(client,
                                                                                            monkeypatch):
    """`engine.normalize` validates every candidate and rejects the WHOLE body on one bad field, so a
    leg aimed at a host this deploy may not name has to be dropped here. Otherwise one pool entry on
    a closed gateway would cost the user the other legs, and the detection.

    The endpoint is repointed rather than switched off, because that is what makes this the host gate
    and not the missing-gateway branch: a disabled provider resolves to no base_url at all and is
    dropped by the first half of the same test, while an admin who moved a provider to a host
    `allowed_llm_hosts` has never heard of yields a perfectly shaped URL that only the allowlist can
    refuse. Written through SQL because the request has to stay alice's -- an admin PATCH would end
    the turn logged in as root, and root has no pool to walk.
    """
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _nv, orr = _pool_native(client, ["pool/top", "or/x", "pool/bottom"],
                            providers={"or/x": "openrouter"})
    with db.session() as c:
        c.execute("UPDATE providers SET base_url = 'https://router.internal.example/v1' "
                  "WHERE id = ?", (orr,))
    calls = _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9),
                                        "pool/bottom": _answer(0.95, 0.8)})
    body = client.post("/detect", json={"text": TEXT}).get_json()
    assert body.get("error_code") != "LANE_CONFIG_INVALID", body
    assert calls == [("primary", "pool/top"), ("secondary", "pool/bottom")], \
        "the unroutable entry is gone and the walk carries on to the next one"


def test_a_walk_leg_never_dialled_spends_no_key_rotation(client, monkeypatch):
    """`rotate=False` answers "which credential WOULD pay for this endpoint" and writes nothing. A
    walk offers legs it may never call, and OpenRouter's free cap is per account: stamping every
    account on the offer would burn a day of quota on requests that did not happen.

    Alice's own key has to go for this to be observable at all -- BYOK-first means the rotation is
    never consulted while she has one -- so what remains is the admin's shared accounts, one spent
    per leg actually dialled and none for the leg that was only offered.
    """
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    nv, _orr = _pool_native(client, ["pool/top", "pool/second", "pool/third"])
    with db.session() as c:
        c.execute("DELETE FROM user_api_keys WHERE user_id = "
                  "(SELECT id FROM users WHERE username = 'alice')")
        for i in range(3):
            cipher, hint = db.encrypt_key(f"nvapi-shared-{i}")
            c.execute("INSERT INTO api_keys (provider_id, label, key_cipher, key_hint) "
                      "VALUES (?,?,?,?)", (nv, f"shared {i}", cipher, hint))
    calls = _native_lanes(monkeypatch, {"pool/top": _answer(0.5, 0.9),
                                        "pool/second": _answer(0.95, 0.8)})
    body = client.post("/detect", json={"text": TEXT}).get_json()
    assert [m for lane, m in calls if lane == "secondary"] == ["pool/second"], \
        "the head answered, so nothing below it was dialled"
    assert body["review_required"] is False, body
    with db.session() as c:
        stamped = [r["label"] for r in c.execute(
            "SELECT label FROM api_keys WHERE last_used_at IS NOT NULL ORDER BY id")]
    assert len(stamped) == 2, (
        f"two legs ran, so two accounts may be marked used; got {stamped}. A third stamp means an "
        f"offered-but-never-dialled leg spent a rotation")


def test_the_workflow_payload_never_carries_a_candidate_list(client, monkeypatch):
    """The shipped node's validator rejects an unknown `lane_config` field, so a hosted deploy handed
    `candidates` would answer 400 on every gray-zone text. The head of the walk is a plain lane and
    does travel; walking it is the in-process engine's own ability."""
    for name in ("DETECTION_ENGINE", "LOCAL_SINGLE_USER"):
        monkeypatch.delenv(name, raising=False)
    _pool_native(client, ["pool/top", "pool/second", "pool/third"])
    calls = _stub_forward_seq(monkeypatch, [(200, _answered("pool/top"))])
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    sec = calls[0]["lane_config"]["secondary"]
    assert sec["model"] == "pool/second", "the lane still starts just after the primary"
    assert "candidates" not in sec
    assert sec["base_url"] == NVIDIA


# --------------------------------------- the lane authority the page has to tell the truth about#
# "Which model actually answers my runs?" is a question a user can only answer off the Ranking
# page -- and the page is not the code that resolves a lane. Two live divergences earned this
# section: the shipped front-end baked a lane into every request, so the deployed default the page
# named never answered anything (2026-10-17, #142); and a pool the page rendered in one order while
# /detect walked it in another would make the page a caption instead of a control. So /ranking
# returns the SAME resolution /detect uses, and the second test below proves it by asking both:
# the model the page promised to call first, and the model it promised next, have to be the models
# the app actually calls.

def test_ranking_names_the_deployed_default_when_the_pool_is_empty(client):
    _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    la = client.get("/ranking").get_json()["lane_authority"]
    assert la["deployed_default"]["model"] == "deployed/primary"
    assert la["deployed_default"]["provider"] == "nvidia"
    # The tie-break ships off, so it is named as absent. Reporting a secondary no row enables
    # would promise a second call that detection never buys -- and never pays for.
    assert la["deployed_tie_break"] is None
    assert la["next"]["source"] == "deployed-default"
    assert la["next"]["model"] == "deployed/primary"
    assert la["failover"] == []


def test_the_pages_next_run_and_failover_are_what_detect_actually_walks(client, monkeypatch):
    _pool(client, ["m/one", "m/two", "m/three"])
    la = client.get("/ranking").get_json()["lane_authority"]
    assert la["next"]["model"] == "m/one" and la["next"]["source"] == "user-pool"
    assert [f["model"] for f in la["failover"]] == ["m/two", "m/three"]

    calls = _stub_forward_seq(monkeypatch,
                              [(200, _model_refused("m/one")), (200, _answered("m/two"))])
    body = client.post("/detect", json={"text": TEXT}).get_json()
    assert _models_called(calls) == [la["next"]["model"], la["failover"][0]["model"]]
    assert body["auto_switch"]["to"] == la["failover"][0]["model"]


def test_a_model_that_stopped_answering_is_not_offered_as_the_next_run(client):
    _pool(client, ["m/one", "m/two"])
    with db.session() as c:
        for _ in range(3):
            pool.note_attempt(c, "nvidia", "m/one", False, "404")
    body = client.get("/ranking").get_json()
    la = body["lane_authority"]
    assert la["next"]["model"] == "m/two", "a down entry must not lead the user's runs"
    # And it must not be promised as the rescue either: pool.next_candidates refuses to pay for
    # a model that has failed three times in a row, so a page that listed it would be advertising
    # a switch the app will not make. It stays visible in the pool, marked down, where the user
    # can see why and remove or re-rank it.
    assert la["failover"] == []
    assert [(p["model"], p["health"]) for p in body["pool"]] == [("m/two", "ok"), ("m/one", "down")]


def test_no_default_and_no_pool_answers_with_nothing_rather_than_a_made_up_lane(client):
    _seed_providers(client)
    with db.session() as c:
        c.execute("UPDATE models SET enabled = 0")
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    la = client.get("/ranking").get_json()["lane_authority"]
    assert la["deployed_default"] is None
    assert la["next"] is None


# ---------------------------------------------------------------- edit the using pool in place
#
# The page has to let a user CHANGE the model he put into a slot, not only take the whole thing
# apart and rebuild it: "remove, then add, then re-drag" costs three writes, and between them the
# slot he was holding is gone -- plus a half-finished sequence leaves either two rows or none.
# One route, one transaction, and the neighbours keep the ranks their owner gave them.

def test_replacing_a_pool_row_keeps_the_slot_it_was_given(client):
    _pool(client, ["m/one", "m/two", "m/three"])
    r = client.post("/my/pool/replace", json={"provider": "nvidia", "model": "m/two",
                                              "new_provider": "nvidia", "new_model": "m/spare"})
    assert r.status_code == 200, r.get_json()
    assert [(p["model"], p["your_rank"]) for p in r.get_json()["pool"]] == [
        ("m/one", 1), ("m/spare", 2), ("m/three", 3)]


def test_replace_refuses_a_missing_row_a_collision_and_a_dead_endpoint(client):
    _pool(client, ["m/one", "m/two"])
    assert client.post("/my/pool/replace", json={
        "provider": "nvidia", "model": "m/nope",
        "new_provider": "nvidia", "new_model": "m/spare"}).status_code == 404
    # Replacing m/two WITH m/one would collide with a row the user already holds: refuse instead
    # of letting INSERT-OR-IGNORE quietly delete one of them.
    r = client.post("/my/pool/replace", json={
        "provider": "nvidia", "model": "m/two",
        "new_provider": "nvidia", "new_model": "m/one"})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error_code"] == "ALREADY_IN_POOL"
    assert client.post("/my/pool/replace", json={
        "provider": "nvidia", "model": "m/two",
        "new_provider": "nope", "new_model": "m/spare"}).status_code == 400
    # and nothing moved: a refused edit must leave the user's order exactly as he left it
    assert [p["model"] for p in client.get("/my/pool").get_json()["pool"]] == ["m/one", "m/two"]


# --------------------------------------------------- what the caller may pick, and the offer
#
# `/ranking` is a measurement, so a model that never answered a text is not in it -- and a model
# that only timed out has never been asked one. Without a second surface those two would simply
# vanish from the page that controls which models answer, which is how "the pool was busy" turns
# into "you may not use this model". `/my/pool/options` lists what the caller may pick, measured
# or not, in the probe state discovery last recorded; `/my/catalog/decision` is the accept-or-
# refuse the user makes when the evidence says "it answered, but only after we asked twice".


def _offered(client, model_id, probe_status, note="stubbed probe", latency_ms=180_000,
             age="+0 seconds"):
    """nvidia with one discovered `models` row in exactly this probe state, and alice signed in.

    This is the shape `discovery` writes for a slow/busy/pending candidate: a `models` row (which
    is what makes it pickable) plus a `catalog_models` row (which is what says why it has no rank).
    `age` moves the probe timestamp, because the offer window counts from it."""
    nv, _ = _seed_providers(client)
    r = client.post("/models", json={"provider_id": nv, "model_id": model_id})
    assert r.status_code in (200, 201), r.get_json()
    with db.session() as c:
        c.execute("""INSERT INTO catalog_models (provider, model_id, listed, probe_status,
                                                probe_note, latency_ms, probed_at)
                     VALUES (?,?,1,?,?,?,datetime('now', ?))
                     ON CONFLICT(provider, model_id) DO UPDATE SET
                       probe_status = excluded.probe_status, probe_note = excluded.probe_note,
                       latency_ms = excluded.latency_ms, probed_at = excluded.probed_at""",
                  ("nvidia", model_id, probe_status, note, latency_ms, age))
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    return nv


def test_the_options_route_lists_a_timed_out_model_as_pickable(client):
    _offered(client, "slow/forever", "slow", "triage 0/1 texts answered: ENGINE_TIMEOUT")
    body = client.get("/my/pool/options").get_json()
    hit = [o for o in body["options"] if o["model"] == "slow/forever"]
    assert hit, body["options"]
    assert hit[0]["probe_status"] == "slow", hit
    assert hit[0]["in_pool"] is False, hit
    assert hit[0]["needs_key"] is True, hit
    assert hit[0]["probe_note"] == "triage 0/1 texts answered: ENGINE_TIMEOUT", hit
    # A `slow` row is not an offer: only a model that answered after a timeout asks for a decision.
    assert hit[0]["offer_seconds_left"] is None, hit


def test_a_pending_offer_carries_its_own_remaining_window(client):
    _offered(client, "slow/recovers", "pending", "screen 2/2 texts answered; only after a retry", 3_000)
    hit = [o for o in client.get("/my/pool/options").get_json()["options"]
           if o["model"] == "slow/recovers"][0]
    assert 0 < hit["offer_seconds_left"] <= discovery.DECISION_WINDOW_S, hit
    assert hit["answer_ms"] == 3_000, hit


def test_an_offer_that_went_unanswered_is_no_longer_offered(client):
    """Letting the clock run out is not a refusal and is not a pin: the model stays pickable and
    the next job is allowed to ask it again, which is the only way a decision the user never saw
    does not silently become a `no`."""
    _offered(client, "slow/stale", "pending", "screen 2/2 texts answered", 3_000,
             age=f"-{discovery.DECISION_WINDOW_S // 60 + 5} minutes")
    hit = [o for o in client.get("/my/pool/options").get_json()["options"]
           if o["model"] == "slow/stale"][0]
    assert hit["offer_seconds_left"] is None, hit
    with db.session() as c:
        assert discovery.needs_screen(c, "nvidia", "slow/stale") is True


def test_accepting_an_offer_adds_it_and_ends_the_offer(client):
    _offered(client, "slow/recovers", "pending", "screen 2/2 texts answered; only after a retry")
    r = client.post("/my/catalog/decision", json={"provider": "nvidia", "model": "slow/recovers",
                                                  "decision": "accept"})
    assert r.status_code == 200, r.get_json()
    assert [p["model"] for p in r.get_json()["pool"]] == ["slow/recovers"], r.get_json()
    with db.session() as c:
        assert c.execute("SELECT probe_status s FROM catalog_models "
                         "WHERE model_id = 'slow/recovers'").fetchone()["s"] == "usable"
    hit = [o for o in client.get("/my/pool/options").get_json()["options"]
           if o["model"] == "slow/recovers"][0]
    assert hit["in_pool"] is True and hit["offer_seconds_left"] is None, hit


def test_refusing_an_offer_keeps_the_model_pickable_and_never_adds_it(client):
    _offered(client, "slow/recovers", "pending")
    r = client.post("/my/catalog/decision", json={"provider": "nvidia", "model": "slow/recovers",
                                                  "decision": "refuse"})
    assert r.status_code == 200, r.get_json()
    assert client.get("/my/pool").get_json()["pool"] == []
    with db.session() as c:
        got = c.execute("SELECT probe_status s, listed FROM catalog_models "
                        "WHERE model_id = 'slow/recovers'").fetchone()
        assert got["s"] == "declined", dict(got)
        # the row that lets HIM pick it later is not deleted by a `no`
        assert c.execute("SELECT COUNT(*) n FROM models WHERE model_id = 'slow/recovers'"
                         ).fetchone()["n"] == 1
        # and a declined decision is terminal: the job does not spend another call re-asking
        assert discovery.needs_screen(c, "nvidia", "slow/recovers") is False


def test_a_decision_needs_a_real_pair_and_a_real_choice(client):
    _offered(client, "slow/here", "slow")
    r = client.post("/my/catalog/decision", json={"provider": "nvidia", "model": "never/offered",
                                                  "decision": "accept"})
    assert r.status_code == 404, r.get_json()
    r = client.post("/my/catalog/decision", json={"provider": "nvidia", "model": "slow/here",
                                                  "decision": "maybe"})
    assert r.status_code == 400, r.get_json()
    assert client.get("/my/pool").get_json()["pool"] == []
    assert client.post("/my/catalog/decision", json={"decision": "accept"}).status_code == 400


def test_the_offer_routes_require_a_session(client):
    assert client.get("/my/pool/options").status_code == 401
    assert client.post("/my/catalog/decision", json={"provider": "nvidia", "model": "m/a",
                                                     "decision": "accept"}).status_code == 401


def test_a_model_the_old_build_blamed_on_a_timeout_is_listable_after_one_boot(client):
    """The install on a user's machine holds the rows an older build wrote, and that build treated
    a timeout as a verdict: no `models` row at all. Since the picker's universe is the `models`
    table, the fix in `discovery` cannot reach those rows retroactively -- they are simply not
    there to find. Reading the legacy status back under the current rule is what turns "invisible"
    into "pickable, unmeasured", and this is the user-visible half of that: the same model, on the
    same page, listed as soon as the install has been told the new rule once."""
    _seed_providers(client)
    with db.session() as c:
        c.execute("""INSERT INTO catalog_models (provider, model_id, listed, probe_status, probe_note)
                     VALUES ('nvidia', 'asleep/old', 1, 'timeout',
                             'triage 0/1 texts answered: timeout')""")
        assert discovery.migrate_probe_vocabulary(c) == {"reclassified": 1, "rows_added": 1}
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})

    hit = [o for o in client.get("/my/pool/options").get_json()["options"]
           if o["model"] == "asleep/old"]
    assert hit, "a model that only ran out of time must be listable, not absent"
    assert hit[0]["probe_status"] == "slow", hit
    assert hit[0]["in_pool"] is False, hit
    assert hit[0]["measured"] is False, hit
    assert hit[0]["offer_seconds_left"] is None, hit


# --------------------------------------------------------------- auto-rank the using pool
#
# "Order my pool by what actually judges best" is a question the Ranking page already answers --
# `/ranking` scores every measured model at its own saved band and sorts it. A second sort written
# beside that one is how the pool and the page disagree, and this repo has paid twice for exactly
# that (#142: the front-end baked its own lane; and the offline audits that hand-copied a band and
# silently changed the shipped rule). So these specs do not assert score arithmetic: they assert
# that the ORDER auto-rank wrote is the order `/ranking` reports, restricted to the pool. If the
# measurement sort moves, the pool follows it by construction and no test has to be updated.

def _scored(model_id, n_clean, wrong, provider="nvidia", corpus="hc3-48"):
    """Give `model_id` a benchmark run whose score is under the test's control.

    Alternating correct-AI / correct-HUMAN rows at p far from any band edge, then `wrong` human
    texts scored as AI. Under the default band (0.69/0.40, no gate) every clean row is decided, so
    `decided` is n_clean minus `wrong` and `wrong` is the only variable that matters -- which is
    what the fewest-wrong sort keys on first.
    """
    tag = f"auto-{model_id}-{n_clean}-{wrong}"
    with db.session() as c:
        c.execute("""INSERT INTO benchmark_runs (tag, provider, model, corpus, n_rows, source_file)
                     VALUES (?,?,?,?,?,'-')""", (tag, provider, model_id, corpus, n_clean))
        rid = c.execute("SELECT id FROM benchmark_runs WHERE tag = ?", (tag,)).fetchone()["id"]
        rows = []
        for i in range(n_clean - wrong):
            if i % 2:
                rows.append((f"ai-{i}", "AI", 1, 0.95, 0.9))
            else:
                rows.append((f"hu-{i}", "HUMAN", 1, 0.05, 0.9))
        for i in range(wrong):
            rows.append((f"bad-{i}", "HUMAN", 1, 0.95, 0.9))
        c.executemany("""INSERT INTO benchmark_results (run_id, text_id, label, ok, error_code,
                         ai_probability, confidence) VALUES (?,?,?,?,NULL,?,?)""",
                      [(rid, *r) for r in rows])
    return tag


def test_auto_rank_orders_the_pool_by_the_page_that_already_ranks_it(client):
    """Which model judges best is one answer, not two sorts."""
    _pool(client, ["m/mid", "m/worst", "m/best", "m/noneed"])
    _scored("m/mid", 30, 2)
    _scored("m/worst", 30, 5)
    _scored("m/best", 30, 0)
    before = [r["model"] for r in client.get("/my/pool").get_json()["pool"]]
    assert before == ["m/mid", "m/worst", "m/best", "m/noneed"]

    r = client.post("/my/pool/auto-rank")
    assert r.status_code == 200, r.get_json()
    after = [e["model"] for e in r.get_json()["pool"]]

    # The promise, asserted against the ranking route rather than against arithmetic repeated
    # here: take /ranking's own order, keep the models this pool holds, and the pool must match it.
    order_of = {e["model"]: e["rank"] for e in client.get("/ranking").get_json()["ranked"]}
    measured = [m for m in after if m in order_of]
    assert measured == sorted(measured, key=lambda m: order_of[m]), (measured, order_of)
    assert measured == ["m/best", "m/mid", "m/worst"], after
    assert after == ["m/best", "m/mid", "m/worst", "m/noneed"], after


def test_auto_rank_leaves_an_unmeasured_model_at_the_back_in_the_users_order(client):
    """A model with no capture has no measurement to rank on, so it cannot be ordered -- but the
    user's rule is that it is never removed for that. It goes behind the measured ones and keeps
    its relative place among the other unranked rows he holds."""
    _pool(client, ["m/unranked1", "m/good", "m/unranked2"])
    _scored("m/good", 30, 0)
    r = client.post("/my/pool/auto-rank")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert [e["model"] for e in body["pool"]] == ["m/good", "m/unranked1", "m/unranked2"], body
    # Still his rows to order afterwards: source says user, not auto.
    assert [e["source"] for e in body["pool"]] == ["user"] * 3
    assert body["ranked"] == 1
    assert body["unranked"] == 2


def test_auto_rank_is_a_no_op_that_says_so_when_no_model_is_measured(client):
    _pool(client, ["m/a", "m/b"])
    r = client.post("/my/pool/auto-rank")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert [e["model"] for e in body["pool"]] == ["m/a", "m/b"], body
    assert body["ranked"] == 0
    assert "no measurement" in body["note"].lower(), body["note"]


def test_auto_rank_never_reaches_into_a_model_he_does_not_hold(client):
    """The button orders HIS pool. A better-scoring model that is not in it stays out: adding
    models is the picker's job, and a silent add would spend his calls on a lane he never chose."""
    _pool(client, ["m/weak"])
    _scored("m/weak", 30, 4)
    _scored("m/stranger", 30, 0)
    r = client.post("/my/pool/auto-rank")
    assert [e["model"] for e in r.get_json()["pool"]] == ["m/weak"]
    assert len(client.get("/my/pool").get_json()["pool"]) == 1


def test_auto_rank_requires_a_session_and_needs_no_body(client):
    assert client.post("/my/pool/auto-rank").status_code == 401


# ------------------------------------------------------------------- the Test button
#
# A ranking card's only action today is "score with this model", so the way a user finds out
# whether a model answers him RIGHT NOW is to spend a whole detection on it. A probe is the cheap
# answer and already exists (`_probe_with_key`: 8 tokens, "reply with the single word: ok"), but
# the only route that reaches it is admin-only -- `POST /providers/<id>/probe` sits behind
# `_admin_required`, which guards every POST under `/providers`. On a hosted deploy a plain user
# therefore has no test button he can be given, and the fix must not quietly use the deployment's
# key, because "does this model answer me" is a question about his own credential and budget.


def test_my_probe_answers_with_the_probes_own_verdict(client, monkeypatch):
    import backend.app as app_mod
    nv, _orr = _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    client.post("/my/keys", json={"provider_id": nv, "api_key": "nvapi-testkey-ABC",
                                  "label": "mine"})

    seen: list = []

    def stub_probe(prov, model_id, key, timeout_s):
        seen.append((prov["name"], model_id, key, timeout_s))
        return {"ok": True, "status": 200, "latency_ms": 1430}

    monkeypatch.setattr(app_mod, "_probe_with_key", stub_probe)
    r = client.post("/my/probe", json={"provider_id": nv, "model_id": "m/probe"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] is True and body["status"] == 200 and body["latency_ms"] == 1430
    assert body["model"] == "m/probe" and body["provider"] == "nvidia", body
    assert seen and seen[-1][2] == "nvapi-testkey-ABC", "must test the CALLER's credential"
    assert seen[-1][3] >= 45.0, "and give it the network patience the verify route gives it"


def test_my_probe_refuses_before_spending_a_key_it_does_not_own(client, monkeypatch):
    """The deployment's key is not the user's test seat. With no stored key for that endpoint the
    route says so and makes no call at all -- the same refusal shape as /my/discover."""
    import backend.app as app_mod
    nv, _orr = _seed_providers(client)
    _admin(client)
    client.post(f"/providers/{nv}/keys", json={"api_key": "nvapi-adminsharedkey"})

    calls: list = []
    monkeypatch.setattr(app_mod, "_probe_with_key",
                        lambda *a, **k: calls.append(a) or {"ok": True})
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    r = client.post("/my/probe", json={"provider_id": nv, "model_id": "m/probe"})
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["error_code"] == "NO_USER_KEY", r.get_json()
    assert calls == [], "a refusal must not make a request"


def test_my_probe_writes_no_health_and_no_catalogue(client, monkeypatch):
    """A test button asks a question; it does not hand down a verdict about the model's future.
    Health is measured from real detections, and a probe that ran out of time must not mark a lane
    down and re-rank the pool -- the user's rule is that a timeout is never a removal."""
    import backend.app as app_mod
    nv, _orr = _seed_providers(client)
    _register(client, "alice")
    client.post("/auth/login", json={"username": "alice", "password": "userpass123"})
    client.post("/my/keys", json={"provider_id": nv, "api_key": "nvapi-testkey-ABC",
                                  "label": "mine"})
    monkeypatch.setattr(app_mod, "_probe_with_key",
                        lambda *a, **k: {"ok": False, "error_code": "UNREACHABLE",
                                         "detail": "timed out", "latency_ms": 45000})
    r = client.post("/my/probe", json={"provider_id": nv, "model_id": "m/probe"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["ok"] is False
    with db.session() as c:
        assert c.execute("SELECT 1 FROM model_health WHERE model_id = 'm/probe'").fetchone() is None
        assert c.execute("SELECT 1 FROM catalog_models WHERE model_id = 'm/probe'").fetchone() is None
        assert c.execute("SELECT COUNT(*) n FROM discovery_jobs").fetchone()["n"] == 0


def test_my_probe_needs_a_session_and_a_model(client):
    assert client.post("/my/probe", json={"model_id": "m/a"}).status_code == 401
    _pool(client, ["m/a"])
    assert client.post("/my/probe", json={"provider_id": 1}).status_code == 400
