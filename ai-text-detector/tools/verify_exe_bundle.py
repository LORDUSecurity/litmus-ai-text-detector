#!/usr/bin/env python3
"""Prove the packaged .exe ships the UI in frontend/dist *and* the lane defaults users install.

PyInstaller embeds whatever was staged, so "I rebuilt the exe" is only worth as much as the
bundle it answers with. Name-matching is not enough: Angular's files are content-hashed, and the
browser loads the hash written into index.html. This checks three things a stale package can still
pass — the served index names the same bundle as dist, the served bytes hash to the same digest,
and a string from the change under test is present in what actually got downloaded.

Then it checks the two halves no hash can show: what the packaged backend *sends* to an engine
(a dev-tree DB copy must come out of the boot with the tie-break switched off), and what a
brand-new install gets seeded by the binary itself (one primary on its measured 240 s ceiling,
a tie-break row that ships disabled). The dev copy proves the upgrade path; only a fresh file
proves the first launch a user of the installer actually sees.

    python tools/verify_exe_bundle.py [substring]

The default substring is the lane-scale note added in round 24. Runs against a throwaway copy of
the dev database and a throwaway empty one, each on its own port, so it never touches the real
history or the shipped settings.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # `python tools/x.py` puts tools/ on sys.path, not the repo
                                # root, and this check reads backend.seed constants
EXE = ROOT / ".localbuild" / "out" / "AI-Text-Detector.exe"
DIST = ROOT / "frontend" / "dist" / "frontend" / "browser"
REAL_DB = ROOT / "backend" / "data" / "detector.sqlite3"
SECRET = ROOT / "backend" / "data" / "backend-secret.key"
PORT = int(os.environ.get("VERIFY_EXE_PORT", "8793"))
BASE = f"http://127.0.0.1:{PORT}"
# The clean-install boot runs after the first process is killed and its port released, but on its
# own number: a socket left in TIME_WAIT by a server that answered a request is not "free" in the
# sense this check needs, and reusing one port for two boots makes "who answered this?" ambiguous.
FRESH_PORT = PORT + 1

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)


def get(path: str, port: int = PORT) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}" + path, timeout=30) as resp:
            return resp.status, resp.read()
    except Exception as exc:  # noqa: BLE001 - a failed serve is a finding, not a crash
        return -1, str(exc).encode()


def ctype(path: str, port: int = PORT) -> str:
    """The Content-Type a URL came back with, because a font served as text/plain is a font the
    browser may not take -- and `get()` throws the headers away."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}" + path, timeout=30) as resp:
            return resp.headers.get("Content-Type") or ""
    except Exception:  # noqa: BLE001
        return ""


def port_is_free(port: int = PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


def owner_of_port(port: int = PORT) -> str:
    """Which PID already answers on a port, so the message names the thing to kill."""
    if sys.platform != "win32":
        return "unknown"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen).OwningProcess"],
            capture_output=True, text=True, timeout=20).stdout.split()
        return " ".join(out) or "unknown"
    except Exception:  # noqa: BLE001 - diagnostics only
        return "unknown"


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop the packaged server *and* its child.

    `proc.terminate()` on a PyInstaller onefile kills the bootloader and leaves the unpacked child
    running: after an earlier run of this tool, PID 12328 was still serving 8793 while the tool had
    "finished", and the port it held is the port the next run reads its proof from.
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True)
        proc.wait(timeout=20)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


