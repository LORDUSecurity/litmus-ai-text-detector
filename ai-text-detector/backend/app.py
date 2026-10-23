"""Flask backend for Litmus, the AI text detector.

Replaces tools/detector-gateway.js. Keeps the two endpoints the Angular service already
speaks (POST /detect, GET /gateway-health) and adds the DB-owned configuration surface:
providers + endpoints, API keys (encrypted), models + tuned parameters, detection history,
the measured benchmark corpus results, and read-only n8n execution status.

Loopback only. API keys are never returned, never logged, and are removed from n8n's
execution rows a second after use by purge.Purger.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, g, jsonify, request

from . import auth, db, discovery, engine, extract, pool, security
from .purge import N8N_DB, Purger, WORKFLOW_ID

PORT = int(os.environ.get("BACKEND_PORT", "8787"))
HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")

LOOPBACK_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/@+-]+$")
KEY_CHARSET = re.compile(r"^[A-Za-z0-9._:+/-]+$")

purger = Purger()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    # Upload ceiling for POST /extract (documents). /detect bodies stay small by
    # contract; the per-type limits in extract.PER_TYPE_BYTES do the fine-grained work.
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
    db.init_db()

    @app.before_request
    def _conn():
        g.conn = db.connect()

    @app.teardown_request
    def _close(exc):
        conn = getattr(g, "conn", None)
        if conn is not None:
            conn.close()

    @app.after_request
    def _cors(resp):
        origin = request.headers.get("Origin")
        resp.headers["Access-Control-Allow-Origin"] = (
            origin if origin and LOOPBACK_ORIGIN.match(origin) else f"http://{HOST}:{PORT}"
        )
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Primary-Api-Key, X-Secondary-Api-Key"
        )
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Vary"] = "Origin, Access-Control-Request-Headers"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.errorhandler(sqlite3.Error)
    def _db_error(exc):
        return jsonify(error="database error", detail=str(exc)), 500

    @app.errorhandler(413)
    def _too_large(exc):
        return jsonify({
            "ok": False, "error_code": "PAYLOAD_TOO_LARGE",
            "error": "Upload exceeds the 20 MB server limit.",
        }), 413

    # Registered AFTER _conn so the guard's _load_user() can read g.conn; before_request
    # handlers run in registration order. Seed the admin account first so a fresh deploy
    # (or the local exe) always has a way in.
    with db.session() as _c:
        db.seed_admin(_c)
    auth.init_auth(app)
    security.init(app)

    register_routes(app)
    return app


# --------------------------------------------------------------------------- helpers

def conn() -> sqlite3.Connection:
    return g.conn


def setting(key: str, default: str = "") -> str:
    return db.get_setting(conn(), key, default) or default


def provider_rows() -> list[sqlite3.Row]:
    return conn().execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM api_keys k WHERE k.provider_id = p.id) AS key_count,
                  (SELECT COUNT(*) FROM models m WHERE m.provider_id = p.id) AS model_count
           FROM providers p ORDER BY p.id"""
    ).fetchall()


