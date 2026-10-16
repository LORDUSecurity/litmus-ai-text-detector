"""Auth, roles and per-user history. Run from ai-text-detector/:
    python -m pytest backend/tests/test_auth.py -q

These opt enforcement back ON (AUTH_FORCE=True) -- the shared `client` fixture in
test_backend.py runs under app.testing, which the guard deliberately skips so the
pre-account suite keeps passing unedited. Here we prove the guard actually bites:
401 for the anonymous, 403 for a user on an admin route, and history that one user
cannot read from another.

Same throwaway DB / stubbed forwarder discipline as test_backend.py: never a real
provider call, never the live n8n database, never a printed key.
"""
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-auth-test-")
os.environ["DETECTOR_DB"] = os.path.join(_TMP, "auth.sqlite3")
os.environ["DETECTOR_SECRET"] = os.path.join(_TMP, "auth-secret.key")
os.environ["N8N_DB"] = os.path.join(_TMP, "no-such-n8n.sqlite")
os.environ["ADMIN_USERNAME"] = "root"
os.environ["ADMIN_PASSWORD"] = "rootpass123"

import pytest  # noqa: E402

from backend import db  # noqa: E402
from backend.app import create_app  # noqa: E402

TEXT = "This is a long enough sample of text for the detector to accept it."


@pytest.fixture()
def app():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    a = create_app()
    a.config["TESTING"] = True
    a.config["AUTH_FORCE"] = True  # enforce the guard even under the test client
    return a


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def _register(client, username, password="userpass123"):
    r = client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["user"]


def _login(client, username, password):
    return client.post("/auth/login", json={"username": username, "password": password})


def _stub_forward(monkeypatch, status=200):
    import backend.app as app_mod

    def fake(url, payload, headers, timeout):
        return status, {
            "classification": "AI_GENERATED", "ai_probability": 0.97, "confidence": 0.9,
            "method": "PRIMARY_DECIDED_AI", "review_required": False,
            "config_used": {"primary": {"model": "test/model"}},
            "_internal": {"text_fingerprint": "fp", "text_length": len(TEXT)},
        }, 10.0

    monkeypatch.setattr(app_mod, "forward", fake)


def _seed_primary(client):
    """Admin creates an allowed provider + primary model so /detect can run."""
    pid = client.post("/providers", json={
        "name": "nvidia", "base_url": "https://integrate.api.nvidia.com/v1"}).get_json()["id"]
    client.post("/models", json={"provider_id": pid, "model_id": "test/model", "lane": "primary"})
    return pid


# ------------------------------------------------------------------ seeding / me

def test_admin_is_seeded_from_env(client):
    # The launcher/deploy seeds an admin so there is always a way in.
    r = _login(client, "root", "rootpass123")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["user"]["role"] == "admin"


def test_me_is_null_when_anonymous(client):
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.get_json()["user"] is None


def test_me_returns_the_signed_in_user(client):
    _register(client, "alice")
    me = client.get("/auth/me").get_json()["user"]
    assert me["username"] == "alice"
    assert me["role"] == "user"


# ------------------------------------------------------------------ the guard

def test_protected_route_requires_login(client):
    # A detection is the whole point of the page; it must not run for the anonymous.
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 401
    assert r.get_json()["error_code"] == "AUTH_REQUIRED"


def test_public_paths_stay_open(client):
    assert client.get("/auth/me").status_code == 200
    assert client.get("/").status_code == 200


def test_login_rejects_bad_password(client):
    _register(client, "bob", "goodpass123")
    r = _login(client, "bob", "wrongpass999")
    assert r.status_code == 401
    assert r.get_json()["error_code"] == "BAD_CREDENTIALS"


