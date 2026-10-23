"""backend/engine.py against the shipped n8n nodes, inside the suite.

Run from ai-text-detector/:  python -m pytest backend/tests -q

`tools/engine_parity.js` EXECUTES the code nodes read out of `n8n/ai-only-workflow.json` over
`n8n/parity-fixtures.json` and records `n8n/parity-golden.json`. `tools/engine_parity.py` runs the
same fixtures through the Python port and diffs the two traces. This test calls that same module, so
the CLI and the suite cannot disagree about what "equal" means, and it stays hermetic: the engine is
handed the recorded gateway bodies through its lane-runner seam, so no socket is opened and n8n does
not have to be running.
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools"))

import engine_parity  # noqa: E402

from backend import engine  # noqa: E402


def test_every_fixture_case_matches_the_recorded_workflow_trace():
    bad = engine_parity.failing_cases()
    assert bad == {}, "\n".join(f"{cid}: " + "; ".join(diffs[:3]) for cid, diffs in bad.items())


def test_golden_was_recorded_against_the_workflow_on_disk():
    """Editing a node silently invalidates every recorded trace -- and every band fitted through it."""
    recorded = json.loads(engine_parity.GOLDEN_PATH.read_text(encoding="utf-8"))["workflow_sha"]
    assert recorded == engine_parity.golden_workflows_sha(), (
        "n8n/ai-only-workflow.json changed since n8n/parity-golden.json was recorded; re-record it "
        "with `node tools/engine_parity.js` and re-measure the bands fitted through those nodes")


def test_the_fixture_set_has_not_been_thinned():
    """61 agreeing cases is the evidence; two agreeing cases would be nothing. A fixture dropped
    from the file would quietly raise this test's floor instead of lowering it, so the floor is a
    floor and not a target."""
    assert engine_parity.checked_cases() >= 60
    golden = json.loads(engine_parity.GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]
    assert len(golden) == engine_parity.checked_cases(), (
        "the golden and the fixtures no longer describe the same set of cases")


def test_the_comparison_notices_a_wrong_fingerprint(monkeypatch):
    monkeypatch.setattr(engine, "fingerprint", lambda text: "0" * 16)
    bad = engine_parity.failing_cases()
    assert bad, "a port that fingerprints text differently must not pass parity"
    assert any("text_fingerprint" in d for diffs in bad.values() for d in diffs)


def test_the_comparison_notices_a_flipped_tie_break(monkeypatch):
    """>= -> > moves exactly the texts sitting on the cut. If parity missed that, its masks would be
    wide enough to hide a verdict, which is the failure mode this whole check exists to prevent."""
    def too_strict(cfg, prob):
        return "AI_GENERATED" if prob > cfg["secondary_decision_threshold"] else "HUMAN_LIKELY"

    monkeypatch.setattr(engine, "secondary_verdict", too_strict)
    assert engine_parity.failing_cases(), "a changed tie-break must not pass parity"


def test_the_comparison_notices_a_reordered_request_body(monkeypatch):
    """The body is compared as the serialized bytes the gateway receives, so a key the workflow
    never sends -- or a re-ordered one -- is a difference, not a detail."""
    real = engine.build_chat_body

    def alphabetical(cfg, lane, text):
        body = real(cfg, lane, text)
        return {k: body[k] for k in sorted(body)}

    monkeypatch.setattr(engine, "build_chat_body", alphabetical)
    assert engine_parity.failing_cases(), "a re-ordered request body must not pass parity"
