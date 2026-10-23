"""Accounts, sessions and role guards for the shared/hostable build.

The detector started as a single-user, loopback-only tool: every route was open and
the browser was trusted. The hostable page needs real accounts --

  * an **admin** who owns providers, API keys, models and the tuned bands, and whose
    choices are the defaults for everyone;
  * **users** who analyse text, keep their own history, and may bring their own key
    for an endpoint the admin has allowed -- but never touch thresholds, tokens or
    sampling.

An account is addressed by **e-mail**: sign-up collects a name, an address, and a password
typed twice, and sign-in takes the address. The legacy ``username`` column stays filled and
stays a way in, because every pre-e-mail account and every deploy script has only that.

The same module serves two very different deployments. A hosted build gates the app behind a
session cookie. A packaged single-owner install (``LOCAL_SINGLE_USER``, loopback only) has no
login page at all: the guard acts as its own admin, and ``/auth/me`` says so with
``auth_mode`` so one built front-end can render either.

Sessions are a Flask signed cookie (itsdangerous), so the same code runs behind nginx
in Docker and in the local single-file exe. The signing key is the Fernet key file the
backend already manages at 0600, unless SECRET_KEY is set in the environment.

Because the credential is a cookie the browser attaches by itself, two hardenings live with it in
``security.py``: a state-changing request that arrives shaped like a browser's must match the
session's CSRF token (and must not arrive from another origin at all), and failed sign-ins are
counted per address and identity.

Enforcement is skipped only under Flask's test client (``app.testing``) so the existing
suite keeps running unauthenticated; auth tests opt back in with ``AUTH_FORCE=True``.
Production servers never set TESTING, so they always enforce. That exemption is the auth guard's
own: the CSRF check and the security headers apply under the test client too, so the suite has to
send the token the deployed app demands.
"""
from __future__ import annotations

import os
import re

from flask import Flask, g, jsonify, request, session

from . import db, security

# Deliberately plain: the account is addressed by e-mail, so this has to accept every address a
# real person already has, not enforce RFC 5322. It rejects the shapes that are never an address
# (no @, spaces, a missing domain, a domain with no dot) while leaving delivery to the user.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
MIN_PASSWORD = 8
MIN_NAME, MAX_NAME = 2, 60
MAX_EMAIL = 254

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

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0"}


