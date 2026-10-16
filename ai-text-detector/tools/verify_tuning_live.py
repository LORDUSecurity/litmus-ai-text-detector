"""Live proof that the parameters stored in SQLite drive the detection.

Three POSTs to the running backend with one number changed between them:

  A  baseline            human_prob_threshold = 0.40 (as calibrated)
  B  edited in the DB    human_prob_threshold = 0.30
  C  restored            human_prob_threshold = 0.40

This asserts the WIRING against the real providers: the response must report the
stored value as the effective cut, and must attribute it to the request
(tuning_applied). It deliberately does not assert that the verdict moved -- the
measured run-to-run jitter on the primary lane is wider than the band being
edited (hu-010 came back at 0.38 in the calibration capture and 0.60 live), so a
verdict difference here would be luck. The deterministic verdict-path proof is in
tools/verify_tuning_echo.py, which pins the model's answer with a loopback echo.

    python tools/verify_tuning_live.py

Requires: n8n running with the published workflow, backend on 127.0.0.1:8787.
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8787"
CORPUS = "benchmark/corpus.json"
TEXT_ID = "hu-010"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, json.loads(r.read().decode())


def primary_model_id():
    _, rows = call("GET", "/models")
    for m in rows:
        if m.get("lane") == "primary":
            return m["id"], m["model_id"]
    raise SystemExit("no primary lane row in the database")


def run(text, label):
    status, resp = call("POST", "/detect", {"text": text, "requested_by": "tuning-proof"})
    if status != 200:
        print(f"{label}: HTTP {status} {json.dumps(resp)[:400]}")
        return None
    cfg = resp.get("config_used", {})
    cs = resp.get("component_scores", {})
    return {
        "label": label,
        "classification": resp.get("classification"),
        "method": resp.get("method"),
        "primary_p": cs.get("primary_probability"),
        "secondary_p": cs.get("secondary_probability"),
        "deciding_lane": cs.get("deciding_lane"),
        "secondary_called": cs.get("secondary_called"),
        "thresholds": cfg.get("thresholds"),
        "sampling": cfg.get("sampling"),
        "tuning_applied": cfg.get("tuning_applied"),
    }


def main():
    mid, model_id = primary_model_id()
    texts = json.load(open(CORPUS, encoding="utf-8"))
    text = next(t["text"] for t in texts if t["id"] == TEXT_ID)
    print(f"primary model: {model_id} (db id {mid}), corpus text {TEXT_ID}, "
          f"{len(text)} chars")

    results = []
    results.append(run(text, "A baseline (cut 0.40)"))
    call("PATCH", f"/models/{mid}", {"human_prob_threshold": 0.30})
    print("  PATCH /models/%d -> human_prob_threshold = 0.30" % mid)
    results.append(run(text, "B edited in the DB (cut 0.30)"))
    call("PATCH", f"/models/{mid}", {"human_prob_threshold": 0.40})
    print("  PATCH /models/%d -> human_prob_threshold = 0.40 (restored)" % mid)
    results.append(run(text, "C restored (cut 0.40)"))

    for r in results:
        if r is None:
            continue
        print(json.dumps(r, indent=2))

    a, b, c = results
    ok = True
    if not a or not b or not c:
        print("FAIL: a run did not return 200")
        ok = False
    else:
        checks = [
            (a["thresholds"]["human_prob"] == 0.4, "A reports the 0.40 cut"),
            (b["thresholds"]["human_prob"] == 0.3, "B reports the 0.30 cut"),
            (c["thresholds"]["human_prob"] == 0.4, "C reports the restored 0.40 cut"),
            (a["tuning_applied"].get("human_prob_threshold") == 0.4,
             "A attributes the cut to the request, not to the baked default"),
            (b["tuning_applied"].get("human_prob_threshold") == 0.3,
             "B attributes the 0.30 to the request"),
            (a["sampling"]["temperature"] == 0.1 and a["sampling"]["max_tokens"] == 700,
             "the sampling block also came from the stored row"),
        ]
        for passed, description in checks:
            print(("  PASS  " if passed else "  FAIL  ") + description)
            ok = ok and passed
        print("  INFO  verdicts: A=%s/%s B=%s/%s C=%s/%s" % (
            a["classification"], a["method"], b["classification"], b["method"],
            c["classification"], c["method"]))
        print("  INFO  primary probabilities: A=%s B=%s C=%s (jitter, not the cut)" % (
            a["primary_p"], b["primary_p"], c["primary_p"]))
    print("tuning wiring proof:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