def test_register_rejects_short_password(client):
    r = client.post("/auth/register", json={"username": "carol", "password": "short"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "BAD_PASSWORD"


def test_register_rejects_duplicate_username(client):
    _register(client, "dave")
    r = client.post("/auth/register", json={"username": "dave", "password": "anotherpass1"})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "USERNAME_TAKEN"


def test_logout_clears_the_session(client):
    _register(client, "erin")
    assert client.get("/auth/me").get_json()["user"] is not None
    client.post("/auth/logout")
    assert client.get("/auth/me").get_json()["user"] is None


# ------------------------------------------------------------------ roles

def test_user_cannot_mutate_providers(client):
    _register(client, "frank")  # a plain user, signed in via register's auto-login
    r = client.post("/providers", json={"name": "x", "base_url": "https://openrouter.ai/api/v1"})
    assert r.status_code == 403
    assert r.get_json()["error_code"] == "FORBIDDEN"


def test_user_cannot_read_admin_key_list(client):
    _register(client, "grace")
    r = client.get("/keys")
    assert r.status_code == 403


def test_admin_can_mutate_providers(client):
    _login(client, "root", "rootpass123")
    r = client.post("/providers", json={"name": "nvidia",
                                        "base_url": "https://integrate.api.nvidia.com/v1"})
    assert r.status_code == 201, r.get_json()


def test_user_sees_providers_but_not_admin_keys(client):
    # BYOK needs the endpoint list; the admin's stored keys must never leak to a user.
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/providers/1/keys", json={"api_key": "nvapi-secret-abcdef-123456", "label": "primary lane"})
    client.post("/auth/logout")

    _register(client, "heidi")
    provs = client.get("/providers").get_json()
    assert provs and provs[0]["models"], "a user still sees the tuned model list"
    assert provs[0]["keys"] == [], "admin key rows are hidden from a user"
    assert provs[0].get("has_admin_key") is True


# ------------------------------------------------------------------ per-user history

def test_history_is_isolated_per_user(client, monkeypatch):
    _stub_forward(monkeypatch)
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/auth/logout")

    # ivan runs one detection
    _register(client, "ivan")
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    ivan_hist = client.get("/detections").get_json()
    assert len(ivan_hist) == 1
    ivan_id = ivan_hist[0]["id"]
    assert ivan_hist[0]["user_id"] is not None
    client.post("/auth/logout")

    # judy sees none of ivan's rows
    _register(client, "judy")
    assert client.get("/detections").get_json() == []
    # and cannot open ivan's detection by id -- it reads as not found, not forbidden
    assert client.get(f"/detections/{ivan_id}").status_code == 404


def test_admin_sees_every_users_history(client, monkeypatch):
    _stub_forward(monkeypatch)
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/auth/logout")

    _register(client, "kate")
    client.post("/detect", json={"text": TEXT})
    client.post("/auth/logout")

    _login(client, "root", "rootpass123")
    hist = client.get("/detections").get_json()
    assert len(hist) >= 1
    # admin can open a row owned by someone else
    assert client.get(f"/detections/{hist[0]['id']}").status_code == 200


# ------------------------------------------------------------------ profile

def test_theme_preference_persists(client):
    _register(client, "liam")
    r = client.post("/auth/theme", json={"theme": "light"})
    assert r.status_code == 200
    assert r.get_json()["user"]["theme"] == "light"
    assert client.get("/auth/me").get_json()["user"]["theme"] == "light"


def test_theme_rejects_unknown_value(client):
    _register(client, "mia")
    r = client.post("/auth/theme", json={"theme": "neon"})
    assert r.status_code == 400


def test_password_change_requires_current(client):
    _register(client, "noah", "firstpass123")
    r = client.post("/auth/password", json={"current": "wrong", "next": "secondpass1"})
    assert r.status_code == 401
    r = client.post("/auth/password", json={"current": "firstpass123", "next": "secondpass1"})
    assert r.status_code == 200
    client.post("/auth/logout")
    assert _login(client, "noah", "secondpass1").status_code == 200


# ------------------------------------------------------------------ BYOK keys

def _stub_probe(monkeypatch, ok=True):
    import backend.app as app_mod
    seen = {}

    def fake(prov, model_id, key, timeout_s):
        seen["model_id"] = model_id
        seen["key"] = key
        return {"ok": ok, "status": 200 if ok else 401, "latency_ms": 5}

    monkeypatch.setattr(app_mod, "_probe_with_key", fake)
    return seen


def test_user_can_add_a_key_for_a_supported_endpoint(client, monkeypatch):
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/auth/logout")

    _stub_probe(monkeypatch, ok=True)
    _register(client, "olivia")
    r = client.post("/my/keys", json={
        "provider_id": 1, "api_key": "nvapi-user-key-abc123", "model_id": "test/model"})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    # The plaintext key is never echoed back; only a hint and the verified flag.
    assert "nvapi-user-key-abc123" not in r.get_data(as_text=True)
    assert body["verified"] is True
    assert body["key_hint"]


def test_user_cannot_add_a_key_for_an_unsupported_endpoint(client):
    _register(client, "peter")
    r = client.post("/my/keys", json={"base_url": "https://evil.example.com/v1",
                                      "api_key": "sk-whatever-123456"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "PROVIDER_NOT_SUPPORTED"


def test_detect_uses_the_callers_own_key_when_present(client, monkeypatch):
    captured = {}
    _stub_forward(monkeypatch)
    import backend.app as app_mod
    real_forward = app_mod.forward

    def capturing(url, payload, headers, timeout):
        captured["headers"] = headers
        return real_forward(url, payload, headers, timeout)

    monkeypatch.setattr(app_mod, "forward", capturing)
    _stub_probe(monkeypatch, ok=True)

    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/auth/logout")

    _register(client, "quinn")
    client.post("/my/keys", json={"provider_id": 1, "api_key": "nvapi-quinn-key-xyz789",
                                  "model_id": "test/model"})
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    # BYOK: the user's own credential paid for the call, not an admin key.
    assert captured["headers"].get("x-primary-api-key") == "nvapi-quinn-key-xyz789"


def test_a_model_picked_from_the_ranking_is_paid_for_with_the_pickers_own_key(client, monkeypatch):
    """The Ranking tab's "Score with this model" button sends lane_config with only a model id --
    no endpoint, no key, because a user cannot and must not supply either. So the explicit-lane
    path is the one the button actually walks, and it has to resolve both halves the same way the
    deployed-lane path does: the endpoint and calibrated band from the model's stored row, and the
    credential from this user's own verified key before the admin's shared one.

    Before this, stored_key_for() returned active_key() only, so the page said "your key ready"
    and then billed the shared account -- or, with no shared key, sent nothing and failed on auth
    for a model the ranking had just promised was usable."""
    captured = {}
    _stub_forward(monkeypatch)
    import backend.app as app_mod
    real_forward = app_mod.forward

    def capturing(url, payload, headers, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return real_forward(url, payload, headers, timeout)

    monkeypatch.setattr(app_mod, "forward", capturing)
    _stub_probe(monkeypatch, ok=True)

    _login(client, "root", "rootpass123")
    pid = _seed_primary(client)
    client.post(f"/providers/{pid}/keys", json={"api_key": "nvapi-shared-111222333",
                                                 "label": "shared"})
    client.post("/auth/logout")

    _register(client, "nadia")
    client.post("/my/keys", json={"provider_id": pid, "api_key": "nvapi-nadia-99887766",
                                  "model_id": "test/model"})

    r = client.post("/detect", json={"text": TEXT, "lane_config": {"primary": {"model": "test/model"}}})
    assert r.status_code == 200, r.get_json()
    assert captured["headers"].get("x-primary-api-key") == "nvapi-nadia-99887766"
    # The endpoint came from the model's row, not from the browser: the pick is a model id only.
    assert captured["payload"]["lane_config"]["primary"]["base_url"] == \
        "https://integrate.api.nvidia.com/v1"
    # Neither credential is echoed to the caller.
    body = r.get_data(as_text=True)
    assert "nvapi-nadia-99887766" not in body and "nvapi-shared-111222333" not in body


def test_my_keys_are_scoped_to_the_owner(client, monkeypatch):
    _stub_probe(monkeypatch, ok=True)
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/auth/logout")

    _register(client, "rosa")
    client.post("/my/keys", json={"provider_id": 1, "api_key": "nvapi-rosa-123",
                                  "model_id": "test/model"})
    assert len(client.get("/my/keys").get_json()) == 1
    client.post("/auth/logout")

    _register(client, "sam")
    assert client.get("/my/keys").get_json() == []


def test_user_can_delete_own_key(client, monkeypatch):
    _stub_probe(monkeypatch, ok=True)
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/auth/logout")

    _register(client, "tina")
    kid = client.post("/my/keys", json={"provider_id": 1, "api_key": "nvapi-tina-123",
                                        "model_id": "test/model"}).get_json()["id"]
    assert client.delete(f"/my/keys/{kid}").status_code == 200
    assert client.get("/my/keys").get_json() == []


# ------------------------------------------------------------------ config / ranking / settings

def test_config_is_readable_by_a_user_and_lists_supported_providers(client):
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/settings", json={"paper_url": "https://zenodo.org/doi/10.5281/zenodo.1",
                                   "api_key_guides": [{"provider": "nvidia",
                                                       "url": "https://build.nvidia.com",
                                                       "steps": "sign up, create key"}]})
    client.post("/auth/logout")

    _register(client, "uma")
    cfg = client.get("/config").get_json()
    assert cfg["paper_url"].startswith("https://zenodo.org")
    assert cfg["api_key_guides"][0]["provider"] == "nvidia"
    assert any(p["name"] == "nvidia" for p in cfg["providers"])
    # A user sees the tuned models but no admin key material.
    nv = next(p for p in cfg["providers"] if p["name"] == "nvidia")
    assert nv["models"] and "key_cipher" not in str(cfg)


def test_settings_is_admin_only(client):
    _register(client, "vince")  # plain user
    assert client.post("/settings", json={"paper_url": "x"}).status_code == 403
    assert client.get("/settings").status_code == 403


def test_settings_rejects_unknown_keys(client):
    _login(client, "root", "rootpass123")
    r = client.post("/settings", json={"paper_url": "https://example.org/paper",
                                       "not_a_real_setting": "boom"})
    assert r.status_code == 200
    written = r.get_json()["written"]
    assert "paper_url" in written and "not_a_real_setting" not in written


# ------------------------------------------------------------------ engine + link settings
#
# n8n_url and webhook_path are how the packaged local exe reaches a detection engine at all:
# /detect posts to setting("n8n_url") + setting("webhook_path"), and the shipped default is
# http://localhost:5678/webhook/ai-detect-ai, which only works on a machine that runs n8n. The
# admin is meant to be able to point it at another host. Before this test, POST /settings stored
# any string, so "localhost:5678" (a scheme-relative typo) was accepted and every later detect
# died inside urllib with "unknown url type" -- a 502 that named the URL but could not be undone
# from the UI, because the same field that broke it was the only way to fix it.

def test_settings_stores_an_engine_url_and_path(client):
    _login(client, "root", "rootpass123")
    r = client.post("/settings", json={"n8n_url": "https://n8n.example.org:5678",
                                       "webhook_path": "/webhook/ai-detect-ai"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["written"]["n8n_url"] == "https://n8n.example.org:5678"
    # blank means "use the built-in default", not "post to an empty string"
    assert client.post("/settings", json={"n8n_url": ""}).status_code == 200
    assert client.get("/settings").get_json()["n8n_url"] == ""
    # GET hands the guides back in the shape POST accepted, so the admin form round-trips them
    # instead of re-serialising a stored JSON string on the next save.
    sent = [{"provider": "nvidia", "url": "https://build.nvidia.com/playground", "steps": "sign up"}]
    client.post("/settings", json={"api_key_guides": sent})
    assert client.get("/settings").get_json()["api_key_guides"] == sent


def test_settings_refuses_an_engine_target_the_forwarder_cannot_use(client):
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    for bad in ("localhost:5678", "http://", "not a url", "ftp://n8n.local/webhook"):
        r = client.post("/settings", json={"n8n_url": bad})
        assert r.status_code == 400, (bad, r.get_json())
        assert r.get_json()["field"] == "n8n_url"
    for bad in ("webhook/ai-detect-ai", "n8n.local:5678"):
        r = client.post("/settings", json={"webhook_path": bad})
        assert r.status_code == 400 and r.get_json()["field"] == "webhook_path"
    # ...and a paper link the ranking page would render as a relative href
    assert client.post("/settings", json={"paper_url": "zenodo.org/doi/1"}).status_code == 400
    # A rejected write leaves what was already stored alone.
    client.post("/settings", json={"n8n_url": "http://127.0.0.1:5678"})
    assert client.post("/settings", json={"n8n_url": "localhost:5678"}).status_code == 400
    assert client.get("/settings").get_json()["n8n_url"] == "http://127.0.0.1:5678"


def test_detect_reports_the_engine_target_when_it_cannot_reach_n8n(client, monkeypatch):
    """The 502 must name the URL the operator can fix, since that is the whole point of the field."""
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/settings", json={"n8n_url": "http://127.0.0.1:1",
                                   "webhook_path": "/webhook/nope"})
    r = client.post("/detect", json={"text": "Some text long enough to be sent to the engine."})
    assert r.status_code == 502
    body = r.get_json()
    assert "http://127.0.0.1:1/webhook/nope" in body["error"]
    assert body["error_code"] == "ENGINE_UNREACHABLE"


def test_a_slow_engine_is_not_reported_as_a_missing_one(client, monkeypatch):
    """A read timeout and a refused connect are different diagnoses with different cures.

    `forward()` catches (URLError, TimeoutError, OSError) in one branch, so before this split a lane
    that simply took longer than `timeout_ms + 30` answered "n8n unreachable … check Settings → engine
    URL". Measured latencies make that reachable in normal use: z-ai/glm-5.3 answers at ~269 s and
    gemma-4-31b-it 504s at ~302 s against a 120 s ceiling, and n8n's own listener is known to refuse
    under slow-pool load. Telling an admin to edit the URL when the URL is correct is worse than
    telling them the truth: the engine answered nothing *in time*.
    """
    import urllib.error

    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/settings", json={"n8n_url": "http://127.0.0.1:5678",
                                   "webhook_path": "/webhook/ai-detect-ai"})

    def raise_timeout(*a, **k):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
    r = client.post("/detect", json={"text": "Some text long enough to be sent to the engine."})
    assert r.status_code == 502
    body = r.get_json()
    assert body["error_code"] == "ENGINE_TIMEOUT", body
    assert body["classification"] == "DETECTION_UNAVAILABLE"     # the UI still says "no verdict"
    assert "http://127.0.0.1:5678" in body["error"]              # the URL is still named
    assert re.search(r"\b\d+ s\b", body["error"]), body["error"]  # and how long it waited
    assert "did not answer" in body["error"]
    assert "timeout_ms" in body["hint"]                          # the knob that governs it
    # The cure it names is the lane's timeout, never the URL. It is allowed to *mention* Settings to
    # say that field is fine — the point is that it must not describe a missing engine.
    assert "unreachable" not in body["hint"].lower()

    # The same route refusing a connection must keep the old label, or the split is worthless.
    # Spelled with the dead port so `forward()`'s reachability probe fails too — a refusal *and* no
    # listener is what "the URL is wrong" means.
    def refuse(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made"))

    client.post("/settings", json={"n8n_url": "http://127.0.0.1:1"})
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    body2 = client.post("/detect", json={"text": "Some text long enough to be sent to the engine."}).get_json()
    assert body2["error_code"] == "ENGINE_UNREACHABLE", body2
    assert "Settings" in body2["hint"]


def test_forward_labels_a_silent_but_accepting_engine_as_a_timeout():
    """The exception cannot say which phase timed out, so `forward()` asks the socket.

    A thread that `accept()`s and never replies is what a slow-pool lane looks like from the client
    seat, and what n8n's own listener does under stacked slow-pool webhooks. Measured 2026-10-16:
    this shape produced `detail='timed out'` — the same two words a refused connect produces — and
    every `glm-5.3` row of the primary-candidate probe was reported as "ENGINE_UNREACHABLE" while
    `/healthz` answered `ok` seconds later.

    Deliberately *not* a `listen()`-without-`accept()` socket: on this host the first handshake to a
    non-accepting listener succeeds and the next one hangs (both measured above), so that fixture
    would be testing the backlog, not the code. n8n always accepts.
    """
    import socket
    import threading

    from backend.app import forward

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    held = []

    def accept_forever():
        while True:
            try:
                held.append(srv.accept()[0])          # handshake completes, nothing is ever sent back
            except OSError:
                return

    threading.Thread(target=accept_forever, daemon=True).start()
    try:
        st, body, ms = forward(f"http://127.0.0.1:{port}/webhook/ai-detect-ai",
                               {"text": "hi"}, {}, timeout=1.0)
        assert st == 502 and body["error_code"] == "ENGINE_TIMEOUT", body
        assert body["classification"] == "DETECTION_UNAVAILABLE"
        assert "accepted the connection" in body["error"]
        assert body["timeout_s"] == 1
        # And once nothing is listening, the same helper has to call it unreachable instead.
        for c in held:
            c.close()
        srv.close()
        st2, body2, _ = forward(f"http://127.0.0.1:{port}/webhook/ai-detect-ai",
                                {"text": "hi"}, {}, timeout=1.0)
        assert st2 == 502 and body2["error_code"] == "ENGINE_UNREACHABLE", body2
    finally:
        try:
            srv.close()
        except OSError:
            pass


def test_ranking_scores_at_the_models_own_band(client):
    _login(client, "root", "rootpass123")
    pid = _seed_primary(client)
    # Give the primary model a band so ranking uses it, not the global default.
    m = client.get("/models").get_json()[0]
    client.patch(f"/models/{m['id']}", json={"ai_prob_threshold": 0.75,
                                             "human_prob_threshold": 0.30})
    # Seed a measured run directly through the DB layer the importer uses.
    from backend import db as _db
    with _db.session() as c:
        c.execute("INSERT INTO benchmark_runs (tag, provider, model, corpus, n_rows, source_file) "
                  "VALUES ('t1','nvidia','test/model','hc3-48',4,'f.jsonl')")
        rid = c.execute("SELECT id FROM benchmark_runs WHERE tag='t1'").fetchone()["id"]
        for label, p, conf in (("AI", 0.95, 0.9), ("AI", 0.80, 0.9),
                               ("HUMAN", 0.10, 0.9), ("HUMAN", 0.50, 0.9)):
            c.execute("INSERT INTO benchmark_results (run_id, text_id, label, ok, ai_probability, confidence) "
                      "VALUES (?,?,?,?,?,?)", (rid, f"x{p}", label, 1, p, conf))
    client.post("/auth/logout")

    _register(client, "wendy")
    body = client.get("/ranking").get_json()
    ranked = body["ranked"]
    assert ranked and ranked[0]["model"] == "test/model"
    top = ranked[0]
    # 0.95 & 0.80 -> AI (correct); 0.10 -> HUMAN (correct); 0.50 sits in the band -> review.
    assert top["correct"] == 3 and top["wrong"] == 0 and top["review"] == 1
    assert top["band"]["ai"] == 0.75 and top["band"]["human"] == 0.30
    assert top["rank"] == 1
    assert top["verified_key"] is False  # wendy has no key yet


def test_ranking_never_puts_a_probe_above_a_measured_model(client):
    """A 1-text probe with 0 wrong used to outrank a 48-text run with 1 wrong.

    `wrong`-first sorting rewards the small sample: a probe that happened to answer one text
    correctly has fewer errors than a full capture, so it lands higher on the page the admin is
    told is "the best result we found". Sample size is now a sorting tier, not just a caption:
    full-corpus rows are ranked among themselves first, probes follow, each labelled. A run with
    no clean row at all is not a measurement and is dropped instead of shown as "no score".
    """
    from backend import db as _db, seed

    def _run(tag, model, rows):
        with _db.session() as c:
            c.execute("INSERT INTO benchmark_runs (tag, provider, model, corpus, n_rows, source_file) "
                      "VALUES (?,?,?,?,?,?)", (tag, "nvidia", model, "hc3-48", len(rows), f"{tag}.jsonl"))
            rid = c.execute("SELECT id FROM benchmark_runs WHERE tag=?", (tag,)).fetchone()["id"]
            for i, (label, p) in enumerate(rows):
                c.execute("INSERT INTO benchmark_results (run_id, text_id, label, ok, ai_probability, confidence) "
                          "VALUES (?,?,?,?,?,?)", (rid, f"t{i}", label, 1, p, 0.9))

    _run("probe", "probe/model", [("AI", 0.95)])                       # 1 text, 0 wrong
    _run("full", "full/model", [("AI", 0.95)] * 47 + [("HUMAN", 0.95)])  # 48 texts, 1 wrong
    _run("dead", "dead/model", [])                                     # no clean row at all
    # The no-clean run needs a result row to exist at all (the importer skips empty runs).
    with _db.session() as c:
        rid = c.execute("SELECT id FROM benchmark_runs WHERE tag='dead'").fetchone()["id"]
        c.execute("INSERT INTO benchmark_results (run_id, text_id, label, ok, ai_probability, confidence) "
                  "VALUES (?, 'x', 'AI', 0, NULL, NULL)", (rid,))

    assert seed.HC3_CORPUS_SIZE == 48
    _register(client, "zoe")
    ranked = client.get("/ranking").get_json()["ranked"]

    assert [e["model"] for e in ranked] == ["full/model", "probe/model"]
    assert [e["rank"] for e in ranked] == [1, 2]
    assert ranked[0]["provisional"] is False and ranked[1]["provisional"] is True
    assert all(e["n_clean"] > 0 for e in ranked)

