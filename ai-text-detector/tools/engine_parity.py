"""Compare backend/engine.py against the shipped n8n nodes, case by case.

    python tools/engine_parity.py                 # diff every case
    python tools/engine_parity.py --case gray      # substring filter

`n8n/parity-golden.json` is produced by `tools/engine_parity.js`, which EXECUTES the code nodes read
out of `n8n/ai-only-workflow.json` over `n8n/parity-fixtures.json`. This tool runs the same fixtures
through the Python port and diffs the two traces stage by stage: normalize, the serialized request
body, each lane's parse, and the ensemble result.

Nothing here touches a provider or n8n: the engine is given a lane runner that returns the recorded
gateway body, so the only thing under test is the port's own logic.

Three differences are deliberate and are masked rather than reported as failures -- each one is
documented in the engine's module docstring:
  * `versions.engine` exists only in the Python answer.
  * `allow_byok` defaults true locally (the workflow's default is false for a hosted install).
  * the missing-key message names the Configuration page instead of `PRIMARY_API_KEY`.
and two more exist only because the Python port has a tie-break walk the workflow cannot be asked
for: `config.secondary_candidates`, and `lane_meta.<lane>.model`, which is a copy of the
already-compared `config.<lane>_model` that the walk re-points at the leg it dialled.
Everything else has to match byte for byte.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import engine  # noqa: E402

GOLDEN_PATH = ROOT / "n8n" / "parity-golden.json"
FIXTURES_PATH = ROOT / "n8n" / "parity-fixtures.json"
WORKFLOW_PATH = ROOT / "n8n" / "ai-only-workflow.json"

# Written by a clock, not by the logic being compared.
VOLATILE_KEYS = {"timestamp", "at", "received_at", "completed_at"}


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def drop_path(value: Any, dotted: str) -> None:
    keys = dotted.split(".")
    node = value
    for key in keys[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
    if isinstance(node, dict):
        node.pop(keys[-1], None)


def differences(mine: Any, theirs: Any, path: str = "", limit: int = 12) -> list[str]:
    """Dotted paths where the two traces part company, with both values. Key presence counts:
    the workflow omits a field where the port would write null, and that is visible to a caller."""
    found: list[str] = []

    def walk(a: Any, b: Any, where: str) -> None:
        if len(found) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in a.keys() - b.keys():
                found.append(f"{where}.{key}: engine={json.dumps(a[key])[:120]} absent in workflow")
            for key in b.keys() - a.keys():
                found.append(f"{where}.{key}: engine absent, workflow={json.dumps(b[key])[:120]}")
            for key in a.keys() & b.keys():
                walk(a[key], b[key], f"{where}.{key}")
            return
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b):
                found.append(f"{where}: engine has {len(a)} items, workflow {len(b)}")
            for i in range(min(len(a), len(b))):
                walk(a[i], b[i], f"{where}[{i}]")
            return
        if a != b and not (type_a_eq(a, b)):
            found.append(f"{where}: engine={json.dumps(a)[:160]} workflow={json.dumps(b)[:160]}")

    walk(mine, theirs, path or "value")
    return found


def type_a_eq(a: Any, b: Any) -> bool:
    """`1` and `1.0` are the same JSON number -- JS has only one kind, Python has two."""
    return (isinstance(a, (int, float)) and not isinstance(a, bool)
            and isinstance(b, (int, float)) and not isinstance(b, bool) and a == b)


def run_case(case: dict) -> dict:
    """The engine's own trace of one fixture, in the same shape as the golden's."""
    env = case.get("env") or {}
    headers = case.get("headers") or {}
    payload = case.get("payload")
    responses = case.get("responses") or {}

    # Same unwrap the shipped node applies to the webhook envelope.
    unwrapped = payload if engine.js_truthy(payload) else {"body": payload, "headers": headers}
    try:
        norm = engine.normalize(unwrapped, headers, env=env)
    except engine.Reject as reject:
        return {"normalize": {"valid": False, "error_message": reject.message,
                              "error_hint": reject.hint or None, "error_code": reject.code}}

    cfg = norm["config"]
    build = {}
    for lane in ("primary", "secondary"):
        build[lane] = {
            "lane": lane,
            "model": cfg[lane + "_model"],
            "base_url": cfg[lane + "_base_url"],
            "api_key_present": bool(cfg[lane + "_api_key"]),
            "body_json": engine._json_str(engine.build_chat_body(cfg, lane, norm["text"])),
        }

    # detect() decides which lanes to run; this runner only hands it the body the fixture says the
    # gateway answered, and keeps the parsed record so the trace shows what actually ran.
    parsed: dict[str, dict] = {}

    def runner(lane_cfg: dict, lane: str, text: str, deadline: float | None = None) -> dict:
        if lane not in responses:
            raise AssertionError(f"detect called the {lane} lane but the fixture gives no gateway body "
                                 f"for it -- the workflow harness would have thrown here too")
        rec = engine.parse_lane(lane_cfg, lane, responses[lane])
        parsed[lane] = rec
        return rec

    status, ens = engine.detect(payload, headers=headers, env=env, lane_runner=runner)
    assert status == 200, "normalize accepted the request but detect rejected it"

    parse = {"primary": parsed["primary"]}
    if "secondary" in responses:
        if parsed["primary"]["need_secondary"]:
            parse["secondary"] = parsed["secondary"]
        else:
            # The workflow's IF node never executes that branch, so a fixture that supplies a
            # secondary body anyway must show as uncalled rather than as unused.
            parse["secondary_not_called"] = True
    return {"normalize": norm, "build": build, "parse": parse, "ensemble": ens}


def masks_for(case: dict) -> list[str]:
    """Paths this comparison intentionally does not check. Each one is a documented divergence,
    not a waived failure -- see the engine's module docstring."""
    payload = case.get("payload")
    masks = ["ensemble.versions.engine",
             # Only ever a copy of `normalize.config.<lane>_model`, which IS compared, and a list the
             # workflow has no field for. Masking them hides no arithmetic: a fixture would have to
             # send `candidates` to make them differ, and the workflow's validator rejects that key.
             "normalize.config.secondary_candidates",
             "normalize.lane_meta.primary.model", "normalize.lane_meta.secondary.model"]
    if not (isinstance(payload, dict) and "document_id" in payload):
        masks += ["normalize.document_id", "ensemble.document_id"]
    if "ALLOW_BYOK" not in (case.get("env") or {}):
        masks += ["normalize.config.allow_byok", "ensemble.config_used.byok_enabled"]
    if case.get("id") == "keyless-remote-rejected":
        masks += ["normalize.error_message", "normalize.error_hint"]
    return masks


