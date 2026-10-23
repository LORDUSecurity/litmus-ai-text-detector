"""Smoke: does backend/engine.py import, and does a fixed gateway fixture round-trip?

Run from ai-text-detector/:  python -X utf8 -u tools/smoke_engine.py
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")

from backend import engine  # noqa: E402

CAPTURED = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        CAPTURED.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "body": body})
        payload = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "ai_probability": 0.91, "confidence": 0.82,
                "signals": [{"name": "uniform rhythm", "direction": "AI", "strength": 0.7}],
                "phrases": [{"quote": "the quick brown fox", "direction": "AI", "strength": 0.8}],
            })}, "finish_reason": "stop"}]
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

env = {
    "PRIMARY_BASE_URL": f"http://127.0.0.1:{port}/v1",
    "PRIMARY_MODEL": "test/primary",
    "SECONDARY_BASE_URL": f"http://127.0.0.1:{port}/v1",
    "SECONDARY_MODEL": "test/secondary",
    "ALLOWED_LLM_HOSTS": "127.0.0.1",
    "SECONDARY_ENABLED": "false",
}

status, out = engine.detect({"text": "the quick brown fox jumps over the lazy dog again and again"},
                            {}, env=env)
print("status:", status)
print("classification:", out.get("classification"), out.get("method"))
print("ai_probability:", out.get("ai_probability"), "confidence:", out.get("confidence"))
print("engine:", out["versions"]["engine"])
print("phrases:", out["evidence"]["models"][0]["result"]["phrases"])
print("requests seen:", len(CAPTURED), CAPTURED[0]["path"], CAPTURED[0]["auth"])
print("body keys:", list(CAPTURED[0]["body"].keys()))
print("user prompt head:", CAPTURED[0]["body"]["messages"][1]["content"][:60].replace("\n", "\\n"))

bad = engine.detect({"text": "short"}, {}, env=env)
print("too-short:", bad[0], bad[1]["error_code"], "|", bad[1]["error"])

nokey = dict(env)
nokey.pop("PRIMARY_MODEL")
st2, out2 = engine.detect({"text": "the quick brown fox jumps over the lazy dog again and again"},
                          {}, env={"ALLOWED_LLM_HOSTS": "integrate.api.nvidia.com"})
print("keyless remote:", st2, out2["error_code"])
srv.shutdown()
