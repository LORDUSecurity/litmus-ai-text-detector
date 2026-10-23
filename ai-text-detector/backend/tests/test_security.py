"""The request-hardening layer. Run from ai-text-detector/:
    python -m pytest backend/tests/test_security.py -q

These specs exist because the layer changes what a working request looks like, so the risk is not
that it fails -- it is that it passes while quietly blocking something honest. Three things are
therefore proven in both directions, not one:

  * a browser-shaped request from another site is refused, and the same request from this app's own
    page (including the `ng serve` -> exe loopback pair, which is two different ports on one
    machine) is answered;
  * a signed-in write needs the session's token, and a script with no browser in it -- every
    `tools/verify_*.py` probe and every benchmark runner -- still works, because nothing about it
    can be forged across origins;
  * the headers the front end depends on stay present, because the evidence plates colour
    themselves with an inline `style` attribute and a CSP that forbids inline style would fail as a
    silent visual bug rather than an error.

Same throwaway DB / stubbed forwarder discipline as the rest of the suite: never a real provider
call, never the live database, never a printed key.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="detector-sec-test-")
os.environ["DETECTOR_DB"] = os.path.join(_TMP, "sec.sqlite3")
os.environ["DETECTOR_SECRET"] = os.path.join(_TMP, "sec-secret.key")
os.environ["N8N_DB"] = os.path.join(_TMP, "no-such-n8n.sqlite")
os.environ["ADMIN_USERNAME"] = "root"
os.environ["ADMIN_PASSWORD"] = "rootpass123"

import pytest  # noqa: E402

from backend import db, engine, security  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.tests._session import arm_csrf, fresh_throttle  # noqa: E402

TEXT = "This is a long enough sample of text for the detector to accept it."
DEV_PAGE = "http://localhost:4321"          # `ng serve`, proxying /api to the exe below
EXE_HOST = "127.0.0.1:8787"                 # what the exe actually answers on
OTHER_SITE = "https://evil.example"

# The write every CSRF case posts is `/auth/theme`, chosen so that the only thing deciding
# 200-vs-403 is the token: it needs a signed-in session like any other write, but no provider row,
# no encrypted key and no stubbed forwarder -- and unlike `/auth/password` it does not rewrite the
# account's credential on the way past the guard.
def _write(client, **kw):
    return client.post("/auth/theme", json={"theme": "light"}, **kw)


@pytest.fixture()
def app():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    a = create_app()
    a.config["TESTING"] = True
    a.config["AUTH_FORCE"] = True
    return fresh_throttle(a)


@pytest.fixture()
def client(app):
    with arm_csrf(app.test_client()) as c:
        yield c


def _stub_forward(monkeypatch):
    """A lane that always answers, and remembers that it was asked."""
    import backend.app as app_mod

    calls = []

    def fake(url, payload, headers, timeout):
        calls.append(payload)
        return 200, {
            "classification": "AI_GENERATED", "ai_probability": 0.97, "confidence": 0.9,
            "method": "PRIMARY_DECIDED_AI", "review_required": False,
            "config_used": {"primary": {"model": "m/one"}},
            "_internal": {"text_fingerprint": "fp", "text_length": len(TEXT)},
        }, 123.4

    monkeypatch.setattr(app_mod, "forward", fake)
    return calls


def _signup(client, handle="alice"):
    r = client.post("/auth/register", json={
        "name": f"Sec {handle.title()}", "email": f"{handle}@example.org",
        "password": "userpass123", "confirm": "userpass123"})
    assert r.status_code == 201, r.get_json()
    return r


# ------------------------------------------------------------------ headers

def test_every_response_carries_the_header_set(client):
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["Permissions-Policy"]
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_the_csp_refuses_injected_script_and_allows_the_plates_inline_colour(client):
    csp = client.get("/auth/me").headers["Content-Security-Policy"]
    directives = {part.strip().split()[0]: part.strip() for part in csp.split(";") if part.strip()}
    # The plates: a per-phrase colour written into a `style` attribute by Angular. Forbidding it
    # does not throw, it just stops showing which direction a quote was found in.
    assert directives["style-src"] == "style-src 'self' 'unsafe-inline'"
    # And no such permission for code: a stored payload that somehow reaches the page as markup
    # still cannot execute, and cannot open a socket anywhere but this origin.
    assert directives["script-src"] == "script-src 'self'"
    assert directives["connect-src"] == "connect-src 'self'"
    assert directives["object-src"] == "object-src 'none'"
    assert directives["frame-ancestors"] == "frame-ancestors 'self'"
    assert "unsafe-inline" not in directives["script-src"]
    # A LAN deploy over plain http would be taken down at first boot by this directive, so the
    # policy says so by not containing it.
    assert "upgrade-insecure-requests" not in csp


def test_the_error_pages_are_covered_too(app):
    # A 401 is the response an attacker reads, and a JSON error body sniffed as HTML is how a
    # stored payload gets a second chance.
    with app.test_client() as c:
        r = c.get("/ranking")
        assert r.status_code in (200, 401)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in r.headers


# ------------------------------------------------------------------ CSRF

def test_a_signed_in_write_from_the_apps_own_page_is_answered(app):
    # The exact shape of the shipped SPA: fetch() from this origin, so the browser puts its own
    # Origin on the request, marks it same-origin, and the interceptor has put the token on the
    # header. All three have to pass, or the app is unusable.
    with arm_csrf(app.test_client()) as c:
        _signup(c)
        r = _write(c, headers=[("Origin", "http://localhost"),
                                    ("Sec-Fetch-Site", "same-origin")])
        assert r.status_code == 200, r.get_json()


def test_a_signed_in_write_with_no_token_is_refused(app):
    # The same cookie-carrying client, only without the header the interceptor adds: this is the
    # shape of a form posted from another site, where the browser sends the session and cannot
    # possibly know the token.
    with app.test_client() as c:
        _signup(c)
        r = _write(c)
        assert r.status_code == 403
        assert r.get_json()["error_code"] == "CSRF_TOKEN_MISSING"


def test_a_signed_in_write_with_a_forged_token_is_refused(app):
    with app.test_client() as c:
        _signup(c)
        r = _write(c, headers=[(security.CSRF_HEADER, "not-the-sessions-token")])
        assert r.status_code == 403
        assert r.get_json()["error_code"] == "CSRF_TOKEN_MISSING"


def test_the_token_is_readable_by_the_page_and_still_lax(app):
    """The double-submit design needs the SPA to read the cookie, so it is NOT HttpOnly.

    Worth pinning as an assertion rather than a comment: an `httponly=True` slipped in here would
    look like a hardening win, and would make every signed-in write 403 with no way for the page to
    help itself.
    """
    with app.test_client() as c:
        r = _signup(c)
        set_cookies = r.headers.getlist("Set-Cookie")
        csrf = [h for h in set_cookies if h.startswith(f"{security.CSRF_COOKIE}=")]
        assert csrf, set_cookies
        assert "HttpOnly" not in csrf[0]
        assert "SameSite=Lax" in csrf[0]
        session_cookie = [h for h in set_cookies if not h.startswith(f"{security.CSRF_COOKIE}=")]
        assert session_cookie and "HttpOnly" in session_cookie[0]


def _cookie_named(headers, name):
    for header in headers.getlist("Set-Cookie"):
        if header.startswith(f"{name}="):
            return header
    return ""


def test_the_token_cookie_is_only_secure_when_the_deploy_says_so(app, monkeypatch):
    # `USE_SECURE_COOKIES` is the TLS switch in docker-compose; on a plain-http LAN box a Secure
    # cookie is a cookie the browser never sends, i.e. nobody can sign in.
    with app.test_client() as c:
        assert "Secure" not in _cookie_named(_signup(c, "amy").headers, security.CSRF_COOKIE)
    monkeypatch.setenv("USE_SECURE_COOKIES", "true")
    with app.test_client() as c:
        assert "Secure" in _cookie_named(_signup(c, "ben").headers, security.CSRF_COOKIE)


def test_a_cross_site_request_is_refused_even_carrying_the_token(app):
    # The token proves the page could read the cookie; `Sec-Fetch-Site` proves the browser itself
    # knows this is not this site. Either alone is a claim, together they are not negotiable, and
    # the order matters -- a forged token from a leaked value still dies here.
    with app.test_client() as c:
        _signup(c)
        token = c.get_cookie(security.CSRF_COOKIE).value
        for header in ((("Sec-Fetch-Site", "cross-site"),),
                       (("Sec-Fetch-Site", "cross-site"), (security.CSRF_HEADER, token))):
            r = _write(c, headers=list(header))
            assert r.status_code == 403, r.get_json()
            assert r.get_json()["error_code"] == "CSRF_CROSS_SITE"


def test_an_origin_that_is_not_this_deployment_is_refused(app):
    with app.test_client() as c:
        _signup(c)
        token = c.get_cookie(security.CSRF_COOKIE).value
        r = _write(c, headers=[("Origin", OTHER_SITE), (security.CSRF_HEADER, token)])
        assert r.status_code == 403
        assert r.get_json()["error_code"] == "CSRF_ORIGIN_MISMATCH"


def test_the_dev_proxy_pair_counts_as_one_site(app):
    """`ng serve` on 4321 proxies /api to the exe on 8787.

    Those are different origins to a browser and one machine to a developer. The CORS hook already
    treats every loopback spelling as one site; if the CSRF rule disagreed, the daily dev loop
    would 403 on the first write and the honest way to test the hosted policy would be to turn it
    off during development.
    """
    with arm_csrf(app.test_client()) as c:
        c.environ_base["HTTP_HOST"] = EXE_HOST
        _signup(c)
        r = _write(c, headers=[("Origin", DEV_PAGE)])
        assert r.status_code == 200, r.get_json()


def test_a_script_with_no_browser_in_it_is_still_answered(app):
    # Every `tools/verify_*.py` probe and benchmark runner: cookie-carrying, session-authenticated,
    # and sending no Origin and no Sec-Fetch-Site, because it is not a browser and has no other
    # site to be confused with. It is armed here with the token it was issued, the way a script
    # that logs in through the real API does.
    with arm_csrf(app.test_client()) as c:
        _signup(c)
        r = _write(c)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["user"]["theme"] == "light"


def test_reads_need_no_token(client):
    # A read is not a state change, so a page that has not yet been issued a token -- the first
    # GET of every session, and every `verify_*.py` that only reads -- still works. If this rule
    # ever covered GETs, the SPA could never bootstrap: the token arrives on a read.
    _signup(client)
    for path in ("/auth/me", "/my/pool", "/ranking", "/models", "/providers"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.get_json())


def test_the_token_survives_a_relogin_and_the_new_session_needs_the_new_one(app):
    with arm_csrf(app.test_client()) as c:
        _signup(c, "alice")
        assert _write(c).status_code == 200
        first = c.get_cookie(security.CSRF_COOKIE).value
        assert c.post("/auth/logout").status_code == 200
        # Logout deleted the cookie, so the interceptor has nothing to send and the next anonymous
        # registration is answered; every write after it carries the NEW session's token.
        _signup(c, "bob")
        assert c.get_cookie(security.CSRF_COOKIE).value != first
        assert _write(c).status_code == 200
    # A stale value from the previous session is not a token, even beside a valid session cookie.
    # That is the shape of a page that never re-read the cookie after its owner signed in again --
    # and the reason the interceptor reads it per request instead of caching it at boot.
    with app.test_client() as late:
        r = late.post("/auth/login", json={"identifier": "bob@example.org",
                                           "password": "userpass123"})
        assert r.status_code == 200, r.get_json()
        r = _write(late, headers=[(security.CSRF_HEADER, first)])
        assert r.status_code == 403
        assert r.get_json()["error_code"] == "CSRF_TOKEN_MISSING"


def test_an_anonymous_write_still_reaches_its_own_answer(app):
    # `/auth/register` itself is the state-changing route a visitor must reach before a session,
    # and therefore before any token exists. Refusing it would make the hosted deploy unwritable
    # to nobody in particular.
    with app.test_client() as c:
        r = c.post("/auth/register", json={
            "name": "No Session", "email": "nosession@example.org",
            "password": "userpass123", "confirm": "userpass123"})
        assert r.status_code == 201, r.get_json()


# ------------------------------------------------------------------ failed-login throttle

def test_eight_failures_then_the_door_closes(app):
    with app.test_client() as c:
        for i in range(security.LOGIN_THROTTLE.limit):
            r = c.post("/auth/login", json={"identifier": "slow@example.org", "password": "wrong"})
            assert r.status_code == 401, (i, r.get_json())
        r = c.post("/auth/login", json={"identifier": "slow@example.org", "password": "wrong"})
        assert r.status_code == 429
        assert r.get_json()["error_code"] == "RATE_LIMITED"
        assert int(r.headers["Retry-After"]) > 0


def test_the_right_password_is_also_late(app):
    # Deliberate: an attacker who can buy an instant 401-vs-200 differential can probe which
    # addresses exist while the window fills. Once throttled, the account is closed to everyone,
    # and the response says when it opens again.
    with arm_csrf(app.test_client()) as c:
        _signup(c, "paul")
        for _ in range(security.LOGIN_THROTTLE.limit):
            c.post("/auth/login", json={"identifier": "paul@example.org", "password": "wrong"})
        r = c.post("/auth/login", json={"identifier": "paul@example.org",
                                        "password": "userpass123"})
        assert r.status_code == 429


def test_a_successful_sign_in_clears_the_window(app):
    with arm_csrf(app.test_client()) as c:
        _signup(c, "rita")
        for _ in range(security.LOGIN_THROTTLE.limit - 1):
            c.post("/auth/login", json={"identifier": "rita@example.org", "password": "wrong"})
        assert c.post("/auth/login", json={"identifier": "rita@example.org",
                                           "password": "userpass123"}).status_code == 200
        # Fresh budget: seven more wrong tries are still 401s, not a 429 for someone who signed in.
        for _ in range(security.LOGIN_THROTTLE.limit - 1):
            assert c.post("/auth/login", json={"identifier": "rita@example.org",
                                               "password": "wrong"}).status_code == 401


def test_one_guesser_does_not_lock_out_the_neighbours(app):
    with arm_csrf(app.test_client()) as c:
        for _ in range(security.LOGIN_THROTTLE.limit):
            c.post("/auth/login", json={"identifier": "guesser@example.org", "password": "wrong"})
        _signup(c, "innocent")
        assert c.post("/auth/login", json={"identifier": "innocent@example.org",
                                           "password": "userpass123"}).status_code == 200


# ------------------------------------------------------------------ payload ceiling

def test_an_oversized_text_is_refused_before_any_lane_is_dialled(app, monkeypatch):
    with arm_csrf(app.test_client()) as c:
        calls = _stub_forward(monkeypatch)
        _signup(c)
        big = TEXT + "x" * (engine.max_text_length() + 1)
        r = c.post("/detect", json={"text": big})
        assert r.status_code == 413
        assert r.get_json()["error_code"] == "TEXT_TOO_LONG"
        assert calls == [], "the body reached the engine after being refused"


def test_the_ceiling_follows_the_engines_own_number(app, monkeypatch):
    # One number, read twice: change MAX_TEXT_LENGTH and the front door moves with it. A cap the app
    # restated by hand is a cap that quietly disagrees with the deploy it ships beside. Same body,
    # two ceilings, opposite answers.
    body = TEXT + "y" * 5000
    monkeypatch.setenv("MAX_TEXT_LENGTH", "800")
    assert engine.max_text_length() == 800
    with arm_csrf(app.test_client()) as c:
        _signup(c, "nadia")
        r = c.post("/detect", json={"text": body})
        assert r.status_code == 413
        assert r.get_json()["error_code"] == "TEXT_TOO_LONG"
    monkeypatch.setenv("MAX_TEXT_LENGTH", "50000")
    assert engine.max_text_length() == 50000
    with arm_csrf(app.test_client()) as c:
        _signup(c, "omar")
        assert c.post("/detect", json={"text": body}).status_code != 413




def test_the_ceiling_is_read_from_the_engine_not_restated_here():
    # If the route ever hardcodes its own number it will disagree with the deploy it ships beside,
    # and the two halves of the app will draw the line on different texts.
    import inspect

    import backend.app as app_mod

    src = inspect.getsource(app_mod)
    assert "engine.max_text_length()" in src
    assert "MAX_TEXT_LENGTH" not in src
