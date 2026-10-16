"""Seed defaults. Run from ai-text-detector/:  python -m pytest backend/tests -q

Written for the defect found while verifying the packaged exe on 2026-10-16: a frozen
build ships no `.env`, `seed_models` inserted rows only for models named in env, so the
installed app had providers and ZERO models and every detection answered
409 NO_PRIMARY_MODEL. These tests pin the opposite: an install with nothing configured
still has a working primary lane.

Throwaway SQLite file, stubbed forwarder: no provider is ever called.
"""
import os
import pathlib
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-seed-test-")
os.environ["DETECTOR_DB"] = os.path.join(_TMP, "test.sqlite3")
os.environ["DETECTOR_SECRET"] = os.path.join(_TMP, "test-secret.key")
os.environ["N8N_DB"] = os.path.join(_TMP, "no-such-n8n.sqlite")

import pytest  # noqa: E402

from backend import db, seed  # noqa: E402
from backend.app import create_app  # noqa: E402

TEXT = "This is a long enough sample of text for the detector to accept it."
WORKFLOW_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "n8n", "generate-workflow.py")


@pytest.fixture()
def conn():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    with db.session() as c:
        yield c


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


def _lane(conn, lane):
    return conn.execute(
        """SELECT m.*, p.name AS pname, p.base_url AS purl FROM models m
           JOIN providers p ON p.id = m.provider_id WHERE m.lane = ?""",
        (lane,),
    ).fetchone()


# ------------------------------------------------------------------ no-env install

def test_an_install_with_no_env_gets_both_lanes(conn):
    """No `.env` must not mean no lane. This is the packaged exe's and a fresh container's
    first boot: read_env() returns {} and the app still has to be able to answer."""
    pids = seed.seed_providers(conn, {})
    made = seed.seed_models(conn, {}, pids)
    assert made == [seed.DEFAULT_LANES["primary"][0], seed.DEFAULT_LANES["secondary"][0]]

    primary = _lane(conn, "primary")
    assert primary["pname"] == "nvidia"
    # A promoted lane carries the band it was MEASURED against, not the workflow's fallback
    # cut fitted on some other model -- see LANE_BANDS.
    measured = seed.LANE_BANDS[seed.DEFAULT_LANES["primary"][0]]
    for col, key in seed.TUNED.items():
        if key in measured:
            assert primary[col] == pytest.approx(measured[key]), f"{col} not the measured band"
    assert _lane(conn, "secondary")["secondary_decision_threshold"] == \
        seed.DEFAULT_BAND["SECONDARY_DECISION_THRESHOLD"]
    # Uncapped by default (policy decided 2026-10-16): 0 means "send no max_tokens field".
    assert primary["max_tokens"] == 0


def test_the_baked_default_band_is_labelled_as_capped_era(conn):
    """The numbers are the deployed ones, but they were fitted under the old 700-token cap
    and #111 has not re-validated them. A default that reads as fully validated would be
    a claim the project has not earned, and admins pick lanes off these notes."""
    seed.seed_models(conn, {}, seed.seed_providers(conn, {}))
    assert "cap" in _lane(conn, "primary")["notes"]


def test_env_values_still_beat_the_baked_defaults(conn):
    """A real deployment configures its own lanes; the fallback must never overwrite them,
    and an env band stays without the capped-era caveat it did not earn."""
    env = {"PRIMARY_MODEL": "z-ai/glm-5.3-flash",
           "PRIMARY_BASE_URL": "https://integrate.api.nvidia.com/v1",
           "AI_PROBABILITY_THRESHOLD": "0.80",
           "HUMAN_PROBABILITY_THRESHOLD": "0.55"}
    seed.seed_models(conn, env, seed.seed_providers(conn, env))
    primary = _lane(conn, "primary")
    assert primary["model_id"] == "z-ai/glm-5.3-flash"
    assert primary["ai_prob_threshold"] == 0.80
    assert primary["human_prob_threshold"] == 0.55
    # Only the thresholds the operator left out fall back, and they fall back to numbers,
    # not to NULL: the row must describe the band that was actually applied. For a model
    # with a measured overlay the fallback is that model's own cut, not another's.
    assert primary["ai_conf_threshold"] == pytest.approx(
        seed.LANE_BANDS["z-ai/glm-5.3-flash"]["AI_CONFIDENCE_THRESHOLD"])
    assert "cap" not in primary["notes"]


