"""Backend tests. Run from ai-text-detector/:  python -m pytest backend/tests -q

These use a throwaway SQLite file and a stubbed n8n forwarder, so they never call a
provider and never touch the live n8n database.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-test-")
os.environ["DETECTOR_DB"] = os.path.join(_TMP, "test.sqlite3")
os.environ["DETECTOR_SECRET"] = os.path.join(_TMP, "test-secret.key")
os.environ["N8N_DB"] = os.path.join(_TMP, "no-such-n8n.sqlite")

import json  # noqa: E402
import sqlite3  # noqa: E402
import urllib.error  # noqa: E402

import pytest  # noqa: E402

from backend import db  # noqa: E402
from backend.app import create_app, purger  # noqa: E402

PLAINTEXT = "nvapi-TESTKEY-test-1234567890abcdef"


@pytest.fixture()
def client():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _provider(client, name="nvidia", base_url="https://integrate.api.nvidia.com/v1"):
    r = client.post("/providers", json={"name": name, "base_url": base_url})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _model(client, pid, model_id, lane=None, **kw):
    body = {"provider_id": pid, "model_id": model_id, "lane": lane}
    body.update(kw)
    r = client.post("/models", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _stub_forward(monkeypatch, captured, status=200, body=None):
    import backend.app as app_mod

    def fake(url, payload, headers, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        captured["timeout"] = timeout
        return status, (body if body is not None else {
            "classification": "AI_GENERATED", "ai_probability": 0.97, "confidence": 0.9,
            "method": "PRIMARY_DECIDED_AI", "review_required": False,
            "config_used": {"primary": {"model": payload["lane_config"]["primary"]["model"]}},
            "_internal": {"text_fingerprint": "fp", "text_length": 42},
        }), 123.4

    monkeypatch.setattr(app_mod, "forward", fake)


TEXT = "This is a long enough sample of text for the detector to accept it."


# ---------------------------------------------------------------- health / contract

def test_gateway_health_carries_the_fields_the_angular_service_reads(client):
    body = client.get("/gateway-health").get_json()
    for field in ("ok", "purge_enabled", "rows_purged", "newest_id", "startup_max_id",
                  "quiet_sweeps", "workflow_id"):
        assert field in body, f"missing {field}"
    assert body["ok"] is True
    assert body["backend"] == "flask"
    assert "db_counts" in body and "n8n" in body


def test_purger_is_not_started_by_an_import(client):
    assert purger._thread is None


def test_purger_start_clears_the_wait_message_once_the_database_appears(tmp_path, monkeypatch):
    """A purger that had to wait must not keep reporting the reason it waited.

    /gateway-health answers `purge_enabled` next to `purge_error`, so a stale "n8n has not
    created ... yet" line beside `purge_enabled: true` reads as a healthy-but-broken purger.
    """
    from backend import purge

    db_file = tmp_path / "database.sqlite"          # n8n's own file, created later
    monkeypatch.setattr(purge, "N8N_DB", str(db_file))

    waiting = purge.Purger()
    waiting.start()
    assert waiting.enabled is False
    assert "has not created" in (waiting.error or ""), waiting.error
    waiting._stop.set()                             # kill the wait thread this test spawned

    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE execution_entity (id INTEGER PRIMARY KEY, workflowId TEXT)")
    con.execute("INSERT INTO execution_entity (workflowId) VALUES ('ai-detection-ai-only')")
    con.commit()
    con.close()

    p = purge.Purger()
    p.error = waiting.error                          # the leftover message from the wait path
    p.start()
    assert p.enabled is True, p.error
    assert p.error is None, f"a successful start kept {p.error!r}"
    assert p.startup_max_id == 1
    p._stop.set()


def test_purger_clears_rows_that_predate_it(tmp_path, monkeypatch):
    """A restart must not strand a key: rows written before this process are purged too.

    The sweep deletes `id > startup_max_id`, so without the startup pass every execution that was
    mid-flight when the backend restarted kept its webhook row -- and with it `execution_data.data`,
    which carries the inbound `x-primary-api-key` -- for the life of the volume. The schema here
    mirrors n8n's: the cascade is what makes the parent delete remove the blob.
    """
    from backend import purge

    db_file = tmp_path / "database.sqlite"
    monkeypatch.setattr(purge, "N8N_DB", str(db_file))
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE execution_entity (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " workflowId TEXT NOT NULL, status TEXT NOT NULL)")
    con.execute("CREATE TABLE execution_data (executionId INTEGER PRIMARY KEY,"
                " data TEXT NOT NULL, FOREIGN KEY(executionId) REFERENCES execution_entity(id)"
                " ON DELETE CASCADE)")
    leaked = '{"headers": {"x-primary-api-key": "nvapi-LEAKED"}}'
    for wf, status, blob in ((purge.WORKFLOW_ID, "running", leaked),
                             (purge.WORKFLOW_ID, "crashed", leaked),
                             ("someone-else's-workflow", "crashed",
                              '{"headers": {"x-primary-api-key": "other-workflow-key"}}')):
        con.execute("INSERT INTO execution_entity (workflowId, status) VALUES (?, ?)", (wf, status))
        con.execute("INSERT INTO execution_data (executionId, data) VALUES (?, ?)",
                    (con.execute("SELECT last_insert_rowid()").fetchone()[0], blob))
    con.commit()
    con.close()

    p = purge.Purger()
    p.start()          # opens, records the workflow-scoped floor, then purges everything under it
    assert p.startup_max_id == 2, "the floor is MAX(id) for THIS workflow, not the whole table"
    assert p.leftovers_purged == 2, p.status()
    assert p.status()["startup_rows_purged"] == 2

    ro = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    left = ro.execute("SELECT workflowId FROM execution_entity").fetchall()
    blobs = ro.execute("SELECT data FROM execution_data").fetchall()
    ro.close()
    p._stop.set()
    assert [w[0] for w in left] == ["someone-else's-workflow"], left
    assert not any("nvapi-LEAKED" in b[0] for b in blobs), "a deleted parent left its key blob"


# ---------------------------------------------------------------- detection routing

def test_detect_rejects_short_text_without_calling_n8n(client, monkeypatch):
    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": "short"})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "INPUT_REJECTED"
    assert captured == {}


def test_detect_without_a_primary_model_is_a_409_not_a_500(client, monkeypatch):
    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 409
    assert r.get_json()["error_code"] == "NO_PRIMARY_MODEL"
    assert captured == {}


def test_detect_builds_lane_config_from_the_db_and_injects_the_stored_key(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
           ai_prob_threshold=0.69, human_prob_threshold=0.40, timeout_ms=90000)
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary",
           secondary_decision_threshold=0.90)
    r = client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT, "label": "primary lane"})
    assert r.status_code == 201

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()

    lane = captured["payload"]["lane_config"]
    assert lane["primary"]["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert lane["primary"]["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert lane["secondary"]["model"] == "google/diffusiongemma-26b-a4b-it"
    assert captured["headers"]["x-primary-api-key"] == PLAINTEXT
    assert captured["headers"]["x-secondary-api-key"] == PLAINTEXT
    assert captured["url"].endswith("/webhook/ai-detect-ai")
    # the key must not travel back to the browser
    assert PLAINTEXT not in r.get_data(as_text=True)
    assert r.get_json()["backend_detection_id"] >= 1


def test_explicit_lane_config_from_the_caller_is_passed_through_untouched(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})
    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://openrouter.ai/api/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b:free"}}
    r = client.post("/detect", json={"text": TEXT, "lane_config": override})
    assert r.status_code == 200
    # The caller's primary entry is not touched -- no rewritten URL, no invented fields. What
    # does get added is the off-flag for the lane this install has no enabled row for: the
    # engine's own env default names a second model, so an absent lane would still run it.
    lane = captured["payload"]["lane_config"]
    assert lane["primary"] == override["primary"]
    assert lane["secondary"] == {"enabled": False}
    # An unknown model has no stored row, so there are no stored parameters to send.
    assert "tuning" not in captured["payload"]


def test_a_caller_cannot_resurrect_a_tie_break_lane_the_admin_switched_off(client, monkeypatch):
    """The Ranking tab's "use this model" sends a primary-only `lane_config`, and the deployed
    engine fills an omitted secondary from its own env default -- so before this rule, picking a
    scorer quietly bought back the second call the operator had switched off (round 25c: 3 texts
    asked, 3 answered wrong). Naming a second lane in the body is not an enablement; only the
    model row is."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary", enabled=False)
    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "nvidia/nemotron-3-ultra-550b-a55b"},
        "secondary": {"model": "google/diffusiongemma-26b-a4b-it"}}})
    assert r.status_code == 200, r.get_json()
    lane = captured["payload"]["lane_config"]
    assert lane["secondary"] == {"enabled": False}, lane
    assert "x-secondary-api-key" not in captured["headers"]
    row = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    # A lane that never ran must not be described as having run.
    assert row["secondary_model"] is None
    # Turning it back on through the row works, and that is the admin's route.
    sec_id = [m["id"] for m in client.get("/models").get_json()
              if m["lane"] == "secondary"][0]
    assert client.patch(f"/models/{sec_id}", json={"enabled": True}).status_code == 200
    captured.clear()
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "nvidia/nemotron-3-ultra-550b-a55b"}}}).status_code == 200
    assert captured["payload"]["lane_config"].get("secondary") != {"enabled": False}


