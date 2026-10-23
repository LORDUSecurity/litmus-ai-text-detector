"""SQLite store for the detector: providers, API keys, models + tuned params,
detection history, and the measured benchmark data.

Keys are encrypted at rest with Fernet. The master key lives in a gitignored file
next to the database, never in the repo and never in a response body.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"
DB_PATH = Path(os.environ.get("DETECTOR_DB", DATA_DIR / "detector.sqlite3"))
SECRET_PATH = Path(os.environ.get("DETECTOR_SECRET", DATA_DIR / "backend-secret.key"))

SCHEMA_VERSION = 7
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    -- v5: an account is addressed by e-mail, like every other signup. Both columns are
    -- nullable/defaults because _migrate() has to add them to a table that already has rows
    -- in them, and SQLite refuses ADD COLUMN with a UNIQUE constraint. The uniqueness index
    -- is NOT declared here for the same reason as idx_detections_user below: on a deployed
    -- database `users` already exists without an `email` column, so a SCHEMA-time index on it
    -- aborts executescript before _migrate can add the column -- the app would not boot.
    -- Creating it in _migrate gives a fresh and an upgraded database the same rule, and NULLs
    -- are distinct to a SQLite UNIQUE index, so several legacy rows with no e-mail are allowed;
    -- every account created through /auth/register has one.
    email         TEXT,
    display_name  TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
    theme         TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('dark','light')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_api_keys (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    -- The gateway this owner's box actually lives at, for a self-hosted provider only. The
    -- provider row holds the deployment's default endpoint; a shared row cannot be the answer
    -- when two owners of the same router run it on different hosts. NULL = use the provider's.
    base_url    TEXT,
    label       TEXT NOT NULL DEFAULT 'my key',
    key_cipher  BLOB NOT NULL,
    key_hint    TEXT NOT NULL DEFAULT '',
    verified    INTEGER NOT NULL DEFAULT 0,
    last_verified_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, provider_id, label)
);

CREATE TABLE IF NOT EXISTS providers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL DEFAULT '',
    base_url    TEXT NOT NULL,
    auth_header TEXT NOT NULL DEFAULT 'Authorization',
    auth_prefix TEXT NOT NULL DEFAULT 'Bearer ',
    free_tier   INTEGER NOT NULL DEFAULT 1,
    self_hosted INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,
    key_hint    TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS models (
    id            INTEGER PRIMARY KEY,
    provider_id   INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    model_id      TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'either'
                  CHECK (role IN ('primary','secondary','either')),
    temperature   REAL NOT NULL DEFAULT 0.1,
    top_p         REAL NOT NULL DEFAULT 0.9,
    max_tokens    INTEGER NOT NULL DEFAULT 0,
    timeout_ms    INTEGER NOT NULL DEFAULT 90000,
    ai_prob_threshold            REAL,
    ai_conf_threshold            REAL,
    human_prob_threshold         REAL,
    human_conf_threshold         REAL,
    secondary_decision_threshold REAL,
    lane          TEXT CHECK (lane IN ('primary','secondary') OR lane IS NULL),
    notes         TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    measured      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider_id, model_id)
);

CREATE TABLE IF NOT EXISTS detections (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    document_id       TEXT,
    text_fingerprint  TEXT,
    text_length       INTEGER,
    text              TEXT,
    classification    TEXT,
    ai_probability    REAL,
    confidence        REAL,
    method            TEXT,
    review_required   INTEGER,
    deciding_lane     TEXT,
    primary_model     TEXT,
    secondary_model   TEXT,
    latency_ms        INTEGER,
    http_status       INTEGER,
    error_code        TEXT,
    warnings          TEXT,
    payload_json      TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    id           INTEGER PRIMARY KEY,
    tag          TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT 'nvidia',
    model        TEXT NOT NULL,
    corpus       TEXT NOT NULL DEFAULT 'hc3-48',
    prompt       TEXT NOT NULL DEFAULT 'direct',
    params_json  TEXT NOT NULL DEFAULT '{}',
    n_rows       INTEGER NOT NULL DEFAULT 0,
    source_file  TEXT NOT NULL DEFAULT '',
    imported_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tag, model, corpus)
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
    text_id        TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    ok             INTEGER NOT NULL DEFAULT 1,
    error_code     TEXT,
    ai_probability REAL,
    confidence     REAL
);

CREATE TABLE IF NOT EXISTS catalog_models (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    listed      INTEGER NOT NULL DEFAULT 1,
    probe_status TEXT NOT NULL DEFAULT 'unknown',
    probe_note  TEXT NOT NULL DEFAULT '',
    latency_ms  INTEGER,
    probed_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, model_id)
);

-- v4: one user's ordered "using pool". position is the user's own ranking and is
-- authoritative; `source` records who put the row there ('user' dragged it in, 'auto'
-- discovery added it) so an auto-added row can be demoted without ever overtaking a
-- position the user set by hand. There is no global pool: two users bring different keys.
CREATE TABLE IF NOT EXISTS user_model_pool (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider  TEXT NOT NULL,
    model_id  TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0,
    source    TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user','auto')),
    added_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, provider, model_id)
);

-- v4: does this model still answer? Fed from evidence.models[] on every detection, so a
-- lane that starts 404ing or timing out is measured from real traffic rather than polled.
-- Keyed by (provider, model_id) rather than models.id: the whole point is to notice a
-- model that has no tuned row yet, and a delisted row may be deleted while its history
-- of failures still matters.
CREATE TABLE IF NOT EXISTS model_health (
    id                   INTEGER PRIMARY KEY,
    provider             TEXT NOT NULL,
    model_id             TEXT NOT NULL,
    state                TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok','degraded','down')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_ok_at           TEXT,
    last_failure_at      TEXT,
    last_error           TEXT NOT NULL DEFAULT '',
    UNIQUE (provider, model_id)
);

-- v4: an auto-discovery run, so the UI can poll progress instead of holding a request open
-- for the minutes a catalogue fetch + screen costs. One row per (user, provider) attempt.
CREATE TABLE IF NOT EXISTS discovery_jobs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','done','failed')),
    stage       TEXT NOT NULL DEFAULT '',
    n_listed    INTEGER NOT NULL DEFAULT 0,
    n_screened  INTEGER NOT NULL DEFAULT 0,
    n_usable    INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    started_at  TEXT,
    finished_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_detections_created ON detections(id DESC);
CREATE INDEX IF NOT EXISTS idx_results_run ON benchmark_results(run_id);
CREATE INDEX IF NOT EXISTS idx_keys_provider ON api_keys(provider_id);
CREATE INDEX IF NOT EXISTS idx_user_keys_user ON user_api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_pool_user ON user_model_pool(user_id, position);
CREATE INDEX IF NOT EXISTS idx_health_model ON model_health(model_id);
CREATE INDEX IF NOT EXISTS idx_discovery_user ON discovery_jobs(user_id, id DESC);
"""


