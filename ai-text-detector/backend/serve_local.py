"""Single-process local launcher: serves the built Angular SPA *and* the JSON API
from one Flask process on 127.0.0.1, then opens the default browser.

This is what the PyInstaller `.exe` runs. It is deliberately NOT the Docker path
(there nginx serves the SPA and reverse-proxies /api). Here one process does both,
so the whole app is a double-click with no reverse proxy and no CORS dance.

Traffic split — the middleware IS the router
-------------------------------------------
The Angular service calls `const API = '/api'`, so every backend request the SPA
makes is prefixed: `/api/gateway-health`, `/api/detect`, `/api/auth/login`, ... .
Everything else (`/`, `/main-*.js`, `/styles-*.css`, `/favicon.ico`) is a static
SPA request. This mirrors the nginx config exactly:

    /api/*   -> strip the prefix, hand to Flask (routes + the auth guard)
    /*       -> serve a file from the built SPA, or index.html (client-side app)

Why the split happens in WSGI, *before* Flask:

1. The /api prefix must be stripped before routing. The backend routes are
   registered WITHOUT it (`@app.route("/detect")`), and auth._admin_required()
   matches *path prefixes* like "/providers" and "/models" to decide admin-only
   routes. If "/api/providers" reached the router the guard would stop matching and
   the gate would break open. Stripping first keeps router and guard on clean paths.

2. Static files must NOT go through Flask's route table. The backend already owns
   `@app.route("/")` (a JSON status endpoint) and its before_request auth guard
   401s unknown paths — so a Flask-registered SPA index/catch-all would be shadowed
   by the JSON root and blocked by the guard. Serving static directly from the
   middleware sidesteps both, exactly as nginx does (nginx never forwards `/` to the
   backend either). The JSON `/` status endpoint stays reachable at `/api/` if ever
   needed, and is a dev-only convenience the hosted SPA never calls.

Secrets: this launcher never bundles or reads a lane key into the binary. It runs
the idempotent seed, which reads a sibling `.env` in dev; a frozen exe ships with no
`.env`, so it seeds providers + measured benchmark/ranking data and NO keys. The
signed-in admin adds a key at runtime through the BYOK UI, same as on the hosted page.
"""
from __future__ import annotations

import mimetypes
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# --- data dir must be chosen BEFORE importing backend.db (it reads env at import) ---
FROZEN = getattr(sys, "frozen", False)