def test_a_single_lane_override_still_records_the_lane_that_actually_ran(client, monkeypatch):
    """The client names one lane; the admin's enabled tie-break row supplies the other and the
    workflow answers with both. The stored row has to say which model decided and which cuts
    produced it, or a history entry cannot be re-read against the config that made it (real case:
    backend row 22 was a TIE_BREAK_AI recorded with secondary_model NULL).

    With the row switched off instead, the same request sends `secondary: {enabled: false}` and no
    second model is attributed -- see test_a_caller_cannot_resurrect_a_tie_break_lane_the_admin_switched_off.
    Filling from the engine's echo is not a fallback here: it echoes its env default even for a
    lane that never ran, which once recorded "diffusiongemma + diffusiongemma" on a single-lane
    detection."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary",
           secondary_decision_threshold=0.90)
    captured = {}
    tie_break_body = {
        "classification": "AI_GENERATED", "ai_probability": 0.98, "confidence": 0.95,
        "method": "TIE_BREAK_AI", "review_required": False,
        "component_scores": {"deciding_lane": "secondary"},
        "config_used": {
            "primary": {"model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                        "base_url": "https://openrouter.ai/api/v1", "key_source": "header"},
            "secondary": {"model": "google/diffusiongemma-26b-a4b-it",
                          "base_url": "https://integrate.api.nvidia.com/v1", "key_source": "env"},
            "thresholds": {"ai_prob": 0.69, "human_prob": 0.4, "secondary_decision": 0.9},
            "sampling": {"temperature": 0.1, "top_p": 0.9, "max_tokens": 700},
            "tuning_applied": {},
        },
        "_internal": {"text_fingerprint": "fp", "text_length": 42},
    }
    _stub_forward(monkeypatch, captured, body=tie_break_body)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"base_url": "https://openrouter.ai/api/v1",
                    "model": "nvidia/nemotron-3-ultra-550b-a55b:free"}}})
    assert r.status_code == 200
    row = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert row["secondary_model"] == "google/diffusiongemma-26b-a4b-it"
    assert row["payload"]["config_used"]["thresholds"]["secondary_decision"] == 0.9
    assert row["payload"]["config_used"]["secondary"]["key_source"] == "env"
    # Only the allowlisted fields survive: config_used is stored as a projection, so a future
    # key-bearing field in the workflow's echo cannot ride into the database by accident.
    assert "api_key" not in json.dumps(row["payload"]["config_used"])


def test_a_secondary_lane_switched_off_is_not_recorded_as_having_run(client, monkeypatch):
    """Measured on the packaged exe, 2026-10-16 17:25 UTC: a single-lane detection (the caller
    sends `secondary: {enabled: false}`) was stored with `secondary_model` filled from the
    workflow's env echo, so the History row read "diffusiongemma + diffusiongemma" and claimed a
    tie-break that never happened. The fill-in above is for a lane the caller did not name; a lane
    the caller explicitly disabled must stay absent."""
    pid = _provider(client)
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="primary")
    captured = {}
    single_lane_body = {
        "classification": "AI_GENERATED", "ai_probability": 0.98, "confidence": 0.95,
        "method": "SINGLE_MODEL_PRIMARY_AI", "review_required": False,
        "component_scores": {"deciding_lane": "primary", "secondary_called": False},
        # the workflow still echoes its env default for the off lane
        "config_used": {
            "primary": {"model": "google/diffusiongemma-26b-a4b-it",
                        "base_url": "https://integrate.api.nvidia.com/v1", "key_source": "header"},
            "secondary": {"model": "google/diffusiongemma-26b-a4b-it",
                          "base_url": "https://integrate.api.nvidia.com/v1",
                          "key_source": "env", "enabled": False},
        },
        "_internal": {"text_fingerprint": "fp", "text_length": 42},
    }
    _stub_forward(monkeypatch, captured, body=single_lane_body)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "google/diffusiongemma-26b-a4b-it"},
        "secondary": {"enabled": False}}})
    assert r.status_code == 200, r.get_json()
    assert captured["payload"]["lane_config"]["secondary"] == {"enabled": False}

    det = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert det["secondary_model"] is None, det["secondary_model"]
    assert det["deciding_lane"] == "primary"
    listed = client.get("/detections").get_json()[0]
    assert listed["secondary_model"] is None


def test_an_absent_tie_break_row_switches_the_lane_off_rather_than_delegating_it(
        client, monkeypatch):
    """`lane_config.secondary` absent does not mean "off" -- it means "the workflow decides",
    and the workflow's own default names a second model. So a lane the admin disabled was still
    being called, still getting the stored key, and still overturning the primary. The shipped
    default now disables the tie-break (round 25c: 3 texts asked, 3 answered wrong), which makes
    this the request every install sends -- the off-flag has to be explicit."""
    pid = _provider(client)
    _model(client, pid, "z-ai/glm-5.3-flash", lane="primary",
           ai_prob_threshold=0.80, human_prob_threshold=0.55, timeout_ms=180000)
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary", enabled=False)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT, "label": "both lanes"})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()

    lane = captured["payload"]["lane_config"]
    assert lane["secondary"] == {"enabled": False}, lane["secondary"]
    assert "x-secondary-api-key" not in captured["headers"]
    assert captured["headers"]["x-primary-api-key"] == PLAINTEXT


def test_an_enabled_tie_break_row_still_builds_a_two_lane_request(client, monkeypatch):
    """The switch is what changed, not the machinery: enable the row and the deployed
    second-opinion lane comes back exactly as before, key and all."""
    pid = _provider(client)
    _model(client, pid, "z-ai/glm-5.3-flash", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary",
           secondary_decision_threshold=0.90)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})
    captured = {}
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    lane = captured["payload"]["lane_config"]
    assert lane["secondary"]["model"] == "google/diffusiongemma-26b-a4b-it"
    assert "enabled" not in lane["secondary"] or lane["secondary"]["enabled"] is not False
    assert captured["headers"]["x-secondary-api-key"] == PLAINTEXT


def test_an_over_long_note_is_rejected_not_silently_cut(client):
    """The notes column is the only record of how a band was measured.

    `str(b["notes"])[:1000]` used to drop the tail with no signal, and the tail is where the
    caveats live -- "in-sample", "tightest margin 0.02", "do not assign a lane". A writer that
    quietly loses them leaves a number in the app that reads like a validated measurement.
    """
    pid = _provider(client)
    m = _model(client, pid, "vendor/long-note-model", notes="short note")

    too_long = "x" * 1001
    r = client.patch(f"/models/{m['id']}", json={"notes": too_long})
    assert r.status_code == 400, r.get_json()
    assert "1000" in (r.get_json().get("error") or ""), r.get_json()

    # Nothing was written, so the row still carries the note it had.
    after = [x for x in client.get("/models").get_json() if x["id"] == m["id"]][0]
    assert after["notes"] == "short note"

    # The same limit is enforced on create, where the truncation was also silent.
    r2 = client.post("/models", json={"provider_id": pid, "model_id": "vendor/other",
                                      "notes": too_long})
    assert r2.status_code == 400, r2.get_json()

    # Exactly at the limit is fine: the cap is a storage bound, not a style rule.
    assert client.patch(f"/models/{m['id']}", json={"notes": "y" * 1000}).status_code == 200


def test_lane_config_naming_a_stored_model_uses_that_models_provider_endpoint(client, monkeypatch):
    """Naming the model picks its gateway too, not just its tuning and its key.

    Observed live: lane_config.primary = {"model": "inclusionai/ling-3.1-flash"} (an
    OpenRouter model) shipped with base_url from the workflow's env default --
    https://integrate.api.nvidia.com/v1 -- so the gateway answered the plain-text
    "404 page not found" and the detection silently fell back to the secondary lane.
    The stored key was OpenRouter's, so the credential and the host disagreed.
    """
    opid = _provider(client, name="openrouter", base_url="https://openrouter.ai/api/v1")
    _model(client, opid, "inclusionai/ling-3.1-flash", ai_prob_threshold=0.63)
    client.post(f"/providers/{opid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "inclusionai/ling-3.1-flash"}}})
    assert r.status_code == 200
    lane = captured["payload"]["lane_config"]["primary"]
    assert lane["base_url"] == "https://openrouter.ai/api/v1", lane
    assert captured["headers"]["x-primary-api-key"] == PLAINTEXT


def test_an_explicit_base_url_from_the_caller_still_wins(client, monkeypatch):
    """A caller that names its own endpoint gets that endpoint -- the DB fills a gap,
    it does not overrule. This is how bring-your-own-endpoint requests work."""
    opid = _provider(client, name="openrouter", base_url="https://openrouter.ai/api/v1")
    _model(client, opid, "inclusionai/ling-3.1-flash")

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "inclusionai/ling-3.1-flash",
                    "base_url": "https://integrate.api.nvidia.com/v1"}}})
    assert r.status_code == 200
    assert captured["payload"]["lane_config"]["primary"]["base_url"] == \
        "https://integrate.api.nvidia.com/v1"


def test_a_model_row_pointing_at_a_non_allowed_host_is_refused(client, monkeypatch):
    """A DB-derived endpoint is a new outbound target, so it passes the same gate the
    no-lane_config path uses -- storing a provider row must not be a way to make the
    server call an arbitrary host through the UI."""
    opid = _provider(client, name="evil", base_url="https://not-allowed.example/v1")
    _model(client, opid, "evil/model")

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT,
                                     "lane_config": {"primary": {"model": "evil/model"}}})
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["error_code"] == "LANE_CONFIG_INVALID"
    assert "url" not in captured


def test_lane_config_naming_a_stored_model_still_carries_that_models_tuning(client, monkeypatch):
    """The Angular form always sends lane_config, so matching the model id is the
    only way the saved parameters can reach a request made from the page."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
           temperature=0.4, top_p=0.7, max_tokens=300, ai_prob_threshold=0.55)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://integrate.api.nvidia.com/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b"}}
    r = client.post("/detect", json={"text": TEXT, "lane_config": override})
    assert r.status_code == 200
    tuning = captured["payload"]["tuning"]
    assert tuning["temperature"] == 0.4
    assert tuning["top_p"] == 0.7
    assert tuning["max_tokens"] == 300
    assert tuning["ai_prob_threshold"] == 0.55


