#!/usr/bin/env python3
"""Prove the packaged .exe ships the UI in frontend/dist *and* the lane defaults users install.

PyInstaller embeds whatever was staged, so "I rebuilt the exe" is only worth as much as the
bundle it answers with. Name-matching is not enough: Angular's files are content-hashed, and the
browser loads the hash written into index.html. This checks three things a stale package can still
pass — the served index names the same bundle as dist, the served bytes hash to the same digest,
and a string from the change under test is present in what actually got downloaded.

Then it checks the three halves no hash can show. The packaged backend answers /detect **in this
process** (2026-10-19), so the first thing to prove is a negative: that it never dials a workflow
server. A listener is left open on the `n8n_url` the app boots with, and it must stay silent -- and
because "nothing arrived" is also what a broken listener looks like, the binary is booted a second
time with `DETECTION_ENGINE=n8n` to show that same listener recording the call and the lane the
boot switched off. The second check is the positive: a loopback stand-in gateway that *does* answer
shows what the in-process engine builds and dials itself, and the verdict comes back stamped
`versions.engine`. The third is what a brand-new install gets seeded by the binary itself (one
primary on its measured 240 s ceiling, a tie-break row that ships disabled). The dev copy proves the
upgrade path; only a fresh file proves the first launch a user of the installer actually sees.

And the third thing no bundle can show either way: that this binary is the deployment with **no
sign-in page**. One built SPA serves both apps and asks the backend which it is talking to, so
"the exe opens straight into Detect" is an answer from /auth/me, not a string in main-*.js — and
the sign-up form the hosted build shows has to be refused here, not merely hidden.

    python tools/verify_exe_bundle.py [substring]

The default substring is the lane-scale note added in round 24. It is searched in *both* served
halves — `main-*.js` and `styles-*.css` — because Angular folds every component `styles:` into the
stylesheet, so a CSS-only pass (the sharp/absolute-black one, needle `--chamfer`) is invisible to a
JS-only grep. Runs against a throwaway copy of the dev database and a throwaway empty one, each on
its own port, so it never touches the real history or the shipped settings.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))   # …and its sibling tools
import check_visual_language as vis  # noqa: E402          # the stylesheet rule, run on served bytes
import make_icon  # noqa: E402                             # …and the icon geometry, so the served favicon has an authority
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
# The forwarding boot (check the docstring: it is the witness test, not the shipped contract) needs
# its own number for the same reason the clean-install boot does -- the first exe is still being
# killed and its port released while the second one is asked to listen.
FWD_PORT = PORT + 2

# Two synthetic catalogue rows this run plants in the database copy (see stage_db_copy). They exist
# so the legacy-vocabulary check tests a fixture that is present on every machine the tool runs on,
# rather than whatever an old install happened to leave behind.
LEGACY_ASLEEP = "litmus-verify/asleep-on-a-timeout"
LEGACY_DUD = "litmus-verify/answered-with-no-score"

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


def get_json_either(path: str, port: int = PORT) -> tuple[int, dict]:
    """Anonymous GET of a JSON route, at whichever prefix this build serves the API.

    Needed because the exe's static responder answers an unknown path with index.html and a **200**
    (measured, 2026-10-16: a bare GET /config on a virgin boot returned the SPA shell). So a status
    code cannot tell "the API is not at this prefix" from "the API agreed" -- the body has to parse
    before the JSON is worth a check. `/api` is tried first because that is what the SPA calls.
    """
    st, raw = -1, b""
    for candidate in (f"/api{path}", path):
        st, raw = get(candidate, port)
        try:
            return st, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            continue
    return st, {"_not_json": True, "_raw": raw[:120].decode("utf-8", "replace")}


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

    def __init__(self, port: int = PORT) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def post(self, path: str, payload: dict, timeout: float) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode("utf-8"),
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

    def get_either(self, path: str, timeout: float = 30) -> tuple[int, dict]:
        """A cookie-carrying GET, same both-prefixes rule as post_either.

        The picker routes answer 401 AUTH_REQUIRED without a session, so the module-level bare
        `get()` can read the SPA shell on a packaged install and call it an empty list."""
        st, raw, ct = 404, b"", ""
        for base in ("/api", ""):
            try:
                with self.opener.open(self.base + base + path, timeout=timeout) as resp:
                    st, raw, ct = resp.status, resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                st, raw, ct = exc.code, exc.read(), exc.headers.get("Content-Type", "")
            except Exception as exc:  # noqa: BLE001 - a failed request is a finding, not a crash
                return -1, {"error": str(exc)}
            if st != 404 and "json" in ct:
                break
        try:
            return st, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return st, {"_not_json": True, "_ct": ct,
                        "_raw": raw[:160].decode("utf-8", "replace")}


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
    # `requests` counts every contact, parsed or not: the native engine is proven by what this
    # listener did NOT receive, and a body filter would let a malformed call look like silence.
    seen: dict = {"bodies": [], "requests": 0}

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
            seen["requests"] += 1
            threading.Thread(target=read_one, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_forever, daemon=True).start()
    return srv, srv.getsockname()[1], seen


# A decisive AI answer on any band the shipped lanes could be sitting on: seed.LANE_BANDS puts the
# highest AI cut at 0.80 (glm, the deployed primary) and everything else on DEFAULT_BAND's 0.69,
# and the highest AI *confidence* cut is DEFAULT_BAND's 0.60. 0.93/0.85 clears both by a margin, so
# the verdict below names the engine rather than a threshold that happened to move.
GATEWAY_AI_PROB = 0.93
GATEWAY_CONFIDENCE = 0.85


def canned_gateway() -> tuple[object, int, dict]:
    """A loopback OpenAI-shaped gateway that *answers*, so the in-process engine has a wire to pull.

    The engine inside the packaged binary is the thing under test, and the only way to see the
    request it builds -- model id, sampling fields, whether the retired token cap is really gone --
    is to be the endpoint it dials. The URL is named in the request's own `lane_config`, so the
    proof needs no lane key, no provider row surgery and no live pool: a loopback host is legal
    keyless by the engine's own rule, which is the point of it.

    Only the path and the parsed body are recorded. The Authorization header carries a real stored
    provider key when the lane has one, so headers are never read into this dict or printed.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: dict = {"paths": [], "bodies": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - the name IS the HTTP method
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                body = {"_unparseable": raw[:120].decode("utf-8", "replace")}
            is_chat = self.path.endswith("/chat/completions")
            answer = {"choices": [{"message": {"content": json.dumps(
                {"ai_probability": GATEWAY_AI_PROB, "confidence": GATEWAY_CONFIDENCE,
                 "signals": [], "phrases": []})}, "finish_reason": "stop"}]} if is_chat \
                else {"error": {"message": "no such endpoint"}}
            blob = json.dumps(answer).encode("utf-8")
            self.send_response(200 if is_chat else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            seen["paths"].append(self.path)
            seen["bodies"].append(body)

        def log_message(self, *args) -> None:   # the exe's own log is this run's transcript
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], seen


def in_process_engine_proof(sess: "Session", gw_port: int, gw: dict, witness: dict) -> None:
    """The packaged process scores the text itself: one request dialed, one verdict, zero calls to
    a workflow server. Assumes `sess` is already signed in as the copy's owner.

    `lane_config` names only the endpoint, never a model, so the lane still comes from the row the
    packaged boot seeded -- the deployed default is what gets dialed, just to a host this tool owns.
    """
    st_gw, gwhealth = get_json_either("/gateway-health")
    engine_name = gwhealth.get("detection_engine")
    n8n_state = gwhealth.get("n8n") or {}
    check(st_gw == 200 and engine_name == "python-native"
          and n8n_state.get("on_request_path") is False,
          "packaged backend answers /detect from its own process, and says so",
          f"health={st_gw} detection_engine={engine_name!r} "
          f"n8n.on_request_path={n8n_state.get('on_request_path')!r} "
          f"n8n_url={n8n_state.get('n8n_url')!r}")

    det_text = ("Some text long enough for the packaged engine to send to its own gateway "
                "for a verdict.")
    st, verdict = sess.post_either("/detect", {
        "text": det_text,
        "lane_config": {"primary": {"base_url": f"http://127.0.0.1:{gw_port}/v1"}}}, timeout=120)
    cs = verdict.get("component_scores") or {}
    used = verdict.get("config_used") or {}
    legs = (verdict.get("evidence") or {}).get("models") or []
    versions = verdict.get("versions") or {}
    # `SINGLE_MODEL_PRIMARY_AI`, not `PRIMARY_DECIDED_AI`: the boot switched the tie-break lane off
    # (proved below), so only the primary answered and engine.py's one-lane branch names the verdict
    # SINGLE_MODEL_PRIMARY + the verdict suffix. `PRIMARY_DECIDED_*` is only reachable when BOTH lanes
    # return a usable answer, so asserting it here would assert a rule this install does not walk --
    # and would go red on a correct binary rather than on a wrong one.
    check(st == 200 and verdict.get("classification") == "AI_GENERATED"
          and versions.get("engine") == "python-native"
          and cs.get("deciding_lane") == "primary"
          and verdict.get("method") == "SINGLE_MODEL_PRIMARY_AI",
          "…and a real detect through the packaged binary returns a verdict it built itself",
          f"{st} {verdict.get('classification')} method={verdict.get('method')} "
          f"engine={versions.get('engine')!r} p={verdict.get('ai_probability')} "
          f":: {str(verdict.get('error') or verdict.get('_raw') or '')[:140]}")

    # What the engine put on the wire, read out of the endpoint it dialed. `max_tokens` is the
    # retired cap: seed.LANE_* ship it as 0 and the engine sends no field at all, so a key here
    # would mean the packaged copy still truncates answers -- the bug #107 removed.
    bodies = gw.get("bodies") or []
    sent = bodies[-1] if bodies else {}
    check(len(bodies) == 1 and sent.get("model")
          and "max_tokens" not in sent and isinstance(sent.get("messages"), list)
          and sent["messages"] and det_text in str(sent["messages"][-1]),
          "…the engine dialed its gateway once, in this process, with the text and no token cap",
          f"hits={len(gw.get('paths') or [])} model={sent.get('model')!r} "
          f"max_tokens={sent.get('max_tokens')!r} temperature={sent.get('temperature')!r} "
          f"paths={gw.get('paths')}")
    check(used.get("primary", {}).get("base_url", "").startswith(f"http://127.0.0.1:{gw_port}")
          and bool(sent.get("model")),
          "…and the answer names the endpoint it actually asked",
          f"config_used.primary={used.get('primary')} "
          f"key_source={used.get('primary', {}).get('key_source')!r}")

    # The tie-break ships off, and the live answer is where a user sees that: the old proof was the
    # `lane_config` this process used to send to a workflow, which it no longer sends to anything.
    check(cs.get("secondary_called") is False and len(legs) == 1
          and used.get("secondary", {}).get("enabled") is False,
          "…with the tie-break lane switched off on its way back",
          f"legs={len(legs)} secondary_called={cs.get('secondary_called')} "
          f"secondary_enabled={used.get('secondary', {}).get('enabled')} "
          f"lanes={[l.get('lane') for l in legs]}")

    # The negative that the whole switch is about. This listener is a real socket that records what
    # it is sent -- the forwarding boot below shows it recording -- so silence here is the answer,
    # not an outage.
    contacts = witness.get("requests") or 0
    check(contacts == 0, "…and nothing at all was ever sent to a workflow server",
          f"connections the n8n_url listener accepted={contacts} "
          f"bodies={len(witness.get('bodies') or [])}")


def forwarding_proof(admin_pw: str, gw_seen: dict) -> None:
    """The same binary booted once onto the workflow path, so the silence above is a witness.

    "The listener accepted nothing" is only evidence if the listener could have accepted something,
    and a check that asserts an absence always risks asserting a hole. So boot with
    `DETECTION_ENGINE=n8n` -- the documented way to put a local install back on the workflow -- and
    read the three things that path still has to get right: the call really arrives, the boot's
    lane-default upgrade says `secondary: {"enabled": false}` on the way out (an unnamed secondary
    means "the workflow decides", and the workflow's own default names a second model), and a
    silent engine is answered as ENGINE_TIMEOUT naming the URL rather than as a verdict. The engine
    that just scored the text on its own must not dial a provider from this path.
    """
    srv, witness_port, captured = silent_engine()
    tmp = Path(tempfile.mkdtemp(prefix="aitd-fwd-"))
    db_copy = tmp / "fwd.sqlite3"
    if not port_is_free(FWD_PORT):
        check(False, "the workflow path still works on the same binary",
              f"port {FWD_PORT} already serving (PID {owner_of_port(FWD_PORT)})")
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)
        return
    admin_name = stage_db_copy(db_copy, witness_port, admin_pw)
    gw_hits_before = len(gw_seen.get("bodies") or [])

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "DETECTOR_SECRET": str(SECRET),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(FWD_PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                "DETECTION_ENGINE": "n8n",
                "N8N_URL": f"http://127.0.0.1:{witness_port}",
                "WEBHOOK_PATH": "/webhook/verify-exe-bundle",
                "LLM_TIMEOUT_MS": "1000"})
    log = open(tmp / "fwd.log", "w", encoding="utf-8")
    proc = None
    try:
        proc = subprocess.Popen([str(EXE)], cwd=str(tmp), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        t0, up = time.time(), False
        while time.time() - t0 < 90:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", FWD_PORT), timeout=1):
                    up = True
                    break
            except OSError:
                time.sleep(1.0)
        if not up:
            log.flush()
            check(False, "the workflow path still works on the same binary",
                  f"exit={proc.poll()} :: "
                  f"{(tmp / 'fwd.log').read_text(encoding='utf-8', errors='replace')[-300:]}")
            return
        st_gw, health = get_json_either("/gateway-health", FWD_PORT)
        n8n_state = health.get("n8n") or {}
        check(st_gw == 200 and health.get("detection_engine") == "n8n"
              and n8n_state.get("on_request_path") is True,
              "…and DETECTION_ENGINE=n8n puts this same binary back on the workflow",
              f"{st_gw} detection_engine={health.get('detection_engine')!r} "
              f"on_request_path={n8n_state.get('on_request_path')!r}")

        sess = Session(FWD_PORT)
        st_login, _ = sess.post_either("/auth/login", {"username": admin_name, "password": admin_pw},
                                       timeout=30)
        st5, body5 = sess.post_either("/detect",
                                      {"text": "Some text long enough to be sent to a workflow "
                                              "for a verdict."}, timeout=170)
        check(st_login == 200 and st5 == 502
              and body5.get("error_code") == "ENGINE_TIMEOUT"
              and body5.get("classification") == "DETECTION_UNAVAILABLE"
              and str(witness_port) in str(body5.get("engine_url", "") or body5.get("error", "")),
              "…a silent engine is answered ENGINE_TIMEOUT naming the URL, not a verdict",
              f"login={st_login} detect={st5} {body5.get('error_code')} "
              f"engine_url={body5.get('engine_url')} timeout_s={body5.get('timeout_s')} "
              f":: {str(body5.get('error'))[:120]}")

        bodies = captured.get("bodies") or []
        sent = bodies[-1].get("lane_config") if bodies else None
        sec_sent = (sent or {}).get("secondary")
        lanes_named = sorted(k for k, v in (sent or {}).items()
                             if isinstance(v, dict) and v.get("model"))
        check(bool(bodies) and sec_sent == {"enabled": False}
              and lanes_named == ["primary"],
              "…and the call it sends names one lane with the tie-break switched OFF",
              f"connections={captured.get('requests')} bodies={len(bodies)} "
              f"secondary={sec_sent!r} lanes_with_a_model={lanes_named}")
        check(len(gw_seen.get("bodies") or []) == gw_hits_before,
              "…while the in-process gateway stays untouched on that path",
              f"gateway hits before={gw_hits_before} after={len(gw_seen.get('bodies') or [])}")
    finally:
        if proc is not None:
            kill_tree(proc)
            for _ in range(10):
                if port_is_free(FWD_PORT):
                    break
                time.sleep(1.0)
            else:
                check(False, "the forwarding run left its port free",
                      f"held by PID {owner_of_port(FWD_PORT)}")
        log.close()
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)


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
        # The first-run experience, which no dev database can show: a brand-new install has never
        # seen a login, so if the owner row were missing the guard would have nobody to sign in as
        # and the page would open on an app that answers 401 to everything -- with no sign-in form
        # to fix it. One check on the HTTP answer covers both halves of that.
        st_me, me = get_json_either("/auth/me", FRESH_PORT)
        owner = me.get("user") or {}
        check(st_me == 200 and me.get("auth_mode") == "local"
              and owner.get("role") == "admin",
              "…a first-time user is signed in as the seeded owner, no sign-in card",
              f"{st_me} auth_mode={me.get('auth_mode')!r} user={owner.get('username')!r} "
              f"role={owner.get('role')!r}")
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