def local_mode() -> bool:
    """True when this process is a single-owner local install rather than a hosted multi-account
    deploy, and so safe to serve signed-in as its owner with no cookie in play.

    The packaged exe sets LOCAL_SINGLE_USER before it imports the app. The flag alone is not
    enough: BACKEND_HOST is an operator knob, and honouring it on 0.0.0.0 would hand an admin
    session to everyone on the network. So the mode is only real while the bind address is a
    loopback one, which is also the only way the launcher ever opens the browser.
    """
    if (os.environ.get("LOCAL_SINGLE_USER") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    host = (os.environ.get("BACKEND_HOST") or "127.0.0.1").strip()
    return host in _LOOPBACK or host.startswith("127.")


def _owner_user(conn):
    """The account a local install acts as, or None when the database has no admin yet."""
    return user_public(db.owner_user(conn))


def _identity(body: dict) -> str:
    """The sign-in identifier from whichever key the caller used.

    `identifier` is what the shipped front-end sends now. `email` and `username` are read so an
    existing script keeps working after one field rename -- they name the same concept, and the
    lookup below accepts either an address or a legacy handle.
    """
    for key in ("identifier", "email", "username"):
        value = (body.get(key) or "").strip()
        if value:
            return value
    return ""


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
        "email": row["email"],
        "display_name": row["display_name"],
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
        if g.user is None and local_mode():
            # A single-owner local install has no login page, so there is no cookie to carry.
            # Every route behaves as though the owner had signed in -- including the admin-only
            # ones, because on their own machine the owner IS the admin (that is where they add
            # their API key). An explicit session still wins if one exists.
            #
            # This runs BEFORE the public-path return below, not after it: the front-end decides
            # whether to render the sign-in card from GET /auth/me, which is public so that an
            # anonymous hosted visitor can reach it. Injecting the owner only for protected paths
            # would leave /auth/me answering `user: null` on the exe, and the app would show the
            # login page it just removed the way out of.
            g.user = _owner_user(g.conn)
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
        # auth_mode is what lets ONE built front-end serve both deployments: the container shows
        # the sign-in gate, the packaged exe opens straight into the app. It has to be readable
        # before anyone is signed in, which is why it rides on this public route.
        if session.get("uid"):
            # The boot read, so it is where a session that predates this change gets its token.
            # Deliberately not minted for an anonymous visitor: that would put a signed cookie and
            # a CSRF value on every hosted stranger who only came to look at the ranking.
            security.ensure_token()
        return jsonify(user=g.user, auth_mode="local" if local_mode() else "accounts")

    @app.route("/auth/register", methods=["POST"])
    def auth_register():
        b = request.get_json(silent=True) or {}
        if local_mode():
            return jsonify(
                error="this install has one owner account and no sign-up; the person who runs "
                      "it signs in automatically",
                error_code="SINGLE_USER_MODE"), 409
        name = (b.get("name") or b.get("display_name") or "").strip()
        email = (b.get("email") or "").strip().lower()
        password = b.get("password") or ""
        confirm = b.get("confirm") if b.get("confirm") is not None else b.get("password_confirm")
        if not (MIN_NAME <= len(name) <= MAX_NAME):
            return jsonify(error=f"name must be {MIN_NAME}-{MAX_NAME} characters",
                           error_code="BAD_NAME"), 400
        if not EMAIL_RE.match(email) or len(email) > MAX_EMAIL:
            return jsonify(error="enter a valid email address", error_code="BAD_EMAIL"), 400
        if len(password) < MIN_PASSWORD:
            return jsonify(error=f"password must be at least {MIN_PASSWORD} characters",
                           error_code="BAD_PASSWORD"), 400
        if confirm is not None and confirm != password:
            # The form checks this before submitting; the server checks it too, so a caller that
            # skips the confirmation field cannot create an account nobody agreed to.
            return jsonify(error="the two passwords do not match",
                           error_code="PASSWORD_MISMATCH"), 400
        try:
            with db.session() as c:
                if db.get_user_by_email(c, email) is not None:
                    return jsonify(error="an account already exists for that email",
                                   error_code="EMAIL_TAKEN"), 409
                # The username column stays: it is how pre-email accounts and the deploy tools
                # still sign in, so a new account gets one derived from its address.
                username = db.derive_username(c, email)
                uid = db.create_user(c, username, password, "user", email=email, display_name=name)
                row = db.get_user_by_id(c, uid)
        except Exception as exc:  # pragma: no cover - defensive
            return jsonify(error="could not create account", detail=str(exc)), 500
        session.clear()
        session["uid"] = row["id"]
        session.permanent = True
        security.ensure_token()
        return jsonify(user=user_public(row), auth_mode="accounts"), 201

    @app.route("/auth/login", methods=["POST"])
    def auth_login():
        b = request.get_json(silent=True) or {}
        identifier = _identity(b)
        password = b.get("password") or ""
        key = security.client_key(identifier)
        wait = security.LOGIN_THROTTLE.remaining(key)
        if wait:
            # Answered before the password hash is touched, so a throttled guesser costs this
            # process no PBKDF2 work either -- the ceiling is meant to slow the attempts, not
            # merely to report them.
            return jsonify(error="too many failed sign-ins; try again later",
                           error_code="RATE_LIMITED", retry_after_s=wait), 429, {
                           "Retry-After": str(wait)}
        with db.session() as c:
            row = db.get_user_by_identifier(c, identifier)
            # Compare even when the user is missing is unnecessary; a constant-ish path
            # is nice but the username enumeration risk is accepted for this app.
            if row is None or not db.check_password(password, row["password_hash"]):
                security.LOGIN_THROTTLE.fail(key)
                return jsonify(error="invalid email or password", error_code="BAD_CREDENTIALS"), 401
            c.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (row["id"],))
            out = user_public(row)
        security.LOGIN_THROTTLE.clear(key)
        session.clear()
        session["uid"] = out["id"]
        session.permanent = True
        # Minted here rather than lazily on a GET so the sign-in response itself carries the
        # cookie: the SPA's first write after signing in is the detection it just typed, and a
        # token that only arrives with the next read would make that request look like an attack.
        security.ensure_token()
        return jsonify(user=out, auth_mode="local" if local_mode() else "accounts")

    @app.route("/auth/logout", methods=["POST"])
    def auth_logout():
        session.clear()
        resp = jsonify(ok=True, auth_mode="local" if local_mode() else "accounts")
        # The token cookie is readable by design, so it is deleted by design too: leaving it
        # standing would put a value from a previous session on the owner's machine.
        resp.delete_cookie(security.CSRF_COOKIE, path="/")
        return resp

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