def test_re_seeding_does_not_duplicate_or_reset_a_lane(conn):
    """serve_local runs the full seed on every start. A lane an admin re-banded in the UI
    must survive a restart, and must not appear twice."""
    pids = seed.seed_providers(conn, {})
    seed.seed_models(conn, {}, pids)
    conn.execute("UPDATE models SET ai_prob_threshold = 0.42 WHERE lane = 'primary'")
    seed.seed_models(conn, {}, pids)
    rows = conn.execute("SELECT * FROM models WHERE lane = 'primary'").fetchall()
    assert len(rows) == 1
    # Honest statement of current behaviour: boot re-seeds and reverts direct DB edits
    # unless they come through /models (documented in memory, 2026-10-16). What it reverts
    # TO is the row's own measured band, which is the point of LANE_BANDS.
    assert rows[0]["ai_prob_threshold"] == pytest.approx(
        seed.LANE_BANDS[seed.DEFAULT_LANES["primary"][0]]["AI_PROBABILITY_THRESHOLD"])


def test_a_promoted_lane_is_seeded_on_its_own_measured_band(conn):
    """The default primary was fitted against 0.80/0.55 (156 decided, 3 wrong, 0.981 on
    train+val), not against the workflow's 0.69/0.40 fallback. Because seed_models() re-runs
    on every boot, a single shared DEFAULT_BAND would silently re-band the promoted model to
    another model's ruler -- a verdict-changing edit nobody made. The per-model overlay is
    what stops that, so assert the numbers the row carries really are the measured ones."""
    model_id = seed.DEFAULT_LANES["primary"][0]
    assert model_id in seed.LANE_BANDS, f"{model_id} is the default primary but has no measured band"
    seed.seed_models(conn, {}, seed.seed_providers(conn, {}))
    primary = _lane(conn, "primary")
    for col, key in seed.TUNED.items():
        if key in seed.LANE_BANDS[model_id]:
            assert primary[col] == pytest.approx(seed.LANE_BANDS[model_id][key]), \
                f"{col}: seeded {primary[col]}, measured {seed.LANE_BANDS[model_id][key]}"
    assert primary["ai_prob_threshold"] != seed.DEFAULT_BAND["AI_PROBABILITY_THRESHOLD"]
    # A measured leg time, not a guess: the model answered a real document in 86 s.
    assert primary["timeout_ms"] == seed.LANE_TIMEOUT_MS[model_id]
    # The same when the operator names the model through env: a timeout is a property of
    # the model's latency, not a policy, and the baked 90 s ceiling is what made this model
    # answer 502 "timed out" once the pool was slow.
    env_named = {"PRIMARY_MODEL": model_id,
                 "PRIMARY_BASE_URL": "https://integrate.api.nvidia.com/v1"}
    seed.seed_models(conn, env_named, seed.seed_providers(conn, env_named))
    assert _lane(conn, "primary")["timeout_ms"] == seed.LANE_TIMEOUT_MS[model_id]
    # An explicit operator timeout still wins.
    seed.seed_models(conn, {**env_named, "LLM_TIMEOUT_MS": "75000"},
                     seed.seed_providers(conn, env_named))
    assert _lane(conn, "primary")["timeout_ms"] == 75000
    # The env still outranks the overlay -- an operator re-banding a lane must win.
    seed.seed_models(conn, {"AI_PROBABILITY_THRESHOLD": "0.61"}, seed.seed_providers(conn, {}))
    assert _lane(conn, "primary")["ai_prob_threshold"] == pytest.approx(0.61)


def test_changing_the_default_lane_does_not_leave_the_old_one_claiming_a_lane(conn):
    """An upgraded install already holds the previous default with lane='primary'. If the
    seed only writes the rows it names, both rows keep the lane and which model answers
    becomes a question about row order -- while the UI shows a lane nobody chose. The old row
    loses the lane and keeps its own band, notes and history."""
    pids = seed.seed_providers(conn, {})
    stale_model = "nvidia/nemotron-3-ultra-550b-a55b"
    conn.execute(
        "INSERT INTO models (provider_id, model_id, display_name, role, lane, "
        "ai_prob_threshold, human_prob_threshold, notes) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (pids["nvidia"], stale_model, "nemotron", "primary", "primary", 0.69, 0.40,
         "measured on its own scale"),
    )
    made = seed.seed_models(conn, {}, pids)
    assert stale_model not in made

    primaries = conn.execute("SELECT model_id FROM models WHERE lane = 'primary'").fetchall()
    assert [p[0] for p in primaries] == [seed.DEFAULT_LANES["primary"][0]]

    kept = conn.execute("SELECT lane, role, ai_prob_threshold, notes FROM models "
                        "WHERE model_id = ?", (stale_model,)).fetchone()
    assert kept["lane"] is None and kept["role"] == "either"
    assert kept["ai_prob_threshold"] == pytest.approx(0.69)
    assert kept["notes"] == "measured on its own scale"