def test_lane_config_sends_the_stored_key_for_that_provider(client, monkeypatch):
    """The page names its lanes explicitly, so the encrypted store has to be read on
    that path too -- otherwise every browser detection silently runs on whatever key
    the n8n environment happens to hold, and the Configuration tab is decoration."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT, "label": "primary lane"})

    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://integrate.api.nvidia.com/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b"}}
    r = client.post("/detect", json={"text": TEXT, "lane_config": override})
    assert r.status_code == 200
    assert captured["headers"]["x-primary-api-key"] == PLAINTEXT
    assert PLAINTEXT not in r.get_data(as_text=True)


def test_lane_config_without_a_stored_key_sends_no_header(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://integrate.api.nvidia.com/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b"}}
    client.post("/detect", json={"text": TEXT, "lane_config": override})
    assert "x-primary-api-key" not in captured["headers"]


def test_a_stored_key_never_travels_to_another_providers_host(client, monkeypatch):
    """Keys are per account and hosts are per provider, so a lane that names a
    different endpoint must not be handed the credential of the one it left."""
    nv = _provider(client)
    or_pid = _provider(client, name="openrouter", base_url="https://openrouter.ai/api/v1")
    _model(client, nv, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, or_pid, "nvidia/nemotron-3-ultra-550b-a55b:free", lane="secondary")
    client.post(f"/providers/{nv}/keys", json={"api_key": PLAINTEXT})
    or_key = "sk-or-v1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    client.post(f"/providers/{or_pid}/keys", json={"api_key": or_key})

    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {
        "primary": {"base_url": "https://openrouter.ai/api/v1",
                    "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
        "secondary": {"base_url": "https://integrate.api.nvidia.com/v1",
                      "model": "nvidia/nemotron-3-ultra-550b-a55b"},
    }
    client.post("/detect", json={"text": TEXT, "lane_config": override})
    assert captured["headers"]["x-primary-api-key"] == or_key
    assert captured["headers"]["x-secondary-api-key"] == PLAINTEXT
    assert PLAINTEXT not in str(captured["headers"]["x-primary-api-key"])


def test_caller_supplied_key_beats_the_stored_one(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})
    byok = "nvapi-BROWSERKEY-0987654321fedcba"
    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://integrate.api.nvidia.com/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b"}}
    client.post("/detect", json={"text": TEXT, "lane_config": override},
                headers={"x-primary-api-key": byok})
    assert captured["headers"]["x-primary-api-key"] == byok


def test_off_secondary_lane_is_sanitized_and_gets_no_key_or_tuning(client, monkeypatch):
    """The UI's optional-tie-break switch has to survive the gateway as exactly
    {"enabled": false}: no leftover base_url the workflow could still call, no
    stored key spent on a call that never happens, and no tuning read from the
    off lane's row steering a verdict it will never supply."""
    nv = _provider(client)
    or_pid = _provider(client, name="openrouter", base_url="https://openrouter.ai/api/v1")
    _model(client, nv, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, or_pid, "google/diffusiongemma-26b-a4b-it", lane="secondary",
           secondary_decision_threshold=0.90)
    client.post(f"/providers/{nv}/keys", json={"api_key": PLAINTEXT})
    or_key = "sk-or-v1-" + "b" * 40
    client.post(f"/providers/{or_pid}/keys", json={"api_key": or_key})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={
        "text": TEXT,
        "lane_config": {
            "primary": {"base_url": "https://integrate.api.nvidia.com/v1",
                        "model": "nvidia/nemotron-3-ultra-550b-a55b"},
            "secondary": {"base_url": "https://openrouter.ai/api/v1",
                          "model": "google/diffusiongemma-26b-a4b-it",
                          "enabled": False},
        },
    })
    assert r.status_code == 200, r.get_json()

    lc = captured["payload"]["lane_config"]
    assert lc["secondary"] == {"enabled": False}, lc
    assert lc["primary"]["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert captured["headers"].get("x-primary-api-key") == PLAINTEXT
    assert "x-secondary-api-key" not in captured["headers"]
    tuning = captured["payload"].get("tuning") or {}
    assert "secondary_decision_threshold" not in tuning
    # The stored OpenRouter key must not appear anywhere in what leaves the gateway.
    assert or_key not in json.dumps(captured["headers"])


def test_a_lane_cannot_tie_break_with_itself(client, monkeypatch):
    """Choosing the deployed tie-break model as the scorer on the Ranking tab leaves the panel's
    secondary lane pointing at that same model, so one model would be asked to confirm its own
    answer: the second call is identical input and identical tuning, therefore worth nothing as
    agreement -- and the history row would claim a two-lane decision that never happened.
    The off-switch is applied here rather than trusting every client to notice the collision."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "google/diffusiongemma-26b-a4b-it"},
        "secondary": {"base_url": "https://integrate.api.nvidia.com/v1",
                      "model": "google/diffusiongemma-26b-a4b-it"},
    }})
    assert r.status_code == 200, r.get_json()
    lc = captured["payload"]["lane_config"]
    assert lc["primary"]["model"] == "google/diffusiongemma-26b-a4b-it"
    assert lc["primary"]["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert lc["secondary"] == {"enabled": False}, lc
    assert "secondary_decision_threshold" not in (captured["payload"].get("tuning") or {})

    det = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert det["secondary_model"] is None, det["secondary_model"]
    assert det["primary_model"] == "google/diffusiongemma-26b-a4b-it"


def test_an_omitted_secondary_lane_still_cannot_tie_break_with_the_picked_model(client, monkeypatch):
    """The collision has a second shape, and the packaged first boot hit it on 2026-10-16: a caller
    that names only a primary leaves `lane_config.secondary` absent, so the *workflow* answers with
    its own default tie-break -- which is the model the caller just picked as the scorer. Nothing was
    compared, yet the run silently cost a second identical call and the history row recorded
    `secondary_model` = the picked model, claiming a corroboration that never happened. The backend
    knows the deployed tie-break, so it has to check against that too, not only against what the
    client happened to send."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "google/diffusiongemma-26b-a4b-it"},
    }})
    assert r.status_code == 200, r.get_json()
    lc = captured["payload"]["lane_config"]
    assert lc["primary"]["model"] == "google/diffusiongemma-26b-a4b-it"
    # Sent explicitly off, not merely absent: absent means "use the workflow's default", which is
    # exactly the self-corroboration being prevented.
    assert lc["secondary"] == {"enabled": False}, lc
    assert "secondary_decision_threshold" not in (captured["payload"].get("tuning") or {})

    det = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert det["secondary_model"] is None, det["secondary_model"]