def case_diffs(case: dict, gold: dict) -> list[str]:
    """Every place the port's trace and the recorded workflow trace part company."""
    mine = scrub(run_case(case))
    theirs = scrub(gold)
    for path in masks_for(case):
        drop_path(mine, path)
        drop_path(theirs, path)

    diffs = differences(mine["normalize"], theirs["normalize"], "normalize")
    if "build" in mine:
        for lane in ("primary", "secondary"):
            if lane in gold.get("build", {}):
                diffs += differences(mine["build"].get(lane), gold["build"][lane], f"build.{lane}")
        diffs += differences(mine["parse"], theirs.get("parse"), "parse")
        diffs += differences(mine["ensemble"], theirs.get("ensemble"), "ensemble")
    return diffs


def golden_workflows_sha() -> str:
    return hashlib.sha256(WORKFLOW_PATH.read_bytes()).hexdigest()


def failing_cases(wanted: str | None = None) -> dict[str, list[str]]:
    """id -> differences, for every fixture the port does not reproduce. Empty means the port and
    the shipped nodes agree on all of them."""
    golden_doc = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))["cases"]
    by_id = {c["id"]: c for c in golden_doc["cases"]}
    out: dict[str, list[str]] = {}
    for case in fixtures:
        if wanted and wanted not in case["id"]:
            continue
        gold = by_id.get(case["id"])
        if gold is None:
            out[case["id"]] = ["no golden trace for this fixture -- re-record it"]
            continue
        if gold.get("harness_error"):
            out[case["id"]] = [f"golden harness error: {gold['harness_error']}"]
            continue
        try:
            diffs = case_diffs(case, gold)
        except Exception as exc:           # a crash in the port is a divergence too, not a skipped case
            diffs = [f"{type(exc).__name__}: {exc}"]
        if diffs:
            out[case["id"]] = diffs
    return out


def checked_cases(wanted: str | None = None) -> int:
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))["cases"]
    return sum(1 for c in fixtures if not wanted or wanted in c["id"])


def main(argv: list[str]) -> int:
    wanted = None
    for i, a in enumerate(argv):
        if a == "--case" and i + 1 < len(argv):
            wanted = argv[i + 1]

    recorded = json.loads(GOLDEN_PATH.read_text(encoding="utf-8")).get("workflow_sha")
    now = golden_workflows_sha()
    if recorded != now:
        print(f"FAIL the golden was recorded against a different workflow: golden={recorded} "
              f"current={now}\n     re-record it with `node tools/engine_parity.js` -- if the nodes changed,"
              f"\n     every band measured through them has to be re-measured too.")

    bad = failing_cases(wanted)
    total = checked_cases(wanted)
    for cid, diffs in bad.items():
        print(f"FAIL {cid}  ({len(diffs)} difference(s))")
        for d in diffs:
            print(f"    {d}")
    print(f"\n{total - len(bad)}/{total} cases identical to the shipped workflow")
    return 0 if not bad and recorded == now else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
