"""Flask backend for the AI text detector.

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

from . import auth, db, extract
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


def model_by_id(model_id: str | None) -> sqlite3.Row | None:
    """The stored row for a model id, so its tuned parameters can follow the model.

    The Angular form always sends lane_config, which means the DB-derived lane
    branch never runs -- yet the page is still asking for the very model the DB
    row describes. Matching on model_id keeps the tuning attached to the model
    rather than to whichever code path built the lane.
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


def active_key(provider_id: int, lane: str | None = None) -> str | None:
    """One key for a provider, honouring a lane's own key and rotating over the rest.

    A key labelled "<lane> lane" serves that lane: the label is the operator saying which
    credential belongs to which lane, and ORDER BY id LIMIT 1 ignored it, so a second stored
    key was never read and its deactivate button governed nothing. Every other key is an
    interchangeable account, and OpenRouter's free cap is per account -- always taking the
    lowest id spends one account's daily quota and leaves the others idle.

    Stalest-use-first, never-used-before-stamped, so a rotation survives a restart without
    in-process state that would leak between tests and workers.
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
                   user_id: int | None = None) -> str | None:
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
    """
    want = host_of(base_url or "")
    if not want:
        row = model_by_id(model_id)
        want = host_of(row["provider_base_url"]) if row else ""
    if not want:
        return None
    for p in conn().execute("SELECT id, base_url FROM providers WHERE enabled = 1"):
        if host_of(p["base_url"]) != want:
            continue
        if user_id is not None:
            own = db.user_key_for_provider(conn(), user_id, p["id"])
            if own:
                return own
        return active_key(p["id"], lane)
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


def host_allowed(base_url: str) -> bool:
    allow = {h.strip().lower() for h in setting(
        "allowed_llm_hosts", "localhost,127.0.0.1,::1,integrate.api.nvidia.com,openrouter.ai"
    ).split(",") if h.strip()}
    return host_of(base_url) in allow


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
            "hint": "The detection engine is the n8n workflow at that URL. The packaged local app "
                    "defaults to http://localhost:5678, so it needs n8n on this machine, or an "
                    "admin has to point Settings -> detection engine at a reachable n8n.",
        }, (time.time() - t0) * 1000
    ms = (time.time() - t0) * 1000
    try:
        return status, json.loads(raw.decode("utf-8")), ms
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, {"error": "non-JSON response from n8n",
                        "detail": raw[:400].decode("utf-8", "replace")}, ms


def _lane_view(lane: dict | None) -> dict:
    """Allowlist, not a dump: only these fields of a lane's config_used are stored. The workflow's
    `configView` already refuses to echo a key value (it reports `key_source`), so this is defence
    against a future field landing there, not a fix for something that leaks today."""
    if not isinstance(lane, dict):
        return {}
    return {k: lane[k] for k in ("model", "base_url", "base_url_source", "model_source",
                                 "key_source") if lane.get(k) is not None}


def record_detection(body: dict, status: int, latency_ms: float, primary_model: str | None,
                     secondary_model: str | None, text_length: int | None = None,
                     secondary_off: bool = False) -> int:
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
           (document_id, text_fingerprint, text_length, classification, ai_probability,
            confidence, method, review_required, deciding_lane, primary_model, secondary_model,
            latency_ms, http_status, error_code, warnings, payload_json, user_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (body.get("document_id"), internal.get("text_fingerprint"),
         # A failed forward has no workflow `internal` block, so without the caller's length the row
         # renders "? chars" and the user cannot tell a 429-char paragraph from a 4-char word.
         internal.get("text_length") if internal.get("text_length") is not None else text_length,
         body.get("classification"), body.get("ai_probability"), body.get("confidence"),
         body.get("method"), 1 if body.get("review_required") else 0, cs.get("deciding_lane"),
         primary_model, secondary_model, int(latency_ms), status, body.get("error_code"),
         json.dumps(body.get("warnings") or []), json.dumps(payload), user_id),
    )
    conn().commit()
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