def stage_db_copy(dest: Path, witness_port: int, admin_pw: str) -> str | None:
    """A throwaway copy of the developer's database, pointed at this run's stand-ins.

    Every value is an upsert rather than only launch-environment, because `seed_settings` fills a
    key just when it is *absent* -- and a developer's database has all of them. Measured the hard
    way: the first run of this tool got a real 200 verdict from the developer's own local n8n, which
    was the packaged app working correctly and the check reading the wrong server. `timeout_ms` is
    a plain UPDATE because `seed_models` rewrites it from LANE_TIMEOUT_MS on every boot; the shipped
    240 s ceiling is proved in the clean-install phase instead, where no timeout is set at all.

    Returns the admin username the copy signs in as, or None when it has no admin.
    """
    from werkzeug.security import generate_password_hash

    shutil.copyfile(REAL_DB, dest) if REAL_DB.is_file() else dest.touch()
    con = sqlite3.connect(dest)
    row = con.execute("SELECT username FROM users WHERE role = 'admin' ORDER BY id").fetchone()
    admin = row[0] if row else None
    if admin:
        con.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                    (generate_password_hash(admin_pw), admin))
    for k, v in (("n8n_url", f"http://127.0.0.1:{witness_port}"),
                 ("webhook_path", "/webhook/verify-exe-bundle"),
                 # The engine's own default list, spelled out so a stored value that predates the
                 # loopback rule cannot reject the gateway this tool stands in for it.
                 ("allowed_llm_hosts", "localhost,127.0.0.1,::1,integrate.api.nvidia.com,"
                                       "openrouter.ai")):
        con.execute("""INSERT INTO settings(key, value) VALUES(?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (k, v))
    con.execute("UPDATE models SET timeout_ms = 1000")
    # Put the copy back into the state a first install was in before the tie-break shipped off:
    # the row switched on, and no record that the lane defaults were ever applied. A developer's
    # database has been through `apply_lane_defaults` already (measured: `enabled = 0` and
    # `lane_defaults_version = 2`), so booting a faithful copy would skip the switch entirely and
    # the two checks that read it afterwards would go green having proved nothing.
    con.execute("DELETE FROM settings WHERE key = 'lane_defaults_version'")
    con.execute("UPDATE models SET enabled = 1 WHERE lane = 'secondary'")
    # And the state the legacy-vocabulary fix exists to repair, planted rather than trusted to
    # whatever the developer's copy holds: two catalogue rows in the words a build from before the
    # timeout/refusal split wrote, and **no `models` row for either** -- which is precisely what made
    # an asleep model invisible to every version of the picker. One ran out of time; the other
    # returned HTTP 200 with a safety label in it, which is the case the old word `answered` cannot
    # tell apart from a score, so the key that records the re-read is deleted for the same reason
    # `lane_defaults_version` is: a copy that already migrated would go green having run nothing.
    con.execute("DELETE FROM settings WHERE key = 'probe_vocab_version'")
    prov = con.execute("SELECT name FROM providers WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    if prov:
        for mid, st, note in ((LEGACY_ASLEEP, "timeout", "This operation was aborted"),
                              (LEGACY_DUD, "answered", "User Safety: safe finish=stop")):
            con.execute("DELETE FROM models WHERE model_id = ?", (mid,))
            con.execute("DELETE FROM catalog_models WHERE model_id = ?", (mid,))
            con.execute("""INSERT INTO catalog_models (provider, model_id, listed, probe_status,
                                                       probe_note, latency_ms)
                           VALUES (?,?,1,?,?,18000)""", (prov[0], mid, st, note))
    con.commit()
    con.close()
    return admin


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

    # Refuse to run rather than report on somebody else's server. A leftover exe from an earlier
    # verification still holding PORT would answer every request below, and when the frontend did
    # not change between two backend builds the digests still match — six green checks about a
    # binary that never booted.
    for probe in (PORT, FWD_PORT, FRESH_PORT):
        if not port_is_free(probe):
            print(f"port {probe} is already serving (PID {owner_of_port(probe)}); it is not this "
                  f"run's exe. Kill it, or run with VERIFY_EXE_PORT=<free port>.")
            shutil.rmtree(tmp, ignore_errors=True)
            return 2

    # Two stand-ins, because this binary is now its own engine. `silent_port` is the workflow server
    # it must NOT contact: it accepts, keeps what it was sent and then goes quiet, so a call there
    # is visible -- and `forwarding_proof()` boots the same binary onto that path once, which is what
    # makes the silence in this phase an answer rather than an outage. `gw_port` is a gateway that
    # *does* answer: the only way to read the request the in-process engine builds and dials. Both
    # wait times are set by the environment, because the lane ceiling comes back out of the boot
    # re-seed: LLM_TIMEOUT_MS=1000 makes the seeded row fast, so the forwarding pause below is ~31 s
    # rather than 270 s (glm's shipped 240 s ceiling is proved in the clean-install phase).
    srv, silent_port, captured = silent_engine()
    gateway, gw_port, gw_seen = canned_gateway()
    # A random password written to the copy only, never printed: the copy's stored hash belongs to
    # the developer's account, and the check below has to be somebody to get past /detect's gate.
    pw = "verify-" + os.urandom(9).hex()
    admin_name = stage_db_copy(db_copy, silent_port, pw)

    env = dict(os.environ)
    env.update({"DETECTOR_DB": str(db_copy), "DETECTOR_SECRET": str(SECRET),
                "BACKEND_HOST": "127.0.0.1", "BACKEND_PORT": str(PORT),
                "AITD_NO_BROWSER": "1", "PYTHONIOENCODING": "utf-8",
                # Stated on top of the DB write: `seed_settings` fills a key only when absent and
                # `seed_models` rewrites timeout_ms every boot, so the environment is the one lever
                # that outranks whatever the developer's copy already holds. 1000 ms keeps this run
                # at ~31 s; the shipped 240000 is proved in the clean-install phase, where nothing
                # is set. SECONDARY_ENABLED and DETECTION_ENGINE are NOT set -- either would
                # override the default under test.
                "N8N_URL": f"http://127.0.0.1:{silent_port}",
                "WEBHOOK_PATH": "/webhook/verify-exe-bundle",
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

        # The change under test can live in either half of the bundle, and a CSS-only pass lives only in
        # the stylesheet: Angular folds every component `styles:` into styles-*.css, so grepping
        # main-*.js alone would fail a package that did ship the change. Both served bytes are searched
        # and the report says which one carried it.
        n_js, n_css = js.count(needle.encode()), served_css.count(needle.encode())
        check(bool(n_js or n_css), f"the change under test is in what the exe serves (contains {needle!r})",
              f"main {n_js} occurrence(s), stylesheet {n_css}")
        # …and the shipped stylesheet is one a parser will *apply*, not merely one that contains the
        # right strings. Same rule tools/check_visual_language.py holds the source to and the static page
        # holds its own <style> to — now measured on the compiler's output, where a dropped :root or a
        # stray top-level token would otherwise be invisible to any grep.
        breaks = vis.style_guards(served_css.decode("utf-8", "replace"), tokens=True)
        check(not breaks, "the served stylesheet is read as written",
              "; ".join(breaks[:4]) if breaks else
              f"{len(served_css):,} B: :root parsed, absolute black + white, chamfer applied, no glass")

        # The serif face is the third thing a 200 cannot vouch for. The static responder answers an
        # unknown path with index.html and a 200, so a stage that dropped `fonts/` -- a `cp` without
        # `-r`, the exact trap installer/BUILD.md records -- still ships a page that *renders*, just in
        # whatever serif the OS has. Nothing fails. So the faces are read out of the served stylesheet
        # and compared to the bytes tools/fetch_fonts.py wrote, and the one thing a byte comparison
        # cannot see (whether the browser is allowed to treat it as a font) is taken from the header.
        # The floor is one, not four: the shipped stack moved to a single variable Literata cut
        # (wght 400-900) precisely so the four static Spectral weights stopped being carried, and an
        # assertion on a count that can no longer grow would only be a claim about the old design.
        faces = sorted(set(re.findall(r"url\(/fonts/([A-Za-z0-9_-]+\.woff2)",
                                      served_css.decode("utf-8", "replace"))))
        check(len(faces) >= 1, "the served stylesheet names its own self-hosted faces",
              f"{len(faces)} faces: {faces}")
        for face in faces:
            src = ROOT / "frontend" / "public" / "fonts" / face
            stf, blob = get(f"/fonts/{face}")
            check(stf == 200 and src.is_file() and blob == src.read_bytes()
                  and blob[:4] == b"wOF2",
                  f"…and the packaged exe serves {face} as the fetched file",
                  f"{stf} {len(blob)} bytes magic={blob[:4]!r} ctype={ctype(f'/fonts/{face}')!r}")

        # The tab icon, same shape of failure and same cure: it is a generated asset, so the served
        # bytes are compared to what tools/make_icon.py's geometry writes, not to a filename.
        for icon in ("favicon.svg", "favicon.ico"):
            st_i, blob_i = get(f"/{icon}")
            want_i = (make_icon.svg_text(make_icon.GEOMETRY).encode() if icon.endswith(".svg")
                      else make_icon.ico_bytes(make_icon.GEOMETRY))
            check(st_i == 200 and blob_i == want_i,
                  f"the packaged exe serves the {icon} the geometry says",
                  f"{st_i} {len(blob_i)} bytes, generator says {len(want_i)} "
                  f"ctype={ctype(f'/{icon}')!r}")

        # The provider logos are the same silent failure one more time, and worse: a mark that 404s
        # falls back to index.html with a 200, so the browser gets an HTML document where a PNG
        # belongs and the row simply shows nothing where a brand should light up. The list is read
        # out of the map the component itself uses, so a logo added to the app without its file (or
        # a file deleted without the map) fails here instead of shipping a grey hole.
        brand_src = (ROOT / "frontend" / "src" / "app" / "core" / "provider-brand.ts").read_text(encoding="utf-8")
        logos = re.findall(r":\s*'((/brand/[^']+\.png))'", brand_src)
        check(len(logos) >= 1, "the brand map names its own logo files", f"{len(logos)} marks")
        for _path in [p for _slug, p in logos]:
            st_b, blob_b = get(_path)
            want_b = (ROOT / "frontend" / "public" / _path.lstrip("/")).read_bytes() \
                if (ROOT / "frontend" / "public" / _path.lstrip("/")).is_file() else b""
            check(st_b == 200 and want_b and blob_b == want_b and blob_b[:8] == b"\x89PNG\r\n\x1a\n",
                  f"…and the packaged exe serves {_path} as the file the map points at",
                  f"{st_b} {len(blob_b)} bytes magic={blob_b[:4]!r} ctype={ctype(_path)!r}")

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
        #
        # Start with the two answers that decide what the page *is*. One built bundle serves both
        # deployments, and the front-end reads `auth_mode` off /auth/me before it renders anything:
        # "local" opens straight into Detect, "accounts" shows the sign-in card. So the exe's lack of
        # a login page is not a frontend property that dist can be searched for -- it is this answer,
        # and it has to be read from the packaged server with no cookie at all. The 409 is the other
        # half: an install that signs its owner in automatically has nothing to sign up for, and a
        # page that offered a form leading here would be a lie.
        st_me, me = get_json_either("/auth/me")
        owner = me.get("user") or {}
        check(st_me == 200 and me.get("auth_mode") == "local"
              and owner.get("role") == "admin",
              "packaged exe says auth_mode=local and answers as its owner with no cookie",
              f"{st_me} auth_mode={me.get('auth_mode')!r} user={owner.get('username')!r} "
              f"role={owner.get('role')!r}")
        st_reg, reg = Session().post_either(
            "/auth/register", {"name": "Second Owner", "email": "second@invalid.example",
                               "password": "verifypass1", "confirm": "verifypass1"}, timeout=30)
        check(st_reg == 409 and reg.get("error_code") == "SINGLE_USER_MODE",
              "…and refuses a sign-up, because a single-owner install has none",
              f"{st_reg} {reg.get('error_code') or reg.get('_raw') or reg}")

        sess = Session()
        st401, _ = sess.post_either("/auth/login",
                                    {"username": admin_name, "password": pw}, timeout=30)
        check(st401 == 200, "…and the owner of the copy can sign in on the packaged API",
              f"login={st401}")
        if st401 != 200:
            check(False, "packaged backend answers /detect from its own process, and says so",
                  f"no session: login answered {st401}, so nothing below could be asked")
        else:
            in_process_engine_proof(sess, gw_port, gw_seen, captured)

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
            from backend import discovery as disc_mod  # …and the ceiling a recovered row is given
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
            # otherwise -- and stage_db_copy() hands the exe exactly such a row (enabled = 1, no
            # lane_defaults_version key). This is the returning-user case; a first install is the
            # insert path, proved in fresh_install().
            sec = con.execute("SELECT enabled FROM models WHERE lane = 'secondary'").fetchone()
            ver = con.execute("SELECT value FROM settings WHERE key = 'lane_defaults_version'").fetchone()
            check(sec and sec["enabled"] == 0
                  and ver and ver[0] == str(seed_mod.LANE_DEFAULTS_VERSION),
                  "…and an install that had the tie-break switched on is switched off exactly once",
                  f"secondary_enabled={sec['enabled'] if sec else None} "
                  f"lane_defaults_version={ver[0] if ver else None}")

            # The last thing an existing install needs from a rebuild, and the only thing a bundle
            # hash cannot show: rows written before the timeout/refusal split used words this code
            # never produces, and a `timeout` row carried no `models` row -- so on the page whose
            # whole job is saying which models answer, the ones that merely ran out of time were
            # absent. The two rows planted in stage_db_copy() are the fixture.
            pv = con.execute("SELECT value FROM settings WHERE key = 'probe_vocab_version'").fetchone()
            st_asleep = con.execute("SELECT probe_status FROM catalog_models WHERE model_id = ?",
                                    (LEGACY_ASLEEP,)).fetchone()
            st_dud = con.execute("SELECT probe_status FROM catalog_models WHERE model_id = ?",
                                 (LEGACY_DUD,)).fetchone()
            dud_row = con.execute("SELECT 1 FROM models WHERE model_id = ?", (LEGACY_DUD,)).fetchone()
            asleep_row = con.execute("SELECT timeout_ms FROM models WHERE model_id = ?",
                                     (LEGACY_ASLEEP,)).fetchone()
            check(bool(pv), "the packaged boot re-reads an older build's vocabulary once",
                  f"probe_vocab_version={pv[0] if pv else None} "
                  f"(fixture planted: {'yes' if st_asleep and st_dud else 'NO'})")
            check(st_asleep and st_asleep[0] == "slow",
                  "…so a model the old build blamed on a timeout is `slow`, not written off",
                  f"probe_status={st_asleep[0] if st_asleep else None}")
            check(bool(asleep_row),
                  "…and it has the `models` row that makes it pickable at all",
                  f"timeout_ms={asleep_row[0] if asleep_row else None} "
                  f"(ceiling {disc_mod.DISCOVERED_TIMEOUT_MS})")
            check(st_dud and st_dud[0] == "dead" and dud_row is None,
                  "…while an `answered` that recorded no score stays a refusal and earns no row",
                  f"probe_status={st_dud[0] if st_dud else None} models_row={bool(dud_row)}")

            st_opt, opts = sess.get_either("/my/pool/options")
            entries = {e.get("model"): e for e in (opts.get("options") or [])}
            pick = entries.get(LEGACY_ASLEEP)
            check(bool(pick), "…and the packaged picker LISTS the model that was asleep",
                  f"GET /my/pool/options={st_opt}, {len(entries)} entries, "
                  f"asleep present={bool(pick)}"
                  + ("" if "options" in opts else f" — no JSON answer either way: {str(opts)[:170]}"))
            if pick:
                check(pick.get("measured") is False and pick.get("in_pool") is False,
                      "…as unmeasured but pickable, not as a model that scored anything",
                      f"measured={pick.get('measured')} in_pool={pick.get('in_pool')} "
                      f"probe_status={pick.get('probe_status')}")
            check(LEGACY_DUD not in entries,
                  "…and the refusal is not offered as a model to add",
                  f"{LEGACY_DUD in entries}")
            con.close()
        except Exception as exc:                                   # noqa: BLE001
            check(False, "the packaged boot seeds the promoted default lane", repr(exc))
    finally:
        gateway.shutdown()
        gateway.server_close()
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

    forwarding_proof(pw, gw_seen)

    fresh_install()

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for f in failed:
        print("  FAILED:", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