def _load_fernet() -> Fernet:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return Fernet(SECRET_PATH.read_bytes().strip())
    key = Fernet.generate_key()
    fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return Fernet(key)


_fernet: Fernet | None = None


def fernet() -> Fernet:
    global _fernet
    with _lock:
        if _fernet is None:
            _fernet = _load_fernet()
    return _fernet


def encrypt_key(plain: str) -> tuple[bytes, str]:
    """Return (cipher, hint). The hint is the only plaintext-derived value ever stored."""
    hint = f"{plain[:6]}...({len(plain)} chars)" if plain else ""
    return fernet().encrypt(plain.encode("utf-8")), hint


def decrypt_key(cipher: bytes) -> str:
    try:
        return fernet().decrypt(cipher).decode("utf-8")
    except InvalidToken as exc:  # secret file rotated out from under the DB
        raise ValueError("stored key cannot be decrypted with the current secret") from exc


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def session():
    """`with sqlite3.Connection` commits but never CLOSES, which on Windows leaves the
    file locked. Every multi-statement block goes through this instead."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM settings WHERE key = 'schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply the changes CREATE TABLE IF NOT EXISTS cannot make to a table that already exists."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)")}
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN last_used_at TEXT")
    # v3: per-user history. An existing detections table predates accounts, so its rows
    # have no owner; add the column and leave them NULL (visible to admins only).
    dcols = {r["name"] for r in conn.execute("PRAGMA table_info(detections)")}
    if "user_id" not in dcols:
        conn.execute("ALTER TABLE detections ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    # v7: the text a run scored. History used to keep a fingerprint and a length, which made a
    # saved verdict impossible to re-read: the sheet could show the number but never the writing
    # the number was about, so the model's own quotes had nowhere to be painted back onto. Rows
    # saved before this stay NULL, and the sheet says "this run predates text storage" rather
    # than inventing an empty one.
    if "text" not in dcols:
        conn.execute("ALTER TABLE detections ADD COLUMN text TEXT")
    # The index lives here, not in SCHEMA: on a pre-account database the detections table
    # exists without user_id, so a SCHEMA-time index on that column would abort executescript
    # before _migrate ever added it. Creating it after the ALTER covers both fresh and upgraded DBs.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detections_user ON detections(user_id)")
    # v5: accounts are addressed by e-mail. ALTER can carry a default but not a UNIQUE, so the
    # constraint is the index -- and it has to be created *after* the column for the same reason
    # the detections index is: on an older database SCHEMA-time would abort before _migrate ran.
    # Existing accounts keep their username as a working sign-in identifier (auth resolves either),
    # so nobody is locked out by the upgrade; email stays NULL until they are edited or recreated.
    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "email" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "display_name" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    # v6: a key for a self-hosted gateway carries the address of the owner's own box. Both halves
    # are NULL-by-default additions, so an existing row keeps behaving exactly as before: no
    # endpoint on a key means "the provider's", and a provider nobody flagged means "common",
    # which is the guard a caller-supplied endpoint used to be refused under.
    if "base_url" not in {r["name"] for r in conn.execute("PRAGMA table_info(user_api_keys)")}:
        conn.execute("ALTER TABLE user_api_keys ADD COLUMN base_url TEXT")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(providers)")}
    if "self_hosted" not in pcols:
        conn.execute("ALTER TABLE providers ADD COLUMN self_hosted INTEGER NOT NULL DEFAULT 0")
        # One-shot, while the column is brand new and every value is still the default: an existing
        # database predates the flag, and its local gateway row is the case the feature exists for.
        # Keyed on the loopback address rather than on the provider name, because "the endpoint is
        # a box on this machine" is what self-hosted means -- an OmniRoute installed under another
        # name gets it too, and a provider the admin flagged by hand is not overwritten, since
        # seed.py leaves this column out of its boot-time rewrite.
        conn.execute("""UPDATE providers SET self_hosted = 1
                        WHERE base_url LIKE 'http://127.0.0.1%' OR base_url LIKE 'http://localhost%'""")
    conn.execute("UPDATE settings SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in ("users", "providers", "api_keys", "models", "detections", "benchmark_runs", "catalog_models"):
        out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return out


# --------------------------------------------------------------------------- users

def hash_password(plain: str) -> str:
    from werkzeug.security import generate_password_hash
    return generate_password_hash(plain)


def check_password(plain: str, hashed: str) -> bool:
    from werkzeug.security import check_password_hash
    try:
        return check_password_hash(hashed, plain)
    except Exception:
        return False


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str = "user",
                email: str | None = None, display_name: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, email, display_name) VALUES (?,?,?,?,?)",
        (username, hash_password(password), role, email, display_name),
    )
    return int(cur.lastrowid)


def get_user_by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()


def get_user_by_identifier(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    """Resolve a sign-in identifier: e-mail first, then the legacy username.

    The username column stays writable and stays a way in on purpose. Every account created
    before this change has only one, and so does every deploy/verify tool that logs in as
    `admin` -- dropping that path would lock the operator out of a deployed instance with a
    database the UI can no longer read.
    """
    ident = (identifier or "").strip()
    if not ident:
        return None
    row = get_user_by_email(conn, ident)
    return row if row is not None else conn.execute(
        "SELECT * FROM users WHERE username = ?", (ident,)).fetchone()


def derive_username(conn: sqlite3.Connection, email: str) -> str:
    """A username from an e-mail's local part, so the legacy column and any code that reads it
    (the admin user list, the history owner, `admin` in the deploy tools) still has something
    meaningful. The charset is the historical username rule -- [A-Za-z0-9_.-], 3-32 chars -- so a
    derived handle is always a value that rule would have accepted; anything else is dropped, and
    a taken stem gains a numeric suffix."""
    stem = re.sub(r"[^A-Za-z0-9_.-]", "", (email or "").split("@")[0])[:24] or "user"
    if len(stem) < 3:
        stem = ("user" + stem)[:32]
    candidate, n = stem, 1
    while get_user_by_name(conn, candidate) is not None:
        n += 1
        candidate = f"{stem}{n}"
    return candidate


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def owner_user(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The account a single-user local install signs in as: the admin row, oldest first."""
    return conn.execute(
        "SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()


def seed_admin(conn: sqlite3.Connection) -> str | None:
    """Ensure at least one admin exists. Credentials come from the environment so a
    hosted deploy never ships a guessable default; the local launcher sets them too.
    Returns the username seeded, or None if an admin already existed."""
    n = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
    if n:
        return None
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin")
    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower() or None
    if get_user_by_name(conn, username) is None:
        create_user(conn, username, password, "admin", email=email,
                    display_name=os.environ.get("ADMIN_NAME", "") or username)
    else:
        conn.execute("UPDATE users SET role = 'admin' WHERE username = ?", (username,))
    return username


# ------------------------------------------------------------------- user-owned keys (BYOK)

def add_user_key(conn: sqlite3.Connection, user_id: int, provider_id: int,
                 plain: str, label: str = "my key", base_url: str | None = None) -> int:
    """Store one of the caller's own credentials. `base_url` is the endpoint the *owner* named,
    and only reaches this function after the route has checked that the provider is self-hosted
    and the URL is diallable -- a NULL keeps the provider row's endpoint, which is the common case
    (NVIDIA, OpenRouter and Gemini each have exactly one)."""
    cipher, hint = encrypt_key(plain)
    label = label.strip()[:80] or "my key"
    endpoint = (base_url or "").strip() or None
    cur = conn.execute(
        """INSERT INTO user_api_keys (user_id, provider_id, label, base_url, key_cipher, key_hint)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, provider_id, label) DO UPDATE SET
             key_cipher = excluded.key_cipher, key_hint = excluded.key_hint,
             base_url = excluded.base_url,
             verified = 0, last_verified_at = NULL, created_at = datetime('now')""",
        (user_id, provider_id, label, endpoint, cipher, hint),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM user_api_keys WHERE user_id = ? AND provider_id = ? AND label = ?",
                       (user_id, provider_id, label)).fetchone()
    return int(row["id"]) if row else int(cur.lastrowid)


def user_key_public(row: sqlite3.Row) -> dict:
    """Never carries key_cipher: a hint and a verified flag are all the browser gets."""
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "label": row["label"],
        "base_url": row["base_url"],
        "key_hint": row["key_hint"],
        "verified": bool(row["verified"]),
        "last_verified_at": row["last_verified_at"],
        "created_at": row["created_at"],
    }


