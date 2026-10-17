"""The per-user "using pool": order, health, and which model answers next.

Two questions live here and they have different answers.

*Whose order?* The user's. `user_model_pool.position` is written only by the user (a drag,
or "add to pool"), and nothing in this module ever rewrites it. Discovery may *append* a
row with `source='auto'`; it may never move one the user placed.

*Who answers next?* Not necessarily the top row. A model that has stopped answering is
demoted at read time, not by editing the user's ranking -- so when it comes back it is
where the user put it again, with no state to undo. That is the whole reason
`effective_order()` exists separately from `db.pool_list()`.

Health is measured from real traffic (`note_detection` reads the workflow's own
`evidence.models[]` on every detection), never from a poll: a poll would spend the same
free-tier budget the user's detection needs, and would report a model as up on the
strength of a one-word "ok" prompt that a delisted model can still fail differently.
"""
from __future__ import annotations

import sqlite3

from . import db

# A failure that says "busy", not "gone". Demoting a model to `down` on a rate limit would
# punish the pool for the provider's queue, and OpenRouter's free tier returns 429 for a
# whole account once its daily cap is spent -- which is a property of the key, not of the
# model, so every model on that endpoint would fall at once. Transient failures still
# count (a pool that is always busy is not usable) but they can never reach `down`.
TRANSIENT_MARKERS = (
    "429", "503", "rate_limit", "rate limit", "ratelimit", "quota", "overloaded",
    "capacity", "too_many_requests", "too many requests", "resource_exhausted", "retry",
)

# Failures that mean the model itself did not produce an answer this app can use are
# everything else. The default has to be "counted", because an unrecognised code from a
# new provider is far more likely to be a real non-response than a new spelling of "busy".

# Only `down` moves a model in the effective order. `degraded` is still reported -- the UI
# badges it and a long busy streak is worth seeing -- but it does not re-rank, for the
# reason in `effective_order`'s docstring. Two classes, not three, is what keeps a blip
# from changing which model a user's next detection pays for.
ORDER_RANK = {"ok": 0, "degraded": 0, "down": 1}


def classify(ok: bool, error_code: str | None, error_detail: str | None = None) -> str:
    """'ok' | 'transient' | 'dead' for one lane attempt."""
    if ok:
        return "ok"
    blob = f"{error_code or ''} {error_detail or ''}".lower()
    if any(m in blob for m in TRANSIENT_MARKERS):
        return "transient"
    return "dead"


def note_attempt(conn: sqlite3.Connection, provider: str, model_id: str, ok: bool,
                 error_code: str | None = None, error_detail: str | None = None,
                 down_after: int = 3) -> str:
    """Fold one lane attempt into model_health and return the new state.

    `provider` is the provider *name* (the pool's key), so a model id that exists on two
    endpoints -- OpenRouter's `:free` mirrors are the common case -- is tracked per
    endpoint rather than being marked down everywhere at once.
    """
    if not provider or not model_id:
        return "ok"
    kind = classify(ok, error_code, error_detail)
    if kind == "ok":
        return db.health_note(conn, provider, model_id, True)
    # A huge streak target is how "never call this down" is expressed: the state machine
    # stays one rule wide instead of growing a second column for "busy but alive".
    return db.health_note(conn, provider, model_id, False,
                          error=f"{error_code or 'error'}",
                          state_down_after=10**9 if kind == "transient" else down_after)


def note_detection(conn: sqlite3.Connection, body: dict, provider_for_base_url) -> list[dict]:
    """Record health for every lane that actually ran, and return the legs that failed.

    `provider_for_base_url` is supplied by the caller (app.py owns the providers table and
    `host_of`); it maps a lane's base_url to a provider name, or returns "" when the host
    is not one this app knows -- an unknown host gets no health row, because a name we
    cannot resolve is a name we cannot key on.

    `skipped` legs are left alone entirely: the tie-break lane only runs on a gray-zone
    text, so "not called" is the normal state of a healthy secondary and counting it would
    demote a model for never being asked.
    """
    out: list[dict] = []
    if not isinstance(body, dict):
        return out
    models = ((body.get("evidence") or {}).get("models")) or []
    cfg = body.get("config_used") or {}
    for leg in models:
        if not isinstance(leg, dict) or leg.get("skipped"):
            continue
        model_id = leg.get("model")
        if not model_id:
            continue
        lane = leg.get("lane") or "primary"
        base_url = ((cfg.get(lane) or {}).get("base_url")) or ""
        provider = provider_for_base_url(base_url) if base_url else ""
        kind = classify(bool(leg.get("ok")), leg.get("error_code"), leg.get("error_detail"))
        if provider:
            note_attempt(conn, provider, str(model_id), kind == "ok",
                         leg.get("error_code"), leg.get("error_detail"))
        if kind != "ok":
            out.append({"lane": lane, "model": str(model_id), "provider": provider,
                        "kind": kind, "error_code": leg.get("error_code")})
    return out


def effective_order(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """The user's order, with models that stopped answering pushed to the back.

    Stable within a health class, so the user's ranking is the tie-break everywhere and a
    recovery restores the exact previous order. `degraded` (one failure, or a busy streak)
    does NOT move: a single 504 on a congested free pool is not a delisting, and demoting
    on one blip would make the pool's order jitter between requests.
    """
    return sorted(rows, key=lambda r: (ORDER_RANK.get(r["health_state"] or "ok", 0),
                                       r["position"], r["id"]))


def pool_view(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """What the UI renders: the effective order, plus the user's own rank and why it moved."""
    rows = effective_order(db.pool_list(conn, user_id))
    out = []
    for i, r in enumerate(rows):
        state = r["health_state"] or "ok"
        out.append({
            "provider": r["provider"],
            "model": r["model_id"],
            "source": r["source"],
            "your_rank": r["position"] + 1,
            "position": i + 1,
            "health": state,
            "consecutive_failures": r["health_failures"] or 0,
            "last_error": r["health_error"] or "",
            "last_ok_at": r["health_last_ok_at"],
            "demoted": state == "down" and i + 1 != (r["position"] + 1),
        })
    return out


def next_candidates(conn: sqlite3.Connection, user_id: int, skip: set[str]) -> list[dict]:
    """Pool members worth switching to, in the order the user put them in.

    `skip` holds the model ids already tried for this detection, so a failover never
    re-calls the model that just failed and never loops. Members already marked `down` are
    excluded: the point of the health table is to stop paying for a call to a model that
    has failed three times in a row.
    """
    out = []
    for r in effective_order(db.pool_list(conn, user_id)):
        if r["model_id"] in skip:
            continue
        if (r["health_state"] or "ok") == "down":
            continue
        out.append({"provider": r["provider"], "model": r["model_id"]})
    return out


def rerank(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Re-derive the pool order after a health change and return the new view.

    Called by the routes so the caller sees the consequence of the detection it just made.
    It writes nothing: the order is a read of (health, position), which is what makes the
    user's ranking survive a model coming back.
    """
    return pool_view(conn, user_id)
