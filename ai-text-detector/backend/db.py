"""SQLite store for the detector: providers, API keys, models + tuned params,
detection history, and the measured benchmark data.

Keys are encrypted at rest with Fernet. The master key lives in a gitignored file
next to the database, never in the repo and never in a response body.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"
DB_PATH = Path(os.environ.get("DETECTOR_DB", DATA_DIR / "detector.sqlite3"))
SECRET_PATH = Path(os.environ.get("DETECTOR_SECRET", DATA_DIR / "backend-secret.key"))

SCHEMA_VERSION = 3
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

CREATE INDEX IF NOT EXISTS idx_detections_created ON detections(id DESC);
CREATE INDEX IF NOT EXISTS idx_results_run ON benchmark_results(run_id);
CREATE INDEX IF NOT EXISTS idx_keys_provider ON api_keys(provider_id);
CREATE INDEX IF NOT EXISTS idx_user_keys_user ON user_api_keys(user_id);
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
    # The index lives here, not in SCHEMA: on a pre-account database the detections table
    # exists without user_id, so a SCHEMA-time index on that column would abort executescript
    # before _migrate ever added it. Creating it after the ALTER covers both fresh and upgraded DBs.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_detections_user ON detections(user_id)")
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


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str = "user") -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
        (username, hash_password(password), role),
    )
    return int(cur.lastrowid)


def get_user_by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def seed_admin(conn: sqlite3.Connection) -> str | None:
    """Ensure at least one admin exists. Credentials come from the environment so a
    hosted deploy never ships a guessable default; the local launcher sets them too.
    Returns the username seeded, or None if an admin already existed."""
    n = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
    if n:
        return None
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin")
    if get_user_by_name(conn, username) is None:
        create_user(conn, username, password, "admin")
    else:
        conn.execute("UPDATE users SET role = 'admin' WHERE username = ?", (username,))
    return username


# ------------------------------------------------------------------- user-owned keys (BYOK)

def add_user_key(conn: sqlite3.Connection, user_id: int, provider_id: int,
                 plain: str, label: str = "my key") -> int:
    cipher, hint = encrypt_key(plain)
    cur = conn.execute(
        """INSERT INTO user_api_keys (user_id, provider_id, label, key_cipher, key_hint)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, provider_id, label) DO UPDATE SET
             key_cipher = excluded.key_cipher, key_hint = excluded.key_hint,
             verified = 0, last_verified_at = NULL, created_at = datetime('now')""",
        (user_id, provider_id, label.strip()[:80] or "my key", cipher, hint),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM user_api_keys WHERE user_id = ? AND provider_id = ? AND label = ?",
                       (user_id, provider_id, label.strip()[:80] or "my key")).fetchone()
    return int(row["id"]) if row else int(cur.lastrowid)


def user_key_public(row: sqlite3.Row) -> dict:
    """Never carries key_cipher: a hint and a verified flag are all the browser gets."""
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "label": row["label"],
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


def user_key_for_provider(conn: sqlite3.Connection, user_id: int, provider_id: int) -> str | None:
    """The caller's own decrypted key for a provider, preferring a verified one.

    This is the BYOK auto-pick: a user who proved a key works should not have to select it
    again per request. Verified-first, then newest, so a stale unverified key never shadows
    one the user already checked. Returns None when the user has no key for that provider,
    and the caller falls back to the admin's stored credential.
    """
    row = conn.execute(
        """SELECT key_cipher FROM user_api_keys
           WHERE user_id = ? AND provider_id = ?
           ORDER BY verified DESC, id DESC LIMIT 1""",
        (user_id, provider_id),
    ).fetchone()
    if not row:
        return None
    try:
        return decrypt_key(row["key_cipher"])
    except ValueError:
        return None

