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
    return a


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


@pytest.fixture()
def conn(app):
    with db.session() as c:
        yield c


def _register(client, username, password="userpass123"):
    r = client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["user"]


def _admin(client):
    r = client.post("/auth/login", json={"username": "root", "password": "rootpass123"})
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


# --------------------------------------- the lane authority the page has to tell the truth about
#
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