def register_routes(app: Flask) -> None:

    @app.route("/gateway-health", methods=["GET"])
    def gateway_health():
        primary, secondary = default_models()
        return jsonify({
            "ok": True,
            "backend": "flask",
            **purger.status(),
            "db_path": str(db.DB_PATH),
            "db_counts": db.counts(conn()),
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
                if not host_allowed(row["provider_base_url"]):
                    return lane_config_invalid(lane)
                lane_config[lane] = {"base_url": row["provider_base_url"], "model": row["model_id"]}
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
                if not host_allowed(row["provider_base_url"]):
                    return lane_config_invalid(lane)
                cfg["base_url"] = row["provider_base_url"]
            if (not secondary_off and "secondary" not in lane_config and secondary):
                # Naming only a primary does not opt the tie-break out: `lane_config.secondary`
                # absent means the workflow answers with its own default second model, so the call
                # really is a two-lane call. Describing it as single-lane made the history row claim
                # `secondary_model = NULL` for a lane that ran (and hid the collision below). The
                # packaged first boot hit both on 2026-10-16, because its default second model is
                # also the model the Ranking tab invites you to pick as the scorer.
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

        payload = {k: v for k, v in body.items() if k not in ("lane_config", "tuning")}
        payload["lane_config"] = lane_config
        if tuning:
            payload["tuning"] = tuning
        payload.setdefault("document_id", body.get("document_id"))

        url = setting("n8n_url", "http://localhost:5678").rstrip("/") + \
            setting("webhook_path", "/webhook/ai-detect-ai")
        timeout = float(tuning.get("request_timeout_ms")
                        or (primary["timeout_ms"] if primary else 90000)) / 1000.0 + 30.0
        status, resp, latency = forward(url, payload, headers, timeout)

        # record_detection fills any lane the caller did not name from the workflow's own
        # config_used, so an overridden primary does not leave the tie-break lane unattributed.
        det_id = record_detection(resp, status, latency, primary_model, secondary_model,
                                 text_length=len(payload.get("text") or ""),
                                 secondary_off=secondary_off)
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
                       free_tier, enabled, notes)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (name, b.get("label", name), base_url, b.get("auth_header", "Authorization"),
                 b.get("auth_prefix", "Bearer "), 1 if b.get("free_tier", True) else 0,
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
        for f in ("free_tier", "enabled"):
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
            with urllib.request.urlopen(req, timeout=float(b.get("timeout_seconds", 45))) as resp:
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
        label = (b.get("label") or "my key").strip()[:80] or "my key"
        kid = db.add_user_key(conn(), user["id"], prov["id"], plain, label)
        # Verify immediately if a model was supplied, so "auto-pick a working key" is honest.
        verified = False
        model_id = (b.get("model_id") or "").strip()
        if model_id and MODEL_PATTERN.match(model_id):
            res = _probe_with_key(prov, model_id, plain, float(b.get("timeout_seconds", 45)))
            verified = bool(res.get("ok"))
            db.set_user_key_verified(conn(), kid, user["id"], verified)
        row = db.get_user_key(conn(), kid, user["id"])
        d = db.user_key_public(row)
        d["provider_name"] = prov["name"]
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
        if prov is None or not host_allowed(prov["base_url"]):
            return jsonify(ok=False, error_code="HOST_NOT_ALLOWED"), 400
        try:
            key = db.decrypt_key(row["key_cipher"])
        except ValueError:
            return jsonify(ok=False, error_code="KEY_UNREADABLE"), 500
        res = _probe_with_key(prov, model_id, key, float(b.get("timeout_seconds", 45)))
        db.set_user_key_verified(conn(), kid, user["id"], bool(res.get("ok")))
        return jsonify({**res, "provider": prov["name"], "model": model_id})

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
    # /detect posts to setting("n8n_url") + setting("webhook_path"), so a scheme-less engine URL
    # ("localhost:5678") raises inside the forwarder and every detection answers 502 ENGINE_UNREACHABLE
    # until someone edits the settings table from a shell -- and the packaged local exe is exactly
    # the install where the engine URL has to be changed and there is no shell. Blank is legal for
    # all three (it falls back to the built-in default); a present value has to be usable.
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

    # ---- ranking: measured models, best first ------------------------------

    # Half the 48-text corpus. Under this a run has been sampled, not measured: it can show a
    # score, but it must not lead the table the admin points users at -- one lucky text beats
    # 47 honest ones under a pure fewest-wrong sort.
    MEASURED_MIN_TEXTS = 24

    @app.route("/ranking", methods=["GET"])
    def ranking():
        """The ranked model table that replaces the old research/benchmark panel.

        One row per measured model, scored at ITS OWN saved band (the deployed decision
        rule), not a single global cut -- a band is per model and applying another model's
        cut would misreport it. Sorted by fewest wrong, then most decided, then accuracy,
        so a model that never errs outranks one that defers less but guesses wrong. Each
        row carries whether the caller has a verified key for that endpoint, which is what
        lets the UI 'pick a working key automatically'.

        Sample size is a sorting tier, not a caption. Fewest-wrong-first made a 1-text probe
        with 0 errors outrank a 48-text capture with 1 -- and this table is the page that
        claims "the best result we found", so a lucky probe must not lead it. Rows measured
        over at least MEASURED_MIN_TEXTS texts rank among themselves first; shorter runs keep
        their scores but follow, flagged `provisional`. A run with no clean row is not a
        measurement at all and is left out rather than shown as a model with no score.
        """
        user = auth.current_user()
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
        return jsonify({
            "ranked": out,
            "paper_url": setting("paper_url", ""),
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
    print("detector backend (flask)")
    print("  listen      =", f"http://{HOST}:{PORT}")
    print("  db          =", db.DB_PATH)
    print("  purge       =", "enabled" if purger.enabled else f"DISABLED ({purger.error})")
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