def test_a_picked_model_that_is_not_the_tie_break_keeps_the_deployed_one(client, monkeypatch):
    """The guard above must not turn the tie-break off for every explicit primary -- that would
    quietly disable the ensemble for the models it was added to protect."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary")
    _model(client, pid, "z-ai/glm-5.3-flash", lane=None)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "z-ai/glm-5.3-flash"},
    }})
    assert r.status_code == 200, r.get_json()
    lc = captured["payload"]["lane_config"]
    assert lc["primary"]["model"] == "z-ai/glm-5.3-flash"
    assert lc.get("secondary") != {"enabled": False}, lc
    det = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert det["secondary_model"] == "google/diffusiongemma-26b-a4b-it", det["secondary_model"]


# ---------------------------------------------------------------- stored tuning

def test_detect_sends_the_stored_tuning_parameters(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
           temperature=0.4, top_p=0.7, max_tokens=300, timeout_ms=45000,
           ai_prob_threshold=0.55, ai_conf_threshold=0.5,
           human_prob_threshold=0.30, human_conf_threshold=0.45)
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary",
           secondary_decision_threshold=0.93)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200

    assert captured["payload"]["tuning"] == {
        "temperature": 0.4, "top_p": 0.7, "max_tokens": 300,
        "request_timeout_ms": 45000,
        "ai_prob_threshold": 0.55, "ai_conf_threshold": 0.5,
        "human_prob_threshold": 0.30, "human_conf_threshold": 0.45,
        "secondary_decision_threshold": 0.93,
    }
    # The Flask-side wait has to cover the tuned timeout, not the 90s default.
    assert captured["timeout"] == 75.0


def test_a_stored_zero_token_cap_is_sent_as_zero_not_dropped(client, monkeypatch):
    """max_tokens 0 means "send no cap", so it has to survive the trip as a value.

    A falsy filter (`if value`) would drop it and the workflow's baked 700 would
    silently win: the answer truncates mid-JSON and the lane reports no phrases at
    all, which is exactly the failure the uncapped default exists to remove.
    """
    pid = _provider(client)
    m = _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
               max_tokens=700)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["payload"]["tuning"]["max_tokens"] == 700

    assert client.patch(f"/models/{m['id']}", json={"max_tokens": 0}).status_code == 200
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["payload"]["tuning"]["max_tokens"] == 0


def test_the_tie_break_cut_comes_from_the_secondary_row(client, monkeypatch):
    """It is read on the secondary's own probability scale, so the primary's
    value for it would be a category error."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
           secondary_decision_threshold=0.60)
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary",
           secondary_decision_threshold=0.93)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["payload"]["tuning"]["secondary_decision_threshold"] == 0.93