# ------------------------------------------------------------------ drift guard

def test_baked_defaults_match_the_workflows_own_defaults():
    """Two copies of the same numbers drift silently: the JS in generate-workflow.py is what
    a lane really runs on when env omits it, seed.py is what the DB claims. If they disagree,
    every stored band is a lie about the call that produced the verdict. This test reads the
    generator and compares, so changing one file without the other fails here."""
    src = open(WORKFLOW_SRC, encoding="utf-8").read()
    js_defaults = dict(re.findall(r"num\(env\.([A-Z_]+),\s*([0-9.]+)\)", src))
    for key, value in seed.DEFAULT_BAND.items():
        assert key in js_defaults, f"{key} is not a num(env.X, default) in the workflow"
        assert float(js_defaults[key]) == pytest.approx(value), f"{key}: seed {value} vs JS {js_defaults[key]}"

    for lane, (model_id, _pname) in seed.DEFAULT_LANES.items():
        m = re.search(rf"env\.{lane.upper()}_MODEL\s*\|\|\s*'([^']+)'", src)
        assert m, f"the workflow has no baked default for {lane}"
        assert m.group(1) == model_id

    for key, value in seed.DEFAULT_LLM.items():
        m = re.search(rf"num\(env\.{key},\s*([0-9.]+)\)", src)
        assert m and float(m.group(1)) == pytest.approx(value), f"{key}: seed {value} vs workflow"


# ------------------------------------------------------------------ env overlay

def test_the_process_environment_overrides_the_file(monkeypatch, tmp_path):
    """docker-compose passes N8N_URL as a container env var and the repo .env has no such
    line; the exe is launched with N8N_URL in a shell and ships no .env at all. Reading only
    the file left both on http://localhost:5678, where neither can reach the engine."""
    f = tmp_path / ".env"
    f.write_text("N8N_URL=http://localhost:5678\nPRIMARY_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("N8N_URL", "http://n8n:5678")
    env = seed.read_env(f)
    assert env["N8N_URL"] == "http://n8n:5678"        # live environment wins
    assert env["PRIMARY_MODEL"] == "from-file"        # untouched keys keep the file's value


def test_only_understood_names_are_read_from_the_environment(monkeypatch, tmp_path):
    """A stray variable must not become configuration. PATH and BACKEND_PORT are real
    environment entries that seed has no business importing."""
    monkeypatch.setenv("BACKEND_PORT", "9999")
    monkeypatch.setenv("PATH", "/tmp")
    env = seed.read_env(tmp_path / "no-such.env")     # no file at all: environment only
    assert "BACKEND_PORT" not in env and "PATH" not in env


def test_every_tuned_key_is_overlayable():
    """KNOWN_ENV_KEYS is spelled out by hand, so it can silently fall behind TUNED and the
    account families; a threshold nobody can set from the environment is a deployment that
    quietly runs the baked band."""
    assert set(seed.TUNED.values()) <= seed.KNOWN_ENV_KEYS
    assert set(seed.DEFAULT_LLM) <= seed.KNOWN_ENV_KEYS
    for name in seed.ACCOUNT_KEY_FAMILIES:
        assert any(p.match(name) for p in seed.ACCOUNT_KEY_PATTERNS)
        assert any(p.match(f"{name}_3") for p in seed.ACCOUNT_KEY_PATTERNS)


def test_a_numbered_account_key_in_the_environment_is_imported(monkeypatch, tmp_path):
    """An OPENROUTER_API_KEY_2 that never reaches the database is a day of free headroom the
    app cannot spend (that is the reason ACCOUNT_KEY_FAMILIES exists)."""
    f = tmp_path / ".env"
    f.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "sk-or-test-value-000000")
    env = seed.read_env(f)
    assert seed.account_key_candidates(env) == [("OPENROUTER_API_KEY_2",
                                                "https://openrouter.ai/api/v1", 2)]


