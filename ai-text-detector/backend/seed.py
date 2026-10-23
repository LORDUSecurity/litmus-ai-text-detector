"""Seed the SQLite store from the project's existing, measured artefacts.

Nothing here invents numbers: providers and tuned parameters come from `.env` overlaid by
the process environment (see `read_env`), lanes with no env at all fall back to
`DEFAULT_LANES`/`DEFAULT_BAND`, which are copies of the workflow's own defaults in
`n8n/generate-workflow.py`. Benchmark rows come from `benchmark/results-*.jsonl` attributed
through `benchmark/runs-manifest.log` (and the four pre-manifest legacy tags), and the
catalog rows come from `benchmark/catalog-probe-*.json`.

  python -m backend.seed            # idempotent
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import db, discovery

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmark"
ENV_PATH = ROOT / ".env"

# Tags captured before runs-manifest.log existed; attribution taken verbatim from
# benchmark/tiebreak-sweep.js LEGACY so the two can never disagree silently.
LEGACY_TAGS = {
    "ultra550": "nvidia/nemotron-3-ultra-550b-a55b",
    "gptoss20b": "openai/gpt-oss-20b",
    "baseline": "nvidia/nemotron-3-super-120b-a12b",
    "repeat01": "nvidia/nemotron-3-super-120b-a12b",
    "t0": "nvidia/nemotron-3-super-120b-a12b",
    "ultra550-evidence": "nvidia/nemotron-3-ultra-550b-a55b",
    "smoke": "nvidia/nemotron-3-ultra-550b-a55b",
}

# (name, label, base_url, free_tier, self_hosted, notes)
#
# `self_hosted` marks the endpoints that are a box the deployment does not serve, and whose owners
# therefore get to name their own address on a key (see app.owner_endpoint). It is deliberately NOT
# in the ON CONFLICT update below, unlike base_url and notes: every other column here is a seed
# fact the boot re-asserts, but this one is also an admin's call on PATCH /providers, and a row
# they flagged would silently revert at the next start. Fresh installs get it from the INSERT.
PROVIDERS = [
    ("nvidia", "NVIDIA NIM", "https://integrate.api.nvidia.com/v1", 1, 0,
     "Free tier saturates a shared worker pool (16/16), not the key. 55 of 80 listed models 404."),
    ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", 1, 0,
     ":free models 429 from the upstream shared pool; transient, not an account cap."),
    ("omniroute", "OmniRoute (self-hosted)", "http://127.0.0.1:20128/v1", 1, 1,
     "Local router; adds models only if fed upstream keys. Must be 127.0.0.1, not localhost: n8n's "
     "fetch resolves localhost to ::1 and the gateway binds v4 only (ECONNREFUSED ::1:20128)."),
    ("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", 1, 0,
     "OpenAI-compatible endpoint; key added 2026-10-11."),
]

# Short, self-serve "how to get a key" cards for the ranking page. Fields match the
# frontend KeyGuide shape ({provider, url, steps, title?}). The urls are the real
# free-tier key pages; omniroute is omitted because it is self-hosted, not sign-up.
#
# Each url was fetched on 2026-10-16 and every one of them is a page behind a sign-in wall, so
# the steps say what a logged-out visitor actually sees and where to click afterwards. This
# matters because these are the three links a confused user follows when a lane 401s: a bare
# deep path that resolves only once authenticated reads as a dead link, and `openrouter.ai`
# answers an unauthenticated GET of /settings/keys with its Sign In shell while NVIDIA's
# /settings/api-keys answers 404 for a logged-out GET despite being the real page. Neither host
# is a placeholder and neither is allowed to become one: see the shape guard in test_seed.py.
DEFAULT_API_KEY_GUIDES = [
    {"provider": "nvidia", "title": "NVIDIA NIM (free)",
     "url": "https://build.nvidia.com",
     "steps": "Sign in with a free account, open any model, and click 'Get API key' "
              "to mint an nvapi- key (the same list lives at build.nvidia.com/settings/api-keys "
              "once you are signed in; it 404s for a logged-out link). Free tier shares a worker "
              "pool, so busy models 429/503."},
    {"provider": "openrouter", "title": "OpenRouter (free)",
     "url": "https://openrouter.ai/settings/keys",
     "steps": "Sign in first -- the link lands on the Sign In page when you are logged out -- "
              "then create a key and use models with the ':free' suffix. Free lanes rate-limit "
              "from the shared upstream pool; retries usually clear it."},
    {"provider": "gemini", "title": "Google AI Studio (free)",
     "url": "https://aistudio.google.com/apikey",
     "steps": "Sign in with a Google account (this link redirects to accounts.google.com first) "
              "and click 'Create API key'. Uses the OpenAI-compatible endpoint listed above."},
]


# Every env name seed understands. Spelled out rather than derived from TUNED/ACCOUNT_KEY_FAMILIES
# so this list can be read where it is used; test_every_tuned_key_is_overlayable keeps the two in
# step, and an unlisted variable can never reach the database by accident.
KNOWN_ENV_KEYS = frozenset({
    "N8N_URL", "WEBHOOK_PATH", "ALLOWED_LLM_HOSTS", "DETECTOR_VERSION", "THRESHOLD_VERSION",
    "ALLOW_BYOK", "API_KEY_GUIDES", "MIN_TEXT_LENGTH", "MAX_TEXT_LENGTH",
    "PRIMARY_MODEL", "PRIMARY_BASE_URL", "PRIMARY_API_KEY",
    "SECONDARY_MODEL", "SECONDARY_BASE_URL", "SECONDARY_API_KEY", "SECONDARY_ENABLED",
    "LLM_TEMPERATURE", "LLM_TOP_P", "LLM_MAX_TOKENS", "LLM_TIMEOUT_MS",
    "AI_PROBABILITY_THRESHOLD", "AI_CONFIDENCE_THRESHOLD",
    "HUMAN_PROBABILITY_THRESHOLD", "HUMAN_CONFIDENCE_THRESHOLD",
    "SECONDARY_DECISION_THRESHOLD",
})

# Numbered account keys are matched by pattern, not enumerated: OPENROUTER_API_KEY_3 in the
# launch environment is a key the operator means to be imported.
ACCOUNT_KEY_PATTERNS = tuple(
    re.compile(rf"^{name}(_\d+)?$")
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY")
)


def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Config = the `.env` file, then the process environment on top of it.

    Reading ONLY the file was wrong twice over, both found on 2026-10-16:
      * docker-compose.yml passes `N8N_URL: http://n8n:5678` as a container env var and the
        repo `.env` has no N8N_URL line at all, so the seeded setting stayed
        http://localhost:5678 — inside that network the backend cannot reach n8n on localhost,
        so every detection in the container would 502.
      * the packaged exe ships no `.env`, so `N8N_URL=... AI-Text-Detector.exe` was ignored
        and it hung 120 s on http://localhost:5678 before answering 502.
    Live environment wins: whoever set it in the launch environment means it for this run.
    Only names seed understands are overlaid, so no stray variable can enter the database.
    """
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip("\"'")
    for name, value in os.environ.items():
        if not value:
            continue
        if name in KNOWN_ENV_KEYS or any(p.match(name) for p in ACCOUNT_KEY_PATTERNS):
            out[name] = value
    return out