def test_editing_a_threshold_in_the_db_changes_what_the_workflow_is_told(client, monkeypatch):
    pid = _provider(client)
    m = _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
               ai_prob_threshold=0.69)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["payload"]["tuning"]["ai_prob_threshold"] == 0.69

    r = client.patch(f"/models/{m['id']}", json={"ai_prob_threshold": 0.42})
    assert r.status_code == 200, r.get_json()
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["payload"]["tuning"]["ai_prob_threshold"] == 0.42


def test_a_caller_cannot_override_the_stored_tuning(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
           ai_prob_threshold=0.69)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={
        "text": TEXT, "tuning": {"ai_prob_threshold": 0.01, "temperature": 2.0}})
    assert r.status_code == 200
    assert captured["payload"]["tuning"]["ai_prob_threshold"] == 0.69
    assert captured["payload"]["tuning"].get("temperature") == 0.1


def test_caller_tuning_is_honoured_alongside_an_explicit_lane_config(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://openrouter.ai/api/v1",
                            "model": "nvidia/nemotron-3-ultra-550b-a55b:free"}}
    r = client.post("/detect", json={"text": TEXT, "lane_config": override,
                                     "tuning": {"temperature": 0.3}})
    assert r.status_code == 200
    assert captured["payload"]["tuning"] == {"temperature": 0.3}


def test_a_malformed_tuning_block_from_the_caller_is_dropped_not_forwarded(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})

    captured = {}
    _stub_forward(monkeypatch, captured)
    override = {"primary": {"base_url": "https://openrouter.ai/api/v1", "model": "m:free"}}
    r = client.post("/detect", json={"text": TEXT, "lane_config": override,
                                     "tuning": ["not", "an", "object"]})
    assert r.status_code == 200
    assert "tuning" not in captured["payload"]


def test_a_disallowed_provider_host_is_refused_before_any_upstream_call(client, monkeypatch):
    pid = _provider(client, name="evil", base_url="https://openrouter.ai.evil.com/v1")
    _model(client, pid, "some/model", lane="primary")
    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 400
    assert r.get_json()["error_code"] == "LANE_CONFIG_INVALID"
    assert captured == {}


def test_n8n_failure_is_surfaced_as_502_and_still_recorded(client, monkeypatch):
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "forward",
                        lambda *a, **k: (502, {"error": "n8n unreachable"}, 5.0))
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 502
    rows = client.get("/detections").get_json()
    assert len(rows) == 1 and rows[0]["http_status"] == 502


def test_a_transport_failure_records_elapsed_time_in_milliseconds(monkeypatch):
    """forward() returns milliseconds on every path. On 2026-10-16 only the success paths multiplied
    by 1000, so a 120 s timeout was stored as 120 and the History row read "120 ms" for two minutes.
    A scripted clock makes the unit exact instead of timing a real socket."""
    import backend.app as app_mod

    class Clock:
        def __init__(self, values):
            self.values, self.i = values, 0

        def time(self):
            v = self.values[min(self.i, len(self.values) - 1)]
            self.i += 1
            return v

    def raise_url_error(*a, **k):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(app_mod, "time", Clock([0.0, 120.023]))
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", raise_url_error)
    # Pinned, because `forward()` now asks the socket whether the engine is listening before it
    # commits to "unreachable" (the ENGINE_TIMEOUT split). This test is about the *unit* of
    # latency_ms, and the URL it uses is the shipped default — on a machine that is running n8n the
    # probe would answer True and the body would legitimately say "did not answer" instead.
    monkeypatch.setattr(app_mod, "_engine_listening", lambda url, probe_s=2.0: False)
    status, body, latency_ms = app_mod.forward(
        "http://127.0.0.1:5678/webhook/ai-detect-ai", {"text": TEXT}, {}, 0.05)
    assert status == 502
    assert latency_ms == 120023.0          # seconds would be 0.023 — the bug this pins
    assert body["error"].endswith("webhook/ai-detect-ai")  # "unreachable" must name the target