class Session:
    """Cookie-holder for the packaged server: /detect is behind the auth gate, so a bare request
    answers 401 AUTH_REQUIRED (measured) and the check below would prove nothing about the engine."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path: str, payload: dict, timeout: float) -> tuple[int, dict]:
        req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                st, raw = resp.status, resp.read()
                ct = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            st, raw = exc.code, exc.read()
            ct = exc.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001 - a failed request is a finding, not a crash
            return -1, {"error": str(exc)}
        try:
            return st, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            # Almost always the SPA fallback answering an unmatched path with index.html. Kept as a
            # body rather than a crash so post_either can retry the other prefix and the detail
            # line can show what actually came back.
            return st, {"_not_json": True, "_ct": ct,
                        "_raw": raw[:160].decode("utf-8", "replace")}

    def post_either(self, path: str, payload: dict, timeout: float) -> tuple[int, dict]:
        """/api/ is the dev server's proxy prefix; the packaged app answers at the root.
        A 404 *or* a non-JSON body both mean "that path is not the API here"."""
        st, body = self.post("/api" + path, payload, timeout)
        if st in (404, -1) or body.get("_not_json"):
            st, body = self.post(path, payload, timeout)
        return st, body


def silent_engine() -> tuple[socket.socket, int, dict]:
    """A listener that completes the handshake, records the body it was sent, then never answers.

    A slow lane's point of view for the 502 check -- and the only way to see what the *packaged*
    backend actually puts on the wire. The shipped lane default is a property of the request the
    binary builds, and no hash of main-*.js or row of SQLite can show it. Only the parsed JSON
    body is kept: headers carry the lane keys, so they are never read into this dict or printed.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    held: list[socket.socket] = []
    seen: dict = {"bodies": []}

    def read_one(conn: socket.socket) -> None:
        buf = b""
        try:
            while True:
                conn.settimeout(5.0)
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                head = buf.find(b"\r\n\r\n")
                if head < 0:
                    continue
                length = 0
                for line in buf[:head].split(b"\r\n")[1:]:
                    k, _, v = line.partition(b":")
                    if k.strip().lower() == b"content-length":
                        try:
                            length = int(v.strip())
                        except ValueError:
                            length = 0
                if len(buf) - head - 4 >= length:
                    break
        except OSError:
            pass                       # the client gives up on us; that is the point of this socket
        held.append(conn)
        try:
            body = buf[buf.find(b"\r\n\r\n") + 4:] if buf.find(b"\r\n\r\n") >= 0 else b""
            parsed = json.loads(body.decode("utf-8", "replace"))
            if isinstance(parsed, dict) and "lane_config" in parsed:
                seen["bodies"].append(parsed)
        except Exception:  # noqa: BLE001 - a body we cannot parse is simply not evidence
            pass

    def accept_forever() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=read_one, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_forever, daemon=True).start()
    return srv, srv.getsockname()[1], seen


