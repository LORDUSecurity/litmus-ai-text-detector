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
from backend.tests._session import arm_csrf, fresh_throttle  # noqa: E402

TEXT = "This is a long enough sample of text for the detector to accept it."

# Sentinel meaning "confirm the same password I passed" -- a test that wants a mismatch or an
# omitted confirmation field passes its own value instead.
_SAME = object()


@pytest.fixture()
def app():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    a = create_app()
    a.config["TESTING"] = True
    a.config["AUTH_FORCE"] = True  # enforce the guard even under the test client
    return fresh_throttle(a)


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield arm_csrf(c)


def _register(client, handle, password="userpass123", name=None, email=None, confirm=_SAME):
    """Sign up the way the shipped form does: name, email, password typed twice.

    `handle` is kept as the argument name because it is still what a test reads back -- an email
    of `handle@example.org` derives the username `handle`, so the pre-email assertions hold. The
    other fields are overridable so a single test can send a bad one.
    """
    body = {
        "name": name if name is not None else f"Test {handle.title()}",
        "email": email if email is not None else f"{handle}@example.org",
        "password": password,
    }
    body["confirm"] = password if confirm is _SAME else confirm
    r = client.post("/auth/register", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["user"]


def _login(client, identifier, password):
    """Sign in by whatever the caller has: an email now, a legacy username still.

    A successful sign-in is also what arms this client to write: the session's CSRF token arrives
    on the response cookie, and the `client` fixture sends it back as a header on every later
    request, the way the SPA's interceptor does.
    """
    return client.post("/auth/login", json={"identifier": identifier, "password": password})


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
    # One built front-end serves both deployments, so it has to learn which one it is talking
    # to before anyone signs in -- that is what this public field is for.
    assert r.get_json()["auth_mode"] == "accounts"


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


def test_login_accepts_the_email_or_the_legacy_username(client):
    """One lookup, two spellings.

    The form asks for an e-mail, but the seeded admin and every account created before this
    change have only a username -- and so do the deploy/verify tools. Refusing the old spelling
    would log everyone out with no way back in from the UI.
    """
    _register(client, "kenji")
    client.post("/auth/logout")
    assert _login(client, "kenji@example.org", "userpass123").status_code == 200, "email is the way in now"
    client.post("/auth/logout")
    assert _login(client, "kenji", "userpass123").status_code == 200, "the derived handle still works"
    client.post("/auth/logout")
    # Case is folded on the stored address, so a caps-lock sign-in is not a failed one.
    assert _login(client, "KENJI@EXAMPLE.ORG", "userpass123").status_code == 200


def test_register_rejects_short_password(client):
    r = client.post("/auth/register", json={"name": "Carol Dan", "email": "carol@example.org",
                                            "password": "short", "confirm": "short"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "BAD_PASSWORD"


def test_register_rejects_a_malformed_email(client):
    # The form cannot verify an address, so the only honest check is the shape: no @, no domain,
    # no dot in the domain. Anything that parses as an address is accepted and simply never used
    # by this app -- there is no mail delivery here at all.
    for bad in ("carol", "carol@example", "carol @example.org", "@example.org", "carol.org"):
        r = client.post("/auth/register", json={"name": "Carol Dan", "email": bad,
                                                "password": "anotherpass1", "confirm": "anotherpass1"})
        assert r.status_code == 400, bad
        assert r.get_json()["error_code"] == "BAD_EMAIL", bad


def test_register_rejects_a_one_character_name(client):
    # A name is what the header chip and the admin's user list show, so "x" is not a person's
    # name -- but it is also the only field with no format to check, so length is the whole rule.
    r = client.post("/auth/register", json={"name": "X", "email": "x@example.org",
                                            "password": "anotherpass1", "confirm": "anotherpass1"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "BAD_NAME"


def test_register_rejects_two_different_passwords(client):
    r = client.post("/auth/register", json={"name": "Dana Ray", "email": "dana@example.org",
                                            "password": "firstpass123", "confirm": "secondpass1"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "PASSWORD_MISMATCH"


def test_register_accepts_a_password_it_cannot_confirm_only_because_the_field_is_absent(client):
    """`confirm` is the form's guard; an API caller that omits it is not being careless twice.

    Only a *present* confirmation that disagrees is refused, so a script posting
    {name, email, password} keeps working -- the shipped front-end always sends both.
    """
    r = client.post("/auth/register", json={"name": "Eli Van", "email": "eli@example.org",
                                            "password": "anotherpass1"})
    assert r.status_code == 201, r.get_json()


def test_register_rejects_a_duplicate_email(client):
    _register(client, "dave")
    r = client.post("/auth/register", json={"name": "David Other", "email": "dave@example.org",
                                            "password": "anotherpass1", "confirm": "anotherpass1"})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "EMAIL_TAKEN"


def test_register_derives_the_legacy_username_from_the_email(client):
    """The username column is not gone: pre-email accounts and the deploy tools still sign in
    with it, so a new account has to arrive with one. The local part is the obvious choice."""
    user = _register(client, "faythe")
    assert user["username"] == "faythe"
    assert user["email"] == "faythe@example.org"
    assert user["display_name"] == "Test Faythe"


def test_register_sanitizes_an_email_local_part_that_is_not_a_handle(client):
    # plus-addressing and dots cannot be a username verbatim ("a.b+c" is fine as a handle but
    # two accounts could collide), so the derivation folds it and adds a suffix on a clash.
    a = _register(client, "glytch", email="Glytch+news@example.org", name="Glytch Prime")
    assert a["username"], a
    assert a["username"].lower().startswith("glytch")
    # A second account with the same local part must not collide on the old unique column.
    b = client.post("/auth/register", json={"name": "Glytch Two", "email": "glytch@sample.org",
                                            "password": "anotherpass1", "confirm": "anotherpass1"})
    assert b.status_code == 201, b.get_json()
    assert b.get_json()["user"]["username"] != a["username"]


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


# ------------------------------------------------- a key for a self-hosted endpoint
#
# A gateway the deployment does not already serve is the one case where "which endpoint" is not a
# fact the app knows: an OmniRoute-style box lives wherever its owner put it, and the same product
# runs on 127.0.0.1:20128 for one person and on a hosted instance for another. So a key attached to
# a provider flagged `self_hosted` carries its own endpoint, and a key for NVIDIA, OpenRouter or
# Gemini still cannot — the guard that keeps a caller from naming an arbitrary endpoint for a
# common provider is the same guard, unchanged.
#
# The trust the flag buys is https-only: an https URL is accepted anywhere, because that is what
# "my gateway is on the web" means, while a plain http one still has to be in allowed_llm_hosts —
# which already lists loopback, so a local box works and a stranger's LAN address does not.

def _seed_gateway(client, name="omniroute", base_url="http://127.0.0.1:20128/v1",
                  self_hosted=True):
    """Admin creates a provider the way the shipped seed does, plus a primary model on it."""
    _login(client, "root", "rootpass123")
    body = {"name": name, "base_url": base_url}
    body["self_hosted"] = self_hosted
    pid = client.post("/providers", json=body).get_json()["id"]
    client.post("/models", json={"provider_id": pid, "model_id": "route/model", "lane": "primary"})
    client.post("/auth/logout")
    return pid


def _stub_probe_urls(monkeypatch, ok=True):
    """Record which endpoint each probe was told to dial, without dialing it."""
    import backend.app as app_mod
    seen = []

    def fake(prov, model_id, key, timeout_s):
        seen.append(prov["base_url"])
        return {"ok": ok, "status": 200 if ok else 401, "latency_ms": 5}

    monkeypatch.setattr(app_mod, "_probe_with_key", fake)
    return seen


def test_a_self_hosted_key_carries_its_own_endpoint(client, monkeypatch):
    pid = _seed_gateway(client)
    seen = _stub_probe_urls(monkeypatch)
    _register(client, "wes")
    r = client.post("/my/keys", json={
        "provider_id": pid, "api_key": "sk-route-123456", "label": "my box",
        "base_url": "https://gateway.example.net/v1", "model_id": "route/model"})
    assert r.status_code == 201, r.get_json()
    got = client.get("/my/keys").get_json()[0]
    # The endpoint is stored per key and read back: two owners of the same gateway row point at
    # two different boxes, and neither one edits the shared provider.
    assert got["base_url"] == "https://gateway.example.net/v1"
    assert got["label"] == "my box"
    assert seen == ["https://gateway.example.net/v1"]


def test_a_plain_http_endpoint_outside_the_allowlist_is_refused(client, monkeypatch):
    pid = _seed_gateway(client)
    _stub_probe_urls(monkeypatch)
    _register(client, "ivy")
    r = client.post("/my/keys", json={"provider_id": pid, "api_key": "sk-route-123456",
                                      "base_url": "http://10.0.0.5:20128/v1"})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error_code"] == "HOST_NOT_ALLOWED"


def test_a_loopback_endpoint_on_any_port_is_accepted(client, monkeypatch):
    pid = _seed_gateway(client)
    _stub_probe_urls(monkeypatch)
    _register(client, "kai")
    r = client.post("/my/keys", json={"provider_id": pid, "api_key": "sk-route-123456",
                                      "base_url": "http://127.0.0.1:29999/v1"})
    assert r.status_code == 201, r.get_json()
    assert client.get("/my/keys").get_json()[0]["base_url"] == "http://127.0.0.1:29999/v1"


def test_an_endpoint_that_is_not_a_gateway_url_is_refused(client, monkeypatch):
    pid = _seed_gateway(client)
    _stub_probe_urls(monkeypatch)
    _register(client, "lena")
    for bad in ("gateway.example.net/v1",              # no scheme: nothing to dial
                "https://user:***@gateway.example.net/v1",  # credentials in a URL the app will fetch
                "ftp://gateway.example.net/v1",        # not an HTTP gateway
                "https://gateway.example.net/v1/../../etc"):
        r = client.post("/my/keys", json={"provider_id": pid, "api_key": "sk-route-123456",
                                          "base_url": bad})
        assert r.status_code == 400, (bad, r.get_json())


def test_a_common_provider_still_refuses_a_supplied_endpoint(client, monkeypatch):
    """The flag is the whole difference: without it, a caller may not name an endpoint at all."""
    _seed_gateway(client, name="nvidia", base_url="https://integrate.api.nvidia.com/v1",
                  self_hosted=False)
    _stub_probe_urls(monkeypatch)
    _register(client, "omar")
    r = client.post("/my/keys", json={"provider_id": 1, "api_key": "nvapi-omar-123",
                                      "base_url": "https://attacker.example.com/v1"})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error_code"] == "PROVIDER_NOT_SUPPORTED"


def test_verify_reuses_the_endpoint_the_key_was_saved_with(client, monkeypatch):
    pid = _seed_gateway(client)
    seen = _stub_probe_urls(monkeypatch)
    _register(client, "pia")
    kid = client.post("/my/keys", json={
        "provider_id": pid, "api_key": "sk-route-123456",
        "base_url": "https://gateway.example.net/v1"}).get_json()["id"]
    r = client.post(f"/my/keys/{kid}/verify", json={"model_id": "route/model"})
    assert r.status_code == 200, r.get_json()
    assert seen[-1] == "https://gateway.example.net/v1"


def test_detect_dials_the_key_s_own_endpoint_for_a_self_hosted_lane(client, monkeypatch):
    """Saving the URL is the easy half; the detection is what the user actually pays for.

    The lane's base_url is resolved from the provider row (`provider_base_url_by_name`), so a key
    that stores its own endpoint changes nothing unless the resolve prefers the key. Otherwise the
    gateway the owner named is verified and then ignored, and the call goes to the seeded default.
    """
    _stub_forward(monkeypatch)
    import backend.app as app_mod
    real_forward = app_mod.forward
    captured = {}

    def capturing(url, payload, headers, timeout):
        captured["payload"] = payload
        captured["headers"] = headers
        return real_forward(url, payload, headers, timeout)

    monkeypatch.setattr(app_mod, "forward", capturing)
    _stub_probe_urls(monkeypatch)
    pid = _seed_gateway(client)
    _register(client, "rex")
    client.post("/my/keys", json={"provider_id": pid, "api_key": "sk-rex-123456",
                                  "base_url": "https://gateway.example.net/v1",
                                  "model_id": "route/model"})
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    assert captured["payload"]["lane_config"]["primary"]["base_url"] == "https://gateway.example.net/v1"
    assert captured["headers"].get("x-primary-api-key") == "sk-rex-123456"


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


def test_config_marks_which_endpoint_the_owner_names(client):
    """/config has to tell the key form which endpoint is a box the caller names for himself.

    A common provider lives at one URL, so a key for it is a key and nothing else. A self-hosted
    gateway is the row whose form carries endpoint and port, because the seeded address belongs to
    whichever machine runs it -- and it is the same column POST /my/keys checks before accepting a
    per-key override, so a page that guessed from the host name could offer a field the backend
    refuses.
    """
    _login(client, "root", "rootpass123")
    _seed_primary(client)
    client.post("/providers", json={"name": "omniroute",
                                    "base_url": "http://127.0.0.1:20128/v1",
                                    "self_hosted": True})
    by_name = {p["name"]: p for p in client.get("/config").get_json()["providers"]}
    assert by_name["omniroute"]["self_hosted"] is True
    assert by_name["nvidia"]["self_hosted"] is False


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


def test_healthz_is_the_public_liveness_route(client):
    """`backend/Dockerfile`'s HEALTHCHECK has to hit a route that answers without a session, or the
    container never becomes healthy and `docker compose up -d` dies on the web service's
    `service_healthy` dependency. It used to point at `/gateway-health`, which this guard protects;
    `/healthz` was already listed in `auth.PUBLIC_PATHS` but no route implemented it, so it 404'd --
    neither failure is visible to `docker compose config`. A fresh deploy only shows up when a real
    daemon runs, which is how #106 found it on 2026-10-17.

    The route stays information-free: the same file that leaks a container's DB path, engine URL and
    seeded lane defaults to an anonymous caller is a reconnaissance gift on a hosted deploy, so the
    rich answer remains behind a login.
    """
    r = client.get("/healthz")
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {"ok": True}

    guarded = client.get("/gateway-health")
    assert guarded.status_code == 401, \
        "the detailed status answers 200 to an anonymous caller"


# ------------------------------------------------------------------ single-owner local mode

def _as_local(monkeypatch, host="127.0.0.1"):
    """Turn the packaged exe's mode on for one test.

    The launcher sets both variables before it imports the app; `local_mode()` reads them per
    request, so a test can do the same. Both spellings have to be set together -- the flag alone
    is deliberately not enough (see test_local_mode_is_refused_on_a_shared_bind).
    """
    monkeypatch.setenv("LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("BACKEND_HOST", host)


def test_local_mode_serves_the_owner_with_no_session(client, monkeypatch):
    """The exe opens straight into the app: no sign-in card, and nothing behind it to fill in.

    There is no cookie in play at all -- the guard acts as the seeded admin. That includes the
    admin-only routes, because on their own machine the owner *is* the admin; it is where they
    paste their API key, and a mode that hid /providers would make the app unusable.
    """
    _as_local(monkeypatch)
    body = client.get("/auth/me").get_json()
    assert body["auth_mode"] == "local"
    assert body["user"] is not None and body["user"]["role"] == "admin"
    assert body["user"]["username"] == "root"

    # Protected and admin-only routes both answer, with no login first.
    assert client.get("/detections").status_code == 200
    assert client.post("/settings", json={"paper_url": "https://example.org/p"}).status_code == 200
    pid = client.post("/providers", json={
        "name": "nvidia", "base_url": "https://integrate.api.nvidia.com/v1"}).status_code
    assert pid == 201


def test_local_mode_closes_sign_up_because_there_is_nothing_to_sign_up_for(client, monkeypatch):
    _as_local(monkeypatch)
    r = client.post("/auth/register", json={"name": "Second Owner", "email": "second@example.org",
                                             "password": "userpass123", "confirm": "userpass123"})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "SINGLE_USER_MODE"


def test_local_mode_is_refused_on_a_shared_bind(client, monkeypatch):
    """`BACKEND_HOST` is an operator knob, and the flag must not beat it.

    Honouring LOCAL_SINGLE_USER on 0.0.0.0 would answer every request on the network as though
    the machine's owner had signed in -- one admin session shared by everyone who can reach the
    port, with their API key and their history. So the mode is only real while the bind address
    is a loopback one, which is the only way the launcher ever opens a browser. A loopback
    spelling in the 127.0.0.0/8 range counts, because Windows answers ::1 and 127.x alike.
    """
    _as_local(monkeypatch, host="0.0.0.0")
    body = client.get("/auth/me").get_json()
    assert body["auth_mode"] == "accounts"
    assert body["user"] is None
    assert client.post("/detect", json={"text": TEXT}).status_code == 401
    # Sign-up is available again, because a shared deploy is where accounts are the point.
    assert _register(client, "nina")["username"] == "nina"

    monkeypatch.setenv("BACKEND_HOST", "127.0.0.53")
    assert client.get("/auth/me").get_json()["auth_mode"] == "local", "the whole /8 is loopback"


def test_local_mode_needs_the_flag_not_just_a_loopback_bind(client, monkeypatch):
    # `docker run -p 8080:80` binds 0.0.0.0 *inside* the container and nginx reaches the backend
    # over a docker network name, so a loopback default cannot be the whole test for "is this the
    # exe". Without the flag the hosted build keeps its login gate.
    monkeypatch.delenv("LOCAL_SINGLE_USER", raising=False)
    monkeypatch.setenv("BACKEND_HOST", "127.0.0.1")
    assert client.get("/auth/me").get_json()["auth_mode"] == "accounts"
    assert client.post("/detect", json={"text": TEXT}).status_code == 401


# ------------------------------------------------------------------ in-place upgrade

LEGACY_USERS_DDL = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    theme         TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('dark','light')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
)
"""


def test_a_deployed_database_is_upgraded_in_place(tmp_path, monkeypatch):
    """An installed exe and a running container both arrive with a `users` table that has rows in it.

    `CREATE TABLE IF NOT EXISTS` cannot add a column to a table that already exists, so v5 has to
    migrate -- and the unique e-mail index has to be created *after* the ALTER, exactly like
    `idx_detections_user` before it. Declaring it in SCHEMA aborts `executescript` with
    "no such column: email" on every installed database, which is a boot failure, not a data
    failure: the app would refuse to start and the fix would be invisible from the UI.

    The account that was already there has to still be able to sign in afterwards.
    """
    import sqlite3

    legacy = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy)
    conn.execute(LEGACY_USERS_DDL)
    conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                 ("legacy_admin", db.hash_password("legacypass1"), "admin"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", legacy)
    monkeypatch.setattr(db, "SECRET_PATH", tmp_path / "legacy-secret.key")
    db._fernet = None
    try:
        db.init_db()  # used to raise here
        cols = {r["name"] for r in db.connect().execute("PRAGMA table_info(users)")}
        assert {"email", "display_name"} <= cols
        with db.session() as c:
            row = db.get_user_by_identifier(c, "legacy_admin")
            assert row is not None and db.check_password("legacypass1", row["password_hash"])
            assert row["email"] is None, "an upgraded account keeps only what it had"
            idx = {r["name"] for r in c.execute("PRAGMA index_list(users)")}
            assert "idx_users_email" in idx, "the uniqueness rule did not survive the upgrade"

        a = create_app()
        a.config["TESTING"] = True
        a.config["AUTH_FORCE"] = True
        with arm_csrf(a.test_client()) as client:
            # the pre-existing account signs in by its username, and now by an address once given one
            assert _login(client, "legacy_admin", "legacypass1").status_code == 200
            # a brand-new signup on the upgraded database works and is guarded against duplicates
            assert _register(client, "omega")["email"] == "omega@example.org"
            dup = client.post("/auth/register", json={
                "name": "Omega Two", "email": "omega@example.org",
                "password": "userpass123", "confirm": "userpass123"})
            assert dup.status_code == 409 and dup.get_json()["error_code"] == "EMAIL_TAKEN"
    finally:
        db._fernet = None