def list_user_keys(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT k.*, p.name AS provider_name, p.base_url AS provider_base_url
           FROM user_api_keys k JOIN providers p ON p.id = k.provider_id
           WHERE k.user_id = ? ORDER BY k.provider_id, k.id""",
        (user_id,),
    ).fetchall()


def get_user_key(conn: sqlite3.Connection, key_id: int, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM user_api_keys WHERE id = ? AND user_id = ?", (key_id, user_id)
    ).fetchone()


def delete_user_key(conn: sqlite3.Connection, key_id: int, user_id: int) -> int:
    cur = conn.execute("DELETE FROM user_api_keys WHERE id = ? AND user_id = ?", (key_id, user_id))
    conn.commit()
    return cur.rowcount


def set_user_key_verified(conn: sqlite3.Connection, key_id: int, user_id: int, verified: bool) -> None:
    conn.execute(
        """UPDATE user_api_keys SET verified = ?, last_verified_at = datetime('now')
           WHERE id = ? AND user_id = ?""",
        (1 if verified else 0, key_id, user_id),
    )
    conn.commit()


def user_key_endpoints(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Every endpoint this caller's own keys name. A detection may only be dialled at a host the
    deployment lists or one its caller has saved a credential for, and the second half is answered
    from here -- see `app.key_hosts`."""
    return [r["base_url"] for r in conn.execute(
        """SELECT DISTINCT base_url FROM user_api_keys
           WHERE user_id = ? AND base_url IS NOT NULL AND base_url <> ''""",
        (user_id,),
    )]


def user_key_row_for_provider(conn: sqlite3.Connection, user_id: int,
                              provider_id: int) -> sqlite3.Row | None:
    """The same pick user_key_for_provider makes, but the whole row: a key that names its own
    endpoint has to be dialled there, so the caller needs more than the credential."""
    return conn.execute(
        """SELECT * FROM user_api_keys
           WHERE user_id = ? AND provider_id = ?
           ORDER BY verified DESC, id DESC LIMIT 1""",
        (user_id, provider_id),
    ).fetchone()


def user_key_for_endpoint(conn: sqlite3.Connection, user_id: int, base_url: str) -> str | None:
    """The caller's own key that was saved FOR this exact endpoint.

    `user_key_for_provider` finds a key by provider, which is right while a provider and its
    endpoint are the same thing. They are not, once a self-hosted gateway carries per-key
    addresses: two owners of the omniroute row point at two boxes, and a lane aimed at
    gateway.example.net must not travel with the key belonging to a neighbour's 127.0.0.1.
    """
    if user_id is None or not base_url:
        return None
    row = conn.execute(
        """SELECT key_cipher FROM user_api_keys
           WHERE user_id = ? AND base_url = ?
           ORDER BY verified DESC, id DESC LIMIT 1""",
        (user_id, base_url.strip().rstrip("/") or None),
    ).fetchone()
    if not row:
        return None
    try:
        return decrypt_key(row["key_cipher"])
    except ValueError:
        return None


def user_key_for_provider(conn: sqlite3.Connection, user_id: int, provider_id: int) -> str | None:
    """The caller's own decrypted key for a provider, preferring a verified one.

    This is the BYOK auto-pick: a user who proved a key works should not have to select it
    again per request. Verified-first, then newest, so a stale unverified key never shadows
    one the user already checked. Returns None when the user has no key for that provider,
    and the caller falls back to the admin's stored credential.
    """
    row = user_key_row_for_provider(conn, user_id, provider_id)
    if not row:
        return None
    try:
        return decrypt_key(row["key_cipher"])
    except ValueError:
        return None


# ------------------------------------------------------- per-user model pool (v4)

def pool_list(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """The caller's using-pool in stored order. Order is re-derived by pool.rerank();
    this is the raw read."""
    return conn.execute(
        """SELECT p.*, h.state AS health_state, h.consecutive_failures AS health_failures,
                  h.last_error AS health_error, h.last_ok_at AS health_last_ok_at
           FROM user_model_pool p
           LEFT JOIN model_health h ON h.model_id = p.model_id AND h.provider = p.provider
           WHERE p.user_id = ? ORDER BY p.position, p.id""",
        (user_id,),
    ).fetchall()


def pool_add(conn: sqlite3.Connection, user_id: int, provider: str, model_id: str,
             source: str = "user") -> None:
    """Append to the end of the caller's pool. An existing row is left where the user put
    it: re-adding a model must not silently reshuffle an order they set by hand."""
    nxt = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS n FROM user_model_pool WHERE user_id = ?",
                       (user_id,)).fetchone()["n"]
    conn.execute(
        """INSERT INTO user_model_pool (user_id, provider, model_id, position, source)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, provider, model_id) DO NOTHING""",
        (user_id, provider, model_id, nxt, source),
    )


def pool_remove(conn: sqlite3.Connection, user_id: int, provider: str, model_id: str) -> int:
    cur = conn.execute(
        "DELETE FROM user_model_pool WHERE user_id = ? AND provider = ? AND model_id = ?",
        (user_id, provider, model_id),
    )
    return cur.rowcount


def pool_set_positions(conn: sqlite3.Connection, user_id: int, ordered: list[tuple[str, str]]) -> None:
    """Write an explicit order. `ordered` is [(provider, model_id), ...] in the wanted
    sequence; anything the caller's own pool holds that is absent from the list keeps its
    row and is appended after, so a drag that names only part of the pool cannot delete
    the rest."""
    seen = set()
    for i, (prov, mid) in enumerate(ordered):
        conn.execute(
            """UPDATE user_model_pool SET position = ?, source = 'user'
               WHERE user_id = ? AND provider = ? AND model_id = ?""",
            (i, user_id, prov, mid),
        )
        seen.add((prov, mid))
    tail = len(ordered)
    for row in conn.execute(
            "SELECT provider, model_id FROM user_model_pool WHERE user_id = ? ORDER BY position, id",
            (user_id,)).fetchall():
        if (row["provider"], row["model_id"]) in seen:
            continue
        conn.execute(
            "UPDATE user_model_pool SET position = ? WHERE user_id = ? AND provider = ? AND model_id = ?",
            (tail, user_id, row["provider"], row["model_id"]),
        )
        tail += 1


def health_get(conn: sqlite3.Connection, provider: str, model_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM model_health WHERE provider = ? AND model_id = ?", (provider, model_id)
    ).fetchone()


def health_note(conn: sqlite3.Connection, provider: str, model_id: str, ok: bool,
                error: str = "", state_down_after: int = 3) -> str:
    """Record one real answer (or non-answer) and return the resulting state.

    A success clears the streak -- health is "is it answering *now*", not a lifetime
    record, so a model that recovers stops being demoted without anyone editing a table.
    `state_down_after` is the streak length at which a model is called down; one failure
    only degrades it, because a single 504 on a congested free pool is not a delisting.
    """
    if ok:
        conn.execute(
            """INSERT INTO model_health (provider, model_id, state, consecutive_failures, last_ok_at, last_error)
               VALUES (?,?, 'ok', 0, datetime('now'), '')
               ON CONFLICT(provider, model_id) DO UPDATE SET
                 state = 'ok', consecutive_failures = 0,
                 last_ok_at = datetime('now'), last_error = ''""",
            (provider, model_id),
        )
        return "ok"
    conn.execute(
        """INSERT INTO model_health (provider, model_id, state, consecutive_failures,
                                     last_failure_at, last_error)
           VALUES (?,?, 'degraded', 1, datetime('now'), ?)
           ON CONFLICT(provider, model_id) DO UPDATE SET
             consecutive_failures = consecutive_failures + 1,
             last_failure_at = datetime('now'),
             last_error = excluded.last_error,
             state = CASE WHEN consecutive_failures + 1 >= ? THEN 'down' ELSE 'degraded' END""",
        (provider, model_id, str(error)[:400], state_down_after),
    )
    row = conn.execute(
        "SELECT state FROM model_health WHERE provider = ? AND model_id = ?", (provider, model_id)
    ).fetchone()
    return row["state"] if row else "degraded"


def discovery_job(conn: sqlite3.Connection, user_id: int | None, provider: str) -> int:
    cur = conn.execute(
        "INSERT INTO discovery_jobs (user_id, provider, status, stage) VALUES (?,?, 'queued', 'queued')",
        (user_id, provider),
    )
    return int(cur.lastrowid)


def discovery_update(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    allowed = {"status", "stage", "n_listed", "n_screened", "n_usable", "error",
               "started_at", "finished_at"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"discovery_update: unknown field {k!r}")
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    conn.execute(f"UPDATE discovery_jobs SET {', '.join(sets)} WHERE id = ?", (*vals, job_id))