def _local_data_dir() -> Path:
    """Where a frozen exe keeps its SQLite DB + Fernet key: %LOCALAPPDATA%\\AITextDetector.

    Never beside the exe (Program Files is not writable) and never in the temp
    _MEIPASS unpack dir (wiped on exit) — history and the encryption key must
    survive across launches.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "AITextDetector"
    d.mkdir(parents=True, exist_ok=True)
    return d


if FROZEN:
    _d = _local_data_dir()
    os.environ.setdefault("DETECTOR_DB", str(_d / "detector.sqlite3"))
    os.environ.setdefault("DETECTOR_SECRET", str(_d / "backend-secret.key"))
    # A frozen exe has no live n8n DB to purge; leave the purger disabled rather than
    # pointing it at a path that does not exist.
    os.environ.setdefault("N8N_DB", "")

# Now it is safe to import the app (backend.db has captured DB_PATH/SECRET_PATH).
from werkzeug.serving import run_simple  # noqa: E402

from . import db, discovery, seed  # noqa: E402
from .app import create_app, purger  # noqa: E402

HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("BACKEND_PORT", "8787"))

# Windows' mimetypes registry maps .js to text/plain, which browsers refuse to run as
# an ES module (Angular's main-*.js is `type="module"`). Pin the types the SPA needs.
for _ext, _type in {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json",
    ".txt": "text/plain",
}.items():
    mimetypes.add_type(_type, _ext)


def resource_dir() -> Path:
    """Root that holds the bundled SPA (frontend/dist/frontend/browser) and benchmark/.

    Frozen: PyInstaller unpacks --add-data under sys._MEIPASS.
    Dev: the repo root (parent of backend/).
    """
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def spa_dir() -> Path:
    return resource_dir() / "frontend" / "dist" / "frontend" / "browser"


class SpaSplit:
    """WSGI middleware: /api/* -> Flask (prefix stripped); everything else -> static SPA."""

    def __init__(self, wsgi, spa_root: Path, prefix: str = "/api"):
        self.wsgi = wsgi
        self.spa_root = spa_root
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "") or "/"
        p = self.prefix
        if path == p or path.startswith(p + "/"):
            environ["PATH_INFO"] = "/" if path == p else path[len(p):]
            return self.wsgi(environ, start_response)
        return self._serve_static(path, environ, start_response)

    # --- minimal static file responder (no Flask, so no route order / auth guard) ---
    def _serve_static(self, path, environ, start_response):
        rel = path.lstrip("/")
        if rel in ("", "."):
            rel = "index.html"
        # Resolve and confine to the SPA root; reject traversal.
        try:
            candidate = (self.spa_root / rel).resolve()
            root = self.spa_root.resolve()
        except OSError:
            return self._bytes(start_response, b"SPA build missing", "404 Not Found", "text/plain")
        if root not in candidate.parents and candidate != root:
            candidate = root / "index.html"
        if not candidate.is_file():
            # SPA fallback: unknown path -> index.html so the client app owns navigation.
            candidate = root / "index.html"
        if not candidate.is_file():
            return self._bytes(
                start_response,
                b"SPA build not found. Run `npm run build` in frontend/ first.",
                "500 Internal Server Error",
                "text/plain",
            )
        data = candidate.read_bytes()
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        return self._bytes(start_response, data, "200 OK", ctype)

    @staticmethod
    def _bytes(start_response, body: bytes, status: str, ctype: str):
        headers = [
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-cache"),
        ]
        start_response(status, headers)
        return [body]


def build_app():
    """Create the Flask app, seed the local DB, and wrap it in the SPA/API splitter."""
    app = create_app()  # runs init_db() + seed_admin(); admin defaults to admin/admin

    # Full idempotent seed so the ranking page has its measured benchmark data even on a
    # fresh local DB. Reads a sibling .env in dev; a frozen exe has none, so this seeds
    # providers + benchmark/catalog rows and no keys.
    try:
        seed.run_seed()
    except Exception as exc:  # never let seeding block the app from opening
        print("  seed skipped:", exc)

    return SpaSplit(app, spa_dir())


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _open_browser_when_ready(url: str) -> None:
    # Opening the browser is the product's behaviour, but it has to be suppressible: verifying a
    # packaged build means starting it from a tool that is not the user, and it also serves anyone
    # who starts the app deliberately in the background. The URL is still printed, so nothing is
    # lost -- only the tab is skipped.
    if os.environ.get("AITD_NO_BROWSER"):
        return
    if _wait_for_port(HOST, PORT):
        try:
            webbrowser.open(url)
        except Exception:
            pass


def main() -> None:
    # The purge thread belongs to a serving process. With N8N_DB empty (frozen default)
    # it stays disabled; start() is a no-op then. In dev it purges the local n8n DB.
    try:
        purger.start()
    except Exception:
        pass
    # Same rule for discovery: it spends a user's key on real provider calls, so only a
    # serving process runs the worker. Without it, verifying a key reports
    # `discovery.status = "disabled"` instead of quietly queueing work nobody picks up.
    try:
        discovery.worker.start()
    except Exception:
        pass

    wrapper = build_app()
    url = f"http://{HOST}:{PORT}"
    spa = spa_dir()

    print("AI Text Detector — local")
    print("  open        =", url)
    print("  db          =", db.DB_PATH)
    print("  spa         =", spa, "(exists)" if spa.is_dir() else "(MISSING — run `npm run build`)")
    print("  purge       =", "enabled" if purger.enabled else f"disabled ({purger.error})")
    print("  discovery   =", "enabled" if discovery.worker.running else "disabled")
    print("  (close this window to stop the server)")

    threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
    run_simple(HOST, PORT, wrapper, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
