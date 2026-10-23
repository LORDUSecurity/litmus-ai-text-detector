#!/usr/bin/env python3
"""Live proof of the request-hardening layer against a real server, not a test client.

    python tools/verify_security_layer.py            # ~20 checks, no provider key, no pool call

`backend/tests/test_security.py` covers the same rules hermetically, and a green suite is what
makes this worth running anyway: the tests exercise `create_app()` through Werkzeug's test client,
which means they cannot see three things a deployment hits for real.

  * `serve_local`'s static responder. On the packaged app, index.html and the hashed bundles never
    reach Flask, so an `after_request` hook cannot put headers on them -- the CSP has to be set in
    that WSGI code path too, and only a live GET `/` shows whether it is.
  * Real cookie flags over real HTTP. `httponly`/`samesite`/`secure` are attributes of the wire, and
    a test client that hands back a `Set-Cookie` string is a weaker witness than a browser-shaped
    jar that refuses to replay a cookie it was told not to send.
  * Hook ORDER. The auth guard and the CSRF guard are both `before_request`; which one answers a
    signed-out write first is a property of the running process, not of the functions.

So this starts `backend.serve_local` on a spare port with a throwaway database and its own key file,
and drives it the way a browser does. The mode is stated rather than inherited: `LOCAL_SINGLE_USER=0`
because single-owner mode answers every request as the owner and the CSRF rule would then be
measuring nothing. Nothing here reads `backend/data/`, the real `.env` keys, or the live lanes: the
one /detect call is refused for length before any lane is dialled, which is the point of that check.

Usage:  python tools/verify_security_layer.py [--port 8793]
Exits non-zero with the failing checks listed.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = next((int(sys.argv[i + 1]) for i, a in enumerate(sys.argv)
             if a == "--port"), int(os.environ.get("VERIFY_PORT", "8793")))
BASE = f"http://127.0.0.1:{PORT}"
OTHER_SITE = "https://evil.example"
# The header names are imported, never retyped: this file proves the deployed behaviour of
# `backend/security.py`, and a constant copied here would keep passing after the protocol changed.
from backend import security  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)


class Client:
    """A cookie jar plus the one thing a browser has and a script does not: it sends the token it
    was handed on every write, unless told otherwise. `armed` mirrors the shipped SPA interceptor."""

    def __init__(self, armed: bool = True) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.armed = armed
        self.last_headers: dict = {}
        self.last_raw: list[tuple[str, str]] = []

    def token(self) -> str:
        for c in self.jar:
            if c.name == security.CSRF_COOKIE:
                return c.value
        return ""

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.armed and method not in ("GET", "HEAD") and self.token():
            req.add_header(security.CSRF_HEADER, self.token())
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with self.opener.open(req, timeout=90) as resp:
                raw, status, hdrs = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as exc:
            raw, status, hdrs = exc.read(), exc.code, exc.headers
        self.last_headers = {k.lower(): v for k, v in dict(hdrs).items()}
        # The raw pairs, because `dict(hdrs)` collapses the two Set-Cookie lines into whichever
        # arrives last -- and the whole point of reading them is to see both the session cookie
        # (HttpOnly) and the token cookie (readable) on the same response.
        self.last_raw = [(k.lower(), v) for k, v in hdrs.items()]
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return status, {"_raw": raw[:200].decode("utf-8", "replace")}

    def set_cookies(self) -> list[str]:
        return [v for k, v in self.last_raw if k == "set-cookie"]


def wait_for_server(proc: subprocess.Popen, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def signup(c: Client, handle: str) -> tuple[int, dict]:
    return c.call("POST", "/api/auth/register", {
        "name": f"Security {handle}", "email": f"{handle}@invalid.example",
        "password": "verifypass1", "confirm": "verifypass1"})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aitd-security-verify-"))
    env = dict(os.environ)
    env.update({
        "DETECTOR_DB": str(tmp / "verify.sqlite3"),
        "DETECTOR_SECRET": str(tmp / "verify-secret.key"),
        "N8N_DB": str(tmp / "no-such-n8n.sqlite"),
        "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(PORT),
        "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
        # Accounts mode, stated not inherited: single-owner mode answers everything as the owner and
        # there is no session cookie for the CSRF rule to protect, so it would verify nothing.
        "LOCAL_SINGLE_USER": "0",
        "ADMIN_USERNAME": "verifyadmin", "ADMIN_PASSWORD": "verify-" + os.urandom(8).hex(),
    })
    for name in ("MAX_TEXT_LENGTH", "USE_SECURE_COOKIES"):
        env.pop(name, None)
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "backend.serve_local"],
                            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_for_server(proc):
            log.flush()
            print("server did not come up -- last lines of its own output:")
            print((tmp / "server.log").read_text(encoding="utf-8", errors="replace")[-2500:])
            print("kept for inspection:", tmp)
            return 2

        # ---------------------------------------------------------------- headers, both paths
        anon = Client()
        st, _ = anon.call("GET", "/api/healthz")
        missing = [h for h in security.RESPONSE_HEADERS if h.lower() not in anon.last_headers]
        check(st == 200 and not missing, "an API response carries the whole header set",
              f"GET /healthz -> {st}, missing={missing}")
        csp_api = anon.last_headers.get("content-security-policy", "")
        check(csp_api == security.CSP, "…and the CSP is the policy the module states",
              csp_api[:70] + "…")

        st, _ = anon.call("GET", "/")
        static_missing = [h for h in security.RESPONSE_HEADERS
                          if h.lower() not in anon.last_headers]
        check(st == 200 and not static_missing,
              "the SPA document itself is hardened too (serve_local's static responder, "
              "which Flask's after_request never sees)",
              f"GET / -> {st} {anon.last_headers.get('content-type', '')}, missing={static_missing}")
        check(anon.last_headers.get("content-security-policy") == csp_api,
              "…with the byte-same CSP as the API",
              "" if anon.last_headers.get("content-security-policy") == csp_api
              else "static and api policies differ")

        st, _ = anon.call("GET", "/api/no-such-route")
        check(st in (401, 404) and "content-security-policy" in anon.last_headers,
              "an error page is not a header-free zone", f"GET /api/no-such-route -> {st}")

        # ---------------------------------------------------------------- CSRF on a real session
        c = Client()
        st, body = signup(c, "alice")
        csrf_cookies = [h for h in c.set_cookies() if h.startswith(f"{security.CSRF_COOKIE}=")]
        check(st == 201 and c.token(), "sign-up issues a session token",
              f"POST /auth/register -> {st} {body.get('error_code', '')}")
        check(bool(csrf_cookies) and "HttpOnly" not in csrf_cookies[0]
              and "SameSite=Lax" in csrf_cookies[0],
              "the token cookie is readable by the page and Lax, as double-submit requires",
              csrf_cookies[0][:80] if csrf_cookies else "no token cookie on the response")
        session_cookies = [h for h in c.set_cookies() if not h.startswith(f"{security.CSRF_COOKIE}=")]
        check(bool(session_cookies) and "HttpOnly" in session_cookies[0],
              "…while the session cookie stays HttpOnly",
              session_cookies[0][:80] if session_cookies else "no session cookie")

        st, body = c.call("POST", "/api/auth/theme", {"theme": "light"})
        check(st == 200, "a signed-in write carrying the token is answered (the SPA's own shape)",
              f"POST /auth/theme -> {st} {body.get('error_code', '')}")

        unarmed = Client(armed=False)
        signup(unarmed, "bob")
        st, body = unarmed.call("POST", "/api/auth/theme", {"theme": "dark"})
        check(st == 403 and body.get("error_code") == "CSRF_TOKEN_MISSING",
              "the same write with the cookie but no token is refused (a forged form from another "
              "site looks exactly like this)",
              f"-> {st} {body.get('error_code')}")

        st, body = unarmed.call("POST", "/api/auth/theme", {"theme": "dark"},
                               headers={security.CSRF_HEADER: "guessed-token"})
        check(st == 403 and body.get("error_code") == "CSRF_TOKEN_MISSING",
              "a guessed token does not pass", f"-> {st} {body.get('error_code')}")

        st, body = unarmed.call("POST", "/api/auth/theme", {"theme": "dark"},
                                headers={security.CSRF_HEADER: unarmed.token(),
                                         "Sec-Fetch-Site": "cross-site"})
        check(st == 403 and body.get("error_code") == "CSRF_CROSS_SITE",
              "…and a real token loses to a browser that says this is not this site",
              f"-> {st} {body.get('error_code')}")

        st, body = unarmed.call("POST", "/api/auth/theme", {"theme": "dark"},
                                headers={security.CSRF_HEADER: unarmed.token(),
                                         "Origin": OTHER_SITE})
        check(st == 403 and body.get("error_code") == "CSRF_ORIGIN_MISMATCH",
              "a page on another origin is refused even holding a valid token",
              f"-> {st} {body.get('error_code')}")
        st, me = unarmed.call("GET", "/api/auth/me")
        check(st == 200 and (me.get("user") or {}).get("email") == "bob@invalid.example",
              "reads need no token -- the SPA bootstraps the token from a read",
              f"GET /auth/me -> {st}")

        # The dev loop: `ng serve` on 4321 proxies /api to this process on another loopback port.
        dev = Client(armed=False)
        signup(dev, "carol")
        st, body = dev.call("POST", "/api/auth/theme", {"theme": "light"},
                            headers={security.CSRF_HEADER: dev.token(),
                                     "Origin": "http://localhost:4321"})
        check(st == 200, "the ng-serve -> exe loopback pair counts as one site",
              f"-> {st} {body.get('error_code', '')}")

        # ---------------------------------------------------------------- token rotation
        rot = Client()
        signup(rot, "dora")
        first = rot.token()
        st, _ = rot.call("POST", "/api/auth/logout")
        check(st == 200 and not rot.token(), "sign-out deletes the token cookie",
              f"POST /auth/logout -> {st}, token still in jar={bool(rot.token())}")
        st, _ = rot.call("POST", "/api/auth/login", {"identifier": "dora@invalid.example",
                                                      "password": "verifypass1"})
        check(st == 200, "signing in again mints a fresh token", f"POST /auth/login -> {st}")
        st, body = rot.call("POST", "/api/auth/theme", {"theme": "light"})
        check(st == 200 and rot.token() != first,
              "a new session gets a new token, and the armed client follows it",
              f"-> {st} {body.get('error_code', '')}")
        stale = Client(armed=False)
        stale.call("POST", "/api/auth/login", {"identifier": "dora@invalid.example",
                                               "password": "verifypass1"})
        st, body = stale.call("POST", "/api/auth/theme", {"theme": "dark"},
                              headers={security.CSRF_HEADER: first})
        check(st == 403, "the previous session's token is not a token in the new one",
              f"-> {st} {body.get('error_code')}")

        # ---------------------------------------------------------------- login throttle
        guesser = Client(armed=False)
        statuses = [guesser.call("POST", "/api/auth/login",
                                 {"identifier": "guesser@invalid.example", "password": "wrong"})[0]
                    for _ in range(security.LOGIN_THROTTLE.limit)]
        st, body = guesser.call("POST", "/api/auth/login",
                                {"identifier": "guesser@invalid.example", "password": "wrong"})
        after = guesser.last_headers.get("retry-after", "")
        check(set(statuses) == {401} and st == 429 and body.get("error_code") == "RATE_LIMITED"
              and int(after or 0) > 0,
              f"{security.LOGIN_THROTTLE.limit} failures then the door closes, with a Retry-After",
              f"first {len(statuses)}={sorted(set(statuses))}, then {st} {body.get('error_code')} "
              f"Retry-After={after}")
        # Armed on purpose: the registration above already gave this jar a session, so its
        # sign-in is a signed-in write and the CSRF guard would answer 403 before the throttle
        # ever got asked. The point of the check is the 200, not the guard's opinion of it.
        neighbour = Client()
        signup(neighbour, "erin")
        st, _ = neighbour.call("POST", "/api/auth/login", {"identifier": "erin@invalid.example",
                                                            "password": "verifypass1"})
        check(st == 200, "one guesser does not lock out the neighbours",
              f"a clean sign-in from the same address -> {st}")

        # ---------------------------------------------------------------- payload ceiling
        # Far past every ceiling this app has ever shipped with (the engine default is 50 000),
        # so the check does not have to guess what the server's own .env said.
        big = "This is a long enough sample of text for the detector to accept it." + "x" * 250_000
        st, body = c.call("POST", "/api/detect", {"text": big})
        check(st == 413 and body.get("error_code") == "TEXT_TOO_LONG",
              "an oversized text is refused at the door, before any lane is dialled",
              f"POST /detect ({len(big)} chars) -> {st} {body.get('error_code')}")

        print(f"\n{sum(1 for ok, _ in results if ok)}/{len(results)} checks passed")
        failed = [label for ok, label in results if not ok]
        if failed:
            print("failed: " + "; ".join(failed))
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()



if __name__ == "__main__":
    sys.exit(main())
