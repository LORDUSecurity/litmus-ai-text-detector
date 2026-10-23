"""The request-hardening layer: response headers, CSRF, and failed-login throttling.

Three facts decide what lives here.

The app ships twice from one codebase -- a packaged single-owner exe bound to loopback, and a
hosted container behind nginx -- so every rule has to hold for a browser that is the owner's own
machine AND for anonymous traffic on a public socket. Cookies are the only credential the app
has (signed Flask sessions; there is no bearer token anywhere), which makes CSRF a real question
rather than a theoretical one. And `backend/requirements.txt` is `flask`, `cryptography`,
`pypdf` -- the container installs exactly that -- so a defence that needed `flask-talisman`,
`Flask-WTF` or `bleach` would be a defence that disappears on deploy. Everything below is
stdlib plus Flask.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import jsonify, request, session

# --------------------------------------------------------------------- response headers

# The report sheet frames `/research/report.html`, which is the app's own file, so frames are
# allowed same-origin and forbidden anywhere else.
#
# `style-src 'unsafe-inline'` is not laziness: the phrase-evidence plates colour themselves with
# an inline `style` attribute, because the colour is computed per quote and per theme and has no
# class to live in. A policy that forbids inline style would silently strip the one channel that
# says which direction a quote was found in -- a visual failure with no error to read.
#
# `upgrade-insecure-requests` is deliberately absent: the deploy recipe serves this app on a LAN
# over plain http (see docker/DEPLOY.md), and the directive would rewrite every same-origin call
# to https and take the install down at first boot. Turn it on with TLS, not before.
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
))

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": ", ".join((
        "accelerometer=()", "camera=()", "geolocation=()", "gyroscope=()",
        "magnetometer=()", "microphone=()", "payment=()", "usb=()",
    )),
    "Cross-Origin-Opener-Policy": "same-origin",
}

# The one list every server in this repo has to agree on. Three processes answer this app: Flask
# routes (`init` below), nginx for static files in the container (`docker/nginx.conf`, checked
# against these exact values by `tools/verify_deploy_recipe.py`), and `serve_local`'s own static
# responder for the packaged exe. A header set that lives in only one of them is a policy that
# covers one deploy and not the other.
RESPONSE_HEADERS = {**STATIC_HEADERS, "Content-Security-Policy": CSP}

# --------------------------------------------------------------------- CSRF

CSRF_COOKIE = "detector_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_SESSION_KEY = "csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# A dev `ng serve` proxies /api to the packaged exe on another loopback port, so the page's origin
# and the socket's host differ while they are the same machine. The CORS hook already treats every
# loopback spelling as one site (app.py `LOOPBACK_ORIGIN`); the CSRF check has to agree with it or
# the only honest way to test the hosted policy would be to break the daily dev loop.
_LOOPBACK = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])$")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_token() -> str:
    """The session's CSRF token, minted on first read.

    Minted on GETs only, by construction: every caller that needs to *send* a token is a route
    handler or an interceptor that has already done a read. A state-changing request never mints,
    because a request that mints for itself is a request that always passes its own check.
    """
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = new_token()
        session[CSRF_SESSION_KEY] = token
    return token


def _origin_of(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _same_site(client_origin: str, page_origin: str) -> bool:
    if client_origin == page_origin:
        return True
    ch = urlsplit(client_origin).hostname or ""
    ph = urlsplit(page_origin).hostname or ""
    return bool(_LOOPBACK.match(ch or "") and _LOOPBACK.match(ph or ""))


def _page_origin() -> str:
    host = request.host.lower()
    # `is_secure` is a property on this Werkzeug, not a method -- calling it raises TypeError
    # inside the guard, which reads as a 500 on every browser-shaped write.
    scheme = "https" if request.is_secure else "http"
    return f"{scheme}://{host}"


def _request_origin() -> str:
    """Where the browser says this request came from, or "" for a non-browser client.

    Origin is sent by every modern browser on every state-changing fetch; Referer is the fallback
    for the older navigations that skip it. A tool that sends neither is not a CSRF risk -- it is
    a script, and it has to produce the cookie by other means -- so it reads as "" and the browser
    rules below simply do not apply. That is what keeps `tools/verify_admin_settings.py` and the
    benchmark probes working while the browser-facing path is locked.
    """
    origin = request.headers.get("Origin")
    if origin:
        return _origin_of(origin)
    referer = request.headers.get("Referer")
    return _origin_of(referer) if referer else ""


def token_matches() -> bool:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    expected = session.get(CSRF_SESSION_KEY) or ""
    if not (cookie and header and expected):
        return False
    return (hmac.compare_digest(header, expected)
            and hmac.compare_digest(cookie, expected))


def reject(reason: str, detail: str):
    return jsonify({
        "error": detail,
        "error_code": reason,
        "classification": "DETECTION_UNAVAILABLE",
        "review_required": False,
        "method": reason,
    }), 403


def check_csrf() -> tuple | None:
    """None to let the request through, or the response to answer it with.

    Ordered weakest-proof-first: a `Sec-Fetch-Site: cross-site` request is refused before anything
    else, because the browser has already told us it is not this site's own page and no token can
    change that. Then any browser-shaped request whose origin is not this site's is refused -- that
    is the rule that pays for the single-owner exe, where there is no session cookie to protect and
    a DNS-rebound page on the owner's machine would otherwise be able to write settings. Only a
    cookie-carrying request then has to produce the token, which is the classic double-submit check
    for the hosted deploy.
    """
    if request.method in SAFE_METHODS:
        return None
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return reject("CSRF_CROSS_SITE",
                      "This browser marked the request as coming from another site.")
    origin = _request_origin()
    if origin and not _same_site(origin, _page_origin()):
        return reject("CSRF_ORIGIN_MISMATCH",
                      "This request did not come from this deployment's origin.")
    if session.get("uid") and not token_matches():
        return reject("CSRF_TOKEN_MISSING",
                      "Sign in again, or reload this page, to refresh the session token.")
    return None


# --------------------------------------------------------------------- failed-login throttle


class Throttle:
    """A sliding window of failures per key, kept in this process.

    In-process is the right size for this: the container runs one gunicorn worker per core and a
    guesser gets a fresh budget per worker, so the ceiling is `limit x workers` rather than `limit`,
    and the alternative -- persisting counters -- means a table to migrate and state to leak. The
    point is not to make password guessing impossible, it is to stop it being free: an attacker who
    has to clear 8 tries per identity per 15 minutes is an attacker who needs days and many source
    addresses for a list of accounts.
    """

    def __init__(self, limit: int = 8, window_s: int = 900):
        self.limit = limit
        self.window_s = window_s
        self._stamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def remaining(self, key: str) -> int:
        """Seconds left before this key may try again; 0 when it may try."""
        now = time.time()
        with self._lock:
            stamps = [t for t in self._stamps.get(key, []) if now - t < self.window_s]
            if stamps:
                self._stamps[key] = stamps
            else:
                self._stamps.pop(key, None)
            if len(stamps) < self.limit:
                return 0
            return max(1, int(self.window_s - (now - min(stamps))) + 1)

    def fail(self, key: str) -> None:
        with self._lock:
            self._stamps.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._stamps.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._stamps.clear()


LOGIN_THROTTLE = Throttle()


def client_key(identifier: str) -> str:
    """The bucket a login attempt belongs to: who, from where.

    `X-Forwarded-For` is read because nginx is the socket this app faces in the hosted deploy, so
    `remote_addr` alone would be the proxy for everyone and the window would become global. It is
    spoofable, which is exactly why the identifier is in the key too: a spoofed address buys an
    attacker a fresh budget for the one name they are hammering, not for anybody else's.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    addr = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return f"{addr}|{(identifier or '').strip().lower()}"