def _num(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(env[key])
    except (KeyError, ValueError):
        return default


def provider_for_base_url(base_url: str) -> str:
    if "openrouter" in base_url:
        return "openrouter"
    if "generativelanguage" in base_url:
        return "gemini"
    if "localhost" in base_url or "127.0.0.1" in base_url:
        return "omniroute"
    return "nvidia"


def seed_providers(conn, env: dict[str, str]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name, label, base_url, free, self_hosted, notes in PROVIDERS:
        conn.execute(
            """INSERT INTO providers (name, label, base_url, free_tier, self_hosted, notes)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 label = excluded.label, base_url = excluded.base_url,
                 free_tier = excluded.free_tier, notes = excluded.notes,
                 updated_at = datetime('now')""",
            (name, label, base_url, free, self_hosted, notes),
        )
        ids[name] = conn.execute("SELECT id FROM providers WHERE name = ?", (name,)).fetchone()["id"]
    # Honour per-lane endpoint overrides from .env so the DB matches what is deployed.
    for env_key, name in (("PRIMARY_BASE_URL", None), ("SECONDARY_BASE_URL", None)):
        val = env.get(env_key)
        if not val:
            continue
        pname = provider_for_base_url(val)
        conn.execute(
            "UPDATE providers SET base_url = ?, updated_at = datetime('now') WHERE name = ?",
            (val, pname),
        )
        ids[pname] = conn.execute("SELECT id FROM providers WHERE name = ?", (pname,)).fetchone()["id"]
    return ids


# Families of interchangeable accounts: same provider, different account. Worth enumerating
# rather than hand-listing, because a cap that is per account stacks -- an
# OPENROUTER_API_KEY_3 that never reaches the database is a day of free headroom the app
# cannot spend, and nothing says which keys were dropped.
ACCOUNT_KEY_FAMILIES = {"OPENROUTER_API_KEY": "https://openrouter.ai/api/v1",
                        "GEMINI_API_KEY": "https://generativelanguage.googleapis.com/v1beta/openai"}
LANE_KEY_ROLES = (("PRIMARY_API_KEY", "PRIMARY_BASE_URL", "primary lane"),
                  ("SECONDARY_API_KEY", "SECONDARY_BASE_URL", "secondary lane"))


def account_key_candidates(env: dict[str, str]) -> list[tuple[str, str, int]]:
    """(env var, base url, account number) for every numbered key of a family.

    The label comes from the provider name, not the env var, so a re-seed matches the rows
    already stored instead of leaving both spellings behind.
    """
    out: list[tuple[str, str, int]] = []
    for base_name, default_url in ACCOUNT_KEY_FAMILIES.items():
        pat = re.compile(rf"^{base_name}(_(\d+))?$")
        found = []
        for name in env:
            m = pat.match(name)
            if m and env[name]:
                found.append((int(m.group(2) or 1), name))
        for n, name in sorted(found):
            out.append((name, default_url, n))
    return out


def seed_keys(conn, env: dict[str, str], provider_ids: dict[str, int]) -> list[str]:
    added: list[str] = []
    candidates: list[tuple[str, str, object]] = [
        (env_key, env.get(base_env, ""), label)
        for env_key, base_env, label in LANE_KEY_ROLES
    ] + account_key_candidates(env)
    for env_key, base_url, label_or_account in candidates:
        plain = env.get(env_key)
        if not plain:
            continue
        pname = provider_for_base_url(base_url or "")
        pid = provider_ids.get(pname)
        if pid is None:
            continue
        label = (label_or_account if isinstance(label_or_account, str)
                 else pname if label_or_account == 1 else f"{pname} #{label_or_account}")
        cipher, hint = db.encrypt_key(plain)
        row = conn.execute(
            "SELECT id, key_hint FROM api_keys WHERE provider_id = ? AND label = ?", (pid, label)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO api_keys (provider_id, label, key_cipher, key_hint) VALUES (?,?,?,?)",
                (pid, label, cipher, hint),
            )
            added.append(f"{pname}/{label}")
        elif row["key_hint"] != hint:
            conn.execute(
                """UPDATE api_keys SET key_cipher = ?, key_hint = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (cipher, hint, row["id"]),
            )
            added.append(f"{pname}/{label} (rotated)")
    return added


TUNED = {
    "ai_prob_threshold": "AI_PROBABILITY_THRESHOLD",
    "ai_conf_threshold": "AI_CONFIDENCE_THRESHOLD",
    "human_prob_threshold": "HUMAN_PROBABILITY_THRESHOLD",
    "human_conf_threshold": "HUMAN_CONFIDENCE_THRESHOLD",
    "secondary_decision_threshold": "SECONDARY_DECISION_THRESHOLD",
}

# Baked defaults for an install that has no `.env`: the packaged exe, and any container
# started without the operator's file. `seed_models` used to insert rows only for models
# named in env, so a fresh exe booted with providers and ZERO models, every `/detect`
# answered 409 NO_PRIMARY_MODEL, and the app was dead on arrival for anyone who just
# installed it. These are not new numbers: they are the workflow's own defaults, read
# verbatim from NORMALIZE_JS in n8n/generate-workflow.py (the `num(env.X, default)`
# literals), which is what a lane actually runs on when `.env` omits them. Keeping them
# here means the DB row tells the truth about the band that was applied.
# test_band_defaults_match_the_workflow asserts the two copies cannot drift.
DEFAULT_LANES = {
    "primary": ("z-ai/glm-5.3-flash", "nvidia"),
    "secondary": ("google/diffusiongemma-26b-a4b-it", "nvidia"),
}
DEFAULT_BAND = {
    "AI_PROBABILITY_THRESHOLD": 0.69,
    "AI_CONFIDENCE_THRESHOLD": 0.60,
    "HUMAN_PROBABILITY_THRESHOLD": 0.40,
    "HUMAN_CONFIDENCE_THRESHOLD": 0.60,
    "SECONDARY_DECISION_THRESHOLD": 0.90,
}
DEFAULT_LLM = {"LLM_TEMPERATURE": 0.1, "LLM_TOP_P": 0.9,
               "LLM_MAX_TOKENS": 0, "LLM_TIMEOUT_MS": 90000}

# A probability must name its own ruler, and a baked default is no exception. `DEFAULT_BAND`
# is the workflow's own fallback -- what a lane runs on when nothing is sent -- so it stays
# one number set for every model. But a model promoted into a lane was measured against its
# OWN band, and `seed_models()` re-runs on every boot: writing DEFAULT_BAND over a promoted
# row would silently re-band that model to another model's cut, which is how a 0.981-accuracy
# lane turns into an unlabelled 0.69 one overnight. The overlay below is the promoted row's
# measured band, copied from `models` where it was fitted, and it is what the DB must claim.
LANE_BANDS = {
    "z-ai/glm-5.3-flash": {
        "AI_PROBABILITY_THRESHOLD": 0.80,
        "AI_CONFIDENCE_THRESHOLD": 0.50,
        "HUMAN_PROBABILITY_THRESHOLD": 0.55,
        "HUMAN_CONFIDENCE_THRESHOLD": 0.50,
    },
}

# Measured leg time on the free NVIDIA pool: glm-5.3-flash answered a real document in 86 s
# (probe_primary_candidates.py, 2026-10-16) against the baked 90 s -- inside the ceiling but
# with no room for a slow pool, and a lane that times out is indistinguishable from a dead
# one to the user. 120 s was tried first and was still not enough: the live default-lane proof
# timed out at exactly 120 s on 2026-10-16. 180 s was the number the capture supported, and on
# 2026-10-17 the live container proved it is the number the *pool* does not support: the shipped
# default answered hu-000 (320 chars) correctly -- HUMAN_LIKELY, p 0.15, 3 phrase quotes -- in
# 214.2 s, four seconds past the 210 s the backend is willing to wait, after three ENGINE_TIMEOUT
# answers in the preceding half hour. The operator priced it as worth waiting for: 240 s, which
# keeps the whole chain inside its outer tiers (240 lane + 30 s headroom = 270 s wait, gunicorn
# 285 s, nginx 330 s) and leaves ~26 s of margin over the measured 214 s.
LANE_TIMEOUT_MS = {"z-ai/glm-5.3-flash": 240000}

# The tie-break lane ships switched off. Measured reason (round 25c, hc3-clean 48 texts, the
# deployed primary on its own band): the lane was asked about 3 texts and answered all 3 wrongly
# -- three human documents accused of being AI -- while the same ensemble with the lane off
# answers 45 of 48 at 44 correct / 1 wrong. A strong primary with a tight band leaves the
# second lane nothing useful to do; on a drifting primary the same authority is worth 13 correct
# calls out of 15, which is why it is a switch and not a deletion. `enabled` is written only on
# the first insert (never in the upsert's DO UPDATE), so an admin who turns the lane back on
# keeps it on across every later boot.
LANE_DEFAULT_ENABLED = {"secondary": False}

# The operator's own name for the same switch. An explicit value in `.env` or the launch
# environment outranks the baked default -- the same rule that lets a threshold in env outrank
# a measured band: whoever sets it means it for this deployment. Any other value (absent, blank,
# not a boolean) means "no opinion, use the default above".
LANE_ENV_SWITCH = {"secondary": "SECONDARY_ENABLED"}


def lane_enabled(env: dict[str, str] | None, lane: str) -> bool:
    """Whether a lane's model row should be enabled on a fresh install.

    Returns the operator's explicit env choice when there is one, else `LANE_DEFAULT_ENABLED`.
    Read by both the first insert and `apply_lane_defaults`, so an upgrade honours the same
    .env a new install would.
    """
    name = LANE_ENV_SWITCH.get(lane)
    raw = str((env or {}).get(name, "")).strip().lower() if name else ""
    if raw in ("true", "false"):
        return raw == "true"
    return LANE_DEFAULT_ENABLED.get(lane, True)

# Bump this when the lane defaults above change, so existing installs are re-told exactly once.
LANE_DEFAULTS_VERSION = 2

# The band above is capped-era for the NVIDIA pair: fitted under the old 700-token
# ceiling and still awaiting the uncapped re-validation tracked as tasks #109/#111. Every
# band in this file was fitted before the cap was removed on 2026-10-16, so a default that
# reads as fully validated would be a claim the project has not earned -- and admins pick
# lanes off these notes.
UNREVALIDATED = "Band measured under the pre-2026-10-16 token cap; uncapped re-validation pending (tasks #109/#111)."

_LANE_NOTES = {
    "primary": "Deployed primary lane. Decision band calibrated on this model's scale. ",
    "secondary": "Tie-break lane, configured but switched OFF by default: on the deployed "
                 "primary it was asked about 3 of 48 benchmark texts and answered all 3 wrongly "
                 "(round 25c). An admin can enable it per install -- it earns its keep on a "
                 "drifting primary. ",
}


def seed_models(conn, env: dict[str, str], provider_ids: dict[str, int]) -> list[str]:
    def tuned_for(model_id: str, col: str, key: str) -> float | None:
        # Real precedence: the operator's env, then the band this model was actually fitted
        # to, then the workflow's neutral fallback. Never another lane model's cut.
        return (_num(env, key, 0.0) or LANE_BANDS.get(model_id, {}).get(key)
                or DEFAULT_BAND.get(key))

    temp = _num(env, "LLM_TEMPERATURE", DEFAULT_LLM["LLM_TEMPERATURE"])
    top_p = _num(env, "LLM_TOP_P", DEFAULT_LLM["LLM_TOP_P"])
    max_tokens = int(_num(env, "LLM_MAX_TOKENS", DEFAULT_LLM["LLM_MAX_TOKENS"]))   # 0 = send no cap; the answer must close its own JSON
    rows = []
    for lane in ("primary", "secondary"):
        model_id = env.get(f"{lane.upper()}_MODEL")
        if not model_id:
            model_id, pname = DEFAULT_LANES[lane]
            notes = _LANE_NOTES[lane] + UNREVALIDATED
            rows.append((model_id, lane, pname, notes,
                         int(_num(env, "LLM_TIMEOUT_MS",
                                  LANE_TIMEOUT_MS.get(model_id, DEFAULT_LLM["LLM_TIMEOUT_MS"])))))
            continue
        pname = provider_for_base_url(env.get(f"{lane.upper()}_BASE_URL", ""))
        # An env-named lane is the operator's own deployment: its band is whatever they
        # measured, so nothing is appended to the note and no baked band competes with it.
        # The TIMEOUT is different in kind -- it is a property of the model's latency on a
        # free pool, not a policy choice, so the measured value still applies whenever the
        # operator left LLM_TIMEOUT_MS unset. Skipping it here is what made a promoted 86 s
        # model run against the baked 90 s ceiling and answer 502 "timed out" under a slow
        # pool (verify_default_lane_live.py, 2026-10-16).
        rows.append((model_id, lane, pname, _LANE_NOTES[lane].rstrip(),
                     int(_num(env, "LLM_TIMEOUT_MS",
                              LANE_TIMEOUT_MS.get(model_id, DEFAULT_LLM["LLM_TIMEOUT_MS"])))))
    made = []
    for model_id, lane, pname, notes, timeout in rows:
        pid = provider_ids.get(pname)
        if pid is None:
            continue
        tuned = {col: tuned_for(model_id, col, key) for col, key in TUNED.items()}
        conn.execute(
            """INSERT INTO models (provider_id, model_id, display_name, role, lane,
                   temperature, top_p, max_tokens, timeout_ms,
                   ai_prob_threshold, ai_conf_threshold, human_prob_threshold,
                   human_conf_threshold, secondary_decision_threshold, notes, measured, enabled)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(provider_id, model_id) DO UPDATE SET
                 lane = excluded.lane, role = excluded.role,
                 temperature = excluded.temperature, top_p = excluded.top_p,
                 max_tokens = excluded.max_tokens, timeout_ms = excluded.timeout_ms,
                 ai_prob_threshold = excluded.ai_prob_threshold,
                 ai_conf_threshold = excluded.ai_conf_threshold,
                 human_prob_threshold = excluded.human_prob_threshold,
                 human_conf_threshold = excluded.human_conf_threshold,
                 secondary_decision_threshold = excluded.secondary_decision_threshold,
                 notes = excluded.notes, updated_at = datetime('now')""",
            (pid, model_id, model_id.split("/")[-1], lane, lane, temp, top_p, max_tokens, timeout,
             tuned["ai_prob_threshold"], tuned["ai_conf_threshold"], tuned["human_prob_threshold"],
             tuned["human_conf_threshold"], tuned["secondary_decision_threshold"], notes,
             # Deliberately absent from DO UPDATE: this is a first-install default, not a
             # policy the boot re-imposes. An admin who enables the tie-break lane would
             # otherwise lose it at the next restart, which is how a re-seed silently
             # reverts a deliberate change.
             1 if lane_enabled(env, lane) else 0),
        )
        made.append(model_id)

    # A lane is claimed, not inherited. Changing DEFAULT_LANES and restarting used to leave
    # the *previous* default still marked lane='primary' beside the new one, because the
    # seed only ever writes the rows it names -- so an upgraded install had two primary rows
    # and which one answered depended on row order, while the UI showed a lane nobody had
    # chosen. Rows this seed did not name lose the lane; their model, band and history stay.
    # `role` cannot go NULL (schema CHECK), and 'either' is the schema's own word for
    # "not bound to a lane", which is also how /models writes an unclaimed row.
    kept = {(provider_ids.get(pname), model_id) for model_id, _lane, pname, _n, _t in rows
            if provider_ids.get(pname) is not None}
    for rid, rpid, rmodel in conn.execute(
            "SELECT id, provider_id, model_id FROM models "
            "WHERE lane IN ('primary', 'secondary')").fetchall():
        if (rpid, rmodel) not in kept:
            conn.execute("UPDATE models SET lane = NULL, role = 'either', "
                         "updated_at = datetime('now') WHERE id = ?", (rid,))
    return made


def _manifest_models() -> dict[str, dict]:
    out = {t: {"model": m} for t, m in LEGACY_TAGS.items()}
    manifest = BENCH / "runs-manifest.log"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^params: (\{.*\})$", line)
            if not m:
                continue
            p = json.loads(m.group(1))
            out[p["tag"]] = p
    return out


def _run_log_params(tag: str) -> dict:
    log = BENCH / f"run-{tag}.log"
    if not log.exists():
        return {}
    head = log.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
    out: dict = {}
    for line in head:
        for k, v in re.findall(r"(\w+)=([^\s]+)", line):
            out.setdefault(k, v)
    return out


HC3_CORPUS_SIZE = 48


def corpus_size() -> int:
    """How many texts the full HC3 corpus holds, read from the file when it is present."""
    try:
        return len(json.loads((BENCH / "corpus.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return HC3_CORPUS_SIZE


def corpus_label(manifest_corpus: str | None, tag: str,
                 n_rows: int | None = None, full_size: int | None = None) -> str:
    """Name the corpus a run scored, from the file the harness actually read.

    Why not infer it from the tag: a 12-text screen imported as 'hc3-48' would claim
    to be the full corpus, and the table it feeds is the one people rank models on.
    The no-provenance fallback can only claim a corpus the row count evidences — a
    4-text smoke is credited with neither.
    """
    if manifest_corpus:
        return {
            "corpus.json": "hc3-48",
            "corpus-screen12.json": "hc3-screen12",
            "corpus-books.json": "gutenberg-books",
        }.get(manifest_corpus, manifest_corpus.removeprefix("corpus-").removesuffix(".json"))
    if "book" in tag:
        return "gutenberg-books"
    if n_rows is not None and n_rows < (full_size or corpus_size()):
        return "unattributed"
    return "hc3-48"


def import_benchmarks(conn) -> list[str]:
    models = _manifest_models()
    imported = []
    for path in sorted(BENCH.glob("results-*.jsonl")):
        tag = path.stem[len("results-"):]
        meta = models.get(tag, {})
        params = _run_log_params(tag)
        # The harness prints tag/model/prompt/... on the first line of its own run log, so a
        # run captured before runs-manifest.log existed is still attributable. Without this,
        # `bookdiffgemma` was stored with model="bookdiffgemma" -- a tag posing as a model.
        model = meta.get("model") or params.get("model") or tag
        provider = provider_for_base_url(params.get("base_url", meta.get("base_url", "")))
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        # benchmark_results holds SCORED corpus rows, each keyed by its corpus text id. Some
        # results-*.jsonl files are not accuracy captures at all: results-appbench.jsonl is the
        # response-time benchmark (model/leg/wall_ms) and results-stressgen.jsonl is generated-text
        # provenance (no label, no probability). Their rows carry no `id`, and inserting NULL
        # text_id trips the NOT NULL constraint and rolls back the whole import. Drop the rows
        # that have no corpus id -- a file made only of those is not a scored run and is skipped.
        rows = [r for r in rows if r.get("id") is not None]
        if not rows:
            continue
        corpus = corpus_label(meta.get("corpus"), tag, len(rows))
        # A capture recorded with max_tokens 0 ran uncapped. `x or 700` would silently rewrite
        # that 0 into 700 and mislabel the run, so only a genuinely missing key falls back to
        # the historical cap.
        _mt = params.get("max_tokens")
        if _mt is None:
            _mt = meta.get("max_tokens")
        if _mt is None:
            _mt = 700
        pjson = {
            "prompt": meta.get("prompt", params.get("prompt", "direct")),
            "temperature": float(params.get("temp", meta.get("temperature", 0.1) or 0.1)),
            "top_p": float(params.get("top_p", meta.get("top_p", 0.9) or 0.9)),
            "max_tokens": int(_mt),
            "concurrency": params.get("concurrency", meta.get("concurrency")),
            "base_url": params.get("base_url") or meta.get("base_url"),
        }
        # One results file is one run. A re-labelled or newly attributed run must not linger
        # under its old key: the same tag at two corpus labels, or once as a tag and once as
        # the model it really used, would read as two independent measurements of one file.
        conn.execute(
            "DELETE FROM benchmark_runs WHERE tag = ? AND (corpus != ? OR model != ?)",
            (tag, corpus, model),
        )
        conn.execute(
            """INSERT INTO benchmark_runs (tag, provider, model, corpus, prompt, params_json,
                   n_rows, source_file)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(tag, model, corpus) DO UPDATE SET
                 provider = excluded.provider, prompt = excluded.prompt,
                 params_json = excluded.params_json, n_rows = excluded.n_rows,
                 source_file = excluded.source_file, imported_at = datetime('now')""",
            (tag, provider, model, corpus, pjson["prompt"], json.dumps(pjson), len(rows),
             path.name),
        )
        run_id = conn.execute(
            "SELECT id FROM benchmark_runs WHERE tag = ? AND model = ? AND corpus = ?",
            (tag, model, corpus),
        ).fetchone()["id"]
        conn.execute("DELETE FROM benchmark_results WHERE run_id = ?", (run_id,))
        conn.executemany(
            """INSERT INTO benchmark_results
               (run_id, text_id, label, ok, error_code, ai_probability, confidence)
               VALUES (?,?,?,?,?,?,?)""",
            [(run_id, r.get("id"), r.get("label", ""), 1 if r.get("ok") else 0,
              r.get("error_code"), r.get("ai_probability"), r.get("confidence")) for r in rows],
        )
        imported.append(f"{tag} -> {model} ({len(rows)} rows, {corpus})")
    return imported


def import_catalog(conn) -> list[str]:
    imported = []
    for fname, provider in (("catalog-probe-nvidia.json", "nvidia"),
                            ("catalog-probe-openrouter.json", "openrouter")):
        path = BENCH / fname
        if not path.exists():
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(rows, list):
            continue
        n = 0
        for r in rows:
            model = r.get("model") or r.get("id")
            if not model:
                continue
            http = r.get("http")
            if http == 200 and r.get("scored", True) is not False:
                status = "scored" if r.get("ai_probability") is not None or r.get("score") is not None else "answered"
            elif http == 200:
                status = "answered"
            elif http == 429:
                status = "rate_limited"
            elif http in (403, 401):
                status = "forbidden"
            elif http == 404:
                status = "not_found"
            elif http is None:
                status = "timeout"
            else:
                status = f"http_{http}"
            note = str(r.get("detail") or r.get("verdict") or "")[:400]
            conn.execute(
                """INSERT INTO catalog_models (provider, model_id, listed, probe_status, probe_note, latency_ms)
                   VALUES (?,?,1,?,?,?)
                   ON CONFLICT(provider, model_id) DO UPDATE SET
                     probe_status = excluded.probe_status, probe_note = excluded.probe_note,
                     latency_ms = excluded.latency_ms, probed_at = datetime('now')""",
                (provider, model, status, note, r.get("ms")),
            )
            n += 1
        imported.append(f"{fname}: {n} catalog rows")
    return imported


def seed_settings(conn, env: dict[str, str]) -> None:
    defaults = {
        "n8n_url": env.get("N8N_URL", "http://localhost:5678"),
        "webhook_path": env.get("WEBHOOK_PATH", "/webhook/ai-detect-ai"),
        "allowed_llm_hosts": env.get(
            "ALLOWED_LLM_HOSTS",
            "localhost,127.0.0.1,::1,integrate.api.nvidia.com,openrouter.ai",
        ),
        "detector_version": env.get("DETECTOR_VERSION", "2.0.0"),
        "threshold_version": env.get("THRESHOLD_VERSION", "2.0.0"),
        "allow_byok": env.get("ALLOW_BYOK", "true"),
        # Self-serve key guides the ranking page links out to. These are the real,
        # stable key-issuance pages for the free tiers we benchmark — not invented.
        # Seeded only when absent (the `is None` guard below), so an admin editing
        # the guides in /settings is never clobbered by a later re-seed.
        "api_key_guides": env.get("API_KEY_GUIDES") or json.dumps(DEFAULT_API_KEY_GUIDES),
        # paper_url is intentionally NOT defaulted: the citable preprint DOI is minted
        # by the author at publication time. A blank url hides the "read paper" link
        # rather than pointing at a fabricated reference. Admin sets it once the DOI exists.
    }
    for k, v in defaults.items():
        if db.get_setting(conn, k) is None:
            db.set_setting(conn, k, str(v))


def apply_lane_defaults(conn, env: dict[str, str] | None = None) -> int:
    """Switch the lanes to the current default once per install, then never again.

    `LANE_DEFAULT_ENABLED` is a first-install default: `enabled` is written on insert and is
    deliberately absent from the upsert, which is what protects an admin's later choice. That
    also means an *existing* install would keep the lane the old seed enabled -- a default nobody
    ever chose, and the one round 25c measured as accuracy-negative on the deployed primary. The
    version key records that this install has been told, so the switch is applied exactly once
    and an admin enabling the tie-break afterwards survives every restart. An install that had
    deliberately enabled it loses it on this one boot and can put it back for good.

    The value applied is `lane_enabled(env, lane)`, so an operator who wrote
    `SECONDARY_ENABLED=true` in `.env` gets the lane left on by the upgrade instead of having a
    baked default outvote their own file.
    """
    if db.get_setting(conn, "lane_defaults_version") is not None:
        return 0
    changed = 0
    for lane in LANE_DEFAULT_ENABLED:
        want = 1 if lane_enabled(env, lane) else 0
        changed += conn.execute(
            "UPDATE models SET enabled = ?, updated_at = datetime('now') "
            "WHERE lane = ? AND enabled != ?", (want, lane, want)).rowcount
    db.set_setting(conn, "lane_defaults_version", str(LANE_DEFAULTS_VERSION))
    return changed


def run_seed() -> dict:
    db.init_db()
    env = read_env()
    with db.session() as conn:
        provider_ids = seed_providers(conn, env)
        keys = seed_keys(conn, env, provider_ids)
        models = seed_models(conn, env, provider_ids)
        lane_defaults = apply_lane_defaults(conn, env)
        benchmarks = import_benchmarks(conn)
        catalog = import_catalog(conn)
        # Catalogue rows written by an older build use a vocabulary in which a timeout was a
        # verdict, so those models have no `models` row and cannot be picked at all. Re-read them
        # under the current rule before anything else claims a lane's worth of candidates.
        vocab = discovery.migrate_probe_vocabulary(conn)
        seed_settings(conn, env)
        summary = {
            "providers": len(provider_ids),
            "keys_imported": keys,
            "models_seeded": models,
            "lane_defaults_applied": lane_defaults,
            "benchmark_runs": len(benchmarks),
            "catalog_files": len(catalog),
            "probe_vocab_reclassified": vocab["reclassified"],
            "probe_vocab_rows_added": vocab["rows_added"],
            "counts": db.counts(conn),
        }
    return summary


if __name__ == "__main__":
    out = run_seed()
    print(json.dumps(out, indent=2))