# ------------------------------------------------------------------ the shipped path

def test_a_freshly_seeded_install_answers_a_detection_instead_of_409(client, monkeypatch):
    """End-to-end for the packaged app: seed exactly the way serve_local.build_app() does
    with no `.env`, then detect. Before 2026-10-16 this returned 409 NO_PRIMARY_MODEL."""
    import backend.app as app_mod

    captured = {}

    def fake(url, payload, headers, timeout):
        captured["payload"] = payload
        return 200, {
            "classification": "AI_GENERATED", "ai_probability": 0.97, "confidence": 0.9,
            "method": "PRIMARY_DECIDED_AI", "review_required": False,
            "config_used": {"primary": {"model": payload["lane_config"]["primary"]["model"]}},
            "_internal": {"text_fingerprint": "fp", "text_length": len(TEXT)},
        }, 123.4

    monkeypatch.setattr(app_mod, "forward", fake)
    with db.session() as conn:
        pids = seed.seed_providers(conn, {})
        seed.seed_models(conn, {}, pids)
        seed.seed_settings(conn, {})

    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    lane = captured["payload"]["lane_config"]
    assert lane["primary"]["model"] == seed.DEFAULT_LANES["primary"][0]
    assert lane["primary"]["base_url"] == "https://integrate.api.nvidia.com/v1"
    # The band travels in the tuning block, so a no-env install gets the recorded cuts --
    # the primary's own measured cuts, not the workflow's fallback for a different model.
    assert captured["payload"]["tuning"]["ai_prob_threshold"] == pytest.approx(
        seed.LANE_BANDS[seed.DEFAULT_LANES["primary"][0]]["AI_PROBABILITY_THRESHOLD"])


def test_the_tie_break_lane_is_seeded_switched_off(conn):
    """Round 25c, measured by executing the shipped node on the deployed primary's own band:
    the tie-break was asked about 3 of 48 texts and answered all 3 wrongly (three human
    documents accused of being AI), while the same ensemble with the lane off answers 45 of 48
    at 44 correct / 1 wrong. A tight band on a strong primary leaves the second lane nothing
    useful to do, so the shipped default is one lane. It is a switch, not a deletion: on a
    drifting primary the same authority was worth 13 correct calls out of 15."""
    pids = seed.seed_providers(conn, {})
    seed.seed_models(conn, {}, pids)
    assert _lane(conn, "primary")["enabled"] == 1
    assert seed.LANE_DEFAULT_ENABLED["secondary"] is False
    assert _lane(conn, "secondary")["enabled"] == 0, "the tie-break must ship off by default"


def test_enabling_the_tie_break_lane_survives_the_next_boot(conn):
    """`enabled` is a first-install default, so it is written on insert and deliberately absent
    from the upsert's DO UPDATE. An admin who turns the lane back on for everyone must not lose
    it at the next restart -- that is the boot-reverts-direct-DB-edits trap, and here the
    re-seed would be reverting a decision the admin made on purpose."""
    pids = seed.seed_providers(conn, {})
    seed.seed_models(conn, {}, pids)
    conn.execute("UPDATE models SET enabled = 1 WHERE lane = 'secondary'")
    seed.seed_models(conn, {}, pids)
    rows = conn.execute("SELECT * FROM models WHERE lane = 'secondary'").fetchall()
    assert len(rows) == 1
    assert rows[0]["enabled"] == 1


def test_an_existing_install_is_told_the_new_lane_default_exactly_once(conn):
    """The first-install default cannot reach an install that already exists: `enabled` is not
    in the upsert, so its secondary row is still switched on by the seed that shipped it -- a
    default nobody chose. apply_lane_defaults() says so once, then stays out of the way."""
    pids = seed.seed_providers(conn, {})
    seed.seed_models(conn, {}, pids)
    conn.execute("UPDATE models SET enabled = 1 WHERE lane = 'secondary'")   # the old default
    assert seed.apply_lane_defaults(conn) == 1
    assert _lane(conn, "secondary")["enabled"] == 0

    # An admin who wants the insurance back turns it on, and a restart keeps it: the version key
    # is already written, so the migration never runs again.
    conn.execute("UPDATE models SET enabled = 1 WHERE lane = 'secondary'")
    seed.seed_models(conn, {}, pids)
    assert seed.apply_lane_defaults(conn) == 0
    assert _lane(conn, "secondary")["enabled"] == 1