def test_a_recorded_failure_keeps_the_text_length_the_user_sent(client, monkeypatch):
    """The workflow's `_internal` block carries the length on a success; a forward that never
    reached n8n has no block, so without the caller's length the row renders "? chars" and a
    42-character paragraph is indistinguishable from a 4-character word."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "forward",
                        lambda *a, **k: (502, {"error": "n8n unreachable"}, 120023.0))
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 502
    row = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert row["text_length"] == len(TEXT)
    assert row["latency_ms"] == 120023


# ---------------------------------------------------------------- stored text for the history sheet

def test_a_stored_detection_keeps_the_text_it_scored(client, monkeypatch):
    """History used to store a fingerprint and a length only, which made a saved verdict
    un-repaintable: the sheet could show the score but never the text the score was about.
    The list route still carries no text -- a row list is not a way to read every user's
    submissions in one request."""
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    captured = {}
    _stub_forward(monkeypatch, captured)
    r = client.post("/detect", json={"text": TEXT, "lane_config": {
        "primary": {"model": "nvidia/nemotron-3-ultra-550b-a55b"}}})
    assert r.status_code == 200, r.get_json()

    det = client.get(f"/detections/{r.get_json()['backend_detection_id']}").get_json()
    assert det["text"] == TEXT
    row = client.get("/detections").get_json()[0]
    assert "text" not in row, "the list route must not carry the submitted text"


def test_a_row_saved_before_the_text_column_is_still_readable(client):
    """The ALTER is the whole point: an existing install has history, and the sheet has to tell
    "this run predates text storage" from "this run has no text" rather than render an empty
    panel over a row that simply never had one."""
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("ALTER TABLE detections RENAME TO detections_old")
    conn.execute("""CREATE TABLE detections (
        id INTEGER PRIMARY KEY, user_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')),
        document_id TEXT, text_fingerprint TEXT, text_length INTEGER, classification TEXT,
        ai_probability REAL, confidence REAL, method TEXT, review_required INTEGER,
        deciding_lane TEXT, primary_model TEXT, secondary_model TEXT, latency_ms INTEGER,
        http_status INTEGER, error_code TEXT, warnings TEXT, payload_json TEXT)""")
    conn.execute("""INSERT INTO detections (id, classification, ai_probability, text_length,
                    payload_json) VALUES (1, 'AI_GENERATED', 0.97, 42, '{}')""")
    conn.commit()
    conn.close()

    db.init_db()

    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(detections)")}
    assert "text" in cols
    legacy = conn.execute("SELECT text FROM detections WHERE id = 1").fetchone()
    assert legacy["text"] is None
    conn.close()


# ---------------------------------------------------------------- secrets at rest

def test_keys_are_encrypted_at_rest_and_never_returned(client):
    pid = _provider(client)
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT, "label": "main"})

    with db.session() as c:
        row = c.execute("SELECT key_cipher, key_hint FROM api_keys").fetchone()
        assert bytes(row["key_cipher"])[:6] == b"gAAAAA"
        assert PLAINTEXT not in bytes(row["key_cipher"]).decode("latin-1")
        assert row["key_hint"].startswith("nvapi-") and str(len(PLAINTEXT)) in row["key_hint"]
        assert PLAINTEXT not in open(db.DB_PATH, "rb").read().decode("latin-1")

    for path in ("/keys", "/providers", f"/providers/{pid}/keys"):
        body = client.get(path).get_data(as_text=True)
        assert PLAINTEXT not in body, f"plaintext key leaked through {path}"


def test_key_charset_is_validated(client):
    pid = _provider(client)
    assert client.post(f"/providers/{pid}/keys", json={"api_key": "bad key with spaces"}).status_code == 400
    assert client.post(f"/providers/{pid}/keys", json={"api_key": ""}).status_code == 400


KEY_1 = "nvapi-AAAA-11111111111111111111"
KEY_2 = "nvapi-BBBB-22222222222222222222"
KEY_3 = "nvapi-CCCC-33333333333333333333"


def test_each_lane_is_served_by_the_key_labelled_for_it(client, monkeypatch):
    """A key labelled for a lane must be the one that lane gets.

    The seed writes 'primary lane' and 'secondary lane' rows, and selection was
    `ORDER BY id LIMIT 1` for both: the second row was never read, so an operator who stored
    a real second account (today both rows hold the same key) could not tell from the panel,
    which offered a deactivate button on a key nothing was using.
    """
    pid = _provider(client)
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary")
    _model(client, pid, "google/diffusiongemma-26b-a4b-it", lane="secondary")
    client.post(f"/providers/{pid}/keys", json={"api_key": KEY_1, "label": "primary lane"})
    client.post(f"/providers/{pid}/keys", json={"api_key": KEY_2, "label": "secondary lane"})

    captured: dict = {}
    _stub_forward(monkeypatch, captured)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["headers"]["x-primary-api-key"] == KEY_1
    assert captured["headers"]["x-secondary-api-key"] == KEY_2


def test_interchangeable_keys_rotate_so_a_per_account_cap_is_shared(client, monkeypatch):
    """OpenRouter's free cap is per account, so one account is one account's worth of headroom.

    Keys with no lane label are interchangeable accounts; taking the lowest id for all of
    them spends one account's daily quota and leaves the others untouched, which is the
    opposite of why three were stored.
    """
    pid = _provider(client, name="openrouter", base_url="https://openrouter.ai/api/v1")
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b:free", lane="primary")
    for k in (KEY_1, KEY_2, KEY_3):
        client.post(f"/providers/{pid}/keys", json={"api_key": k, "label": "openrouter"})

    seen = set()
    for _ in range(4):
        captured: dict = {}
        _stub_forward(monkeypatch, captured)
        assert client.post("/detect", json={"text": TEXT}).status_code == 200
        seen.add(captured["headers"]["x-primary-api-key"])
    assert seen == {KEY_1, KEY_2, KEY_3}


def test_a_deactivated_key_leaves_the_rotation(client, monkeypatch):
    pid = _provider(client, name="openrouter", base_url="https://openrouter.ai/api/v1")
    _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b:free", lane="primary")
    ids = [client.post(f"/providers/{pid}/keys", json={"api_key": k, "label": "openrouter"})
           .get_json()["id"] for k in (KEY_1, KEY_2, KEY_3)]
    assert client.patch(f"/keys/{ids[2]}", json={"is_active": False}).status_code == 200

    seen = set()
    for _ in range(6):
        captured: dict = {}
        _stub_forward(monkeypatch, captured)
        assert client.post("/detect", json={"text": TEXT}).status_code == 200
        seen.add(captured["headers"]["x-primary-api-key"])
    assert seen == {KEY_1, KEY_2}


def test_seed_stores_every_numbered_key_for_a_provider(client):
    """The .env may hold OPENROUTER_API_KEY_3; a hand-list of two labels silently drops it.

    Re-seeding must update the rows in place, too: the label is derived from the provider, so
    a second run finds the first run's rows instead of leaving 'openrouter' and
    'openrouter_api_key' side by side as if they were two accounts.
    """
    from backend import seed

    env = {"OPENROUTER_API_KEY": KEY_1, "OPENROUTER_API_KEY_2": KEY_2,
           "OPENROUTER_API_KEY_3": KEY_3}
    want = sorted(["openrouter", "openrouter #2", "openrouter #3"])
    with db.session() as conn:
        ids = seed.seed_providers(conn, env)
        for _ in range(2):
            seed.seed_keys(conn, env, ids)
            stored = sorted(r["label"] for r in conn.execute(
                "SELECT label FROM api_keys WHERE provider_id = ?", (ids["openrouter"],)))
            assert stored == want, stored


# ---------------------------------------------------------------- models + params

def test_seed_ships_default_key_guides_without_a_fabricated_paper(client):
    """A fresh deploy (and the local exe) must show the key guides out of the box.

    The guides were previously only ever written by an admin through /settings, so a
    brand-new database served an empty list -- the 'get an API key' help the UI renders
    was invisible on first run. seed_settings now defaults it. paper_url stays blank on
    purpose: the citable preprint DOI is minted at publication, and defaulting it would
    point the 'read paper' link at a reference that does not exist.
    """
    from backend import seed

    with db.session() as conn:
        seed.seed_settings(conn, env={})
        guides = json.loads(db.get_setting(conn, "api_key_guides"))
        assert isinstance(guides, list) and len(guides) >= 2, guides
        for g in guides:
            assert g.get("provider") and g.get("url").startswith("https://"), g
            assert g.get("steps"), g
        # No paper link is invented.
        assert db.get_setting(conn, "paper_url", "") == ""

    # Idempotency: a re-seed must not clobber an admin's edited guides.
    with db.session() as conn:
        db.set_setting(conn, "api_key_guides",
                      '[{"provider": "only", "url": "https://x", "steps": "s"}]')
        seed.seed_settings(conn, env={})
        assert json.loads(db.get_setting(conn, "api_key_guides"))[0]["provider"] == "only"


def test_model_id_charset_is_enforced(client):
    pid = _provider(client)
    r = client.post("/models", json={"provider_id": pid, "model_id": "bad model;rm -rf"})
    assert r.status_code == 400


def test_tuned_parameters_round_trip(client):
    pid = _provider(client)
    m = _model(client, pid, "nvidia/nemotron-3-ultra-550b-a55b", lane="primary",
               temperature=0.1, top_p=0.9, max_tokens=700, timeout_ms=90000,
               ai_prob_threshold=0.69, ai_conf_threshold=0.60,
               human_prob_threshold=0.40, human_conf_threshold=0.60,
               secondary_decision_threshold=0.90)
    assert m["ai_prob_threshold"] == 0.69 and m["human_prob_threshold"] == 0.40
    r = client.patch(f"/models/{m['id']}", json={"ai_prob_threshold": 0.80, "max_tokens": 900})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ai_prob_threshold"] == 0.80 and body["max_tokens"] == 900
    assert body["human_prob_threshold"] == 0.40


def test_assigning_a_lane_clears_the_previous_holder(client):
    pid = _provider(client)
    a = _model(client, pid, "model-a", lane="primary")
    b = _model(client, pid, "model-b")
    r = client.post(f"/models/{b['id']}/assign-lane", json={"lane": "primary"})
    assert r.status_code == 200 and r.get_json()["primary"] == "model-b"
    models = {m["model_id"]: m for m in client.get("/models").get_json()}
    assert models["model-a"]["lane"] is None
    assert models["model-b"]["lane"] == "primary"
    assert a["id"] != b["id"]


def test_duplicate_provider_and_model_are_conflicts(client):
    pid = _provider(client)
    assert client.post("/providers", json={"name": "nvidia",
                                           "base_url": "https://x.example/v1"}).status_code == 409
    _model(client, pid, "dup/model")
    r = client.post("/models", json={"provider_id": pid, "model_id": "dup/model"})
    assert r.status_code == 409


def test_deleting_a_provider_cascades_its_keys_and_models(client):
    pid = _provider(client)
    _model(client, pid, "child/model", lane="primary")
    client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT})
    assert client.delete(f"/providers/{pid}").get_json()["deleted"] == 1
    assert client.get("/models").get_json() == []
    assert client.get("/keys").get_json() == []


# ---------------------------------------------------------------- benchmark data

def test_benchmark_summary_matches_a_hand_computed_value(client):
    with db.session() as c:
        c.execute("INSERT INTO benchmark_runs (tag, provider, model, corpus, n_rows, source_file) "
                  "VALUES ('synthetic','nvidia','m/x','hc3-48',4,'-')")
        rid = c.execute("SELECT id FROM benchmark_runs WHERE tag='synthetic'").fetchone()["id"]
        rows = [("ai-1", "AI", 1, None, 0.95, 0.9), ("ai-2", "AI", 1, None, 0.50, 0.9),
                ("hu-1", "HUMAN", 1, None, 0.10, 0.9), ("hu-2", "HUMAN", 1, None, 0.80, 0.9)]
        c.executemany("INSERT INTO benchmark_results (run_id, text_id, label, ok, error_code, "
                      "ai_probability, confidence) VALUES (?,?,?,?,?,?,?)",
                      [(rid, *r) for r in rows])
    s = [r for r in client.get("/benchmark/models").get_json() if r["tag"] == "synthetic"][0]
    # cuts 0.69 / 0.40: ai-1 correct, ai-2 deferred, hu-1 correct, hu-2 wrong
    assert (s["correct"], s["wrong"], s["deferred"]) == (2, 1, 1)
    assert s["n_scored"] == 4
    # Two-element lists: the median is the mean of the pair, not the larger one. This
    # assertion used to read 0.95 / 0.8 / 0.15, which is the upper-of-pair the summary
    # took with probs[len // 2] -- a median that quietly disagreed with every analyzer.
    assert s["median_ai_text"] == pytest.approx(0.725)
    assert s["median_human_text"] == pytest.approx(0.45)
    assert s["separation"] == pytest.approx(0.275)


def test_benchmark_run_detail_lists_every_row(client):
    with db.session() as c:
        c.execute("INSERT INTO benchmark_runs (tag, provider, model, corpus, n_rows) "
                  "VALUES ('t2','nvidia','m/y','hc3-48',2)")
        rid = c.execute("SELECT id FROM benchmark_runs WHERE tag='t2'").fetchone()["id"]
        c.executemany("INSERT INTO benchmark_results (run_id, text_id, label, ok, ai_probability) "
                      "VALUES (?,?,?,?,?)", [(rid, "ai-1", "AI", 1, 0.9), (rid, "hu-1", "HUMAN", 1, 0.1)])
    body = client.get(f"/benchmark/runs/{rid}").get_json()
    assert body["n_rows"] == 2 and len(body["results"]) == 2
    assert body["summary"]["correct"] == 2


def test_benchmark_run_list_reports_row_counts(client):
    """The list view aggregates over benchmark_results, which has no latency column.

    Why this test exists: the query once averaged a column that does not exist, and
    the endpoint answered 200 with {"error": "database error"} — a JSON body a test
    asserting only on status would have waved through.
    """
    with db.session() as c:
        c.execute("INSERT INTO benchmark_runs (tag, provider, model, corpus, n_rows, params_json) "
                  "VALUES ('t3','openrouter','m/z','hc3-48',2,'{\"temperature\":0.1}')")
        rid = c.execute("SELECT id FROM benchmark_runs WHERE tag='t3'").fetchone()["id"]
        c.executemany("INSERT INTO benchmark_results (run_id, text_id, label, ok, ai_probability) "
                      "VALUES (?,?,?,?,?)", [(rid, "ai-1", "AI", 1, 0.8), (rid, "ai-2", "AI", 0, None)])
    runs = client.get("/benchmark/runs").get_json()
    assert isinstance(runs, list), runs
    row = [r for r in runs if r["tag"] == "t3"][0]
    assert row["n_results"] == 2 and row["params"]["temperature"] == 0.1


def test_benchmark_import_actually_writes_rows(client):
    """The import response is a list of what it did; only the commit makes it true.

    Why this test exists: the route used to skip conn().commit(), so every measured
    run was rolled back at request teardown while the API still reported 25 imports.
    """
    body = client.post("/benchmark/import").get_json()
    assert isinstance(body.get("benchmark_runs"), list) and body["benchmark_runs"]
    first_tag = body["benchmark_runs"][0].split(" -> ")[0]
    persisted = {r["tag"] for r in client.get("/benchmark/runs").get_json()}
    assert first_tag in persisted, (first_tag, sorted(persisted))


def test_run_without_manifest_line_is_attributed_from_its_own_log(tmp_path, monkeypatch, client):
    """A measured run must never be stored under its tag as if the tag were a model.

    Why this test exists: `bookdiffgemma` was imported as model="bookdiffgemma", because
    only runs-manifest.log supplied the model and that run predates it -- even though the
    harness prints the model on the first line of its own run log. The panel then listed a
    model id that no provider serves, under a name that is only a filename fragment.
    """
    from backend import seed

    (tmp_path / "results-nomanifest.jsonl").write_text(json.dumps(
        {"id": "book-000", "label": "HUMAN", "ok": True, "ai_probability": 0.02,
         "confidence": 0.9}) + "\n", encoding="utf-8")
    (tmp_path / "run-nomanifest.log").write_text(
        "tag=nomanifest model=google/diffusiongemma-26b-a4b-it prompt=direct temp=0.1 "
        "top_p=0.9 max_tokens=700 n=1 concurrency=2\n"
        "base_url=https://integrate.api.nvidia.com/v1 keys=1\n", encoding="utf-8")
    monkeypatch.setattr(seed, "BENCH", tmp_path)

    # The mis-attributed predecessor the fix has to replace, not sit beside.
    with db.session() as conn:
        conn.execute(
            "INSERT INTO benchmark_runs (tag, provider, model, corpus, source_file, n_rows) "
            "VALUES ('nomanifest', 'nvidia', 'nomanifest', 'hc3-48', "
            "'results-nomanifest.jsonl', 1)")

    with db.session() as conn:
        seed.import_benchmarks(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT model, provider, corpus FROM benchmark_runs WHERE tag = 'nomanifest'")]

    assert rows == [{"model": "google/diffusiongemma-26b-a4b-it",
                     "provider": "nvidia", "corpus": "unattributed"}]
    # 'unattributed' rather than 'hc3-48': one row cannot evidence a 48-text corpus. The row that
    # used to be left behind claimed hc3-48 while holding a single book excerpt.


def test_run_without_provenance_is_not_credited_with_the_corpus_it_cannot_evidence(tmp_path, monkeypatch):
    """A 4-row results file with no manifest line and no run log cannot claim 'hc3-48'.

    The fallback in corpus_label names the full corpus because that is what the harness ran
    before --corpus was recorded. For a run that covers the whole corpus that inference is sound;
    for a 4-text smoke it is a guess, and the guess is what puts a smoke on the ranking table --
    `smoke` (n=3) and `or-gemma26b-smoke4` (n=4) both sat under hc3-48, and the doc then had to
    warn readers away from rows the database itself had mislabelled. A row count below the corpus
    size is not evidence of a corpus, so the label says unattributed instead.
    """
    from backend import seed

    row = {"label": "HUMAN", "ok": False, "error_code": "http_429"}
    for tag, n in (("shortsmoke", 4), ("fullish", seed.HC3_CORPUS_SIZE)):
        (tmp_path / f"results-{tag}.jsonl").write_text(
            "".join(json.dumps(dict(row, id=f"ai-{i:03d}")) + "\n" for i in range(n)),
            encoding="utf-8")
    monkeypatch.setattr(seed, "BENCH", tmp_path)

    with db.session() as conn:
        seed.import_benchmarks(conn)
        got = dict(conn.execute(
            "SELECT tag, corpus FROM benchmark_runs WHERE tag IN ('shortsmoke', 'fullish')").fetchall())

    assert got == {"shortsmoke": "unattributed", "fullish": "hc3-48"}


def test_seeded_catalog_groups_by_probe_status(client):
    with db.session() as c:
        c.execute("INSERT INTO catalog_models (provider, model_id, probe_status, latency_ms) "
                  "VALUES ('nvidia','a/b','scored',300)")
        c.execute("INSERT INTO catalog_models (provider, model_id, probe_status) "
                  "VALUES ('nvidia','c/d','not_found')")
    body = client.get("/benchmark/catalog").get_json()
    assert body["nvidia"]["scored"][0]["model"] == "a/b"
    assert body["nvidia"]["not_found"][0]["model"] == "c/d"


def test_run_summary_median_is_the_standard_median():
    """An even-length list has no single middle element, and the summary took
    probs[len // 2] -- the *upper* of the pair. So the panel's 'med human' read 0.050 for
    the 46-text Gutenberg run while every analyzer reported 0.045 for the same file: one
    statistic, two numbers, in the same product. A benchmark claim you cannot check twice
    is not a claim, so the median must mean the same thing everywhere."""
    from backend.app import summarize_run
    even = [{"label": "HUMAN", "ai_probability": p} for p in (0.01, 0.04, 0.05, 0.18)]
    s = summarize_run(even)
    assert s["median_human_text"] == pytest.approx(0.045)
    odd = [{"label": "HUMAN", "ai_probability": p} for p in (0.01, 0.05, 0.18)]
    assert summarize_run(odd)["median_human_text"] == pytest.approx(0.05)
    # separation is built from the same two medians and must move with them
    pair = even + [{"label": "AI", "ai_probability": p} for p in (0.80, 0.85, 0.90, 0.95)]
    s2 = summarize_run(pair)
    assert s2["median_ai_text"] == pytest.approx(0.875)
    assert s2["separation"] == pytest.approx(0.83)


def test_run_summary_scores_against_the_whole_corpus_too():
    """accuracy_over_scored divides by the texts the model answered, so a model that
    answers nothing scores no wrong calls and looks perfect -- or, measured for real, a
    free model that failed to return a parseable verdict on 13 of 24 human texts still
    posted 0.774, above the shipped paid primary. A detector that produces no verdict on
    a text has not detected anything, so that text counts against it."""
    from backend.app import summarize_run
    results = [
        {"label": "AI", "ai_probability": 0.90},
        {"label": "HUMAN", "ai_probability": 0.10},
        {"label": "HUMAN", "ai_probability": 0.30},
        {"label": "HUMAN", "ai_probability": None},          # unparseable response
    ]
    s = summarize_run(results)
    assert s["n_rows"] == 4 and s["n_scored"] == 3
    assert s["accuracy_over_scored"] == pytest.approx(1.0)
    assert s["accuracy_over_corpus"] == pytest.approx(0.75)
    assert summarize_run([])["accuracy_over_corpus"] is None


def test_unscored_rows_separate_a_dead_gateway_from_a_model_that_answered_badly():
    """n_parse_failed used to be `n_rows - n_scored`, so the panel reported that the free
    nemotron run "failed to parse JSON" 19 times when the model never answered at all: OpenRouter
    returns HTTP 200 with the upstream failure in the body (error_code "503", "Upstream error
    from Nvidia: Service temporarily overloaded"), and all 19 rows in that capture are exactly
    that, at attempt 1. A transport failure and an unusable answer are different defects with
    different fixes, and calling one the other silently blames the model for the gateway."""
    from backend.app import summarize_run, row_never_answered
    rows = [
        {"label": "AI", "ai_probability": 0.90},
        {"label": "AI", "ai_probability": None, "error_code": "503",
         "http_status": 200, "error_detail": "Upstream error from Nvidia"},
        {"label": "AI", "ai_probability": None, "error_code": "http_429", "http_status": 429},
        {"label": "AI", "ai_probability": None, "error_code": "exception", "http_status": None},
        {"label": "AI", "ai_probability": None, "error_code": "null_content", "http_status": 200},
        {"label": "AI", "ai_probability": None, "error_code": "json_parse_failed", "http_status": 200},
        {"label": "AI", "ai_probability": None, "error_code": "no_json_object", "http_status": 200},
        {"label": "AI", "ai_probability": None, "error_code": "missing_ai_probability",
         "http_status": 200},
    ]
    unscored = [r for r in rows if r["ai_probability"] is None]
    assert [r["error_code"] for r in unscored if row_never_answered(r)] == \
        ["503", "http_429", "exception", "null_content"]
    assert [r["error_code"] for r in unscored if not row_never_answered(r)] == \
        ["json_parse_failed", "no_json_object", "missing_ai_probability"]

    s = summarize_run(rows)
    assert s["n_no_answer"] == 4
    assert s["n_parse_failed"] == 3
    # The split must stay exhaustive, or a future error label hides rows from both buckets.
    assert s["n_no_answer"] + s["n_parse_failed"] == s["n_rows"] - s["n_scored"] == 7


# ---------------------------------------------------------------- cors

def test_non_loopback_origin_is_not_reflected(client):
    r = client.get("/gateway-health", headers={"Origin": "https://evil.example"})
    assert r.headers["Access-Control-Allow-Origin"] != "https://evil.example"
    r = client.get("/gateway-health", headers={"Origin": "http://localhost:4200"})
    assert r.headers["Access-Control-Allow-Origin"] == "http://localhost:4200"


def test_options_preflight_is_allowed(client):
    r = client.options("/detect", headers={"Origin": "http://localhost:4200",
                                           "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200
    assert "X-Primary-Api-Key" in r.headers["Access-Control-Allow-Headers"]
