"""Deterministic proof that SQLite-stored parameters reach the LLM request.

The real providers jitter, so a verdict change can never be attributed to a
threshold edit with certainty. This runs the primary lane against a loopback
echo (tools/tuning-echo.js) that always answers with the same probability, which
makes every observed difference attributable to the number that was edited.

    node tools/tuning-echo.js 20128 0.62 &
    python tools/verify_tuning_echo.py

What it asserts:
  1. temperature / top_p / max_tokens read off the DB row appear in the outbound
     chat-completion body (edited once, then edited again to show it tracks).
  2. ai_prob_threshold read off the same row changes the decision path
     (primary decides vs defers to the tie-break lane).
  3. The lanes are restored to the calibrated pair and the probe rows deleted.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
ECHO = "http://127.0.0.1:20128"
TEXT = "The quick brown fox jumps over the lazy dog and then sits down again."
PROVIDER = "tuning-echo"


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def echo_body():
    with urllib.request.urlopen(ECHO + "/last", timeout=10) as r:
        last = json.loads(r.read().decode())
    return (last or {}).get("body") or {}


def detect():
    status, resp = call("POST", BASE + "/detect", {"text": TEXT, "requested_by": "tuning-echo-proof"})
    if status != 200:
        raise SystemExit(f"detect returned {status}: {json.dumps(resp)[:500]}")
    return resp


def main():
    checks = []

    def check(ok, description):
        checks.append((bool(ok), description))
        print(("  PASS  " if ok else "  FAIL  ") + description)

    # A crashed earlier run can leave the probe row installed as the primary lane;
    # clear it before snapshotting, or the snapshot records the wrong "shipped" state.
    _, providers0 = call("GET", BASE + "/providers")
    for p in [x for x in providers0 if x["name"] == PROVIDER]:
        call("DELETE", f"{BASE}/providers/{p['id']}")

    _, models_before = call("GET", BASE + "/models")
    shipped = {m["model_id"]: m["lane"] for m in models_before}
    primary_id = next(m["id"] for m in models_before if m["lane"] == "primary")

    status, prov = call("POST", BASE + "/providers", {"name": PROVIDER, "label": "loopback tuning echo",
                                                      "base_url": ECHO + "/v1",
                                                      "notes": "verification instrument, not a real provider"})
    if status == 409:
        _, providers = call("GET", BASE + "/providers")
        prov = next(p for p in providers if p["name"] == PROVIDER)
    status, model = call("POST", BASE + "/models", {
        "provider_id": prov["id"], "model_id": "echo/tuned-probe",
        "temperature": 0.77, "top_p": 0.33, "max_tokens": 123,
        "ai_prob_threshold": 0.55, "ai_conf_threshold": 0.6,
        "human_prob_threshold": 0.40, "human_conf_threshold": 0.6,
        "secondary_decision_threshold": 0.90,
    })
    if status == 409:
        _, models = call("GET", BASE + "/models")
        model = next(m for m in models if m["model_id"] == "echo/tuned-probe")
    elif status != 201:
        raise SystemExit(f"model create returned {status}: {json.dumps(model)[:400]}")
    mid = model["id"]
    print(f"probe model id {mid} on provider {prov['id']}")

    _, lanes = call("POST", f"{BASE}/models/{mid}/assign-lane", {"lane": "primary"})
    check(lanes.get("primary") == "echo/tuned-probe", f"echo row is now the primary lane ({lanes})")

    try:
        # ---- sampling parameters on the wire ------------------------------
        resp = detect()
        sent = echo_body()
        print(f"  run 1: {resp['classification']} / {resp['method']} / "
              f"primary p={resp['component_scores']['primary_probability']}")
        print(f"  wire : {json.dumps({k: sent.get(k) for k in ('model', 'temperature', 'top_p', 'max_tokens')})}")
        check(sent.get("temperature") == 0.77, "temperature 0.77 from the DB row reached the request body")
        check(sent.get("top_p") == 0.33, "top_p 0.33 from the DB row reached the request body")
        check(sent.get("max_tokens") == 123, "max_tokens 123 from the DB row reached the request body")
        check(sent.get("model") == "echo/tuned-probe", "the DB model_id reached the request body")
        check(resp["config_used"]["tuning_applied"]["max_tokens"] == 123,
              "the workflow echoed max_tokens as request-supplied")
        check("response_format" not in sent, "response_format is still never sent")

        call("PATCH", f"{BASE}/models/{mid}", {"temperature": 0.05, "top_p": 0.95, "max_tokens": 57})
        resp = detect()
        sent = echo_body()
        print(f"  run 2: {resp['classification']} / {resp['method']}")
        print(f"  wire : {json.dumps({k: sent.get(k) for k in ('model', 'temperature', 'top_p', 'max_tokens')})}")
        check((sent.get("temperature"), sent.get("top_p"), sent.get("max_tokens")) == (0.05, 0.95, 57),
              "editing the DB row moved all three sampling values on the wire")

        # ---- the threshold changes the decision path ----------------------
        check(resp["method"] in ("PRIMARY_DECIDED_AI", "SINGLE_MODEL_PRIMARY_AI"),
              "p=0.62 >= stored cut 0.55 -> the primary decided (method=%s)" % resp["method"])
        check(resp["component_scores"]["deciding_lane"] == "primary", "the primary was the deciding lane")
        check(resp["component_scores"]["secondary_called"] is False, "tie-break lane was not called")

        call("PATCH", f"{BASE}/models/{mid}", {"ai_prob_threshold": 0.70})
        resp = detect()
        cs = resp["component_scores"]
        print(f"  run 3: {resp['classification']} / {resp['method']} / "
              f"deciding lane={cs['deciding_lane']} secondary_called={cs['secondary_called']} / "
              f"secondary p={cs['secondary_probability']}")
        check(cs["primary_probability"] == 0.62, "the echo probability did not move between runs")
        check(cs["secondary_called"] is True,
              "raising the stored cut to 0.70 pushed the same text into the tie-break lane")
        check(resp["config_used"]["thresholds"]["ai_prob"] == 0.70,
              "the response reports the stored 0.70 as the effective cut")
    finally:
        # ---- restore the calibrated lanes and drop the probe rows ---------
        back = call("POST", f"{BASE}/models/{primary_id}/assign-lane", {"lane": "primary"})
        _, providers = call("GET", BASE + "/providers")
        pid = next((p["id"] for p in providers if p["name"] == PROVIDER), None)
        if pid:
            call("DELETE", f"{BASE}/providers/{pid}")
        _, models_after = call("GET", BASE + "/models")
        lanes_after = {m["model_id"]: m["lane"] for m in models_after}
        check(lanes_after == shipped, f"lanes restored to {shipped} (got {lanes_after})")
        print(f"  restore: primary={back[1].get('primary')} secondary={back[1].get('secondary')}")

    failed = [d for ok, d in checks if not ok]
    print(f"tuning echo proof: {len(checks) - len(failed)}/{len(checks)} checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