def test_an_explicit_env_switch_outranks_the_baked_lane_default(conn):
    """`SECONDARY_ENABLED` is the operator's own name for this switch, and the project rule is
    that env beats a baked value -- the same way a threshold in env beats a measured band. Left
    alone, the new default would silently overrule a `.env` that says "buy the tie-break back",
    and `.env.example` now ships `false`, so the two must agree by construction."""
    pids = seed.seed_providers(conn, {})
    seed.seed_models(conn, {"SECONDARY_ENABLED": "true"}, pids)
    assert _lane(conn, "secondary")["enabled"] == 1
    # A fresh install with no opinion in env gets the shipped default.
    seed.seed_models(conn, {}, seed.seed_providers(conn, {}))
    assert seed.lane_enabled({}, "secondary") is False
    assert seed.lane_enabled({"SECONDARY_ENABLED": "false"}, "secondary") is False
    # A value that is not a boolean is not an opinion: keep the default rather than guess.
    assert seed.lane_enabled({"SECONDARY_ENABLED": "maybe"}, "secondary") is False
    # The primary lane has no env switch and is never switched off by a default.
    assert seed.lane_enabled({"SECONDARY_ENABLED": "false"}, "primary") is True
    # And the one-shot upgrade honours the same file instead of outvoting it.
    conn.execute("DELETE FROM settings WHERE key = 'lane_defaults_version'")
    conn.execute("UPDATE models SET enabled = 0 WHERE lane = 'secondary'")
    assert seed.apply_lane_defaults(conn, {"SECONDARY_ENABLED": "true"}) == 1
    assert _lane(conn, "secondary")["enabled"] == 1


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_TEMPLATE = os.path.join(REPO, ".env.example")
COMPOSE = os.path.join(REPO, "docker-compose.yml")

# Keys that outrank a per-model value the moment they are set, because `tuned_for` and the
# timeout fallback both consult env first. One line each applies to every lane, so a template
# that sets them re-bands whichever model the operator names to a different model's cut.
PER_LANE_TUNING = ("AI_PROBABILITY_THRESHOLD", "AI_CONFIDENCE_THRESHOLD",
                   "HUMAN_PROBABILITY_THRESHOLD", "HUMAN_CONFIDENCE_THRESHOLD",
                   "LLM_TIMEOUT_MS")