def provider_public(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["free_tier"] = bool(d["free_tier"])
    # Whether this endpoint is a box the deployment does not serve. The key form shows an
    # endpoint+port field only for these, so the flag has to reach the browser as a bool.
    d["self_hosted"] = bool(d.get("self_hosted"))
    return d


def key_public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "label": row["label"],
        "key_hint": row["key_hint"],
        "is_active": bool(row["is_active"]),
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def model_public(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["measured"] = bool(d["measured"])
    prov = conn().execute(
        "SELECT name, base_url FROM providers WHERE id = ?", (d["provider_id"],)
    ).fetchone()
    if prov:
        d["provider_name"] = prov["name"]
        d["provider_base_url"] = prov["base_url"]
    return d


TEXT_LIMITS = {"display_name": 120, "notes": 1000}


def over_long_text(field: str, value) -> str | None:
    """Reject an over-long text instead of storing its first 1000 characters.

    The notes column is the only record of how a band was measured, and the caveats are written
    last -- "in-sample", "tightest margin 0.02", "do not assign a lane". Silently truncating
    leaves a number in the app that reads like a validated measurement whose fine print is gone.
    """
    if value is None:
        return None
    text = str(value)
    limit = TEXT_LIMITS[field]
    if len(text) > limit:
        return (f"{field} must be <= {limit} chars (got {len(text)}); the value is stored as "
                f"written, so a longer one would lose its tail")
    return None


def default_models() -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    rows = conn().execute(
        """SELECT m.*, p.base_url AS provider_base_url, p.name AS provider_name
           FROM models m JOIN providers p ON p.id = m.provider_id
           WHERE m.enabled = 1 ORDER BY m.id"""
    ).fetchall()
    primary = next((r for r in rows if r["lane"] == "primary"), None)
    secondary = next((r for r in rows if r["lane"] == "secondary"), None)
    return primary, secondary


def key_target(c: sqlite3.Connection, user_id: int | None, prov) -> dict:
    """The provider row as this caller's key sees it: unchanged, except dialled at the endpoint the
    key was saved with when it names one.

    For NVIDIA, OpenRouter and Gemini a provider and its endpoint are the same thing, so this is a
    no-op copy. For a self-hosted gateway they are not -- one row, many boxes -- and every path that
    spends a credential (verify, the Test button, discovery) has to ask the owner's router, not the
    address the seed recorded. `lane_base_url` does the same job for a detection lane; this is the
    shape the probe and discovery helpers take, so `_probe_with_key` keeps its four arguments.
    """
    out = dict(prov)
    if user_id is None:
        return out
    row = db.user_key_row_for_provider(c, user_id, out["id"])
    if row is not None and row["base_url"]:
        out["base_url"] = row["base_url"]
    return out


def lane_base_url(c: sqlite3.Connection, user_id: int | None, provider_name: str) -> str:
    """Where a lane built on this provider is actually dialled, for this caller.

    `provider_base_url_by_name` answers for the deployment, and that is right wherever a provider
    and its endpoint are one thing. A self-hosted gateway is not: one shared `omniroute` row covers
    every owner's box, and the address that belongs to *this* caller is the one written on the key
    they saved. So the key answers first, and a key that names no endpoint leaves the answer exactly
    where it was.
    """
    if not provider_name:
        return ""
    prov = c.execute("SELECT id, base_url FROM providers WHERE name = ? AND enabled = 1",
                     (provider_name,)).fetchone()
    if prov is None:
        return ""
    if user_id is not None:
        key = db.user_key_row_for_provider(c, user_id, prov["id"])
        if key is not None and key["base_url"]:
            return key["base_url"]
    return prov["base_url"]


def pool_primary_lane(c: sqlite3.Connection, user_id: int) -> dict | None:
    """The lane a plain Detect resolves from this user's ordered pool, or None.

    One function for two callers on purpose. /detect synthesises the request's primary lane from
    it; /ranking reports it as the user's "next run". The Ranking page is the only place a user
    can read which model will answer their runs, so it must not re-derive that answer and be free
    to drift from the request path -- a page that promises an order the app does not walk turns
    the ranking into a caption instead of a control (#142 is the shipped version of exactly that:
    the deployed default the page named never answered anything, because every request carried a
    baked override).

    The order is `pool.effective_order`, i.e. the user's own ranking with anything that stopped
    answering served last, so the page cannot name a down model as the first call.
    `provider_base_url_by_name` and `host_allowed` read through `conn()` -- the same request
    connection this caller passes -- so they are not a second source of truth.
    """
    rows = pool.effective_order(db.pool_list(c, user_id))
    if not rows:
        return None
    top = rows[0]
    base = lane_base_url(c, user_id, top["provider"]) or provider_base_url_by_name(top["provider"])
    if not base or not endpoint_for_lane(c, base, user_id):
        return None
    return {"base_url": base, "model": top["model_id"], "provider": top["provider"]}


def pool_tie_break_order(c: sqlite3.Connection, user_id: int,
                         primary_model: str | None) -> list[sqlite3.Row]:
    """The caller's pool BELOW the model that answered as primary, in walk order.

    Starts immediately after `primary_model` rather than at the top of the pool: the model that
    just scored the text cannot be the one that corroborates it, and the entry the user ranked
    next is the one they said they would rather have. This is the second lane's version of the
    primary's own failover walk, done inside one detection so a non-answer does not cost the
    user a second full call to the model that already answered.

    Empty when the primary is the pool's last entry, or is not in the pool at all. That is the
    pool having nothing to offer, not a misconfiguration, and the caller falls back to the row
    that owns the lane.
    """
    rows = pool.effective_order(db.pool_list(c, user_id))
    want = (primary_model or "").strip()
    pos = next((i for i, r in enumerate(rows) if (r["model_id"] or "").strip() == want), None)
    if pos is None:
        return []
    return rows[pos + 1:]


def model_by_id(model_id: str | None) -> sqlite3.Row | None:
    """The stored row for a model id, so its tuned parameters can follow the model.

    A request that names a lane -- the Ranking tab's "Score with this model", or a pool entry
    synthesised into a lane by pool_primary_lane() -- carries a model id and no calibration, and
    this is how the admin's stored band gets attached to it. Matching on model_id keeps the
    tuning attached to the model rather than to whichever code path built the lane. (Before
    #142 the Angular form sent a baked lane_config on EVERY request, which meant the
    DB-derived default-lane branch never ran; an untouched form now sends nothing.)
    """
    if not model_id:
        return None
    return conn().execute(
        """SELECT m.*, p.base_url AS provider_base_url, p.name AS provider_name
           FROM models m JOIN providers p ON p.id = m.provider_id
           WHERE m.model_id = ? AND m.enabled = 1
           ORDER BY m.lane IS NULL, m.id LIMIT 1""",
        (model_id,),
    ).fetchone()


# Column -> workflow tuning key. timeout_ms is renamed because the workflow calls
# it request_timeout_ms.
TUNING_FIELDS = ("temperature", "top_p", "max_tokens", "timeout_ms",
                 "ai_prob_threshold", "ai_conf_threshold", "human_prob_threshold",
                 "human_conf_threshold")


def tuning_block(primary: sqlite3.Row | None, secondary: sqlite3.Row | None) -> dict:
    """The stored parameters that the workflow should apply to this request.

    The workflow takes one tuning block per request, not one per lane, so the
    deciding lane owns it: sampling and the four decision cuts come from the row
    that describes the primary. With no primary row to describe, nothing is sent
    and the workflow's own defaults stand -- borrowing the secondary's sampling
    for the primary would be inventing a calibration nobody measured.

    The tie-break cut is the exception: it is read against the *secondary's*
    probability scale -- half of diffusiongemma's real-human scores on the HC3
    corpus sit at 0.75 or above, while nemotron's human median is 0.25 and its
    worst score on published book prose is 0.18 -- so it comes from the secondary
    row and applying the primary's value to it would be a category error.
    """
    tuning: dict[str, object] = {}
    if primary is not None:
        for field in TUNING_FIELDS:
            value = primary[field]
            if value is not None:
                tuning["request_timeout_ms" if field == "timeout_ms" else field] = value
    cut_row = secondary if secondary is not None else primary
    if cut_row is not None and cut_row["secondary_decision_threshold"] is not None:
        tuning["secondary_decision_threshold"] = cut_row["secondary_decision_threshold"]
    return tuning


def active_key(provider_id: int, lane: str | None = None,
               rotate: bool = True) -> str | None:
    """One key for a provider, honouring a lane's own key and rotating over the rest.

    A key labelled "<lane> lane" serves that lane: the label is the operator saying which
    credential belongs to which lane, and ORDER BY id LIMIT 1 ignored it, so a second stored
    key was never read and its deactivate button governed nothing. Every other key is an
    interchangeable account, and OpenRouter's free cap is per account -- always taking the
    lowest id spends one account's daily quota and leaves the others idle.

    Stalest-use-first, never-used-before-stamped, so a rotation survives a restart without
    in-process state that would leak between tests and workers.

    `rotate=False` answers "which credential would pay for this endpoint" and writes nothing:
    a tie-break walk lists its legs before it dials any of them, and stamping every account on
    the list would spend a rotation for calls that never happened.
    """
    row = None
    if lane:
        row = conn().execute(
            """SELECT id, key_cipher FROM api_keys
               WHERE provider_id = ? AND is_active = 1 AND label = ? ORDER BY id LIMIT 1""",
            (provider_id, f"{lane} lane"),
        ).fetchone()
    if row is None:
        row = conn().execute(
            """SELECT id, key_cipher FROM api_keys
               WHERE provider_id = ? AND is_active = 1
               ORDER BY last_used_at IS NOT NULL, last_used_at, id LIMIT 1""",
            (provider_id,),
        ).fetchone()
    if not row:
        return None
    if rotate:
        conn().execute(
            "UPDATE api_keys SET last_used_at = strftime('%Y-%m-%d %H:%M:%f','now') WHERE id = ?",
            (row["id"],),
        )
        # Committed here rather than left to the route: a probe returns without writing anything,
        # so an uncommitted stamp would roll back at teardown and every probe would pick the same key.
        conn().commit()
    try:
        return db.decrypt_key(row["key_cipher"])
    except ValueError:
        return None


def stored_key_for(lane: str, base_url: str | None, model_id: str | None,
                   user_id: int | None = None, rotate: bool = True) -> str | None:
    """The stored credential for a lane the caller named explicitly.

    A key belongs to a provider and a provider is its endpoint, so the host being called
    decides which credential may travel -- matching on the model id alone would hand an
    OpenRouter key to a request aimed at NVIDIA. A lane that names a host with no stored
    provider gets nothing, and the workflow falls back to its own environment key.

    The signed-in user's own key wins over the admin's shared one, exactly as it does on the
    deployed-lane path: "pick them auto if a verified key is there" promised on the Ranking tab
    has to hold on the path the pick actually takes. It did not -- this returned active_key()
    only, so a user shown "your key ready" was silently billed to the shared account, and with
    no shared key the run went out with no credential at all (test_auth, 2026-10-16).

    `rotate` is passed straight to active_key() and only ever means anything on that shared path:
    a caller's own key has no use stamp to spend. The tie-break walk reads it as False, because it
    names the credential that WOULD pay for each candidate leg and may never dial it.
    """
    want = host_of(base_url or "")
    if not want:
        row = model_by_id(model_id)
        want = host_of(row["provider_base_url"]) if row else ""
    if not want:
        return None
    # Before any host matching: a self-hosted key's address belongs to no provider row, so the
    # loop below cannot find it by host. The credential saved FOR this endpoint pays for it.
    if user_id is not None and base_url:
        own = db.user_key_for_endpoint(conn(), user_id, base_url)
        if own:
            return own
    for p in conn().execute("SELECT id, base_url FROM providers WHERE enabled = 1"):
        if host_of(p["base_url"]) != want:
            continue
        if user_id is not None:
            own = db.user_key_for_provider(conn(), user_id, p["id"])
            if own:
                return own
        return active_key(p["id"], lane, rotate=rotate)
    return None


def host_of(base_url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)", base_url or "")
    if not m:
        return ""
    netloc = m.group(1)
    if "@" in netloc:
        return ""
    host = netloc.rsplit(":", 1)[0] if ":" in netloc and not netloc.startswith("[") else netloc
    return host.strip("[]").lower()


def allowed_hosts(c=None) -> tuple[str, ...]:
    """The endpoint allowlist, in the form the in-process engine takes it.

    One reader for both engines: this list decides which lane `/detect` may name, and the workflow
    and `engine.normalize()` each re-check the URL they were handed against their own copy. If the
    two defaults drifted, a deploy would accept a lane at request time and the engine would reject
    it as `LANE_CONFIG_INVALID` -- so the default is the engine's constant, not a fourth copy.

    `c` lets a caller outside a request supply its own connection. The discovery worker is one: it
    has a `db.session()` of its own and no `g` to read, and a second copy of this parse is exactly
    the drift this function was written to prevent.
    """
    raw = db.get_setting(c or conn(), "allowed_llm_hosts", engine.DEFAULT_ALLOWED_HOSTS)
    return tuple(h.strip().lower() for h in (raw or engine.DEFAULT_ALLOWED_HOSTS).split(",") if h.strip())


def host_allowed(base_url: str) -> bool:
    return host_of(base_url) in set(allowed_hosts())


def owner_endpoint(raw: str) -> tuple[str, str]:
    """Canonicalise and vet an endpoint the *owner* of a self-hosted gateway named.

    Returns `("", code)` when the URL must be refused, `(canonical_url, "")` when it may be saved
    and dialled.

    The shape is judged by `engine.check_base_url` -- the same validator a lane is re-checked with
    at detect time -- so a URL accepted here cannot later be rejected as `LANE_CONFIG_INVALID`, and
    the stored string is the engine's own canonical form (lowercased host, no trailing slash,
    no credentials, API root rather than `/chat/completions`). Two rulers for one gate has bitten
    this app before; see `allowed_hosts()`.

    What that validator cannot do is decide *trust*: it matches the host against the deployment's
    allowlist, and an owner's own box is exactly the address the allowlist does not know. So the
    exchange rate is set here, in one visible line, the way the operator chose it: an https endpoint
    is believed anywhere, because that is what a gateway hosted on the web looks like, while a plain
    http one still has to be on the allowlist -- which already names loopback, so your own machine
    works and a stranger's LAN address does not. A dotted path segment is the one thing the engine's
    validator waves through (`.` is a legal path character) and this app will not store: a URL that
    walks out of its own API root is not a gateway address.
    """
    value = (raw or "").strip()
    host = host_of(value)
    scheme = value.split("://", 1)[0].lower()
    if not host or scheme not in ("http", "https"):
        return "", "ENDPOINT_INVALID"
    if ".." in value.split("://", 1)[1]:
        return "", "ENDPOINT_INVALID"
    checked = engine.check_base_url(value, allowed_hosts=(host,) + tuple(allowed_hosts()))
    if not checked.get("ok"):
        return "", "ENDPOINT_INVALID"
    if scheme == "http" and host not in set(allowed_hosts()):
        return "", "HOST_NOT_ALLOWED"
    return checked["value"], ""


def key_hosts(c, user_id: int | None) -> tuple[str, ...]:
    """Hosts the caller's own saved keys name, ready to join the engine's allowlist.

    Widening the list a *detection* is checked against is the other half of saving an endpoint:
    without it the in-process engine would reject the gateway its owner just verified, because the
    deployment's `allowed_llm_hosts` has never heard of it. Scoped to the caller's own rows, so one
    user's box cannot open that host for anybody else's lane.
    """
    if user_id is None:
        return ()
    out = []
    for url in db.user_key_endpoints(c, user_id):
        h = host_of(url)
        if h:
            out.append(h)
    return tuple(out)


def endpoint_for_lane(c, base_url: str, user_id: int | None) -> bool:
    """May this lane be dialled at `base_url` -- on the deployment's list, or at a host whose
    owner saved a key for it?"""
    return host_allowed(base_url) or host_of(base_url) in set(key_hosts(c, user_id))


def provider_name_for_base_url(base_url: str) -> str:
    """The provider *name* that owns an endpoint, or "" when no enabled provider does.

    Health and the using-pool are keyed by (provider name, model id) rather than by a models
    row id, because the whole point is to notice a model that has no tuned row yet -- and
    because the same model id can be served from two endpoints (OpenRouter's `:free` mirrors)
    whose fates are independent. An unknown host gets no name and therefore no health row:
    a name this app cannot resolve is a name it cannot key on, and inventing one from the
    hostname would let a caller's arbitrary base_url plant rows in a table the pool trusts.
    """
    want = host_of(base_url or "")
    if not want:
        return ""
    for p in conn().execute("SELECT name, base_url FROM providers WHERE enabled = 1"):
        if host_of(p["base_url"]) == want:
            return p["name"]
    return ""


def provider_base_url_by_name(name: str) -> str:
    """The endpoint of an enabled provider, by name. "" when there is none.

    The pool stores a provider *name* (portable across re-seeds, and the key model_health
    already uses); calling it needs the endpoint back. A disabled provider yields nothing,
    so an admin switching an endpoint off takes its pooled models out of circulation without
    anyone having to edit a user's ranking.
    """
    if not name:
        return ""
    row = conn().execute("SELECT base_url FROM providers WHERE name = ? AND enabled = 1",
                         (name,)).fetchone()
    return row["base_url"] if row else ""


def lane_config_invalid(lane: str):
    """The one shape a rejected lane endpoint is reported in, whichever path built it."""
    return jsonify({
        "classification": "DETECTION_UNAVAILABLE",
        "error": f"{lane} provider host is not in allowed_llm_hosts",
        "error_code": "LANE_CONFIG_INVALID", "review_required": False,
        "method": "LANE_CONFIG_INVALID",
    }), 400


def lane_secondary_off(cfg) -> bool:
    """True when a request explicitly turns the tie-break lane off.

    The UI contract is lane_config.secondary = {"enabled": false}. It means "do not
    buy a second call": the backend must not fill the lane's gateway or send its
    stored key, and the workflow gets the bare off-flag so its Parse Primary skips
    the branch. Anything that is not an explicit false leaves the lane on -- the
    workflow's own env default (SECONDARY_ENABLED) still has the last word.
    """
    if not isinstance(cfg, dict):
        return False
    v = cfg.get("enabled")
    return v is False or (isinstance(v, str) and v.strip().lower() in ("false", "0", "off"))


def _engine_listening(url: str, probe_s: float = 2.0) -> bool:
    """Can a bare TCP connection still be opened to the engine's host:port right now?

    Only consulted *after* a request failed, to answer what the exception cannot: `timed out` says
    nothing about which phase timed out. On this host a connect to an address where nothing answers
    hung for the whole client timeout instead of refusing (measured 2026-10-16: 127.0.0.1:1 came back
    as `<urlopen error timed out>`), and a server that accepts and then thinks for four minutes
    surfaces the same words. The cures are opposites — wait, or fix Settings → engine URL — so the
    diagnosis has to be earned, not guessed. One handshake, two seconds, no request sent, no budget.
    """
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not parts.hostname:
            return False
        with socket.create_connection((parts.hostname, port), timeout=probe_s):
            return True
    except (OSError, ValueError):        # unresolvable, refused, filtered, or a bogus port
        return False


def forward(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict, float]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # MILLISECONDS, like every other return of this function: the caller stores it straight into
        # detections.latency_ms. A 2026-10-16 history row read "120 ms" for a 120 s timeout because
        # this branch returned `time.time() - t0` while the success paths returned *1000.
        #
        # `error_code` is what separates "the engine was never reached" from "the model answered
        # badly": without it a History row and the result card look like a scored deferral, and the
        # operator cannot tell whether to wait for the gateway or fix Settings -> engine URL.
        #
        # Two of those diagnoses, not one (2026-10-16). This branch also catches a *read timeout*, and
        # a timeout is the opposite finding: the URL is right, the engine is alive, the lane was slower
        # than the ceiling. The ceiling is `timeout_ms + 30 s`, so a lane measured at 269 s
        # (z-ai/glm-5.3) or one the gateway 504s at ~302 s (gemma-4-31b-it) used to be reported as
        # "n8n unreachable, check the engine URL" — sending the one person allowed to edit settings to
        # the field that is not broken. Same 502, same DETECTION_UNAVAILABLE verdict-absent contract,
        # different words and a different knob named.
        timed_out = ("read operation timed out" in str(exc).lower()
                     or _engine_listening(url))
        if timed_out:
            return 502, {
                "error": f"n8n at {url} accepted the connection but did not answer within "
                         f"{timeout:.0f} s",
                "detail": str(exc),
                "error_code": "ENGINE_TIMEOUT", "method": "ENGINE_TIMEOUT",
                "classification": "DETECTION_UNAVAILABLE", "review_required": False,
                "engine_url": url, "timeout_s": round(timeout),
                "hint": "The engine was reached but its lane did not finish in time, so there is no "
                        "verdict to show. The ceiling is the lane's own timeout (the model row's "
                        "timeout_ms, plus 30 s of slack) — not Settings → engine URL, which is working. "
                        "A saturated free pool or a long-reasoning model looks exactly like this; retry "
                        "shortly, or ask the admin to point the lane at a model that answers faster.",
            }, (time.time() - t0) * 1000
        return 502, {
            "error": f"n8n unreachable at {url}", "detail": str(exc),
            "error_code": "ENGINE_UNREACHABLE", "method": "ENGINE_UNREACHABLE",
            "classification": "DETECTION_UNAVAILABLE", "review_required": False,
            "engine_url": url,
            "hint": "This deployment answers detections with the n8n workflow at that URL, and that "
                    "URL did not answer. Start n8n, or point Settings -> detection engine at a "
                    "reachable one. A single-user local install needs neither: with "
                    "DETECTION_ENGINE=python (which is what LOCAL_SINGLE_USER=1 defaults to) the "
                    "text is scored in this process.",
        }, (time.time() - t0) * 1000
    ms = (time.time() - t0) * 1000
    try:
        return status, json.loads(raw.decode("utf-8")), ms
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, {"error": "non-JSON response from n8n",
                        "detail": raw[:400].decode("utf-8", "replace")}, ms


def engine_url() -> str:
    """The webhook address of the workflow engine, as this deployment is configured."""
    return (setting("n8n_url", "http://localhost:5678").rstrip("/")
            + setting("webhook_path", "/webhook/ai-detect-ai"))


def run_detection(payload: dict, headers: dict, timeout: float,
                  extra_hosts: tuple[str, ...] = ()) -> tuple[int, dict, float]:
    """Ask whichever engine this deployment runs, through one contract.

    A `LOCAL_SINGLE_USER` install -- the packaged exe -- answers inside this process. It already
    holds the model rows, the decrypted keys and the text; making it start a second program on
    :5678 to score a paragraph was the bug this closes. A hosted container keeps forwarding to n8n,
    because every band in the ranking was measured through that workflow and that is what is
    deployed. `DETECTION_ENGINE=n8n|python` overrides the derived choice in either direction.

    Same `(status, body, latency_ms)` either way, so lane resolution, the failover walk,
    `record_detection` and `lane_source` never learn which engine answered -- and
    `tools/engine_parity.py` is what guarantees they would not have needed to.

    `extra_hosts` carries the endpoints the caller's own keys name. The in-process engine re-checks
    every lane against its own copy of the allowlist, so a gateway this app accepted at save time
    would come back `LANE_CONFIG_INVALID` here -- the pair-of-rulers trap `allowed_hosts()` is
    documented against. It reaches the n8n workflow the same way it always has: through the
    workflow's own `ALLOWED_LLM_HOSTS`, which this function cannot widen, so a hosted deploy has to
    list an owner's box in Settings for that lane to answer.
    """
    if engine.native_enabled():
        t0 = time.time()
        status, resp = engine.detect(payload, headers=headers, timeout=timeout,
                                     allowed_hosts=allowed_hosts() + tuple(extra_hosts))
        return status, resp, (time.time() - t0) * 1000
    return forward(engine_url(), payload, headers, timeout)


def _lane_view(lane: dict | None) -> dict:
    """Allowlist, not a dump: only these fields of a lane's config_used are stored. The workflow's
    `configView` already refuses to echo a key value (it reports `key_source`), so this is defence
    against a future field landing there, not a fix for something that leaks today."""
    if not isinstance(lane, dict):
        return {}
    return {k: lane[k] for k in ("model", "base_url", "base_url_source", "model_source",
                                 "key_source") if lane.get(k) is not None}


# ---- failover: when a second model may be tried, and how far ------------------

# One switch, plus one more if the second also fails and time remains. Bounded because each
# attempt is a full detection (up to 270 s on the shipped lane ceiling) and the transport
# above this gives up at 330 s: an unbounded walk would be a request that can never answer.
MAX_FAILOVER_ATTEMPTS = 2

# Seconds that must remain on the request's own budget before another model is tried. Below
# this the retry cannot finish, and starting it converts a clean 502 into a gateway timeout
# with no verdict at all -- strictly worse for the user than failing immediately.
FAILOVER_RESERVE_S = 20.0

# The whole time a single /detect may spend across EVERY lane call it makes. This belongs to the
# request, not to whichever model happens to be at the top of the pool, and the distinction cost
# a user their verdict on 2026-10-17: a pool of [nemotron-3-ultra (row timeout 90 s),
# z-ai/glm-5.3-flash (240 s)] refused in 0.7 s, failover correctly decided to switch -- and handed
# the replacement 109 s, because the deadline had been computed from the *first* model's ceiling.
# glm needed longer than that on a congested pool, so 120 s bought two non-answers.
#
# It is set from the tier above, the same way the rest of the chain is (see verify_deploy_recipe.py:
# lane ceiling -> backend wait -> gunicorn -> nginx). One lane call at the shipped 240 s ceiling
# already needs 240 + 30 = 270 s, so a two-attempt request has to fit under that 270 s and still
# leave time to store the row and write the response: 250 s, 20 s clear of gunicorn's 285 s.
REQUEST_BUDGET_S = 250.0

# Error codes that say the ENGINE or the REQUEST failed, not the model. Retrying another
# model against a dead engine, a bad text, or a disallowed host spends budget to reproduce
# the same answer.
#
# PROVIDER_KEY_MISSING / BYOK_DISABLED are the in-process engine's own rejections (#172): both
# refuse the request before a lane is called, so no candidate could answer differently, and the
# walk would stamp `failover` attempts onto an answer that says "add a key on the Configuration
# page". The first is the packaged first boot with zero stored keys.
NO_FAILOVER_CODES = frozenset({
    "ENGINE_UNREACHABLE", "ENGINE_TIMEOUT", "INPUT_REJECTED", "LANE_CONFIG_INVALID",
    "NO_PRIMARY_MODEL", "PAYLOAD_TOO_LARGE", "PROVIDER_KEY_MISSING", "BYOK_DISABLED",
})


def no_verdict(status: int, resp) -> bool:
    """True when this answer carries no classification a user could act on.

    Only that question, and no other. Whether a *different model* could fix it belongs to
    `engine_level_failure`: the two used to be one check, and the `status >= 500` branch
    answered first, so the 502s `forward()` produces for a dead or too-slow engine were treated
    as model failures and bought up to two more full detections to reproduce the same 502 --
    minutes on Windows, where a dead local port hangs instead of refusing.

    Still deliberately narrow in the other direction: a verdict rescued by the tie-break lane is
    a real answer (`SINGLE_MODEL_SECONDARY`) and is kept -- the failed primary is marked
    unhealthy so the pool reorders, but the user is not charged a second full call to replace
    an answer with maybe-an-answer.
    """
    if status >= 500:
        return True
    if not isinstance(resp, dict):
        return True
    return (resp.get("classification") == "DETECTION_UNAVAILABLE"
            or resp.get("method") == "ALL_MODELS_FAILED"
            or resp.get("classification") is None)


def engine_level_failure(resp) -> bool:
    """True when the failure belongs to the engine or to the request, not to the model.

    Retrying another model against a dead engine, a blown ceiling, a rejected text or a
    disallowed host spends the caller's budget and their remaining request time to reproduce
    the same answer -- and then stamps `lane_source: failover` and an `auto_switch` onto a body
    that never carried a verdict, which is an unattributable number of exactly the kind this
    app has spent rounds removing.
    """
    return isinstance(resp, dict) and resp.get("error_code") in NO_FAILOVER_CODES


# A credential test is timed like the call it stands in for.
#
# `/my/keys/<id>/verify` proves a user's key by asking one model for one word, and it gave up on
# a bare 45 s. On 2026-10-17 that was a false negative on exactly the model this app promotes
# because it is thorough: z-ai/glm-5.3-flash did not answer the probe inside 60 s on a congested
# NVIDIA pool. The user was told a working key was broken -- and because a verified key is the
# ONLY thing that queues auto-discovery, the whole "enter it, test it, save it, get your models
# ranked" flow stopped at step one.
#
# So the ceiling comes from the row of the model being probed: a probe may take as long as a real
# detection against that model is allowed to. It is a ceiling, not a wait -- a healthy gateway
# answers this prompt in a couple of seconds and returns exactly as fast as it always did.
PROBE_FLOOR_S = 45.0


def probe_timeout_s(model_id: str, requested=None, provider_id: int | None = None) -> float:
    """Seconds a one-word connectivity probe may wait for `model_id` to answer."""
    if requested is not None:
        try:
            return max(1.0, float(requested))
        except (TypeError, ValueError):
            return PROBE_FLOOR_S
    sql, args = "SELECT timeout_ms FROM models WHERE model_id = ?", [model_id]
    if provider_id is not None:
        # The same id can live on two endpoints (OpenRouter's `:free` mirrors), and they do not
        # share a ceiling. Keying on the provider keeps a slow mirror from inheriting a fast
        # row's patience, or losing it.
        sql += " AND provider_id = ?"
        args.append(provider_id)
    row = conn().execute(sql + " LIMIT 1", args).fetchone()
    if row is None or not row["timeout_ms"]:
        return PROBE_FLOOR_S
    return min(max(float(row["timeout_ms"]) / 1000.0, PROBE_FLOOR_S), REQUEST_BUDGET_S)


def leg_failure(resp) -> tuple[str | None, str | None, str | None]:
    """(model, error_code, error_detail) for the primary leg that did not answer.

    Read from the workflow's own per-lane report rather than from the request's lane_config,
    because the workflow is what actually called the model and is what knows how it failed.
    """
    if not isinstance(resp, dict):
        return None, None, None
    for leg in ((resp.get("evidence") or {}).get("models")) or []:
        if isinstance(leg, dict) and leg.get("lane") == "primary" and not leg.get("ok"):
            return (str(leg.get("model") or "") or None, leg.get("error_code"),
                    leg.get("error_detail"))
    return None, resp.get("error_code"), resp.get("error")


def _pid_by_name(name: str) -> int | None:
    row = conn().execute("SELECT id FROM providers WHERE name = ? AND enabled = 1", (name,)).fetchone()
    return int(row["id"]) if row else None


def record_detection(body: dict, status: int, latency_ms: float, primary_model: str | None,
                     secondary_model: str | None, text_length: int | None = None,
                     secondary_off: bool = False, text: str | None = None) -> int:
    cs = body.get("component_scores") or {}
    internal = body.get("_internal") or {}
    versions = body.get("versions") or {}
    cfg = body.get("config_used") or {}
    # The caller may override one lane only, and the workflow's env supplies the other, so the ids
    # the request knows about can be incomplete: row 22 recorded a TIE_BREAK_AI with
    # `secondary_model: NULL` because only the primary was ever named by the client. Take the
    # missing id from what actually ran.
    primary_model = primary_model or (cfg.get("primary") or {}).get("model")
    # ...but only for a lane that could have run. The workflow echoes its env default for the
    # secondary even when the request switched it off, and filling from that echo recorded
    # "diffusiongemma + diffusiongemma" on a single-lane detection (packaged exe, 2026-10-16
    # 17:25 UTC): a History row claiming a tie-break that never happened.
    if not secondary_off:
        secondary_model = secondary_model or (cfg.get("secondary") or {}).get("model")

    payload = {k: v for k, v in body.items() if k != "config_used"}
    # ...and keep the provenance rather than throwing it away. Without it a stored verdict cannot
    # be re-read against the cuts that produced it, which is the whole point of a DB-driven config.
    provenance = {"primary": _lane_view(cfg.get("primary")), "secondary": _lane_view(cfg.get("secondary")),
                  "thresholds": cfg.get("thresholds"), "sampling": cfg.get("sampling"),
                  "tuning_applied": cfg.get("tuning_applied")}
    payload["config_used"] = {k: v for k, v in provenance.items() if v}
    # Stamp the signed-in owner so a user's history is theirs alone; admins see everyone.
    # Under the test client / unauthenticated local runs there is no user, so this is NULL.
    user = auth.current_user()
    user_id = user["id"] if user else None
    cur = conn().execute(
        """INSERT INTO detections
           (document_id, text_fingerprint, text_length, text, classification, ai_probability,
            confidence, method, review_required, deciding_lane, primary_model, secondary_model,
            latency_ms, http_status, error_code, warnings, payload_json, user_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (body.get("document_id"), internal.get("text_fingerprint"),
         # A failed forward has no workflow `internal` block, so without the caller's length the row
         # renders "? chars" and the user cannot tell a 429-char paragraph from a 4-char word.
         internal.get("text_length") if internal.get("text_length") is not None else text_length,
         text,
         body.get("classification"), body.get("ai_probability"), body.get("confidence"),
         body.get("method"), 1 if body.get("review_required") else 0, cs.get("deciding_lane"),
         primary_model, secondary_model, int(latency_ms), status, body.get("error_code"),
         json.dumps(body.get("warnings") or []), json.dumps(payload), user_id),
    )
    conn().commit()
    # Fold this attempt into model_health. It reads the workflow's own per-lane report, so a
    # model that has started 404ing or timing out is measured from traffic the app was already
    # making -- no poll, no extra budget, and no second opinion about what "did not answer"
    # means. Committed separately: a health write is a side-effect of the detection, and a
    # failure to record it must not roll back the history row the user is about to be shown.
    try:
        pool.note_detection(conn(), body, provider_name_for_base_url)
        conn().commit()
    except sqlite3.Error:
        conn().rollback()
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- routes

def _resolve_provider(provider_id=None, base_url=None):
    """The enabled provider row a BYOK key may be attached to, by id or by endpoint host.

    A user brings a key for an endpoint the app already supports -- never a new one. Matching
    on the host (not just the id) lets the UI post {base_url} from the /config provider list.
    """
    if provider_id is not None:
        return conn().execute("SELECT * FROM providers WHERE id = ? AND enabled = 1",
                              (int(provider_id),)).fetchone()
    want = host_of(base_url or "")
    if not want:
        return None
    for p in conn().execute("SELECT * FROM providers WHERE enabled = 1"):
        if host_of(p["base_url"]) == want:
            return p
    return None


def _probe_with_key(prov, model_id: str, key: str, timeout_s: float) -> dict:
    """One chat/completions round-trip to prove a credential works. Mirrors the admin probe
    but takes the key directly, so a user can verify their own before it is marked 'working'.
    Never logs or returns the key."""
    url = prov["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": model_id,
               "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
               "max_tokens": 8, "temperature": 0.0}
    t0 = time.time()
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header(prov["auth_header"] or "Authorization", f"{prov['auth_prefix'] or ''}{key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error_code": "UNREACHABLE", "detail": str(exc),
                "latency_ms": int((time.time() - t0) * 1000)}
    return {"ok": status == 200, "status": status,
            "latency_ms": int((time.time() - t0) * 1000)}


# ---- ranking: measured models, best first ------------------------------

# Half the 48-text corpus. Under this a run has been sampled, not measured: it can show a
# score, but it must not lead the table the admin points users at -- one lucky text beats
# 47 honest ones under a pure fewest-wrong sort.
MEASURED_MIN_TEXTS = 24


def measured_ranking(user) -> list[dict]:
    """Every measured model, scored at its OWN saved band and sorted best first.

    One sort, two readers: `/ranking` renders this list and `/my/pool/auto-rank` orders the
    caller's pool by it, so the page and the pool cannot disagree about which model judges
    best. An order written twice is how #142 happened -- the front-end baked its own lane,
    and the deployed default the page named never answered anything.

    A band is per model, so each run is re-scored at the row the engine would actually use
    for it: applying another model's cut would misreport it. Sorted by fewest wrong, then
    most decided, then accuracy, then coverage, so a model that never errs outranks one that
    defers less but guesses wrong. Sample size is a sorting tier, not a caption: a run over
    at least MEASURED_MIN_TEXTS texts ranks among the full captures, and shorter runs keep
    their scores but follow, flagged `provisional`. A run with no clean row measured nothing
    and is left out rather than shown as a model with no score.

    `user` may be None -- a signed-out visitor sees the measurement with no key lit.
    """

    bands = {}
    for m in conn().execute(
            """SELECT model_id, provider_id, lane, ai_prob_threshold, human_prob_threshold,
                      secondary_decision_threshold FROM models"""):
        bands.setdefault(m["model_id"], m)
    runs = conn().execute("SELECT * FROM benchmark_runs ORDER BY id").fetchall()
    best: dict[str, dict] = {}
    for run in runs:
        rows = [dict(r) for r in conn().execute(
            """SELECT label, ok, error_code, ai_probability, confidence
               FROM benchmark_results WHERE run_id = ?""", (run["id"],)).fetchall()]
        if not rows:
            continue
        mrow = bands.get(run["model"])
        ai_cut = float(mrow["ai_prob_threshold"]) if mrow and mrow["ai_prob_threshold"] is not None else 0.69
        hu_cut = float(mrow["human_prob_threshold"]) if mrow and mrow["human_prob_threshold"] is not None else 0.40
        gate = float(mrow["secondary_decision_threshold"]) if mrow and mrow["secondary_decision_threshold"] is not None else 0.0
        clean = [r for r in rows if r.get("ok") and r.get("ai_probability") is not None and r.get("label")]
        correct = wrong = review = 0
        for r in clean:
            conf = r.get("confidence") if isinstance(r.get("confidence"), (int, float)) else 1.0
            sure = conf >= gate
            p = r["ai_probability"]
            verdict = "AI" if (sure and p >= ai_cut) else "HUMAN" if (sure and p <= hu_cut) else None
            if verdict is None:
                review += 1
            elif verdict == r["label"]:
                correct += 1
            else:
                wrong += 1
        decided = correct + wrong
        entry = {
            "model": run["model"], "tag": run["tag"], "corpus": run["corpus"],
            "provider": run["provider"],
            "n_rows": len(rows), "n_clean": len(clean),
            "correct": correct, "wrong": wrong, "review": review, "decided": decided,
            "accuracy": round(correct / decided, 4) if decided else None,
            "coverage": round(decided / len(clean), 4) if clean else None,
            "band": {"ai": ai_cut, "human": hu_cut, "gate": gate},
            "lane": (mrow["lane"] if mrow else None),
        }
        prev = best.get(run["model"])
        # Keep the run with the most clean rows for this model (its fullest measurement).
        if prev is None or entry["n_clean"] > prev["n_clean"]:
            best[run["model"]] = entry
    # Attach endpoint + the caller's verified-key availability.
    out = []
    for entry in best.values():
        if not entry["n_clean"]:
            continue  # every row errored or went unparsed: nothing was measured
        entry["provisional"] = entry["n_clean"] < MEASURED_MIN_TEXTS
        mrow = bands.get(entry["model"])
        prov = None
        if mrow is not None:
            prov = conn().execute("SELECT id, name, base_url, free_tier FROM providers WHERE id = ?",
                                  (mrow["provider_id"],)).fetchone()
        if prov is not None:
            entry["provider_id"] = prov["id"]
            entry["provider_name"] = prov["name"]
            entry["base_url"] = prov["base_url"]
            entry["free_tier"] = bool(prov["free_tier"])
            entry["host"] = host_of(prov["base_url"])
            entry["verified_key"] = bool(user and db.user_key_for_provider(conn(), user["id"], prov["id"]))
            entry["admin_key"] = bool(conn().execute(
                "SELECT 1 FROM api_keys WHERE provider_id = ? AND is_active = 1 LIMIT 1",
                (prov["id"],)).fetchone())
        else:
            entry["provider_id"] = None
            entry["verified_key"] = False
            entry["admin_key"] = False
        out.append(entry)
    out.sort(key=lambda e: (e["provisional"], e["wrong"], -(e["decided"] or 0),
                            -(e["accuracy"] or 0), -(e["coverage"] or 0)))
    for i, e in enumerate(out):
        e["rank"] = i + 1
    # Health and pool membership, so the page can show why a model that measured well is
    # being served last: the ranking is a measurement, the health column is availability,
    # and a model can be the best judge on the corpus and still have stopped answering.
    health = {r["model_id"]: r["state"] for r in conn().execute(
        "SELECT model_id, state FROM model_health")}
    pool_rows = pool.pool_view(conn(), user["id"]) if user else []
    in_pool = {(r["provider"], r["model"]): r for r in pool_rows}
    for e in out:
        key = (e.get("provider_name") or e.get("provider") or "", e["model"])
        hit = in_pool.get(key)
        e["in_pool"] = hit is not None
        e["pool_position"] = hit["position"] if hit else None
        e["your_rank"] = hit["your_rank"] if hit else None
        e["health"] = (hit["health"] if hit else health.get(e["model"])) or "unknown"
        e["consecutive_failures"] = hit["consecutive_failures"] if hit else 0
    return out


def register_routes(app: Flask) -> None:

    @app.route("/healthz", methods=["GET"])
    def healthz():
        """Liveness, and the only authenticated-surface route that needs no session.

        `auth.PUBLIC_PATHS` has listed `/healthz` since the auth guard was written, but no route
        ever answered it -- so `GET /healthz` was a 404, and `backend/Dockerfile`'s HEALTHCHECK
        pointed instead at `/gateway-health`, which the guard protects. The container therefore
        never turned healthy and `docker compose up -d` died on
        "dependency failed to start: container ...-backend-1 is unhealthy": the web service never
        started, on every fresh deploy. Found by the first real daemon run (#106, 2026-10-17);
        `docker compose config` could not see it because it only reads YAML.

        Deliberately carries no content. `/gateway-health` answers the DB path, row counts, the
        engine URL and the seeded defaults, which is exactly what a hosted deploy must not hand an
        anonymous caller -- so the probe gets a liveness flag and the operator keeps the rich
        status behind a login.
        """
        return jsonify({"ok": True}), 200

    @app.route("/gateway-health", methods=["GET"])
    def gateway_health():
        primary, secondary = default_models()
        return jsonify({
            "ok": True,
            "backend": "flask",
            **purger.status(),
            "db_path": str(db.DB_PATH),
            "db_counts": db.counts(conn()),
            # Which engine answers /detect for this process, before anything else: an operator
            # reading a red `n8n.reachable` below needs to know whether that engine is even on the
            # request path, and a benchmark run has to record the implementation that produced it.
            "detection_engine": engine.ENGINE_NAME if engine.native_enabled() else "n8n",
            "n8n": n8n_status(),
            "defaults": {
                "primary_model": primary["model_id"] if primary else None,
                "secondary_model": secondary["model_id"] if secondary else None,
                "primary_provider": primary["provider_name"] if primary else None,
                "secondary_provider": secondary["provider_name"] if secondary else None,
            },
            "thresholds": {
                "ai_probability": setting("ai_prob_threshold") or (primary["ai_prob_threshold"] if primary else None),
                "human_probability": setting("human_prob_threshold") or (primary["human_prob_threshold"] if primary else None),
                "secondary_decision": setting("secondary_decision_threshold") or (primary["secondary_decision_threshold"] if primary else None),
            },
        })

    @app.route("/extract", methods=["POST"])
    def extract_document():
        """Multipart file -> plain text for the detection box. Read-only: nothing is
        stored, the text goes back to the browser and follows the same /detect path
        as pasted text, with the same limits."""
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({
                "ok": False, "error_code": "NO_FILE",
                "error": "Attach the document in the multipart field 'file'.",
            }), 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in extract.SUPPORTED:
            return jsonify({
                "ok": False, "error_code": "UNSUPPORTED_TYPE", "ext": ext or None,
                "allowed": sorted(extract.SUPPORTED),
                "error": f"Unsupported file type '{ext or '(no extension)'}'. "
                         "Allowed: .txt, .md, .docx, .pdf.",
            }), 415
        data = f.read()
        limit = extract.PER_TYPE_BYTES.get(ext, 0)
        if not data:
            return jsonify({"ok": False, "error_code": "EMPTY_FILE",
                            "error": "That file is empty."}), 400
        if len(data) > limit:
            return jsonify({
                "ok": False, "error_code": "FILE_TOO_LARGE",
                "error": f"That file is {len(data):,} bytes; the limit for {ext} is {limit:,} bytes.",
            }), 413
        try:
            text = extract.extract_text(ext, data)
        except extract.ExtractError as e:
            return jsonify({"ok": False, "error_code": "EXTRACT_FAILED", "error": str(e)}), 422
        except Exception:
            # a parser choking on a malformed file is the user's problem to hear about,
            # but the traceback is ours to keep out of the response
            return jsonify({"ok": False, "error_code": "EXTRACT_FAILED",
                            "error": "Could not read that file."}), 422
        if len(text) < 10:
            return jsonify({
                "ok": False, "error_code": "EXTRACT_EMPTY",
                "error": "No readable text found in that document. "
                         "Scanned or image-only PDFs contain no text to extract.",
            }), 422
        truncated = len(text) > extract.MAX_TEXT_CHARS
        text = text[:extract.MAX_TEXT_CHARS]
        return jsonify({
            "ok": True, "text": text, "chars": len(text), "truncated": truncated,
            "kind": ext, "filename": f.filename[:200],
        })

    @app.route("/detect", methods=["POST"])
    def detect():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="body must be a JSON object"), 400
        text = body.get("text")
        if not isinstance(text, str) or len(text.strip()) < 10:
            return jsonify({
                "classification": "DETECTION_UNAVAILABLE",
                "error": "Field 'text' is required and must be a non-empty string (>= 10 chars).",
                "error_code": "INPUT_REJECTED", "review_required": False, "method": "INPUT_REJECTED",
            }), 400
        # The in-process engine already refuses past its own ceiling, and a request that only
        # reaches the workflow never gets that far: it would post a multi-megabyte body through n8n
        # and pay for a lane that cannot read it. `engine.max_text_length()` is the same number the
        # engine reads, including its env override, so the two deploys cannot disagree about what
        # one detection is allowed to be. Measured the engine's way, because an emoji in the text
        # is two UTF-16 units there and one Python character here.
        ceiling = engine.max_text_length()
        if engine.js_len(text) > ceiling:
            return jsonify({
                "classification": "DETECTION_UNAVAILABLE",
                "error": f"Field 'text' is longer than {ceiling} characters.",
                "error_code": "TEXT_TOO_LONG", "review_required": False, "method": "TEXT_TOO_LONG",
            }), 413

        # A caller may still override a lane explicitly (that is the UI contract).
        # Otherwise the lanes come from the DB: provider endpoint + model + tuned params,
        # and the key is fetched from the encrypted store, never from the browser.
        lane_config = body.get("lane_config") or {}
        caller_tuning = body.get("tuning")
        primary, secondary = default_models()
        primary_model = secondary_model = None
        secondary_off = False
        tuning: dict = {}
        headers: dict[str, str] = {}
        user = auth.current_user()
        for k in ("x-primary-api-key", "x-secondary-api-key"):
            v = request.headers.get(k)
            if v and KEY_CHARSET.match(v) and len(v) <= 512:
                headers[k] = v

        def byok(provider_id: int) -> str | None:
            """The caller's own key for this provider if they stored one, else None. A user
            brings a credential, never a calibration: the model, band and sampling still come
            from the admin's row below."""
            if user is None:
                return None
            return db.user_key_for_provider(conn(), user["id"], provider_id)

        # ---- the caller's using-pool -------------------------------------------------
        #
        # Precedence: an explicit lane_config in the request > the signed-in user's ordered
        # pool > the admin's deployed default lane. The pool is the user's own ranking of
        # models they can pay for, so it outranks a default chosen for everyone -- but it
        # never outranks what this request asked for, because "use this model" on the Ranking
        # tab is an explicit choice for one detection.
        #
        # The order used here is pool.effective_order(), not the stored one: a model marked
        # `down` by three consecutive non-answers sinks to the back for this request without
        # anyone editing the user's ranking, and comes back when it answers again. That rule
        # lives in pool_primary_lane(), which /ranking reads for the same answer it reads here,
        # so the page cannot promise an order the request path does not walk.
        pool_used = False
        if user is not None and not lane_config:
            syn = pool_primary_lane(conn(), user["id"])
            if syn:
                # Synthesised as a plain lane_config and then handled by the same branch a
                # caller-named primary takes, so the tie-break policy, the base_url fill, the
                # BYOK-first key pick and the tuning resolution stay one code path instead of
                # growing a parallel one that can drift from it.
                lane_config = {"primary": {"base_url": syn["base_url"], "model": syn["model"]}}
                pool_used = True

        # ---- the tie-break lane, in the caller's own order -----------------------------
        #
        # Two rules decide this lane, and both came from the owner: it starts at the pool entry
        # ranked just after the model that answered, and on a non-answer it keeps going down for
        # every model ranked below -- the same logique the primary's failover walk uses, but
        # inside the one request, because re-posting /detect would bill the primary again for a
        # leg the user already paid for.
        #
        # How far it goes is theirs; how far it CAN go is the clock. The engine refuses to start a
        # leg that cannot finish inside the request's budget, so the list is offered whole and the
        # legs that no longer fit are reported as skipped rather than dialled into a gateway
        # timeout that carries no verdict at all.
        #
        # Walking is the in-process engine's own ability: the shipped workflow's validator
        # rejects an unknown `lane_config` field, so `candidates` travels only when
        # `engine.native_enabled()`. On a hosted deploy the lane still STARTS at the next pooled
        # model -- that is a plain `base_url` + `model` -- it just cannot walk.
        def tie_break_entry(model_id: str, provider_name: str) -> dict | None:
            """A lane the tie-break may dial, or None when this app may not dial it.

            A candidate the request names and the engine then refuses is a `LANE_CONFIG_INVALID`
            on the whole body, so an endpoint outside the allowlist is dropped here instead of
            sent: one pool entry on a gateway this deploy has not opened must not cost the user
            the other legs, or the detection itself.
            """
            uid = user["id"] if user else None
            row = model_by_id(model_id)
            base = (lane_base_url(conn(), uid, provider_name)
                    or provider_base_url_by_name(provider_name)
                    or (row["provider_base_url"] if row else ""))
            if not base or not endpoint_for_lane(conn(), base, uid):
                return None
            return {"base_url": base, "model": model_id}

        def tie_break_for(primary_id: str | None) -> dict | None:
            """Resolve the second lane for a primary that answered as `primary_id`.

            The head is the pool entry below it; everything under the head walks after it. A pool
            with nothing below falls back to the admin's enabled `models` row, because the walk is
            a pool property and a one-entry pool has none to spend. A head naming the primary
            itself is refused here rather than sent -- one model cannot corroborate itself, and
            doing it here is what keeps a failover candidate from re-creating that collision with
            the lane that used to sit above it.
            """
            below = (pool_tie_break_order(conn(), user["id"], primary_id)
                     if (pool_used and user is not None) else [])
            head: dict | None = None
            rest: list[dict] = []
            for r in below:
                entry = tie_break_entry(r["model_id"], r["provider"])
                if entry is None:
                    continue
                if head is None:
                    head = entry
                else:
                    rest.append(entry)
            if head is None and secondary is not None:
                head = tie_break_entry(secondary["model_id"], secondary["provider_name"])
            if head is None or head["model"].strip() == (primary_id or "").strip():
                return None
            # `engine.normalize` accepts only base_url/model/enabled/candidates on a lane, so the
            # head's own cut travels in `tuning` (whose tie-break value `tuning_block` reads from
            # the secondary row) while each candidate carries its cut beside it: a substituted leg
            # is read on the ruler of the model that actually answered.
            lane = {"base_url": head["base_url"], "model": head["model"]}
            if rest and engine.native_enabled():
                for e in rest:
                    # `rotate=False`: these are legs that may never be dialled, and stamping every
                    # account on the offer would spend a rotation for calls that did not happen.
                    key = stored_key_for("secondary", e["base_url"], e["model"],
                                         user_id=user["id"] if user else None, rotate=False)
                    if key:
                        e["api_key"] = key
                lane["candidates"] = rest
            return lane

        if not lane_config:
            if primary is None:
                return jsonify({
                    "classification": "DETECTION_UNAVAILABLE",
                    "error": "No primary model is configured in the database.",
                    "error_code": "NO_PRIMARY_MODEL", "review_required": False,
                    "method": "NO_PRIMARY_MODEL",
                    "hint": "POST /models with lane='primary', or run python -m backend.seed",
                }), 409
            for row, lane in ((primary, "primary"), (secondary, "secondary")):
                if row is None:
                    continue
                # The provider row is the deployment's answer to "which endpoint"; a caller's own
                # key is the answer for their box, and only a self-hosted provider can have one.
                uid = user["id"] if user else None
                base = (lane_base_url(conn(), uid, row["provider_name"])
                        or row["provider_base_url"])
                if not endpoint_for_lane(conn(), base, uid):
                    return lane_config_invalid(lane)
                lane_config[lane] = {"base_url": base, "model": row["model_id"]}
                header = f"x-{lane}-api-key"
                if header not in headers:
                    # BYOK first: a user's own verified key pays for the call; only fall back
                    # to the admin's shared credential when the user has none for this endpoint.
                    key = byok(row["provider_id"]) or active_key(row["provider_id"], lane)
                    if key:
                        headers[header] = key
            primary_model = primary["model_id"]
            secondary_model = secondary["model_id"] if secondary else None
            if secondary is None:
                # No enabled tie-break row means the lane is OFF -- and that has to be said out
                # loud. An absent `lane_config.secondary` is not "off", it is "the workflow
                # decides", and the workflow's own env default names a second model, so a silently
                # omitted lane still bought a second call and still let a model nobody had enabled
                # overturn the primary. The shipped default switches the lane off (round 25c: 3
                # texts asked, 3 answered wrong), so the off-flag travels with the request.
                lane_config["secondary"] = {"enabled": False}
                secondary_off = True
            tuning = tuning_block(primary, secondary)
        else:
            primary_model = (lane_config.get("primary") or {}).get("model")
            secondary_model = (lane_config.get("secondary") or {}).get("model")
            secondary_off = lane_secondary_off(lane_config.get("secondary"))
            if secondary is None and not secondary_off:
                # The row owns the lane, not the request body. A caller naming only a primary
                # (exactly what the Ranking tab's "use this model" sends) leaves
                # `lane_config.secondary` absent, and absent means "the workflow decides": the
                # engine's env default names a second model, so a tie-break the admin switched
                # off would answer anyway and could overturn the primary. Round 25c measured that
                # authority as 3 questions and 3 wrong answers on the deployed lane, which is why
                # it ships off. Turning it back on is an admin action on the row (PATCH /models),
                # never a property of one request.
                lane_config["secondary"] = {"enabled": False}
                secondary_off = True
                secondary_model = None
            if secondary_off:
                # An off lane carries no model identity into the log or the tuning:
                # its row's parameters must not steer a call that never happens.
                secondary_model = None
            # A lane that names a stored model has to be sent to that model's gateway.
            # Filling only the key and the tuning here left base_url absent, so the
            # workflow applied its env default: naming inclusionai/ling-3.1-flash (an
            # OpenRouter model) posted it to integrate.api.nvidia.com with an OpenRouter
            # token, the gateway answered the plain-text "404 page not found", and the
            # tie-break quietly decided from the other lane. A model with no row keeps
            # the env default -- that is an unknown model, not a misconfigured one.
            for lane in ("primary", "secondary"):
                if lane == "secondary" and secondary_off:
                    continue
                cfg = lane_config.get(lane)
                if not isinstance(cfg, dict) or cfg.get("base_url"):
                    continue
                row = model_by_id(cfg.get("model"))
                if row is None:
                    continue
                base = (lane_base_url(conn(), user["id"] if user else None,
                                      row["provider_name"]) or row["provider_base_url"])
                if not endpoint_for_lane(conn(), base, user["id"] if user else None):
                    return lane_config_invalid(lane)
                cfg["base_url"] = base
            if not secondary_off and "secondary" not in lane_config:
                # The row owns the lane, not the request body -- and now the engine is told so.
                # Naming only a primary does not opt the tie-break out: `lane_config.secondary`
                # absent means "whatever the engine's env default says", and the app used to write
                # the deployed row's id into its own bookkeeping while leaving the lane absent. The
                # leg that answered was then some other model entirely: the packaged first boot hit
                # both at once on 2026-10-16, because the env default happened to be the model the
                # Ranking tab invites you to pick as the scorer, and a History row claimed a
                # tie-break by a model that had never been dialled.
                resolved = tie_break_for(primary_model)
                if resolved is not None:
                    lane_config["secondary"] = resolved
                    secondary_model = resolved["model"]
                elif secondary is not None:
                    # Nothing this request may dial: no pool entry below the primary, and the
                    # deployed row's endpoint is not one this app is allowed to name. The lane
                    # stays absent, so the engine's default answers -- and the row that was
                    # supposed to own it is at least recorded, rather than logged as no lane.
                    secondary_model = secondary["model_id"]
            if (not secondary_off and primary_model and secondary_model
                    and primary_model.strip() == secondary_model.strip()):
                # A model cannot corroborate itself: same input, same stored tuning, so the
                # second call is a coin-flip copy of the first and any "agreement" it reports is
                # an artefact. Reached from the UI by picking the deployed tie-break model as the
                # scorer on the Ranking tab, which leaves the panel's secondary lane where it was.
                secondary_off = True
                secondary_model = None
            # A browser never sees the stored key, and an explicit lane still has to be
            # paid for with the credential the operator saved for that endpoint.
            for lane in ("primary", "secondary"):
                if lane == "secondary" and secondary_off:
                    continue
                header = f"x-{lane}-api-key"
                if header in headers:
                    continue
                cfg = lane_config.get(lane) or {}
                key = stored_key_for(lane, cfg.get("base_url"), cfg.get("model"),
                                     user_id=user["id"] if user else None)
                if key:
                    headers[header] = key
            if secondary_off:
                # Strip the UI's leftover base_url/model from the off lane: the
                # workflow must see the switch, not a lane it could still call.
                lane_config["secondary"] = {"enabled": False}
            # An explicit tuning block still wins (that is how a tuning experiment
            # gets run), but absent one the stored row for the model actually being
            # called supplies the parameters -- the lane override changed which
            # model runs, not whose calibration applies to it. A model with no row
            # gets the workflow's baked defaults rather than another model's cuts.
            tuning = caller_tuning if isinstance(caller_tuning, dict) else tuning_block(
                model_by_id(primary_model), model_by_id(secondary_model))

        def build_payload(lane_cfg: dict, tune: dict) -> dict:
            p = {k: v for k, v in body.items() if k not in ("lane_config", "tuning")}
            p["lane_config"] = lane_cfg
            if tune:
                p["tuning"] = tune
            p.setdefault("document_id", body.get("document_id"))
            return p

        timeout = float(tuning.get("request_timeout_ms")
                        or (primary["timeout_ms"] if primary else 90000)) / 1000.0 + 30.0
        # The first attempt keeps its OWN lane ceiling; the request keeps the budget. Deriving the
        # deadline from `timeout` made whichever row sits on top of the pool size every later
        # attempt, which is how a 0.7 s refusal left a 240 s replacement with 109 s. max(), not
        # min(): a single lane at the shipped 240 s ceiling is what the 270 s -> 285 s -> 330 s
        # chain above is derived from, so the budget must never cut that call short.
        deadline = time.time() + max(timeout, REQUEST_BUDGET_S)
        # Endpoints this caller's own keys name, for the engine's re-check of every lane it dials.
        lane_hosts = key_hosts(conn(), user["id"] if user else None)
        payload = build_payload(lane_config, tuning)
        status, resp, latency = run_detection(payload, headers, timeout, lane_hosts)

        # ---- failover: the next model in the caller's own order ------------------------
        #
        # "Auto switching if the model stops responding" happens here, in the backend, rather
        # than in the workflow: the engine has two lanes and no concept of a third candidate,
        # and teaching it an N-model walk would mean rebuilding the shipped, measured
        # two-lane graph. Retrying here reuses it unchanged.
        #
        # Three limits, each of which is a real cost rather than a taste:
        #   * only when the answer carries NO verdict. A detection the tie-break lane rescued
        #     is a verdict the user already paid for; throwing it away to try a different
        #     primary would spend a second full call to replace an answer with maybe an answer.
        #     The failed primary is still marked unhealthy, so the pool reorders next time.
        #   * never on ENGINE_UNREACHABLE / ENGINE_TIMEOUT / INPUT_REJECTED and the rest of
        #     NO_FAILOVER_CODES. Those say the engine or the ceiling or the request failed, not
        #     the model, and a second model would fail the same way. This is its own guard
        #     (`engine_level_failure`) and not a clause inside `no_verdict`, because the two
        #     502s `forward()` synthesises are both "no verdict" AND "unfixable by another
        #     model", and one check cannot say both.
        #   * only while there is time left. The chain above this is 270 s wait -> 285 s
        #     gunicorn -> 330 s nginx, so a retry started with 5 s on the clock cannot
        #     finish; it would turn a clean 502 into a gateway timeout with no answer.
        failover: list[dict] = []
        switched_to = None
        tried = {m for m in (primary_model,) if m}
        if user is not None and no_verdict(status, resp) and not engine_level_failure(resp):
            failed_model, failed_code, failed_detail = leg_failure(resp)
            failed_provider = provider_name_for_base_url(
                (lane_config.get("primary") or {}).get("base_url") or "")
            if failed_model and failed_provider:
                # The health row for the model that just failed. record_detection can only
                # read the FINAL body, whose evidence describes the model that answered.
                pool.note_attempt(conn(), failed_provider, failed_model, False,
                                  failed_code, failed_detail)
                conn().commit()
            for cand in pool.next_candidates(conn(), user["id"], tried):
                if len(failover) >= MAX_FAILOVER_ATTEMPTS:
                    break
                left = deadline - time.time()
                base = (lane_base_url(conn(), user["id"] if user else None, cand["provider"])
                        or provider_base_url_by_name(cand["provider"]))
                entry = {"from": primary_model, "to": cand["model"],
                         "provider": cand["provider"]}
                if left < FAILOVER_RESERVE_S:
                    entry["skipped"] = (f"no time left in this request's budget "
                                        f"({left:.0f}s < {FAILOVER_RESERVE_S:.0f}s)")
                    failover.append(entry)
                    break
                if not base or not endpoint_for_lane(conn(), base,
                                                     user["id"] if user else None):
                    entry["skipped"] = "endpoint is not in allowed_llm_hosts"
                    failover.append(entry)
                    continue
                tried.add(cand["model"])
                cand_cfg = {"primary": {"base_url": base, "model": cand["model"]}}
                if pool_used:
                    # The tie-break moves with the primary, so it is resolved again rather than
                    # carried over: the head that served the model which just failed to answer is
                    # that failed model's neighbour, and the entry the user ranked below THIS
                    # candidate is the one the second lane should start at. With nothing left
                    # below, the lane is switched off instead of left pointing at a leg that may
                    # name the new primary itself -- one model cannot corroborate itself.
                    cand_lane = tie_break_for(cand["model"])
                    secondary_off = cand_lane is None
                    secondary_model = None if cand_lane is None else cand_lane["model"]
                    cand_cfg["secondary"] = cand_lane or {"enabled": False}
                else:
                    cand_cfg["secondary"] = lane_config.get("secondary") or {"enabled": False}
                cand_headers = {k: v for k, v in headers.items() if k != "x-primary-api-key"}
                if pool_used:
                    # ...and the credential is re-resolved with it. A lane aimed at a second
                    # gateway, still paid for with the first one's key, comes back 401 -- which is
                    # a refusal the health table files against the MODEL, freezing a healthy model
                    # for a mistake the request made.
                    if secondary_off:
                        cand_headers.pop("x-secondary-api-key", None)
                    else:
                        sec_key = stored_key_for("secondary", cand_cfg["secondary"]["base_url"],
                                                 secondary_model, user_id=user["id"])
                        if sec_key:
                            cand_headers["x-secondary-api-key"] = sec_key
                        else:
                            cand_headers.pop("x-secondary-api-key", None)
                cand_row = model_by_id(cand["model"])
                cand_tuning = caller_tuning if isinstance(caller_tuning, dict) else tuning_block(
                    cand_row, model_by_id(secondary_model))
                cand_key = (db.user_key_for_provider(conn(), user["id"], _pid_by_name(cand["provider"]))
                            if user else None) or active_key(_pid_by_name(cand["provider"]), "primary")
                if cand_key:
                    cand_headers["x-primary-api-key"] = cand_key
                cand_timeout = float(cand_tuning.get("request_timeout_ms") or 90000) / 1000.0 + 30.0
                payload = build_payload(cand_cfg, cand_tuning)
                status, resp, latency = run_detection(payload, cand_headers,
                                                      min(cand_timeout, left))
                lane_config, tuning, primary_model = cand_cfg, cand_tuning, cand["model"]
                entry["ok"] = not no_verdict(status, resp)
                entry["error_code"] = None if entry["ok"] else (
                    (resp.get("error_code") if isinstance(resp, dict) else None) or f"http_{status}")
                failover.append(entry)
                if entry["ok"]:
                    switched_to = cand["model"]
                    break
                if engine_level_failure(resp):
                    # The engine went away mid-request. Every remaining candidate would be
                    # dialled against the same dead URL, so stop here and let the 502 reach the
                    # caller with the attempts that were really made attached to it.
                    break
        if switched_to and isinstance(resp, dict):
            # Say so out loud, in the response and in the stored row: a verdict that silently
            # came from a different model than the one the page says it used is exactly the
            # kind of unattributable number this app has spent rounds removing.
            resp["auto_switch"] = {
                "from": failover[0]["from"] if failover else None, "to": switched_to,
                "reason": "the first model returned no usable verdict",
                "attempts": failover,
            }
            warns = resp.get("warnings")
            note = (f"Auto-switched to {switched_to}: "
                    f"{failover[0]['from'] if failover else 'the first model'} did not answer.")
            resp["warnings"] = (warns if isinstance(warns, list) else []) + [note]
        elif failover and isinstance(resp, dict):
            resp["failover"] = {"attempts": failover, "switched_to": None}
        if isinstance(resp, dict):
            # Which authority picked the model that answered. The workflow's own
            # `model_source` says "request" for all four cases, because from its side they are
            # identical -- the distinction only exists here, and without it a user cannot tell
            # whether a verdict came from the admin's deployed lane, from their own pool, or
            # from a failover they did not ask for.
            resp["lane_source"] = (
                "failover" if switched_to else
                "request" if body.get("lane_config") else
                "user-pool" if pool_used else "deployed-default")

        # record_detection fills any lane the caller did not name from the workflow's own
        # config_used, so an overridden primary does not leave the tie-break lane unattributed.
        if isinstance(resp, dict):
            # A tie-break that walked the pool dialled several models and reports one lane, and
            # `config_used.secondary` names the endpoint the lane ENDED UP on. History has to name
            # the model whose reply actually decided -- the same question `auto_switch` answers for
            # the primary, and the same one a row naming `deployed/tie` for a text scored by
            # something else used to make unanswerable.
            walked = resp.get("secondary_walk")
            if isinstance(walked, dict) and walked.get("switched_to"):
                secondary_model = walked["switched_to"]
        det_id = record_detection(resp, status, latency, primary_model, secondary_model,
                                 text_length=len(payload.get("text") or ""),
                                 secondary_off=secondary_off, text=payload.get("text"))
        if isinstance(resp, dict):
            resp["backend_detection_id"] = det_id
        return jsonify(resp), status

    # ---- providers -------------------------------------------------------

    @app.route("/providers", methods=["GET"])
    def list_providers():
        user = auth.current_user()
        is_admin = user is None or user["role"] == "admin"
        rows = provider_rows()
        out = []
        for r in rows:
            d = provider_public(r)
            if is_admin:
                d["keys"] = [key_public(k) for k in conn().execute(
                    "SELECT * FROM api_keys WHERE provider_id = ? ORDER BY id", (r["id"],)).fetchall()]
            else:
                # A user may see WHICH endpoints exist (to bring their own key) and how many
                # keys the admin has configured, but never the admin's key rows themselves.
                d["keys"] = []
                d["has_admin_key"] = bool(d.get("key_count"))
            d["models"] = [model_public(m) for m in conn().execute(
                "SELECT * FROM models WHERE provider_id = ? ORDER BY lane IS NULL, lane, id",
                (r["id"],)).fetchall()]
            out.append(d)
        return jsonify(out)

    @app.route("/providers", methods=["POST"])
    def create_provider():
        b = request.get_json(silent=True) or {}
        name = (b.get("name") or "").strip()
        base_url = (b.get("base_url") or "").strip()
        if not re.match(r"^[a-z0-9_-]{2,40}$", name):
            return jsonify(error="name must be 2-40 chars of [a-z0-9_-]"), 400
        if not base_url or not host_of(base_url):
            return jsonify(error="base_url must be an absolute http(s) URL"), 400
        try:
            cur = conn().execute(
                """INSERT INTO providers (name, label, base_url, auth_header, auth_prefix,
                       free_tier, self_hosted, enabled, notes)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, b.get("label", name), base_url, b.get("auth_header", "Authorization"),
                 b.get("auth_prefix", "Bearer "), 1 if b.get("free_tier", True) else 0,
                 1 if b.get("self_hosted") else 0,
                 1 if b.get("enabled", True) else 0, b.get("notes", "")),
            )
            conn().commit()
        except sqlite3.IntegrityError:
            return jsonify(error=f"provider '{name}' already exists"), 409
        row = conn().execute("SELECT * FROM providers WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(provider_public(row)), 201

    @app.route("/providers/<int:pid>", methods=["PATCH"])
    def update_provider(pid: int):
        row = conn().execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
        if row is None:
            return jsonify(error="no such provider"), 404
        b = request.get_json(silent=True) or {}
        fields = {}
        for f in ("label", "base_url", "auth_header", "auth_prefix", "notes"):
            if f in b:
                fields[f] = str(b[f])[:500]
        for f in ("free_tier", "enabled", "self_hosted"):
            if f in b:
                fields[f] = 1 if b[f] else 0
        if "base_url" in fields and not host_of(fields["base_url"]):
            return jsonify(error="base_url must be an absolute http(s) URL"), 400
        if fields:
            assigns = ", ".join(f"{k} = ?" for k in fields)
            conn().execute(
                f"UPDATE providers SET {assigns}, updated_at = datetime('now') WHERE id = ?",
                (*fields.values(), pid),
            )
            conn().commit()
        row = conn().execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
        return jsonify(provider_public(row))

    @app.route("/providers/<int:pid>", methods=["DELETE"])
    def delete_provider(pid: int):
        cur = conn().execute("DELETE FROM providers WHERE id = ?", (pid,))
        conn().commit()
        return jsonify(deleted=cur.rowcount)

    # ---- api keys --------------------------------------------------------

    @app.route("/providers/<int:pid>/keys", methods=["POST"])
    def add_key(pid: int):
        if conn().execute("SELECT id FROM providers WHERE id = ?", (pid,)).fetchone() is None:
            return jsonify(error="no such provider"), 404
        b = request.get_json(silent=True) or {}
        plain = (b.get("api_key") or "").strip()
        if not plain or len(plain) > 512 or not KEY_CHARSET.match(plain):
            return jsonify(error="api_key must be 1-512 chars of [A-Za-z0-9._:+/-]"), 400
        cipher, hint = db.encrypt_key(plain)
        cur = conn().execute(
            "INSERT INTO api_keys (provider_id, label, key_cipher, key_hint) VALUES (?,?,?,?)",
            (pid, (b.get("label") or "key").strip()[:80], cipher, hint),
        )
        conn().commit()
        row = conn().execute("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(key_public(row)), 201

    @app.route("/keys", methods=["GET"])
    def list_keys():
        rows = conn().execute(
            """SELECT k.*, p.name AS provider_name FROM api_keys k
               JOIN providers p ON p.id = k.provider_id ORDER BY k.id"""
        ).fetchall()
        out = []
        for r in rows:
            d = key_public(r)
            d["provider_name"] = r["provider_name"]
            out.append(d)
        return jsonify(out)

    @app.route("/keys/<int:kid>", methods=["PATCH", "DELETE"])
    def mutate_key(kid: int):
        row = conn().execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()
        if row is None:
            return jsonify(error="no such key"), 404
        if request.method == "DELETE":
            conn().execute("DELETE FROM api_keys WHERE id = ?", (kid,))
            conn().commit()
            return jsonify(deleted=1)
        b = request.get_json(silent=True) or {}
        if "is_active" in b:
            conn().execute("UPDATE api_keys SET is_active = ?, updated_at = datetime('now') WHERE id = ?",
                           (1 if b["is_active"] else 0, kid))
        if "label" in b:
            conn().execute("UPDATE api_keys SET label = ?, updated_at = datetime('now') WHERE id = ?",
                           (str(b["label"])[:80], kid))
        conn().commit()
        row = conn().execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()
        return jsonify(key_public(row))

    # ---- models + tuned parameters ---------------------------------------

    MODEL_NUM_FIELDS = ("temperature", "top_p", "max_tokens", "timeout_ms",
                        "ai_prob_threshold", "ai_conf_threshold", "human_prob_threshold",
                        "human_conf_threshold", "secondary_decision_threshold")

    @app.route("/models", methods=["GET"])
    def list_models():
        rows = conn().execute("SELECT * FROM models ORDER BY lane IS NULL, lane, id").fetchall()
        return jsonify([model_public(r) for r in rows])

    @app.route("/models", methods=["POST"])
    def create_model():
        b = request.get_json(silent=True) or {}
        model_id = (b.get("model_id") or "").strip()
        pid = b.get("provider_id")
        if not model_id or not MODEL_PATTERN.match(model_id) or len(model_id) > 200:
            return jsonify(error="model_id must match ^[A-Za-z0-9._:/@+-]+$ (<= 200 chars)"), 400
        if not isinstance(pid, int) or conn().execute(
                "SELECT id FROM providers WHERE id = ?", (pid,)).fetchone() is None:
            return jsonify(error="provider_id must reference an existing provider"), 400
        lane = b.get("lane")
        if lane not in (None, "primary", "secondary"):
            return jsonify(error="lane must be 'primary', 'secondary' or null"), 400
        vals = {
            "display_name": str(b.get("display_name") or model_id.split("/")[-1]),
            "role": b.get("role") if b.get("role") in ("primary", "secondary", "either") else "either",
            "lane": lane,
            "notes": str(b.get("notes") or ""),
            "enabled": 1 if b.get("enabled", True) else 0,
            "measured": 1 if b.get("measured") else 0,
        }
        for f in ("display_name", "notes"):
            bad = over_long_text(f, vals[f])
            if bad:
                return jsonify(error=bad), 400
        for f in MODEL_NUM_FIELDS:
            if f in b and b[f] is not None:
                vals[f] = float(b[f]) if f in ("temperature", "top_p") or "threshold" in f else int(b[f])
        cols = ["provider_id", "model_id", *vals.keys()]
        try:
            cur = conn().execute(
                f"INSERT INTO models ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (pid, model_id, *vals.values()),
            )
            conn().commit()
        except sqlite3.IntegrityError:
            return jsonify(error=f"model '{model_id}' already exists for this provider"), 409
        if lane:
            clear_other_lane(conn(), cur.lastrowid, lane)
        row = conn().execute("SELECT * FROM models WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(model_public(row)), 201

    @app.route("/models/<int:mid>", methods=["PATCH"])
    def update_model(mid: int):
        row = conn().execute("SELECT * FROM models WHERE id = ?", (mid,)).fetchone()
        if row is None:
            return jsonify(error="no such model"), 404
        b = request.get_json(silent=True) or {}
        fields: dict = {}
        for f in ("display_name", "notes"):
            if f in b:
                bad = over_long_text(f, b[f])
                if bad:
                    return jsonify(error=bad), 400
                fields[f] = "" if b[f] is None else str(b[f])
        for f in MODEL_NUM_FIELDS:
            if f in b:
                fields[f] = None if b[f] is None else (
                    float(b[f]) if f in ("temperature", "top_p") or "threshold" in f else int(b[f]))
        for f in ("enabled", "measured"):
            if f in b:
                fields[f] = 1 if b[f] else 0
        if "role" in b and b["role"] in ("primary", "secondary", "either"):
            fields["role"] = b["role"]
        if "lane" in b:
            if b["lane"] not in (None, "primary", "secondary"):
                return jsonify(error="lane must be 'primary', 'secondary' or null"), 400
            fields["lane"] = b["lane"]
        if fields:
            assigns = ", ".join(f"{k} = ?" for k in fields)
            conn().execute(
                f"UPDATE models SET {assigns}, updated_at = datetime('now') WHERE id = ?",
                (*fields.values(), mid),
            )
            conn().commit()
            if fields.get("lane"):
                clear_other_lane(conn(), mid, fields["lane"])
        row = conn().execute("SELECT * FROM models WHERE id = ?", (mid,)).fetchone()
        return jsonify(model_public(row))

    @app.route("/models/<int:mid>", methods=["DELETE"])
    def delete_model(mid: int):
        cur = conn().execute("DELETE FROM models WHERE id = ?", (mid,))
        conn().commit()
        return jsonify(deleted=cur.rowcount)

    @app.route("/models/<int:mid>/assign-lane", methods=["POST"])
    def assign_lane(mid: int):
        b = request.get_json(silent=True) or {}
        lane = b.get("lane")
        if lane not in ("primary", "secondary"):
            return jsonify(error="lane must be 'primary' or 'secondary'"), 400
        if conn().execute("SELECT id FROM models WHERE id = ?", (mid,)).fetchone() is None:
            return jsonify(error="no such model"), 404
        conn().execute(
            "UPDATE models SET lane = ?, role = ?, updated_at = datetime('now') WHERE id = ?",
            (lane, lane, mid),
        )
        conn().commit()
        clear_other_lane(conn(), mid, lane)
        primary, secondary = default_models()
        return jsonify(primary=primary["model_id"] if primary else None,
                       secondary=secondary["model_id"] if secondary else None)

    # ---- probe: real connectivity test against a provider/model ----------

    @app.route("/providers/<int:pid>/probe", methods=["POST"])
    def probe_provider(pid: int):
        prov = conn().execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
        if prov is None:
            return jsonify(error="no such provider"), 404
        b = request.get_json(silent=True) or {}
        model_id = (b.get("model_id") or "").strip()
        if not model_id or not MODEL_PATTERN.match(model_id):
            return jsonify(error="model_id required and must match ^[A-Za-z0-9._:/@+-]+$"), 400
        if not host_allowed(prov["base_url"]):
            return jsonify(ok=False, error_code="HOST_NOT_ALLOWED",
                           host=host_of(prov["base_url"]),
                           allowed=setting("allowed_llm_hosts")), 400
        key = active_key(pid) or (b.get("api_key") if KEY_CHARSET.match(str(b.get("api_key") or "x")) else None)
        if not key:
            return jsonify(ok=False, error_code="NO_API_KEY",
                           hint="add a key with POST /providers/<id>/keys"), 409
        url = prov["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 8, "temperature": 0.0,
        }
        t0 = time.time()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header(prov["auth_header"] or "Authorization",
                       f"{prov['auth_prefix'] or ''}{key}")
        try:
            with urllib.request.urlopen(
                    req, timeout=probe_timeout_s(model_id, b.get("timeout_seconds"),
                                                 prov["id"])) as resp:
                raw = resp.read()
                status, err = resp.status, None
        except urllib.error.HTTPError as exc:
            raw, status, err = exc.read(), exc.code, str(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return jsonify(ok=False, error_code="UNREACHABLE", status=None, detail=str(exc),
                           latency_ms=int((time.time() - t0) * 1000))
        ms = int((time.time() - t0) * 1000)
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            parsed = None
        content = None
        if isinstance(parsed, dict):
            choices = parsed.get("choices") or []
            if choices and isinstance(choices[0], dict):
                content = (choices[0].get("message") or {}).get("content")
        return jsonify({
            "ok": status == 200,
            "status": status,
            "latency_ms": ms,
            "provider": prov["name"],
            "base_url": prov["base_url"],
            "model": model_id,
            "reply_excerpt": (content or "")[:60] if content else None,
            "error": None if status == 200 else (str(parsed)[:400] if parsed else err),
        })

    # ---- bring-your-own-key (a signed-in user's own credentials) -----------
    #
    # A user may attach a key for an endpoint the admin has already allowed, and the app
    # uses the admin's tuned model + band with it. The user never sets thresholds, tokens
    # or sampling -- only the credential. Keys are encrypted at rest exactly like the
    # admin's, and only a hint is ever returned. _resolve_provider / _probe_with_key live at
    # module scope so they are reusable and testable.

    @app.route("/my/keys", methods=["GET"])
    def my_keys():
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        rows = db.list_user_keys(conn(), user["id"])
        out = []
        for r in rows:
            d = db.user_key_public(r)
            d["provider_name"] = r["provider_name"]
            d["provider_base_url"] = r["provider_base_url"]
            out.append(d)
        return jsonify(out)

    @app.route("/my/keys", methods=["POST"])
    def my_keys_add():
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        plain = (b.get("api_key") or "").strip()
        if not plain or len(plain) > 512 or not KEY_CHARSET.match(plain):
            return jsonify(error="api_key must be 1-512 chars of [A-Za-z0-9._:+/-]"), 400
        prov = _resolve_provider(b.get("provider_id"), b.get("base_url"))
        if prov is None:
            return jsonify(error="that endpoint is not one this app supports",
                           error_code="PROVIDER_NOT_SUPPORTED"), 400
        if not host_allowed(prov["base_url"]):
            return jsonify(error="that endpoint host is not allowed",
                           error_code="HOST_NOT_ALLOWED"), 400
        # Which address this credential is for. The provider row answers that on its own wherever a
        # provider and its endpoint are the same thing -- NVIDIA, OpenRouter, Gemini each have one.
        # A self-hosted gateway is the case the owner knows better than the deployment does: the
        # same omniroute row runs at a different address for every person who has one, so a key for
        # it carries the box it belongs to. Naming an endpoint for a provider NOT flagged that way
        # stays refused -- posting an attacker's URL at a common provider is exactly what the guard
        # in front of this line was built for, and that guard is unchanged.
        named = (b.get("base_url") or "").strip()
        row_url = (prov["base_url"] or "").rstrip("/")
        endpoint = ""
        if named and named.rstrip("/") != row_url:
            if not prov["self_hosted"]:
                return jsonify(error="that provider's endpoint is fixed; only a self-hosted "
                                     "gateway can be named on a key",
                               error_code="PROVIDER_NOT_SUPPORTED"), 400
            endpoint, code = owner_endpoint(named)
            if code:
                return jsonify(error=("that endpoint is not a diallable gateway API root"
                                      if code == "ENDPOINT_INVALID" else
                                      "that http endpoint is off the allowlist; an https one is "
                                      "trusted anywhere"),
                               error_code=code), 400
            if endpoint == row_url:
                endpoint = ""  # the row already says this address: no override to store
        label = (b.get("label") or "my key").strip()[:80] or "my key"
        kid = db.add_user_key(conn(), user["id"], prov["id"], plain, label, endpoint)
        # The address to dial, in the shape `_probe_with_key` and discovery already take. Substituted
        # inside the provider mapping rather than passed as a fifth argument, because the probe's
        # signature is the contract five test stubs and the admin probe share.
        target = {**dict(prov), "base_url": endpoint or row_url}
        # Verify immediately if a model was supplied, so "auto-pick a working key" is honest.
        verified = False
        model_id = (b.get("model_id") or "").strip()
        if model_id and MODEL_PATTERN.match(model_id):
            res = _probe_with_key(target, model_id, plain,
                                  probe_timeout_s(model_id, b.get("timeout_seconds"), prov["id"]))
            verified = bool(res.get("ok"))
            db.set_user_key_verified(conn(), kid, user["id"], verified)
        row = db.get_user_key(conn(), kid, user["id"])
        d = db.user_key_public(row)
        d["provider_name"] = prov["name"]
        d["provider_base_url"] = prov["base_url"]
        if verified:
            # Same trigger as the explicit verify route: saving a key with a model_id probes
            # it inline, and a key that just answered is the moment its budget can be spent
            # on finding out what else that endpoint serves.
            d["discovery"] = enqueue_discovery(user, target, plain, "key saved and verified")
        return jsonify(d), 201

    @app.route("/my/keys/<int:kid>", methods=["DELETE"])
    def my_keys_delete(kid: int):
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        n = db.delete_user_key(conn(), kid, user["id"])
        if not n:
            return jsonify(error="no such key"), 404
        return jsonify(deleted=n)

    @app.route("/my/keys/<int:kid>/verify", methods=["POST"])
    def my_keys_verify(kid: int):
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        row = db.get_user_key(conn(), kid, user["id"])
        if row is None:
            return jsonify(error="no such key"), 404
        b = request.get_json(silent=True) or {}
        model_id = (b.get("model_id") or "").strip()
        if not model_id or not MODEL_PATTERN.match(model_id):
            return jsonify(error="model_id required and must match ^[A-Za-z0-9._:/@+-]+$"), 400
        prov = conn().execute("SELECT * FROM providers WHERE id = ?", (row["provider_id"],)).fetchone()
        if prov is None:
            return jsonify(ok=False, error_code="HOST_NOT_ALLOWED"), 400
        # Dial where this key lives, not where the seed put the row. The owner of a self-hosted
        # gateway named his own box when he saved the credential, and verifying the deployment's
        # default address instead would answer a question about somebody else's router.
        own = (row["base_url"] or "").strip()
        if own:
            # Re-gated on every verify, not only at save: `allowed_llm_hosts` is an admin setting
            # that can move underneath a stored http endpoint, and a URL that was trusted the day
            # it was written is not trusted forever.
            _again, code = owner_endpoint(own)
            if code:
                return jsonify(ok=False, error_code=code), 400
        elif not host_allowed(prov["base_url"]):
            return jsonify(ok=False, error_code="HOST_NOT_ALLOWED"), 400
        target = {**dict(prov), "base_url": own or prov["base_url"]}
        try:
            key = db.decrypt_key(row["key_cipher"])
        except ValueError:
            return jsonify(ok=False, error_code="KEY_UNREADABLE"), 500
        res = _probe_with_key(target, model_id, key,
                              probe_timeout_s(model_id, b.get("timeout_seconds"), prov["id"]))
        db.set_user_key_verified(conn(), kid, user["id"], bool(res.get("ok")))
        out = {**res, "provider": prov["name"], "model": model_id}
        if res.get("ok"):
            # A key that just proved it works is the only trustworthy moment to spend its
            # budget on discovery: before this the app cannot tell an unusable model from an
            # unusable credential, and screening on a broken key would mark good models dead.
            out["discovery"] = enqueue_discovery(user, target, key, "key verified")
        return jsonify(out)

    @app.route("/my/probe", methods=["POST"])
    def my_probe():
        """Ask one model, in one sentence, whether it answers THIS caller right now.

        The Ranking card could only "score with this model", so the cheap question -- is it
        awake? -- used to cost a whole detection. `_probe_with_key` already exists and the admin
        has had it since #20 (`POST /providers/<id>/probe`), but that path is behind
        `_admin_required`, so a plain user on a hosted deploy could not be given a test button at
        all. This is the user-facing half, and it is what the card's Test button calls.

        It spends the caller's own credential and never the deployment's: "does this answer me" is
        a question about his quota and his key, and answering it with someone else's would make
        the result a lie. So no stored key for that endpoint is a refusal (409), the same shape
        `/my/discover` uses.

        And it writes nothing. Health is measured from real detections, catalogue rows from
        discovery -- a probe that ran out of time must not mark a lane down, re-rank a pool, or
        retire a model, because a timeout is not a verdict.
        """
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        model_id = (b.get("model_id") or "").strip()
        if not model_id or not MODEL_PATTERN.match(model_id):
            return jsonify(error="model_id required and must match ^[A-Za-z0-9._:/@+-]+$"), 400
        prov = _resolve_provider(b.get("provider_id"), b.get("base_url"))
        if prov is None:
            return jsonify(error="that endpoint is not one this app supports",
                           error_code="PROVIDER_NOT_SUPPORTED"), 400
        # Dial the box this caller's key belongs to, not the seed's default address.
        target = key_target(conn(), user["id"], prov)
        if not endpoint_for_lane(conn(), target["base_url"], user["id"]):
            return jsonify(error="that endpoint host is not allowed",
                           error_code="HOST_NOT_ALLOWED"), 400
        key = db.user_key_for_provider(conn(), user["id"], prov["id"])
        if not key:
            return jsonify({
                "ok": False, "error_code": "NO_USER_KEY",
                "error": "a test call runs on your own key for that endpoint; add and verify "
                         "one first",
            }), 409
        res = _probe_with_key(target, model_id, key,
                              probe_timeout_s(model_id, b.get("timeout_seconds"), prov["id"]))
        return jsonify({**res, "provider": prov["name"], "model": model_id})

    # ---- auto-discovery: bring the provider's free models in, screen them, rank them ----

    def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
        """An admin-tunable integer, clamped. A hand-edited settings row must not be able to
        set the screen limit to 500 (an hour of the user's budget) or the timeout to 0."""
        try:
            v = int(setting(key, "") or default)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    def enqueue_discovery(user, prov, key: str, reason: str) -> dict:
        """Queue one discovery job for this caller + provider, or report the one in flight.

        De-duplicated on (user, provider) because the trigger is a key verification and a
        user re-checking a key three times must not queue three identical screens.

        Refuses when the worker thread is not running in this process. The entry points start
        it; a Flask test client does not, and queuing a job there would launch real provider
        calls against a throwaway database the next test deletes. Saying so out loud (rather
        than silently dropping it) is what keeps the refusal from looking like a success.
        """
        if not discovery.worker.running:
            return {"job_id": None, "queued": False, "status": "disabled",
                    "reason": "the discovery worker is not running in this process"}
        busy = conn().execute(
            """SELECT id, status, stage FROM discovery_jobs
               WHERE user_id = ? AND provider = ? AND status IN ('queued','running')
               ORDER BY id DESC LIMIT 1""", (user["id"], prov["name"])).fetchone()
        if busy:
            return {"job_id": busy["id"], "queued": False, "status": busy["status"],
                    "stage": busy["stage"], "reason": "a discovery run is already in flight"}
        job_id = db.discovery_job(conn(), user["id"], prov["name"])
        conn().commit()
        discovery.worker.enqueue({
            "job_id": job_id, "user_id": user["id"], "provider_id": prov["id"],
            "provider_name": prov["name"], "base_url": prov["base_url"], "key": key,
            "auth_header": prov["auth_header"] or "Authorization",
            "auth_prefix": prov["auth_prefix"] if prov["auth_prefix"] is not None else "Bearer ",
            "screen_limit": _int_setting("discovery_screen_limit",
                                         discovery.DEFAULT_SCREEN_LIMIT, 0, 24),
            "screen_timeout_ms": _int_setting("discovery_screen_timeout_ms",
                                              discovery.DEFAULT_SCREEN_TIMEOUT_MS, 5_000, 300_000),
        })
        return {"job_id": job_id, "queued": True, "status": "queued", "reason": reason}

    @app.route("/my/discover", methods=["POST"])
    def my_discover():
        """Screen a provider's free models on the caller's own key, on demand.

        Requires a stored key for that endpoint -- discovery spends the caller's budget, so
        it uses the caller's credential and never the admin's shared one.
        """
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        prov = _resolve_provider(b.get("provider_id"), b.get("base_url"))
        if prov is None:
            return jsonify(error="that endpoint is not one this app supports",
                           error_code="PROVIDER_NOT_SUPPORTED"), 400
        if not host_allowed(prov["base_url"]):
            return jsonify(error="that endpoint host is not allowed",
                           error_code="HOST_NOT_ALLOWED"), 400
        key = db.user_key_for_provider(conn(), user["id"], prov["id"])
        if not key:
            return jsonify({
                "ok": False, "error_code": "NO_USER_KEY",
                "error": "discovery runs on your own key for that endpoint; add and verify one first",
            }), 409
        out = enqueue_discovery(user, key_target(conn(), user["id"], prov), key, "requested")
        if not out.get("queued") and out.get("status") == "disabled":
            return jsonify({"ok": False, "error_code": "DISCOVERY_DISABLED", **out}), 503
        return jsonify({"ok": True, **out}), 202

    @app.route("/my/discover", methods=["GET"])
    def my_discover_status():
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        rows = conn().execute(
            """SELECT id, provider, status, stage, n_listed, n_screened, n_usable, error,
                      started_at, finished_at, created_at
               FROM discovery_jobs WHERE user_id = ? ORDER BY id DESC LIMIT 20""",
            (user["id"],)).fetchall()
        return jsonify({"jobs": [dict(r) for r in rows],
                        "screen_texts": discovery.SCREEN_CORPUS,
                        "note": "each screened model is judged on 2 real texts through the "
                                "shipped engine path, so it lands in the ranking's probe tier "
                                "and cannot outrank a full capture"})

    # ---- the caller's ordered using-pool ---------------------------------------------

    @app.route("/my/pool", methods=["GET"])
    def my_pool():
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        return jsonify({
            "pool": pool.pool_view(conn(), user["id"]),
            "note": "your order is authoritative; a model marked down (three consecutive "
                    "non-answers) is served last until it answers again, and your rank is "
                    "restored untouched when it does",
        })

    @app.route("/my/pool/options", methods=["GET"])
    def my_pool_options():
        """Every model this app knows the caller may pick, and what the last probe said about it.

        `/ranking` cannot be the picker's only source of truth: it is a measurement, so a model
        whose lane ran out of time -- or that was never asked a text -- is simply absent from it.
        Absence on that page reads as "unavailable", which is exactly the mistake this rule set
        exists to stop. Here the row that matters is the `models` row (the thing that makes a
        model pinnable at all), joined to the last probe so a card can show `slow`, `busy` or
        `pending` in the engine's own words instead of the user inferring `dead` from a blank.

        A live `pending` offer is sorted first, because it is the one entry waiting on him.
        """
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        window = discovery.DECISION_WINDOW_S
        rows = conn().execute(
            """SELECT m.provider_id, p.name AS provider, p.base_url, p.free_tier,
                      m.model_id, m.measured, m.timeout_ms, m.notes,
                      c.probe_status, c.probe_note, c.latency_ms,
                      CAST(strftime('%s', datetime(c.probed_at, ?)) - strftime('%s', 'now')
                           AS INTEGER) AS offer_left
               FROM models m
               JOIN providers p ON p.id = m.provider_id AND p.enabled = 1
               LEFT JOIN catalog_models c ON c.provider = p.name AND c.model_id = m.model_id
               WHERE m.enabled = 1""", (f"+{window} seconds",)).fetchall()
        mine = {(r["provider"], r["model"]): r for r in pool.pool_view(conn(), user["id"])}
        out = []
        for r in rows:
            status = r["probe_status"] or "unknown"
            left = r["offer_left"]
            offer = status == "pending" and left is not None and left > 0
            hit = mine.get((r["provider"], r["model_id"]))
            admin_key = bool(conn().execute(
                "SELECT 1 FROM api_keys WHERE provider_id = ? AND is_active = 1 LIMIT 1",
                (r["provider_id"],)).fetchone())
            own_key = bool(db.user_key_for_provider(conn(), user["id"], r["provider_id"]))
            out.append({
                "provider": r["provider"], "provider_id": r["provider_id"],
                "provider_name": r["provider"], "base_url": r["base_url"],
                "free_tier": bool(r["free_tier"]), "host": host_of(r["base_url"]),
                "model": r["model_id"], "measured": bool(r["measured"]),
                "timeout_ms": r["timeout_ms"], "notes": r["notes"] or "",
                "probe_status": status, "probe_note": r["probe_note"] or "",
                "answer_ms": r["latency_ms"],
                "verified_key": own_key, "admin_key": admin_key,
                "needs_key": not (own_key or admin_key),
                "in_pool": hit is not None,
                "your_rank": hit["your_rank"] if hit else None,
                "health": hit["health"] if hit else "unknown",
                "offered": offer,
                "offer_seconds_left": left if offer else None,
            })
        out.sort(key=lambda e: (0 if e["offered"] else 1, 0 if e["measured"] else 1,
                                e["provider"], e["model"]))
        return jsonify({
            "options": out, "window_s": window, "fast_ms": discovery.FAST_ANSWER_MS,
            "note": "a model with no rank is not a broken model: `slow` and `busy` are the pool "
                    "being saturated at that minute, so the row stays pickable and the next "
                    "discovery run tries it again. `pending` is a model that only answered after "
                    "we asked twice -- it is yours to accept or refuse while the window holds."})

    @app.route("/my/catalog/decision", methods=["POST"])
    def my_catalog_decision():
        """Accept or refuse a model that answered the screen only after a timeout.

        The job holds the evidence ("it answered in 3 s, but the first ask timed out") and the
        user holds the judgement ("do I want this in the lane that scores my texts"), so this is
        the route that hands him the decision instead of spending his call budget on a pin he
        never agreed to. Accept appends to his pool at the end and marks the catalogue row
        `usable`, which ends the offer. Refuse marks it `declined` and leaves the `models` row
        exactly where it was: a `no` today is not a deletion, and the same card can add it later.
        """
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        model_id = (b.get("model") or "").strip()
        provider = (b.get("provider") or "").strip()
        decision = (b.get("decision") or "").strip().lower()
        if not model_id or not MODEL_PATTERN.match(model_id) or not provider or len(provider) > 60:
            return jsonify(error="provider and model required, model must match "
                                 "^[A-Za-z0-9._:/@+-]+$"), 400
        if decision not in ("accept", "refuse"):
            return jsonify(error="decision must be 'accept' or 'refuse'",
                           error_code="BAD_DECISION"), 400
        cat = conn().execute(
            "SELECT probe_status FROM catalog_models WHERE provider = ? AND model_id = ?",
            (provider, model_id)).fetchone()
        if cat is None:
            return jsonify(error="that model has no probe on that endpoint to decide about",
                           error_code="NOT_OFFERED"), 404
        if decision == "accept":
            base = provider_base_url_by_name(provider)
            if not base:
                return jsonify(error="no enabled provider by that name",
                               error_code="PROVIDER_NOT_SUPPORTED"), 400
            if not host_allowed(base):
                return jsonify(error="that endpoint host is not allowed",
                               error_code="HOST_NOT_ALLOWED"), 400
            # source='user', not 'auto': this one he said yes to, so it is his row to order.
            db.pool_add(conn(), user["id"], provider, model_id, source="user")
        conn().execute(
            """UPDATE catalog_models SET probe_status = ?,
                   probe_note = probe_note || ?
               WHERE provider = ? AND model_id = ?""",
            ("usable" if decision == "accept" else "declined",
             f" -- accepted into the pool by user {user['id']}" if decision == "accept"
             else f" -- refused by user {user['id']}; still pickable",
             provider, model_id))
        conn().commit()
        return jsonify({"provider": provider, "model": model_id, "decision": decision,
                        "probe_status": "usable" if decision == "accept" else "declined",
                        "pool": pool.pool_view(conn(), user["id"])})

    @app.route("/my/pool", methods=["POST"])
    def my_pool_add():
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        model_id = (b.get("model") or "").strip()
        provider = (b.get("provider") or "").strip()
        if not model_id or not MODEL_PATTERN.match(model_id):
            return jsonify(error="model required and must match ^[A-Za-z0-9._:/@+-]+$"), 400
        if not provider or len(provider) > 60:
            return jsonify(error="provider required (its name, e.g. nvidia)"), 400
        base = provider_base_url_by_name(provider)
        if not base:
            return jsonify(error="no enabled provider by that name",
                           error_code="PROVIDER_NOT_SUPPORTED"), 400
        if not host_allowed(base):
            return jsonify(error="that endpoint host is not allowed",
                           error_code="HOST_NOT_ALLOWED"), 400
        db.pool_add(conn(), user["id"], provider, model_id,
                    source="user" if b.get("source") != "auto" else "auto")
        conn().commit()
        return jsonify({"pool": pool.pool_view(conn(), user["id"])}), 201

    @app.route("/my/pool", methods=["DELETE"])
    def my_pool_remove():
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        n = db.pool_remove(conn(), user["id"], (b.get("provider") or "").strip(),
                           (b.get("model") or "").strip())
        conn().commit()
        if not n:
            return jsonify(error="that model is not in your pool"), 404
        return jsonify({"pool": pool.pool_view(conn(), user["id"])})

    @app.route("/my/pool/replace", methods=["POST"])
    def my_pool_replace():
        """Change which model fills one slot of the caller's pool, keeping the slot.

        The page's edit control needs this as one action: "remove, then add, then re-drag" costs
        three round trips, leaves the held slot empty between them, and a half-finished sequence
        ends with either two rows or none. The neighbours keep the ranks their owner gave them
        because the new order is built from the caller's STORED order (not the health-served one)
        with exactly one entry replaced.
        """
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        old_prov = (b.get("provider") or "").strip()
        old_model = (b.get("model") or "").strip()
        new_prov = (b.get("new_provider") or old_prov).strip()
        new_model = (b.get("new_model") or "").strip()
        if not new_model or not MODEL_PATTERN.match(new_model):
            return jsonify(error="new_model required and must match ^[A-Za-z0-9._:/@+-]+$"), 400
        base = provider_base_url_by_name(new_prov)
        if not base or not host_allowed(base):
            return jsonify(error="no enabled provider by that name, or its host is not allowed",
                           error_code="PROVIDER_NOT_SUPPORTED"), 400
        rows = db.pool_list(conn(), user["id"])
        old_key, new_key = (old_prov, old_model), (new_prov, new_model)
        if not any((r["provider"], r["model_id"]) == old_key for r in rows):
            return jsonify(error="that model is not in your pool"), 404
        if any((r["provider"], r["model_id"]) == new_key for r in rows):
            # INSERT OR IGNORE would silently no-op the add and then DELETE the row the user was
            # editing: a collision has to be refused, not absorbed.
            return jsonify(error="that model is already in your pool",
                           error_code="ALREADY_IN_POOL"), 400
        wanted = [new_key if (r["provider"], r["model_id"]) == old_key
                  else (r["provider"], r["model_id"]) for r in rows]
        db.pool_remove(conn(), user["id"], *old_key)
        db.pool_add(conn(), user["id"], new_prov, new_model, source="user")
        db.pool_set_positions(conn(), user["id"], wanted)
        conn().commit()
        return jsonify({"pool": pool.pool_view(conn(), user["id"])})

    @app.route("/my/pool/order", methods=["POST"])
    def my_pool_order():
        """Write the caller's drag order. This is the only route that changes `position`,
        and it can only ever change the caller's own rows."""
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        order = b.get("order")
        if not isinstance(order, list) or len(order) > 100:
            return jsonify(error="order must be a list of <= 100 {provider, model} objects"), 400
        pairs = []
        for it in order:
            if not isinstance(it, dict):
                return jsonify(error="each order entry must be an object"), 400
            prov, mid = (it.get("provider") or "").strip(), (it.get("model") or "").strip()
            if not prov or not mid or not MODEL_PATTERN.match(mid):
                return jsonify(error="each order entry needs provider and model"), 400
            pairs.append((prov, mid))
        db.pool_set_positions(conn(), user["id"], pairs)
        conn().commit()
        return jsonify({"pool": pool.pool_view(conn(), user["id"])})

    @app.route("/my/pool/auto-rank", methods=["POST"])
    def my_pool_auto_rank():
        """Order the caller's pool by the measurement the Ranking page already reports.

        "Rank them for me, best to unranked" is a question this app already answers once, and a
        second sort written beside it is how a page becomes a caption: #142 was the front-end
        keeping its own lane, and the offline audits were bands hand-copied from a ruler that had
        moved. So this route reads the order out of `measured_ranking` -- the same scored, sorted
        list /ranking draws -- and only ever permutes rows he already holds.

        A model with no capture has no measurement, and no measurement is not a removal: those
        rows go behind the judged ones and keep their relative order among themselves. Nothing is
        added either -- bringing a model in is the picker's job, and a silent add would spend his
        calls on a lane he never chose.
        """
        user = auth.current_user()
        if user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        rows = db.pool_list(conn(), user["id"])
        order_of = {e["model"]: e["rank"] for e in measured_ranking(user)}
        judged = [r for r in rows if r["model_id"] in order_of]
        judged.sort(key=lambda r: order_of[r["model_id"]])
        tail = [r for r in rows if r["model_id"] not in order_of]
        if not judged:
            return jsonify({
                "pool": pool.pool_view(conn(), user["id"]),
                "ranked": 0, "unranked": len(tail),
                "note": "nothing in your pool has a measurement to rank on: no measurement has "
                        "been captured for these models yet. Run a detection, or let discovery "
                        "screen a model, then rank again.",
            })
        db.pool_set_positions(conn(), user["id"],
                              [(r["provider"], r["model_id"]) for r in judged + tail])
        conn().commit()
        return jsonify({
            "pool": pool.pool_view(conn(), user["id"]),
            "ranked": len(judged), "unranked": len(tail),
            "note": f"ordered by the Ranking table's own measurement: {len(judged)} ranked best "
                    f"first, {len(tail)} left at the back in your order because no capture "
                    f"exists to judge them on",
        })

    # ---- public config: paper link, key guides, supported endpoints --------

    @app.route("/config", methods=["GET"])
    def public_config():
        """Everything the ranking/detect pages show a signed-in user that is NOT an admin
        secret: where the paper lives, how to get a key per provider, and which endpoints
        are supported for BYOK. Values are admin-editable through /settings."""
        user = auth.current_user()
        guides = json.loads(setting("api_key_guides", "[]") or "[]")
        providers = []
        for p in conn().execute("SELECT * FROM providers WHERE enabled = 1 ORDER BY id"):
            models = conn().execute(
                "SELECT model_id, display_name, lane FROM models WHERE provider_id = ? AND enabled = 1 "
                "ORDER BY lane IS NULL, lane, id", (p["id"],)).fetchall()
            providers.append({
                "id": p["id"], "name": p["name"], "label": p["label"],
                "base_url": p["base_url"], "free_tier": bool(p["free_tier"]),
                # The key form reads this: one fixed address needs a key and nothing else, while a
                # gateway the owner runs needs the address his box actually lives at. Same flag
                # POST /my/keys checks before it accepts a per-key endpoint.
                "self_hosted": bool(p["self_hosted"]),
                "host": host_of(p["base_url"]),
                "has_admin_key": bool(conn().execute(
                    "SELECT 1 FROM api_keys WHERE provider_id = ? AND is_active = 1 LIMIT 1",
                    (p["id"],)).fetchone()),
                "models": [dict(m) for m in models],
            })
        return jsonify({
            "paper_url": setting("paper_url", ""),
            "paper_title": setting("paper_title", "Read the paper"),
            "api_key_guides": guides,
            "providers": providers,
            "user": user,
        })

    # ---- admin settings (paper link, key guides, thresholds, allowed hosts) --

    # An allowlist, not a free-for-all: only keys the UI actually reads may be written, so a
    # stray POST cannot plant an arbitrary setting that some other code path later trusts.
    WRITABLE_SETTINGS = (
        "paper_url", "paper_title", "api_key_guides", "allowed_llm_hosts",
        "n8n_url", "webhook_path", "ai_prob_threshold", "human_prob_threshold",
        "secondary_decision_threshold",
        # Auto-discovery budget. Both are clamped on read (_int_setting), so a bad value here
        # degrades to the default rather than being trusted: the limit is how many of the
        # caller's own free-tier calls one verification may spend.
        "discovery_screen_limit", "discovery_screen_timeout_ms",
    )

    @app.route("/settings", methods=["GET"])
    def get_settings():
        out = {k: setting(k, "") for k in WRITABLE_SETTINGS}
        # POST accepts a list of guides and stores its JSON text, so GET has to hand back the same
        # shape the admin form sent -- otherwise the save button re-posts the serialised string and
        # json.dumps a second time on every write. A value that is not JSON (hand-seeded) stays as
        # text rather than being silently emptied, because the field is still editable.
        try:
            out["api_key_guides"] = json.loads(out["api_key_guides"] or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        return jsonify(out)

    # Validated on write, because two of these keys are load-bearing for the whole product:
    # when the workflow is the engine, `run_detection` posts to setting("n8n_url") +
    # setting("webhook_path"), so a scheme-less engine URL ("localhost:5678") raises inside the
    # forwarder and every detection answers 502 ENGINE_UNREACHABLE until someone edits the settings
    # table from a shell. A single-user local install answers in-process and never reads them -- but
    # an admin can still point it at a workflow with DETECTION_ENGINE=n8n, and the hosted container
    # is the deploy where this field decides everything, so the check stays. Blank is legal for all
    # three (it falls back to the built-in default); a present value has to be usable.
    def setting_problem(key: str, value: str) -> str | None:
        if not value:
            return None
        if key == "webhook_path":
            return None if value.startswith("/") else (
                "webhook_path must start with '/', e.g. /webhook/ai-detect-ai")
        if key in ("n8n_url", "paper_url"):
            label = "detection engine URL" if key == "n8n_url" else "paper link"
            if not value.lower().startswith(("http://", "https://")) or not host_of(value):
                return (f"{label} ({key}) must be an absolute http(s) URL with a host, "
                        f"e.g. http://localhost:5678 or https://zenodo.org/doi/10.5281/zenodo.x")
        if key in ("discovery_screen_limit", "discovery_screen_timeout_ms"):
            lo, hi = (0, 24) if key == "discovery_screen_limit" else (5_000, 300_000)
            label = ("models screened per discovery run" if key == "discovery_screen_limit"
                     else "per-text screen timeout in ms")
            try:
                n = int(value)
            except ValueError:
                return f"{label} ({key}) must be a whole number between {lo} and {hi}"
            if not lo <= n <= hi:
                return f"{label} ({key}) must be between {lo} and {hi} (got {n})"
        return None

    @app.route("/settings", methods=["POST"])
    def put_settings():
        b = request.get_json(silent=True) or {}
        if not isinstance(b, dict):
            return jsonify(error="body must be a JSON object"), 400
        prepared: dict[str, str] = {}
        for k, v in b.items():
            if k not in WRITABLE_SETTINGS:
                continue
            if k == "api_key_guides" and not isinstance(v, str):
                v = json.dumps(v)  # accept a list, store the JSON text
            value = "" if v is None else str(v)
            problem = setting_problem(k, value)
            if problem:
                # Refuse the whole request rather than half-applying it, so a typo in one field
                # cannot leave the engine pointed somewhere else from the same save.
                return jsonify(ok=False, error="invalid_value", field=k, detail=problem), 400
            prepared[k] = value
        for k, value in prepared.items():
            db.set_setting(conn(), k, value)
        written = {k: setting(k, "") for k in prepared}
        conn().commit()
        return jsonify(ok=True, written=written)


    @app.route("/ranking", methods=["GET"])
    def ranking():
        """The ranked model table that replaces the old research/benchmark panel.

        The measurement and the sort are `measured_ranking`, shared with /my/pool/auto-rank so
        the order this page shows and the order the pool can be ranked into are one thing. This
        route adds what only the page needs: whose key is behind each row, and which lane would
        actually answer this caller's next run.
        """
        user = auth.current_user()
        out = measured_ranking(user)
        # ---- who answers this caller's next run -------------------------------------
        #
        # Read through the same functions /detect walks (pool_primary_lane, default_models,
        # pool.next_candidates), never re-derived: this block is the page's answer to "which
        # model is used for my runs, and what gets switched to when it stops answering", and
        # an answer invented beside the real one is how a ranking page becomes decoration.
        default_primary, default_secondary = default_models()
        deployed_default = ({"model": default_primary["model_id"],
                             "provider": default_primary["provider_name"]}
                            if default_primary else None)
        deployed_tie_break = ({"model": default_secondary["model_id"],
                               "provider": default_secondary["provider_name"]}
                              if default_secondary else None)
        top = pool_primary_lane(conn(), user["id"]) if user else None
        if top is not None:
            next_run = {"model": top["model"], "provider": top["provider"],
                        "source": "user-pool"}
            failover = [{"model": c["model"], "provider": c["provider"]}
                        for c in pool.next_candidates(conn(), user["id"], {top["model"]})]
        elif deployed_default is not None:
            next_run = {**deployed_default, "source": "deployed-default"}
            failover = []
        else:
            next_run, failover = None, []
        return jsonify({
            "ranked": out,
            "pool": pool.pool_view(conn(), user["id"]) if user else [],
            "paper_url": setting("paper_url", ""),
            "lane_authority": {
                "deployed_default": deployed_default,
                "deployed_tie_break": deployed_tie_break,
                "next": next_run,
                "failover": failover,
                "note": "an untouched Detect names no lane: your pool's first entry answers, "
                        "then the models you ranked behind it; with no pool the admin's deployed "
                        "default answers. A model marked down is neither first nor a rescue.",
            },
            "note": f"full captures (>= {MEASURED_MIN_TEXTS} texts) ranked first, then probes; "
                    f"each model at its own saved band, wrong-first then coverage",
        })

    # ---- detection history ------------------------------------------------

    @app.route("/detections", methods=["GET"])
    def list_detections():
        limit = min(200, max(1, int(request.args.get("limit", 50))))
        user = auth.current_user()
        # Admins audit everyone; a signed-in user sees only their own rows. With no session
        # (local single-user build, test client) there is no owner filter, so history stays
        # visible exactly as it was before accounts existed.
        if user is not None and user["role"] != "admin":
            rows = conn().execute(
                """SELECT id, created_at, document_id, text_length, classification, ai_probability,
                          confidence, method, review_required, deciding_lane, primary_model,
                          secondary_model, latency_ms, http_status, error_code, user_id
                   FROM detections WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user["id"], limit)
            ).fetchall()
        else:
            rows = conn().execute(
                """SELECT id, created_at, document_id, text_length, classification, ai_probability,
                          confidence, method, review_required, deciding_lane, primary_model,
                          secondary_model, latency_ms, http_status, error_code, user_id
                   FROM detections ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/detections/<int:did>", methods=["GET"])
    def get_detection(did: int):
        row = conn().execute("SELECT * FROM detections WHERE id = ?", (did,)).fetchone()
        if row is None:
            return jsonify(error="no such detection"), 404
        user = auth.current_user()
        # A user may open only their own detection; anyone else's row reads as "not found"
        # so the id space is not enumerable across accounts.
        if user is not None and user["role"] != "admin" and row["user_id"] != user["id"]:
            return jsonify(error="no such detection"), 404
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
        return jsonify(d)

    # ---- benchmark data (measured, never invented) ------------------------

    @app.route("/benchmark/runs", methods=["GET"])
    def benchmark_runs():
        rows = conn().execute(
            """SELECT r.*, COUNT(b.id) AS n_results
               FROM benchmark_runs r
               LEFT JOIN benchmark_results b ON b.run_id = r.id
               GROUP BY r.id ORDER BY r.id"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d.pop("params_json") or "{}")
            out.append(d)
        return jsonify(out)

    @app.route("/benchmark/runs/<int:rid>", methods=["GET"])
    def benchmark_run(rid: int):
        run = conn().execute("SELECT * FROM benchmark_runs WHERE id = ?", (rid,)).fetchone()
        if run is None:
            return jsonify(error="no such run"), 404
        d = dict(run)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        d["results"] = [dict(r) for r in conn().execute(
            "SELECT text_id, label, ok, error_code, ai_probability, confidence "
            "FROM benchmark_results WHERE run_id = ? ORDER BY text_id", (rid,)).fetchall()]
        d["summary"] = summarize_run(d["results"])
        return jsonify(d)

    @app.route("/benchmark/models", methods=["GET"])
    def benchmark_models():
        """One row per measured model: separation stats under the shipped single-lane rule."""
        primary, _ = default_models()
        ai_cut = float((primary["ai_prob_threshold"] if primary and primary["ai_prob_threshold"] is not None
                        else setting("ai_prob_threshold", "0.69") or 0.69))
        hu_cut = float((primary["human_prob_threshold"] if primary and primary["human_prob_threshold"] is not None
                        else setting("human_prob_threshold", "0.40") or 0.40))
        runs = conn().execute("SELECT * FROM benchmark_runs ORDER BY id").fetchall()
        out = []
        for run in runs:
            results = [dict(r) for r in conn().execute(
                "SELECT text_id, label, ok, error_code, ai_probability, confidence "
                "FROM benchmark_results WHERE run_id = ?", (run["id"],)).fetchall()]
            s = summarize_run(results, ai_cut, hu_cut)
            s.update({
                "run_id": run["id"], "tag": run["tag"], "model": run["model"],
                "provider": run["provider"], "corpus": run["corpus"], "prompt": run["prompt"],
                "params": json.loads(run["params_json"] or "{}"),
                "ai_cut": ai_cut, "human_cut": hu_cut,
            })
            out.append(s)
        return jsonify(out)

    @app.route("/benchmark/catalog", methods=["GET"])
    def benchmark_catalog():
        provider = request.args.get("provider")
        sql = "SELECT provider, model_id, probe_status, latency_ms, probed_at FROM catalog_models"
        args: list = []
        if provider:
            sql += " WHERE provider = ?"
            args.append(provider)
        sql += " ORDER BY provider, probe_status, model_id"
        rows = conn().execute(sql, args).fetchall()
        grouped: dict[str, dict[str, list]] = {}
        for r in rows:
            grouped.setdefault(r["provider"], {}).setdefault(r["probe_status"], []).append({
                "model": r["model_id"], "latency_ms": r["latency_ms"], "probed_at": r["probed_at"]})
        return jsonify(grouped)

    @app.route("/benchmark/import", methods=["POST"])
    def benchmark_import():
        from .seed import import_benchmarks, import_catalog
        runs = import_benchmarks(conn())
        catalog = import_catalog(conn())
        # Without this the whole import is rolled back when the request's connection
        # closes: the route happily returns the list of runs it "wrote".
        conn().commit()
        return jsonify(benchmark_runs=runs, catalog=catalog)

    # ---- n8n status -------------------------------------------------------

    @app.route("/n8n/status", methods=["GET"])
    def n8n_status_route():
        return jsonify(n8n_status())

    @app.route("/n8n/executions", methods=["GET"])
    def n8n_executions():
        limit = min(50, max(1, int(request.args.get("limit", 10))))
        st = n8n_status()
        if not st.get("db_readable"):
            return jsonify(st), 503
        try:
            c = sqlite3.connect(N8N_DB, timeout=5.0)
            c.row_factory = sqlite3.Row
            rows = c.execute(
                """SELECT id, status, mode, startedAt, stoppedAt, workflowId
                   FROM execution_entity WHERE workflowId = ?
                   ORDER BY id DESC LIMIT ?""", (WORKFLOW_ID, limit)).fetchall()
            c.close()
        except sqlite3.Error as exc:
            return jsonify(ok=False, error=str(exc)), 503
        return jsonify({"workflow_id": WORKFLOW_ID, "count": len(rows),
                        "executions": [dict(r) for r in rows],
                        "note": "rows this gateway created are purged continuously; "
                                "an empty list with a recent detection means the purge is working"})

    @app.route("/", methods=["GET"])
    def index():
        return jsonify({
            "backend": "flask",
            "endpoints": ["/auth/login", "/auth/register", "/auth/logout", "/auth/me",
                          "/auth/password", "/auth/theme",
                          "/gateway-health", "/detect", "/providers", "/keys", "/models",
                          "/models/<id>/assign-lane", "/providers/<id>/keys",
                          "/providers/<id>/probe", "/my/keys", "/my/keys/<id>/verify",
                          "/config", "/ranking", "/settings", "/detections", "/benchmark/runs",
                          "/benchmark/models", "/benchmark/catalog", "/benchmark/import",
                          "/n8n/status", "/n8n/executions"],
            "db": str(db.DB_PATH),
            "user": auth.current_user(),
        })


def clear_other_lane(c: sqlite3.Connection, keep_id: int, lane: str) -> None:
    c.execute("UPDATE models SET lane = NULL, role = 'either', updated_at = datetime('now') "
              "WHERE lane = ? AND id != ?", (lane, keep_id))
    c.commit()


# An unscored row means one of two completely different things, and the published table used to
# call both of them "parse failure": the gateway never produced an answer, or the model answered
# and its text could not be turned into a score. `error_code` separates them -- for an in-body
# provider error the harness stores the upstream status itself ("503" arrived on an HTTP 200,
# because OpenRouter does not relay its upstream's code), otherwise it stores its own label.
NO_ANSWER_CODES = frozenset({"exception", "empty_response", "no_choices", "null_content"})


def row_never_answered(r: dict) -> bool:
    code = str(r.get("error_code") or "")
    if code.startswith("http_") or code in NO_ANSWER_CODES:
        return True
    return code.isdigit() and int(code) >= 400


def median(sorted_probs: list) -> float | None:
    """True median: for an even-length list the mean of the two middle values. The panel
    and the JS analyzers quote the same statistic, so they have to compute it the same way
    -- see test_run_summary_median_is_the_standard_median."""
    n = len(sorted_probs)
    if not n:
        return None
    mid = n // 2
    return sorted_probs[mid] if n % 2 else (sorted_probs[mid - 1] + sorted_probs[mid]) / 2


def summarize_run(results: list[dict], ai_cut: float = 0.69, hu_cut: float = 0.40) -> dict:
    scored = [r for r in results if r.get("ai_probability") is not None]
    unscored = [r for r in results if r.get("ai_probability") is None]
    n_no_answer = sum(1 for r in unscored if row_never_answered(r))
    ai = [r for r in scored if r["label"] == "AI"]
    hu = [r for r in scored if r["label"] == "HUMAN"]
    correct = wrong = deferred = 0
    for r in scored:
        p = r["ai_probability"]
        if p >= ai_cut:
            verdict = "AI"
        elif p <= hu_cut:
            verdict = "HUMAN"
        else:
            deferred += 1
            continue
        if verdict == r["label"]:
            correct += 1
        else:
            wrong += 1
    probs = sorted(r["ai_probability"] for r in scored)
    ai_probs = sorted(r["ai_probability"] for r in ai)
    hu_probs = sorted(r["ai_probability"] for r in hu)
    return {
        "n_rows": len(results),
        "n_scored": len(scored),
        # Two different failures, deliberately not merged: n_no_answer is a gateway/upstream
        # problem (recoverable by retry, says nothing about the model), n_parse_failed is the
        # model answering in a form that carries no score. Their sum is n_rows - n_scored.
        "n_no_answer": n_no_answer,
        "n_parse_failed": len(unscored) - n_no_answer,
        "correct": correct, "wrong": wrong, "deferred": deferred,
        "accuracy_over_scored": round(correct / len(scored), 4) if scored else None,
        # Denominator = every text in the run, parse failures included. Dividing only by the
        # answered texts lets a model that goes silent on real text look better than one that
        # answers: inclusionai/ling-3.0-flash-sante:free posted 0.774 while returning nothing
        # parseable for 13 of the 24 human texts.
        "accuracy_over_corpus": round(correct / len(results), 4) if results else None,
        "median_probability": median(probs),
        "median_ai_text": median(ai_probs),
        "median_human_text": median(hu_probs),
        "min_ai_text": ai_probs[0] if ai_probs else None,
        "max_human_text": hu_probs[-1] if hu_probs else None,
        "separation": round(median(ai_probs) - median(hu_probs), 4)
                      if ai_probs and hu_probs else None,
    }


def n8n_status() -> dict:
    url = setting("n8n_url", "http://localhost:5678") if "conn" in g else "http://localhost:5678"
    out: dict = {"n8n_url": url, "workflow_id": WORKFLOW_ID}
    if engine.native_enabled():
        # A local install scores text in this process, so this server is not on the request path
        # and its absence says nothing about whether detections work. Probing it would spend five
        # seconds of a socket timeout to print a red `reachable: false` next to an app that just
        # answered a detect -- so the status says which engine is in use and stops there.
        out["on_request_path"] = False
        out["note"] = (f"this process answers /detect with the {engine.ENGINE_NAME} engine; the "
                       "n8n workflow is not contacted")
        return out
    out["on_request_path"] = True
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=5) as resp:
            out["healthz"] = resp.status
            out["reachable"] = resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        out["reachable"] = False
        out["healthz_error"] = str(exc)
    try:
        c = sqlite3.connect(N8N_DB, timeout=5.0)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT id, name, active, activeVersionId, updatedAt FROM workflow_entity WHERE id = ?",
            (WORKFLOW_ID,)).fetchone()
        out["db_readable"] = True
        if row:
            out["workflow"] = dict(row)
            out["published"] = bool(row["activeVersionId"])
            out["webhook_registered"] = bool(row["active"]) and bool(row["activeVersionId"])
            n = c.execute("SELECT COUNT(*) AS n FROM execution_entity WHERE workflowId = ?",
                          (WORKFLOW_ID,)).fetchone()["n"]
            out["execution_rows_present"] = n
        else:
            out["workflow"] = None
            out["published"] = False
            out["webhook_registered"] = False
        c.close()
    except sqlite3.Error as exc:
        out["db_readable"] = False
        out["db_error"] = str(exc)
    return out


app = create_app()


def main() -> None:
    # Started here, not at import: a test client or `flask run` must not spin up a
    # thread that deletes rows from the live n8n database.
    purger.start()
    # Same rule, different hazard: the discovery worker makes real provider calls on a
    # user's key, so only a serving process may run one.
    discovery.worker.start()
    print("detector backend (flask)")
    print("  listen      =", f"http://{HOST}:{PORT}")
    print("  db          =", db.DB_PATH)
    print("  purge       =", "enabled" if purger.enabled else f"DISABLED ({purger.error})")
    print("  discovery   =", "enabled" if discovery.worker.running else "DISABLED")
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