# --------------------------------------------------------------------- wiring


def secure_cookies() -> bool:
    return os.environ.get("USE_SECURE_COOKIES", "false").strip().lower() == "true"


def init(app) -> None:
    """Attach the layer to a Flask app. Call AFTER `auth.init_auth`, so the guard has already put
    `g.user` and the session on the request: an unauthenticated call must read as 401 AUTH_REQUIRED
    rather than a CSRF complaint about a token no anonymous visitor could have."""

    @app.after_request
    def _security_headers(resp):
        for name, value in RESPONSE_HEADERS.items():
            resp.headers[name] = value
        return resp

    @app.before_request
    def _csrf():
        return check_csrf()

    @app.after_request
    def _csrf_cookie(resp):
        # The token lives in a readable cookie on purpose: the SPA is the only reader, it reads it
        # to set the header, and an attacker's page on another origin cannot read it even though it
        # would be sent. HttpOnly here would defeat the double-submit design.
        if session.get(CSRF_SESSION_KEY):
            resp.set_cookie(
                CSRF_COOKIE,
                session[CSRF_SESSION_KEY],
                httponly=False,
                samesite="Lax",
                secure=secure_cookies(),
                path="/",
            )
        # And it returns the response, because an `after_request` that falls off the end hands
        # `None` to the next one in the chain -- which is every other header in this module, and
        # then a 500 on every route in the app.
        return resp