def test_the_deploy_templates_cannot_outvote_the_shipped_default_lane():
    """`.env.example` and `docker-compose.yml` are what a fresh deploy copies, and the seed reads
    them as *env* -- which outranks a baked value. So a template naming the demoted 550B, or saying
    the tie-break is on, ships an operating point nobody chose: the primary the promotion in #119
    replaced, and a second call on every gray-zone text. Same class of failure for the tuning keys:
    they are per-lane, one line applies to both, and setting them is what silently re-bands a
    promoted model to another model's ruler -- the thing `LANE_BANDS` exists to prevent. The
    templates therefore name the promoted lane, switch the tie-break off, and leave per-lane
    tuning to `LANE_BANDS`/`LANE_TIMEOUT_MS` by not mentioning those keys as live assignments.
    """
    with open(ENV_TEMPLATE, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    live = {}
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        live[k.strip()] = v.strip()

    assert live.get("PRIMARY_MODEL") == seed.DEFAULT_LANES["primary"][0], \
        f".env.example names {live.get('PRIMARY_MODEL')!r} as the primary lane"
    assert live.get("SECONDARY_MODEL") == seed.DEFAULT_LANES["secondary"][0]
    assert live.get("SECONDARY_ENABLED", "false") == str(
        seed.lane_enabled({}, "secondary")).lower()
    for key in PER_LANE_TUNING:
        assert key not in live, f".env.example sets {key}, which outranks a measured per-lane band"

    with open(COMPOSE, encoding="utf-8") as fh:
        compose = fh.read()
    for name, want in (("SECONDARY_ENABLED", "false"),
                       ("PRIMARY_MODEL", seed.DEFAULT_LANES["primary"][0]),
                       ("SECONDARY_MODEL", seed.DEFAULT_LANES["secondary"][0])):
        m = re.search(r"\$\{" + name + r":-([^}]*)\}", compose)
        assert m, f"docker-compose.yml no longer interpolates {name}"
        assert m.group(1) == want, \
            f"docker-compose.yml falls back to {name}={m.group(1)!r}, expected {want!r}"


def test_seeding_from_the_template_gives_the_shipped_operating_point(conn):
    """Reading the strings is not the claim: the claim is what a fresh deploy's boot writes.
    So seed from `.env.example` itself -- the file an operator copies -- and require the two
    rows to land on the shipped operating point. This is the check that would have caught the
    template naming the delisted 550B (a deploy whose first detection is a 502) and the
    tie-break switched on, neither of which is visible in the text of the file.
    """
    env = seed.read_env(pathlib.Path(ENV_TEMPLATE))
    pids = seed.seed_providers(conn, env)
    seed.seed_models(conn, env, pids)
    seed.apply_lane_defaults(conn, env)

    primary, secondary = _lane(conn, "primary"), _lane(conn, "secondary")
    assert primary["model_id"] == seed.DEFAULT_LANES["primary"][0]
    band = seed.LANE_BANDS[seed.DEFAULT_LANES["primary"][0]]
    assert (primary["ai_prob_threshold"], primary["human_prob_threshold"]) == (
        band["AI_PROBABILITY_THRESHOLD"], band["HUMAN_PROBABILITY_THRESHOLD"])
    assert primary["timeout_ms"] == seed.LANE_TIMEOUT_MS[seed.DEFAULT_LANES["primary"][0]]
    # The tie-break keeps its own ruler and stays off.
    assert secondary["model_id"] == seed.DEFAULT_LANES["secondary"][0]
    assert (secondary["ai_prob_threshold"], secondary["human_prob_threshold"]) == (
        seed.DEFAULT_BAND["AI_PROBABILITY_THRESHOLD"],
        seed.DEFAULT_BAND["HUMAN_PROBABILITY_THRESHOLD"])
    assert secondary["enabled"] == 0



def test_the_shipped_key_guides_are_links_a_logged_out_user_can_follow():
    """These three cards are the whole "help me get an API key" affordance on the ranking page,
    and they are the links a confused user follows when a lane 401s -- so a placeholder host, a
    plain-http hop, or a deep path that only exists once authenticated silently strands them.
    Fetched 2026-10-16: build.nvidia.com resolves, openrouter.ai/settings/keys answers its Sign
    In shell to a logged-out GET, aistudio.google.com/apikey 302s to accounts.google.com, and
    build.nvidia.com/settings/api-keys answers 404 to a logged-out GET even though it is the real
    page. Every one of them therefore has to say in the steps that signing in comes first; that
    sentence is not decoration, it is the difference between a guide and a dead end.
    """
    import urllib.parse
    placeholder = ("example.com", "example.org", "zenodo.org", "localhost", "127.0.0.1",
                   "your-omniroute-instance")
    assert len(seed.DEFAULT_API_KEY_GUIDES) >= 3
    for g in seed.DEFAULT_API_KEY_GUIDES:
        assert {"provider", "url", "steps"} <= set(g), g
        u = urllib.parse.urlsplit(g["url"])
        assert u.scheme == "https", g["url"]
        assert u.hostname and not any(h in u.hostname for h in placeholder), g["url"]
        assert re.search(r"sign in|Sign in|signed in", g["steps"]),             f"{g['provider']} guide never tells the user to sign in first"

def test_a_global_env_cut_rebands_every_lane(conn):
    """The negative path, so the rule above is a measurement and not a superstition. One line
    applies to both lanes and env is consulted first, so a deploy that sets the primary's own
    band globally re-bands the tie-break model to a ruler it was never fitted on -- and a global
    timeout quietly reverts the ceiling the promoted row exists to carry. Nothing here is
    hypothetical: both were live in this repo's own `.env` until 2026-10-16.
    """
    env = dict(seed.read_env(pathlib.Path(ENV_TEMPLATE)))
    env.update({"AI_PROBABILITY_THRESHOLD": "0.80", "HUMAN_PROBABILITY_THRESHOLD": "0.55",
                "LLM_TIMEOUT_MS": "90000"})
    pids = seed.seed_providers(conn, env)
    seed.seed_models(conn, env, pids)

    secondary = _lane(conn, "secondary")
    assert (secondary["ai_prob_threshold"], secondary["human_prob_threshold"]) == (0.80, 0.55), \
        "the global env line no longer reaches the secondary row -- the mechanism changed"
    assert secondary["timeout_ms"] == 90000
    assert _lane(conn, "primary")["timeout_ms"] == 90000, \
        "env no longer outranks the measured ceiling -- the mechanism changed"
