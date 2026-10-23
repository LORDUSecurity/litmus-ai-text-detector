"""Shared test plumbing for the CSRF layer.

A signed-in client now has to carry the session's CSRF token on every write, which is the same
contract the shipped SPA meets: its interceptor reads the token cookie and puts it on the header,
per request. So the test client does the same thing here, per request, rather than each of the ~40
existing writes in the auth and pool suites remembering a header -- and rather than a suite that
quietly skipped the check proving that the app works with the defence off.

Reading the cookie each time is not laziness, it is the difference between passing and lying: a
test that signs in as one account and then re-signs in as another has a NEW session token, and a
header stamped once at first login would keep sending the old one.

The failed-login window is process-wide by design, and one pytest run signs in as the same seeded
admin dozens of times, so `fresh_throttle` zeroes it for a freshly built app -- otherwise a module
that tests the throttle would starve every later module with a 429 it knows nothing about.
"""

from __future__ import annotations

from flask import Flask

from backend import security

_TOKEN_ENV_KEY = f"HTTP_{security.CSRF_HEADER.upper().replace('-', '_')}"


def arm_csrf(client):
    """Send whatever token this client's cookie jar currently holds, on every request."""
    original = client.open

    def open(*args, **kwargs):
        cookie = client.get_cookie(security.CSRF_COOKIE)
        if cookie is None:
            # Anonymous, exactly as an anonymous browser is: the guard's token rule only bites on a
            # request that carries a signed-in session, so sending nothing is the honest state.
            client.environ_base.pop(_TOKEN_ENV_KEY, None)
        else:
            client.environ_base[_TOKEN_ENV_KEY] = cookie.value
        return original(*args, **kwargs)

    client.open = open  # type: ignore[assignment]
    return client


def fresh_throttle(app: Flask) -> Flask:
    security.LOGIN_THROTTLE.reset()
    return app
