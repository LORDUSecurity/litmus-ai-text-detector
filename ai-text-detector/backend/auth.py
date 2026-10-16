"""Accounts, sessions and role guards for the shared/hostable build.

The detector started as a single-user, loopback-only tool: every route was open and
the browser was trusted. The hostable page needs real accounts --

  * an **admin** who owns providers, API keys, models and the tuned bands, and whose
    choices are the defaults for everyone;
  * **users** who analyse text, keep their own history, and may bring their own key
    for an endpoint the admin has allowed -- but never touch thresholds, tokens or
    sampling.

Sessions are a Flask signed cookie (itsdangerous), so the same code runs behind nginx
in Docker and in the local single-file exe. The signing key is the Fernet key file the
backend already manages at 0600, unless SECRET_KEY is set in the environment.

Enforcement is skipped only under Flask's test client (``app.testing``) so the existing
suite keeps running unauthenticated; auth tests opt back in with ``AUTH_FORCE=True``.
Production servers never set TESTING, so they always enforce.
"""
from __future__ import annotations

import os
import re

from flask import Flask, g, jsonify, request, session

from . import db

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 8

# Paths reachable with no session at all. Everything else requires login.
PUBLIC_PATHS = {
    "/auth/login",
    "/auth/register",
    "/auth/logout",
    "/auth/me",
    "/",
    "/index.html",
    "/favicon.ico",
    "/healthz",
}
PUBLIC_PREFIXES = ("/assets/", "/static/")
PUBLIC_SUFFIXES = (".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".txt")

# Read-only shares (users need the provider/endpoint list to bring a key, and the model
# list to see what is tuned); everything mutating, plus keys/benchmark/n8n/settings/users,
# is admin-only.
_ADMIN_PREFIXES_ANY_METHOD = ("/keys", "/n8n", "/benchmark", "/settings", "/users")


def _enforce(app: Flask) -> bool:
    if not app.config.get("AUTH_ENABLED", True):
        return False
    # Under the test client, skip unless a test explicitly forces enforcement.
    if app.testing and not app.config.get("AUTH_FORCE"):
        return False
    return True


def _secret_key() -> bytes | str:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    db.fernet()  # ensures the 0600 key file exists
    return db.SECRET_PATH.read_bytes()


def user_public(row) -> dict:
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "theme": row["theme"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def current_user():
    """The signed-in user dict for this request, or None. Set by the guard."""
    return getattr(g, "user", None)


def _load_user():
    uid = session.get("uid")
    if not uid:
        return None
    row = db.get_user_by_id(g.conn, int(uid))
    return user_public(row) if row else None


def _admin_required(path: str, method: str) -> bool:
    for p in _ADMIN_PREFIXES_ANY_METHOD:
        if path == p or path.startswith(p + "/"):
            return True
    if path.startswith("/providers") or path.startswith("/models"):
        if method in ("POST", "PATCH", "DELETE"):
            return True
        if path.endswith("/probe") or path.endswith("/keys"):
            return True
    return False


def init_auth(app: Flask) -> None:
    app.secret_key = _secret_key()
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_SECURE",
                          os.environ.get("USE_SECURE_COOKIES", "false").lower() == "true")
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", 60 * 60 * 24 * 7)

    @app.before_request
    def _auth_guard():
        g.user = _load_user()
        if not _enforce(app):
            return None
        if request.method == "OPTIONS":
            return None
        path = request.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or path.endswith(PUBLIC_SUFFIXES):
            return None
        if g.user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        if _admin_required(path, request.method) and g.user["role"] != "admin":
            return jsonify(error="admin role required", error_code="FORBIDDEN"), 403
        return None

    # ---- auth routes ----------------------------------------------------

    @app.route("/auth/me", methods=["GET"])
    def auth_me():
        return jsonify(user=g.user)

    @app.route("/auth/register", methods=["POST"])
    def auth_register():
        b = request.get_json(silent=True) or {}
        username = (b.get("username") or "").strip()
        password = b.get("password") or ""
        if not USERNAME_RE.match(username):
            return jsonify(error="username must be 3-32 chars of letters, digits, . _ -",
                           error_code="BAD_USERNAME"), 400
        if len(password) < MIN_PASSWORD:
            return jsonify(error=f"password must be at least {MIN_PASSWORD} characters",
                           error_code="BAD_PASSWORD"), 400
        try:
            with db.session() as c:
                if db.get_user_by_name(c, username) is not None:
                    return jsonify(error="that username is taken", error_code="USERNAME_TAKEN"), 409
                uid = db.create_user(c, username, password, "user")
                row = db.get_user_by_id(c, uid)
        except Exception as exc:  # pragma: no cover - defensive
            return jsonify(error="could not create account", detail=str(exc)), 500
        session.clear()
        session["uid"] = row["id"]
        session.permanent = True
        return jsonify(user=user_public(row)), 201

    @app.route("/auth/login", methods=["POST"])
    def auth_login():
        b = request.get_json(silent=True) or {}
        username = (b.get("username") or "").strip()
        password = b.get("password") or ""
        with db.session() as c:
            row = db.get_user_by_name(c, username)
            # Compare even when the user is missing is unnecessary; a constant-ish path
            # is nice but the username enumeration risk is accepted for this app.
            if row is None or not db.check_password(password, row["password_hash"]):
                return jsonify(error="invalid username or password", error_code="BAD_CREDENTIALS"), 401
            c.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (row["id"],))
            out = user_public(row)
        session.clear()
        session["uid"] = out["id"]
        session.permanent = True
        return jsonify(user=out)

    @app.route("/auth/logout", methods=["POST"])
    def auth_logout():
        session.clear()
        return jsonify(ok=True)

    @app.route("/auth/password", methods=["POST"])
    def auth_password():
        if g.user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        current = b.get("current") or ""
        nxt = b.get("next") or ""
        if len(nxt) < MIN_PASSWORD:
            return jsonify(error=f"password must be at least {MIN_PASSWORD} characters",
                           error_code="BAD_PASSWORD"), 400
        with db.session() as c:
            row = db.get_user_by_id(c, g.user["id"])
            if row is None or not db.check_password(current, row["password_hash"]):
                return jsonify(error="current password is wrong", error_code="BAD_CREDENTIALS"), 401
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (db.hash_password(nxt), row["id"]))
        return jsonify(ok=True)

    @app.route("/auth/theme", methods=["POST"])
    def auth_theme():
        if g.user is None:
            return jsonify(error="authentication required", error_code="AUTH_REQUIRED"), 401
        b = request.get_json(silent=True) or {}
        theme = (b.get("theme") or "").strip().lower()
        if theme not in ("dark", "light"):
            return jsonify(error="theme must be 'dark' or 'light'", error_code="BAD_THEME"), 400
        with db.session() as c:
            c.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, g.user["id"]))
        g.user = {**g.user, "theme": theme}
        return jsonify(user=g.user)