def fresh_install() -> None:
    """Boot the binary on a database that does not exist yet, then read what it seeded.

    The phase above runs on a copy of the developer's DB. That is the *upgrade* path -- a returning
    user -- and it cannot show what a first launch gets, because that install already has rows and
    `enabled` is deliberately absent from the seed's upsert. Whoever runs the installer is in the
    fresh case, so this boots against an empty path and reads the rows the packaged seed wrote.
    No engine is reachable on purpose: this phase never asks for a verdict.
    """
    from backend import seed as seed_mod

    tmp = Path(tempfile.mkdtemp(prefix="aitd-fresh-"))
    db, key = tmp / "fresh.sqlite3", tmp / "fresh.key"
    if not port_is_free(FRESH_PORT):
        check(False, "a clean install boots the packaged exe on an empty database",
              f"port {FRESH_PORT} already serving (PID {owner_of_port(FRESH_PORT)})")
        shutil.rmtree(tmp, ignore_errors=True)
        return

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db), "DETECTOR_SECRET": str(key),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(FRESH_PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                # Nothing listens here, so a detection could only ever be a refused connection.
                # LLM_TIMEOUT_MS and SECONDARY_ENABLED are NOT set: either would override the very
                # default under test, and SECONDARY_ENABLED in the launching shell is reported in
                # the detail line below so a failure explains itself.
                "N8N_URL": "http://127.0.0.1:1"})
    log = open(tmp / "fresh.log", "w", encoding="utf-8")
    proc = None
    try:
        proc = subprocess.Popen([str(EXE)], cwd=str(tmp), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        t0, up = time.time(), False
        while time.time() - t0 < 90:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", FRESH_PORT), timeout=1):
                    up = True
                    break
            except OSError:
                time.sleep(1.0)
        if not up:
            log.flush()
            check(False, "a clean install boots the packaged exe on an empty database",
                  f"exit={proc.poll()} :: "
                  f"{(tmp / 'fresh.log').read_text(encoding='utf-8', errors='replace')[-300:]}")
            return
        st, html = get("/", FRESH_PORT)
        check(st == 200, "a clean install boots the packaged exe on an empty database",
              f"listen after {time.time() - t0:.1f}s, GET / -> {st}, "
              f"db created={db.is_file()}, SECONDARY_ENABLED in launch env="
              f"{env.get('SECONDARY_ENABLED')!r}")
        served = re.findall(r'(main-[A-Z0-9]{8}\.js)', html.decode("utf-8", "replace"))
        check(bool(served), "…and serves the same current bundle from a virgin install",
              f"{served[:1]}")
    finally:
        if proc is not None:
            kill_tree(proc)
            for _ in range(10):
                if port_is_free(FRESH_PORT):
                    break
                time.sleep(1.0)
            else:
                check(False, "the clean-install run left its port free",
                      f"held by PID {owner_of_port(FRESH_PORT)}")
        log.close()

    try:
        if not db.is_file():
            check(False, "…and the boot seeds the shipped lane defaults", "no database file appeared")
            return
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = {r["lane"]: r for r in con.execute(
            "SELECT lane, model_id, enabled, timeout_ms, ai_prob_threshold, human_prob_threshold "
            "FROM models WHERE lane IS NOT NULL")}
        n_primary = con.execute("SELECT COUNT(*) FROM models WHERE lane = 'primary'").fetchone()[0]
        version = con.execute("SELECT value FROM settings WHERE key = 'lane_defaults_version'").fetchone()
        # The API-key guides and the paper link are what a first-time user reads on the Ranking
        # page, and a real install has no .env, so the frozen seed's defaults are the whole story.
        # Read from the seeded file rather than over HTTP on purpose: /config is authenticated and
        # the packaged app answers the bare path with the SPA index (measured 2026-10-16: a raw
        # GET /config on a virgin boot returned 200 text/html, /api/config returned 401
        # AUTH_REQUIRED), so an unprefixed fetch proves nothing about what was seeded.
        seeded = {r["key"]: r["value"] for r in
                  con.execute("SELECT key, value FROM settings WHERE key IN "
                              "('api_key_guides', 'paper_url')")}
        con.close()
        want = seed_mod.DEFAULT_LANES["primary"][0]
        band = seed_mod.LANE_BANDS.get(want, {})
        ceiling = seed_mod.LANE_TIMEOUT_MS.get(want, seed_mod.DEFAULT_LLM["LLM_TIMEOUT_MS"])
        prim, sec = rows.get("primary"), rows.get("secondary")
        check(n_primary == 1 and prim and prim["model_id"] == want,
              "…seeds one primary lane on the promoted model",
              f"n_primary={n_primary} rows={ {k: v['model_id'] for k, v in rows.items()} } want={want}")
        check(bool(band) and prim
              and abs(prim["ai_prob_threshold"] - band["AI_PROBABILITY_THRESHOLD"]) < 1e-9
              and abs(prim["human_prob_threshold"] - band["HUMAN_PROBABILITY_THRESHOLD"]) < 1e-9,
              "…on that model's own measured band",
              f"{prim['ai_prob_threshold']}/{prim['human_prob_threshold']} " if prim else "no primary")
        check(prim and prim["timeout_ms"] == ceiling,
              f"…with the {ceiling / 1000:.0f} s ceiling the capture actually supports",
              f"timeout_ms={prim['timeout_ms'] if prim else None} "
              f"(backend waits {(ceiling / 1000) + 30:.0f} s)")
        check(sec is not None and sec["enabled"] == 0
              and sec["model_id"] == seed_mod.DEFAULT_LANES["secondary"][0],
              "…and ships the tie-break lane configured but switched OFF",
              f"secondary={dict(sec) if sec else None} "
              f"lane_defaults_version={version[0] if version else None} "
              f"(expect {seed_mod.LANE_DEFAULTS_VERSION})")

        # Each card's wording is the part a stale bundle hides: the 2026-10-16 fetches showed
        # build.nvidia.com/settings/api-keys answering 404 to a logged-out GET, openrouter.ai/
        # settings/keys serving its own Sign In shell, and aistudio.google.com/apikey 302-ing to
        # accounts.google.com. A card that links straight in without saying "sign in first" reads
        # to the user as a broken link, so the shipped copy has to carry that wording.
        try:
            guides = json.loads(seeded.get("api_key_guides") or "[]")
        except Exception:  # noqa: BLE001
            guides = []
        check(len(guides) == len(seed_mod.DEFAULT_API_KEY_GUIDES)
              and [g.get("url") for g in guides]
              == [g["url"] for g in seed_mod.DEFAULT_API_KEY_GUIDES],
              "…seeds every provider's key guide, on the URLs the dev tree ships",
              f"{len(guides)} card(s) {[g.get('url') for g in guides]}")
        dead = [g.get("url") for g in guides
                if not str(g.get("url", "")).startswith("https://")
                or re.search(r"example\.(com|org)|localhost|127\.0\.0\.1|your-|\bTBD\b|\byour-domain\b",
                             str(g.get("url", "")))]
        check(not dead, "…no guide links a placeholder host", f"{dead}")
        no_signin = [g.get("provider") for g in guides
                     if not re.search(r"sign in|signed in|log ?in", str(g.get("steps", "")), re.I)]
        check(not no_signin, "…each card tells the user signing in comes first",
              f"{no_signin or 'ok'}")
        # paper_url is deliberately NOT defaulted (seed.py:621): a placeholder DOI would be a dead
        # citation on a page that tells people which model to trust. Absent or empty means the UI
        # renders no link, which is the honest state until the author sets the real one.
        check(seeded.get("paper_url") in (None, ""),
              "…and ships no fake paper link",
              f"paper_url={seeded.get('paper_url')!r} (seed.py:621 leaves it to the author)")
    except Exception as exc:  # noqa: BLE001
        check(False, "…and the boot seeds the shipped lane defaults", repr(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    needle = sys.argv[1] if len(sys.argv) > 1 else "tie-break lane"
    for path, hint in ((EXE, "run installer/BUILD.md step 2 first"),
                       (DIST, "run: npx --prefix frontend ng build")):
        if not path.exists():
            print(f"missing {path} — {hint}")
            return 2

    dist_index = (DIST / "index.html").read_text(encoding="utf-8", errors="replace")
    found = re.findall(r'(main-[A-Z0-9]{8}\.js)', dist_index)
    if not found:
        print("could not read a bundle name out of frontend/dist/index.html")
        return 2
    wanted = found[0]
    dist_bytes = (DIST / wanted).read_bytes()
    dist_sha = hashlib.sha256(dist_bytes).hexdigest()
    print(f"dist bundle: {wanted}  sha256={dist_sha[:16]}…  {len(dist_bytes)} bytes")

    tmp = Path(tempfile.mkdtemp(prefix="aitd-exe-bundle-"))
    db_copy = tmp / "run.sqlite3"
    shutil.copyfile(REAL_DB, db_copy) if REAL_DB.is_file() else db_copy.touch()

    # Refuse to run rather than report on somebody else's server. A leftover exe from an earlier
    # verification still holding PORT would answer every request below, and when the frontend did
    # not change between two backend builds the digests still match — six green checks about a
    # binary that never booted.
    if not port_is_free():
        print(f"port {PORT} is already serving (PID {owner_of_port()}); it is not this run's exe. "
              f"Kill it, or run with VERIFY_EXE_PORT=<free port>.")
        shutil.rmtree(tmp, ignore_errors=True)
        return 2

    # Point the packaged app at a stand-in engine that accepts the connection, keeps the body, and
    # then goes quiet. Digest checks prove the *UI* shipped; this proves the *backend* inside the
    # same binary -- what it sends to an engine, and how long it is willing to wait. The wait is
    # timeout_ms/1000 + 30, and timeout_ms comes from the boot re-seed, so the lever has to be the
    # environment: LLM_TIMEOUT_MS=1000 makes the seeded row itself fast, and the ~31 s pause below
    # replaces a 270 s one (glm's shipped ceiling is 240 s, proved separately in the fresh install).
    srv, silent_port, captured = silent_engine()
    # A random password written to the copy only, never printed: the copy's stored hash belongs to
    # the developer's account, and the check below has to be somebody to get past /detect's gate.
    from werkzeug.security import generate_password_hash
    pw = "verify-" + os.urandom(9).hex()
    admin_name = None
    con = sqlite3.connect(db_copy)
    row = con.execute("SELECT username FROM users WHERE role = 'admin' ORDER BY id").fetchone()
    admin_name = row[0] if row else None
    if admin_name:
        con.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                    (generate_password_hash(pw), admin_name))
    for k, v in (("n8n_url", f"http://127.0.0.1:{silent_port}"),
                 ("webhook_path", "/webhook/verify-exe-bundle")):
        con.execute("""INSERT INTO settings(key, value) VALUES(?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (k, v))
    con.execute("UPDATE models SET timeout_ms = 1000")
    con.commit()
    con.close()

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "DETECTOR_SECRET": str(SECRET),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                # Set on top of the DB write, because boot runs seed.run_seed(): seed_settings
                # rewrites `n8n_url` from N8N_URL and the models upsert rewrites `timeout_ms` back
                # to 90000. Measured — the first run of this check got a real 200 verdict from the
                # developer's local n8n, which is the packaged app working correctly and the check
                # pointing at the wrong thing. Env is the only lever a re-seed cannot take away.
                "N8N_URL": f"http://127.0.0.1:{silent_port}",
                "WEBHOOK_PATH": "/webhook/verify-exe-bundle",
                # ...and the same for the lane ceiling, which `seed_models` rewrites on every boot
                # from LANE_TIMEOUT_MS (240 s for glm). Setting it to 1000 keeps this run at ~31 s;
                # the shipped 240000 is proved in its own phase, below, where no timeout is set.
                "LLM_TIMEOUT_MS": "1000"})
    log = open(tmp / "exe.log", "w", encoding="utf-8")
    t0 = time.time()
    proc = subprocess.Popen([str(EXE)], cwd=str(tmp), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        up = False
        while time.time() - t0 < 90:                     # onefile unpacks on first run
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                    up = True
                    break
            except OSError:
                time.sleep(1.0)
        if not up:
            log.flush()
            print(f"exe did not listen on {PORT} (exit={proc.poll()}) — its own output:")
            print((tmp / "exe.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 2
        print(f"exe pid={proc.pid} listening after {time.time() - t0:.1f}s of unpack + boot")

        st, html = get("/")
        body = html.decode("utf-8", "replace")
        check(st == 200, "packaged exe serves the SPA at /", f"{st}")
        served = re.findall(r'(main-[A-Z0-9]{8}\.js)', body)
        check(bool(served), "its index names a bundle", str(served))
        check(served[:1] == [wanted], "served index names the dist bundle",
              f"served={served[:1]} dist={wanted}")

        st2, js = get(f"/{wanted}")
        sha = hashlib.sha256(js).hexdigest()
        check(st2 == 200 and len(js) == len(dist_bytes) and sha == dist_sha,
              "the served bundle is byte-identical to dist",
              f"{st2} {len(js)} bytes sha256={sha[:16]}…")
        check(needle.encode() in js, f"the change under test is in it (contains {needle!r})",
              f"{js.count(needle.encode())} occurrence(s)")

        st3, css = get("/")
        styles = re.findall(r'(styles-[A-Z0-9]{8}\.css)', css.decode("utf-8", "replace"))
        served_css = b""
        if styles:
            d = DIST / styles[0]
            st4, got = get(f"/{styles[0]}")
            served_css = got
            check(d.is_file() and st4 == 200
                  and hashlib.sha256(got).hexdigest() == hashlib.sha256(d.read_bytes()).hexdigest(),
                  "stylesheet too", styles[0])

        # The serif faces are the third thing a 200 cannot vouch for. The static responder answers an
        # unknown path with index.html and a 200, so a stage that dropped `fonts/` -- a `cp` without
        # `-r`, the exact trap installer/BUILD.md records -- still ships a page that *renders*, just in
        # whatever serif the OS has. Nothing fails. So the faces are read out of the served stylesheet
        # and compared to the bytes tools/fetch_fonts.py wrote, and the one thing a byte comparison
        # cannot see (whether the browser is allowed to treat it as a font) is taken from the header.
        faces = sorted(set(re.findall(r"url\(/fonts/([A-Za-z0-9_-]+\.woff2)",
                                      served_css.decode("utf-8", "replace"))))
        check(len(faces) >= 4, "the served stylesheet names its own self-hosted faces",
              f"{len(faces)} faces: {faces}")
        for face in faces:
            src = ROOT / "frontend" / "public" / "fonts" / face
            stf, blob = get(f"/fonts/{face}")
            check(stf == 200 and src.is_file() and blob == src.read_bytes()
                  and blob[:4] == b"wOF2",
                  f"…and the packaged exe serves {face} as the fetched file",
                  f"{stf} {len(blob)} bytes magic={blob[:4]!r} ctype={ctype(f'/fonts/{face}')!r}")

        # The citation is an asset of this binary as well. The static responder falls back to
        # index.html with a 200 for any path it does not have, so a status code proves nothing here --
        # the served bytes have to equal the report the repo cites.
        repo_rep = (ROOT / "research-zenodo" / "report.html").read_bytes()
        st_rep, rep = get("/research/report.html")
        check(st_rep == 200 and rep == repo_rep,
              "the packaged exe serves the technical report the Ranking page links",
              f"{st_rep} {len(rep)} bytes identical_to_deposit={rep == repo_rep}")
        st_pdf, pdf = get("/research/report.pdf")
        check(st_pdf == 200 and pdf.startswith(b"%PDF"), "…and the printable copy as a PDF",
              f"{st_pdf} {len(pdf)} bytes magic={pdf[:5]!r}")

        # The backend half of the same binary: a hash of main-*.js can never show this.
        sess = Session()
        st401, _ = sess.post_either("/auth/login",
                                    {"username": admin_name, "password": pw}, timeout=30)
        det_text = "Some text long enough to be sent to the engine for a verdict."
        st5, body5 = sess.post_either("/detect", {"text": det_text}, timeout=170)
        check(st401 == 200 and st5 == 502
              and body5.get("error_code") == "ENGINE_TIMEOUT"
              and body5.get("classification") == "DETECTION_UNAVAILABLE"
              and str(silent_port) in str(body5.get("engine_url", "") or body5.get("error", "")),
              "packaged backend calls a silent engine ENGINE_TIMEOUT, not unreachable",
              f"login={st401} detect={st5} {body5.get('error_code')} "
              f"engine_url={body5.get('engine_url')} timeout_s={body5.get('timeout_s')} "
              f":: {str(body5.get('error'))[:120]}")

        # What the packaged backend actually put on the wire. The dev-tree database it booted from
        # holds `enabled = 1` on the tie-break row and no lane-defaults key, so this is the upgrade
        # path a returning user takes, and the boot is what has to switch the lane off -- then say
        # so in the request, because an unnamed secondary means "the workflow decides" and the
        # workflow's own default names a second model. Keys live in headers only, and headers are
        # not read here; `lane_config` carries no credential.
        bodies = captured.get("bodies") or []
        sent = bodies[-1].get("lane_config") if bodies else None
        sec_sent = (sent or {}).get("secondary")
        lanes_named = sorted(k for k, v in (sent or {}).items()
                             if isinstance(v, dict) and v.get("model"))
        check(bool(bodies) and sec_sent == {"enabled": False}
              and lanes_named == ["primary"],
              "packaged backend sends the tie-break lane switched OFF",
              f"bodies={len(bodies)} secondary={sec_sent!r} lanes_with_a_model={lanes_named}")

        # The exe's own boot re-seed, read out of the database it just wrote. This is the
        # packaged app's real default lane -- not the dev tree's, not the DB the developer
        # has been editing -- and the promotion in #119 only reaches a user if the shipped
        # backend seeds it. Also catches the upgrade hazard: the previous default keeps
        # lane='primary' unless the seed un-claims it, and two primary rows means the answer
        # depends on row order while the UI shows a lane nobody chose.
        try:
            con = sqlite3.connect(f"file:{db_copy}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            lanes = {r["lane"]: r["model_id"] for r in con.execute(
                "SELECT lane, model_id FROM models WHERE lane IS NOT NULL")}
            n_primary = con.execute(
                "SELECT COUNT(*) FROM models WHERE lane = 'primary'").fetchone()[0]
            prim = con.execute(
                "SELECT model_id, ai_prob_threshold, human_prob_threshold, timeout_ms "
                "FROM models WHERE lane = 'primary'").fetchone()
            from backend import seed as seed_mod      # noqa: PLC0415  (ROOT is on sys.path
                                                       # only because this file puts it there)
            want = seed_mod.DEFAULT_LANES["primary"][0]
            band = seed_mod.LANE_BANDS.get(want, {})
            ok_lane = (n_primary == 1 and prim and prim["model_id"] == want
                       and lanes.get("secondary") == seed_mod.DEFAULT_LANES["secondary"][0])
            ok_band = bool(band) and prim and abs(prim["ai_prob_threshold"]
                          - band["AI_PROBABILITY_THRESHOLD"]) < 1e-9 and \
                abs(prim["human_prob_threshold"]
                    - band["HUMAN_PROBABILITY_THRESHOLD"]) < 1e-9
            check(ok_lane, "the packaged boot seeds the promoted default lane",
                  f"lanes={lanes} n_primary={n_primary} want={want}")
            check(ok_band, "…on that model's own measured band, not the workflow fallback",
                  f"{prim['ai_prob_threshold']}/{prim['human_prob_threshold']} "
                  f"measured={band.get('AI_PROBABILITY_THRESHOLD')}/"
                  f"{band.get('HUMAN_PROBABILITY_THRESHOLD')} timeout={prim['timeout_ms']}")

            # The upgrade half of the same boot. `enabled` never appears in the seed's DO UPDATE,
            # so a row an old install switched on stays on forever unless apply_lane_defaults says
            # otherwise -- and the copy handed to the exe is exactly such a row (enabled = 1, no
            # lane_defaults_version key). This is the returning-user case; a first install is the
            # insert path, proved in fresh_install().
            sec = con.execute("SELECT enabled FROM models WHERE lane = 'secondary'").fetchone()
            ver = con.execute("SELECT value FROM settings WHERE key = 'lane_defaults_version'").fetchone()
            check(sec and sec["enabled"] == 0
                  and ver and ver[0] == str(seed_mod.LANE_DEFAULTS_VERSION),
                  "…and an install that had the tie-break switched on is switched off exactly once",
                  f"secondary_enabled={sec['enabled'] if sec else None} "
                  f"lane_defaults_version={ver[0] if ver else None}")
            con.close()
        except Exception as exc:                                   # noqa: BLE001
            check(False, "the packaged boot seeds the promoted default lane", repr(exc))
    finally:
        srv.close()
        kill_tree(proc)
        released = True
        for _ in range(10):
            if port_is_free():
                break
            released = False
            time.sleep(1.0)
        check(released, "the run left the port free (no orphaned server)",
              f"still held by PID {owner_of_port()}" if not released else "")
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    fresh_install()

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for f in failed:
        print("  FAILED:", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
