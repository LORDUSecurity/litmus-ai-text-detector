"""backend/engine.py and the dispatch that chooses it.

Run from ai-text-detector/:  python -m pytest backend/tests -q

The equivalence of the port and the shipped workflow is `test_engine_parity.py`'s job. This file
covers what parity CANNOT see, because both sides of that comparison are transcriptions of the same
JS:

  * the decisions the port makes about itself -- which engine is in use, and that a local install
    never dials the workflow URL;
  * the divergences parity deliberately masks (a failed lane naming its own phase, `ALLOW_BYOK`
    defaulting true, the missing-key prose);
  * the exact boundary values, since a `>=` and a `>` agree on every fixture that was chosen to
    sit away from the cut -- the texts that sit ON it are the ones a band decides.

Nothing here opens a socket: the lanes are handed recorded-shaped gateway bodies, and the
gateway round-trip is tested against a stubbed `urlopen`.
"""
import io
import json
import os
import tempfile
import time
import urllib.error

_TMP = tempfile.mkdtemp(prefix="detector-test-")
os.environ.setdefault("DETECTOR_DB", os.path.join(_TMP, "test.sqlite3"))
os.environ.setdefault("DETECTOR_SECRET", os.path.join(_TMP, "test-secret.key"))
os.environ.setdefault("N8N_DB", os.path.join(_TMP, "no-such-n8n.sqlite"))

import pytest  # noqa: E402

from backend import db, engine  # noqa: E402
from backend.app import create_app  # noqa: E402

TEXT = "This is a long enough sample of text for the detector to accept it."
PLAINTEXT = "nvapi-TESTKEY-test-1234567890abcdef"

# Bands as the workflow ships them, so a test that expects a verdict names the ruler it was read on.
ENV = {"PRIMARY_API_KEY": "testkey", "SECONDARY_API_KEY": "testkey"}


def completion(prob, conf, finish="stop", extra=None) -> dict:
    """A chat-completion body for a model that answered with the JSON the prompt asked for."""
    obj = {"ai_probability": prob, "confidence": conf, "signals": [], "phrases": []}
    obj.update(extra or {})
    return {"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": finish}]}


def cut_completion(text: str) -> dict:
    """A body cut off mid-answer -- what a capped generation looks like on the wire."""
    return {"choices": [{"message": {"content": text}, "finish_reason": "length"}]}


def runner_for(bodies: dict, calls: list):
    """A lane runner that parses the bodies a fixture says the gateway answered.

    `parse_lane` is the engine's own, so the ensemble is reached through the real parser rather
    than through a hand-written record that could flatter it.
    """
    def run(cfg, lane, text, deadline=None):
        calls.append(lane)
        return engine.parse_lane(cfg, lane, bodies[lane])
    return run


def detect(bodies: dict, payload: dict | None = None, env: dict | None = None,
           headers: dict | None = None, calls: list | None = None):
    calls = calls if calls is not None else []
    return engine.detect({"text": TEXT} if payload is None else payload,
                         headers=headers or {}, env={**ENV, **(env or {})},
                         lane_runner=runner_for(bodies, calls))


# ------------------------------------------------------------------ normalize

def test_a_body_that_is_not_an_object_is_rejected_as_a_missing_field():
    """The webhook envelope is what the node unwraps, so an empty POST fails on `text`, not on
    "not an object" -- and the two messages send the user to different fixes."""
    with pytest.raises(engine.Reject) as e:
        engine.normalize({}, {}, env=ENV)
    assert e.value.message == "Field 'text' is required and must be a non-empty string."

    with pytest.raises(engine.Reject) as e:
        engine.normalize("hello there, a plain string body", {}, env=ENV)
    assert e.value.message == "Request body must be a JSON object."


def test_a_json_array_is_an_object_to_js_and_reaches_the_text_check():
    with pytest.raises(engine.Reject) as e:
        engine.normalize([{"text": TEXT}], {}, env=ENV)
    assert "Field 'text' is required" in e.value.message


def test_the_length_gates_quote_the_count_and_the_limit_in_js_words():
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": "abc"}, {}, env=ENV)
    assert e.value.message == "Text too short: 3 characters (minimum 10)."
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": "x" * 21}, {}, env=ENV | {"MAX_TEXT_LENGTH": "20"})
    assert e.value.message == "Text too long: 21 characters (maximum 20)."


def test_text_length_counts_utf16_units_like_js_not_code_points_like_python():
    """`text_length` is stored, printed and used by the max-length gate. An emoji is two units in
    JS and one in Python, so a 50k-character paste of CJK or emoji text would otherwise pass a gate
    the workflow would have failed."""
    text = "hello \U0001F600 world"
    assert len(text) == 13
    norm = engine.normalize({"text": text}, {}, env=ENV)
    assert norm["text_length"] == 14


def test_the_fingerprint_collapses_whitespace_runs_like_the_workflow():
    assert engine.fingerprint("a\t\tb") == engine.fingerprint("a b")
    assert len(engine.fingerprint(TEXT)) == 16


def test_a_keyless_lane_off_box_is_refused_before_any_call():
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": TEXT}, {}, env={})
    assert e.value.code == "PROVIDER_KEY_MISSING"
    assert "Configuration page" in e.value.hint, "a local user needs the page, not an env var name"


def test_a_keyless_loopback_gateway_is_allowed_and_says_so():
    """A self-hosted gateway that needs no credential is the one case where keyless is correct, and
    the divergence the parity harness masks: the workflow would refuse this on a hosted install."""
    env = {"PRIMARY_BASE_URL": "http://127.0.0.1:20128/v1", "SECONDARY_BASE_URL":
           "http://127.0.0.1:20128/v1"}
    norm = engine.normalize({"text": TEXT}, {}, env=env)
    assert norm["valid"] is True
    assert norm["lane_meta"]["primary"]["key_source"] == "none"
    assert any("without an API key" in w for w in norm["lane_warnings"])


def test_a_host_that_only_ends_with_an_allowed_name_is_rejected():
    for bad, why in (("https://openrouter.ai.evil.com/v1", "is not in ALLOWED_LLM_HOSTS"),
                     ("https://integrate.api.nvidia.com/v1/chat/completions", "API root"),
                     ("https://user:pw@integrate.api.nvidia.com/v1", "must not embed credentials"),
                     ("http://localhost:99999/v1", "port is out of range")):
        with pytest.raises(engine.Reject) as e:
            engine.normalize({"text": TEXT, "lane_config": {"primary": {"base_url": bad}}},
                             {}, env=ENV)
        assert e.value.code == "LANE_CONFIG_INVALID", bad
        assert why in e.value.message, bad


def test_an_inverted_band_is_refused_because_it_would_classify_everything_twice():
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": TEXT, "tuning": {"human_prob_threshold": 0.8}}, {}, env=ENV)
    assert "human_prob_threshold (0.8) must be below ai_prob_threshold (0.69)" in e.value.message


def test_tuning_bounds_are_the_workflows_own_numbers_and_only_named_fields_get_through():
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": TEXT, "tuning": {"temperature": 3}}, {}, env=ENV)
    assert e.value.message == "tuning.temperature must be between 0 and 2 (got 3)."
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": TEXT, "tuning": {"bogus": 1}}, {}, env=ENV)
    assert 'tuning has unsupported field "bogus"' in e.value.message
    # A field the UI left blank means "not specified", not "invalid".
    norm = engine.normalize({"text": TEXT, "tuning": {"temperature": ""}}, {}, env=ENV)
    assert norm["tuning_applied"] == {}


def test_a_browser_key_is_ignored_when_byok_is_off_and_accepted_when_it_is_on():
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": TEXT}, {"x-primary-api-key": PLAINTEXT},
                         env=ENV | {"ALLOW_BYOK": "false"})
    assert e.value.code == "BYOK_DISABLED"
    norm = engine.normalize({"text": TEXT}, {"x-primary-api-key": PLAINTEXT}, env=ENV)
    assert norm["lane_meta"]["primary"]["key_source"] == "header"
    assert norm["config"]["primary_api_key"] == PLAINTEXT


# ------------------------------------------------------------------ the request body

def test_the_request_body_is_the_workflows_bytes_in_the_workflows_order():
    """`max_tokens` sits ahead of `stream`, `1.0` prints as `1`, and no `response_format` is sent:
    on free-tier upstreams that field provably triggered 400s and duplicated-key garbage."""
    norm = engine.normalize({"text": TEXT}, {}, env=ENV)
    body = engine.build_chat_body(norm["config"], "primary", TEXT)
    assert list(body) == ["model", "messages", "temperature", "top_p", "stream"]
    assert "response_format" not in body
    assert "max_tokens" not in json.dumps(body)
    assert engine._json_str(body).startswith(
        '{"model":"z-ai/glm-5.3-flash","messages":[{"role":"system","content":')
    # An explicit cap from the model row is sent as an integer, the way JSON.stringify writes it.
    capped = engine.build_chat_body({**norm["config"], "max_tokens": 700}, "primary", TEXT)
    assert list(capped) == ["model", "messages", "temperature", "top_p", "max_tokens", "stream"]
    assert '"max_tokens":700' in engine._json_str(capped)


def test_the_text_travels_inside_the_triple_quotes_of_the_judge_prompt():
    body = engine.build_chat_body(engine.normalize({"text": TEXT}, {}, env=ENV)["config"],
                                  "primary", TEXT)
    user = body["messages"][1]["content"]
    assert user.startswith('Was the following text likely written by an AI?')
    assert f'"""\n{TEXT}\n"""' in user
    assert user.endswith("Return ONLY the JSON object.")


# ------------------------------------------------------------------ the tolerant parser

def test_a_fenced_answer_and_a_reasoning_only_answer_are_both_read():
    fenced = {"choices": [{"message": {"content": "```json\n" + json.dumps(
        {"ai_probability": 0.8, "confidence": 0.7}) + "\n```"}, "finish_reason": "stop"}]}
    out = engine.extract(fenced)
    assert out["ok"] is True and out["result"]["ai_probability"] == 0.8

    reasoning = {"choices": [{"message": {"reasoning": json.dumps(
        {"ai_probability": 0.2, "confidence": 0.9})}, "finish_reason": "stop"}]}
    out = engine.extract(reasoning)
    assert out["ok"] is True and out["result"]["ai_probability"] == 0.2


def test_a_number_cut_short_by_the_token_ceiling_is_refused_not_invented():
    """`0.` or `0.7` matching as a number is not a missing answer, it is a fabricated one:
    confidently HUMAN, ok:true, and it would decide alone while the other lane failed."""
    for cut in ('{"ai_probability": 0.', '{"ai_probability": 0.7'):
        out = engine.extract(cut_completion(cut))
        assert out["ok"] is False and out["error_code"] == "json_parse_failed", cut


def test_numbers_the_cut_left_complete_are_salvaged_and_marked_as_needing_corroboration():
    cut = cut_completion('{"ai_probability": 0.95, "confidence": 0.9, "signals": [')
    out = engine.extract(cut)
    assert out["ok"] is True and out["salvaged"] is True and out["truncated"] is True
    assert out["result"]["ai_probability"] == 0.95
    cfg = engine.normalize({"text": TEXT}, {}, env=ENV)["config"]
    assert engine.needs_secondary(cfg, out) is True


def test_a_non_2xx_that_carries_no_error_object_names_the_status():
    """Divergence #1. The HTTP node runs `neverError: true`, so a plain-text `404 page not found`
    reached the workflow as `no_choices` -- "the model answered badly" when the truth was "the id was
    posted to the wrong host"."""
    out = engine.extract({"_status": 404, "_body": "page not found"})
    assert out["error_code"] == "http_404"
    # A body that DOES carry an error object is still reported exactly as the workflow reports it.
    out = engine.extract({"error": {"code": 429, "message": "rate limited"}})
    assert out["error_code"] == "429" and out["error_detail"] == "rate limited"


def test_a_lane_that_died_on_the_transport_says_which_phase():
    """Divergence #2. Waiting for a pool and fixing Settings -> engine URL are opposite cures, and
    the workflow could not see the difference because it never made the call itself."""
    assert engine.extract({"_transport": "timeout", "_transport_detail": "timed out"})["error_code"] \
        == "timeout"
    assert engine.extract({"_transport": "unreachable"})["error_code"] == "unreachable"


def test_an_empty_array_response_is_no_choices_because_js_reads_a_list_as_an_object():
    assert engine.extract([])["error_code"] == "no_choices"
    assert engine.extract("upstream said nothing at all")["error_code"] == "empty_response"
    assert engine.extract(None)["error_code"] == "empty_response"


# ------------------------------------------------------------------ the ensemble

def test_a_decisive_primary_does_not_buy_the_second_opinion():
    calls: list[str] = []
    status, resp = detect({"primary": completion(0.95, 0.9)}, calls=calls)
    assert status == 200 and calls == ["primary"]
    assert resp["classification"] == "AI_GENERATED"
    assert resp["component_scores"]["secondary_called"] is False


def test_a_gray_zone_primary_buys_it_and_the_tie_break_answers_on_its_own_scale():
    calls: list[str] = []
    status, resp = detect({"primary": completion(0.5, 0.9),
                           "secondary": completion(0.95, 0.8)}, calls=calls)
    assert calls == ["primary", "secondary"]
    assert resp["method"] == "TIE_BREAK_AI"
    assert resp["component_scores"]["deciding_lane"] == "secondary"
    assert resp["ai_probability"] == 0.95, "the answer is the tie-break's probability, not a blend"


def test_the_tie_break_cut_is_inclusive_parity_cannot_see_this():
    """Parity compares two transcriptions of one `>=`, so a port that wrote `>` would agree with
    itself on every fixture that was chosen to sit away from the cut. This is the text ON the cut."""
    def tie(prob):
        _, resp = detect({"primary": completion(0.5, 0.9), "secondary": completion(prob, 0.8)})
        return resp["classification"]

    assert tie(0.90) == "AI_GENERATED"
    assert tie(0.8999) == "HUMAN_LIKELY"


def test_a_low_confidence_extreme_probability_is_still_a_gray_zone():
    calls: list[str] = []
    detect({"primary": completion(0.99, 0.4), "secondary": completion(0.2, 0.7)}, calls=calls)
    assert calls == ["primary", "secondary"], "confidence gates the decision, not the probability"


def test_a_salvaged_primary_is_called_decisive_but_still_corroborated():
    """"The primary was decisive on its own" is only true of an answer that arrived complete: a
    truncated JSON is read, then checked against a second model."""
    calls: list[str] = []
    status, resp = detect({"primary": cut_completion('{"ai_probability": 0.95, "confidence": 0.9, '
                                                    '"signals": ['),
                           "secondary": completion(0.95, 0.9)}, calls=calls)
    assert calls == ["primary", "secondary"]
    assert resp["method"] == "PRIMARY_DECIDED_AI"
    assert any("ignored rather than blended" in w for w in resp["warnings"])
    assert resp["evidence"]["models"][0]["salvaged"] is True


def test_a_tie_break_switched_off_defers_instead_of_overturning():
    """Round 25c: three texts asked, three answered wrong. The lane ships off, so a gray-zone text
    must come back as REVIEW_REQUIRED rather than being decided by one second opinion."""
    _, resp = detect({"primary": completion(0.5, 0.9)},
                     payload={"text": TEXT, "lane_config": {"secondary": {"enabled": False}}})
    assert resp["classification"] == "REVIEW_REQUIRED"
    assert resp["method"] == "SINGLE_MODEL_PRIMARY_UNCERTAIN"
    assert resp["review_required"] is True
    assert resp["component_scores"]["secondary_called"] is False
    assert any("lane is disabled" in w for w in resp["warnings"])


def test_a_decisive_primary_with_the_lane_off_reports_the_leg_that_ran():
    _, resp = detect({"primary": completion(0.3, 0.9)},
                     payload={"text": TEXT, "lane_config": {"secondary": {"enabled": False}}})
    assert resp["method"] == "SINGLE_MODEL_PRIMARY_HUMAN"
    assert resp["classification"] == "HUMAN_LIKELY"


def test_a_failed_primary_hands_the_verdict_to_the_fallback_lane_and_says_so():
    _, resp = detect({"primary": {"error": {"code": 503, "message": "upstream unavailable"}},
                      "secondary": completion(0.95, 0.8)})
    assert resp["method"] == "SINGLE_MODEL_SECONDARY_AI"
    assert any("SECONDARY_DECISION_THRESHOLD" in w for w in resp["warnings"])
    assert resp["component_scores"]["primary_probability"] is None


def test_every_lane_failing_answers_about_the_engine_not_about_the_text():
    _, resp = detect({"primary": {"error": {"code": 429, "message": "capped"}},
                      "secondary": {"error": {"code": 503, "message": "down"}}})
    assert resp["classification"] == "DETECTION_UNAVAILABLE"
    assert resp["method"] == "ALL_MODELS_FAILED" and resp["review_required"] is True
    assert resp["ai_probability"] is None, "an unavailable answer must not render as 0%"
    assert [m["error_code"] for m in resp["evidence"]["models"]] == ["429", "503"]


def test_the_key_value_is_never_echoed_anywhere_in_the_answer():
    _, resp = detect({"primary": completion(0.95, 0.9)},
                     headers={"x-primary-api-key": PLAINTEXT})
    dumped = json.dumps(resp)
    assert PLAINTEXT not in dumped and "testkey" not in dumped
    assert resp["config_used"]["primary"]["key_source"] == "header"


# ------------------------------------------------- the tie-break lane walks the pool
#
# "The 2nd lane starts just after the 1st model that answered, and keeps going on a non-response."
# The primary already has that behaviour (`/detect` re-runs the request with the next pooled model),
# but a tie-break walk cannot be built the same way: the second opinion is bought INSIDE one
# detection, so re-posting the request would bill the primary a second time for a leg the user
# already paid for. That is why the walk lives here, after the primary's answer is in hand.
#
# The list arrives as `lane_config.secondary.candidates`, in the caller's own pool order, built by
# the backend. Each entry is a lane: an endpoint and a model, plus the cut that model was measured
# with -- a different model read against the previous model's ruler is the two-rulers bug this repo
# keeps re-measuring.

NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
REFUSED = {"error": {"code": 503, "message": "upstream unavailable"}}


def walk_detect(answers: dict, secondary: dict, calls: list, timeout: float | None = None,
                env: dict | None = None, cfgs: list | None = None):
    """One detection whose tie-break legs answer from `answers`, keyed by model id.

    `runner_for` keys by lane, which is exactly the shape a walk breaks: three tie-break attempts
    would collapse into one "secondary" entry and a test could not see who was actually called.
    `cfgs` collects the config each tie-break leg was handed, which is where a substituted leg's
    own endpoint, credential and cut are visible.
    """
    def run(cfg, lane, text, deadline=None):
        model = cfg[lane + "_model"]
        calls.append((lane, model))
        if lane == "secondary" and cfgs is not None:
            cfgs.append(dict(cfg))
        if lane == "primary":
            body = answers.get("__primary__")
        else:
            body = answers.get(model, REFUSED)
        return engine.parse_lane(cfg, lane, body)

    payload = {"text": TEXT, "lane_config": {"secondary": secondary}}
    return engine.detect(payload, headers={}, env={**ENV, **(env or {})},
                         timeout=timeout, lane_runner=run)


def test_a_tie_break_that_does_not_answer_walks_to_the_next_pooled_model():
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": REFUSED, "pool/fourth": completion(0.95, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second", "candidates": [
            {"base_url": NVIDIA_URL, "model": "pool/third"},
            {"base_url": NVIDIA_URL, "model": "pool/fourth"}]}, calls)
    assert [c for c in calls if c[0] == "primary"] == [("primary", engine.DEFAULT_PRIMARY_MODEL)], \
        "the primary is billed once: that is the whole reason the walk is in the engine"
    assert [m for lane, m in calls if lane == "secondary"] == ["pool/second", "pool/third",
                                                                "pool/fourth"]
    assert resp["method"] == "TIE_BREAK_AI"
    assert resp["classification"] == "AI_GENERATED"
    assert resp["ai_probability"] == 0.95


def test_the_answer_names_the_tie_break_leg_that_ran_not_the_one_that_was_asked_for():
    """A verdict from `pool/fourth` stored against `pool/second` is an unattributable number: the
    History row, the band and the health table would all blame and credit the wrong model."""
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": completion(0.95, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second",
         "candidates": [{"base_url": NVIDIA_URL, "model": "pool/third"},
                        {"base_url": NVIDIA_URL, "model": "pool/fourth"}]}, calls)
    assert resp["config_used"]["secondary"]["model"] == "pool/third"
    assert resp["config_used"]["secondary"]["base_url"] == NVIDIA_URL
    legs = {m["lane"]: m for m in resp["evidence"]["models"]}
    assert legs["secondary"]["model"] == "pool/third"
    assert legs["secondary"]["ok"] is True
    # The walk stops at the first answer: pool/fourth was never called.
    assert [m for lane, m in calls if lane == "secondary"] == ["pool/second", "pool/third"]


def test_every_leg_the_walk_bought_is_reported_including_the_one_that_answered():
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": completion(0.95, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second",
         "candidates": [{"base_url": NVIDIA_URL, "model": "pool/third"}]}, calls)
    walk = resp["secondary_walk"]
    assert walk["from"] == "pool/second"
    assert walk["switched_to"] == "pool/third"
    assert [(a["model"], a["ok"]) for a in walk["attempts"]] == [("pool/second", False),
                                                                 ("pool/third", True)]
    assert walk["attempts"][0]["error_code"] == "503"
    assert any("pool/third" in w for w in resp["warnings"])


def test_a_walk_that_never_answers_reports_why_and_leaves_the_verdict_alone():
    """No second opinion arrived, so the text keeps the answer one lane gives it -- and the page can
    still say which models were asked and how each one failed."""
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED, "pool/third": REFUSED},
        {"base_url": NVIDIA_URL, "model": "pool/second",
         "candidates": [{"base_url": NVIDIA_URL, "model": "pool/third"}]}, calls)
    assert resp["classification"] == "REVIEW_REQUIRED"
    assert resp["method"] == "SINGLE_MODEL_PRIMARY_UNCERTAIN"
    assert resp["config_used"]["secondary"]["model"] == "pool/third", "the leg that ran last"
    walk = resp["secondary_walk"]
    assert walk["switched_to"] is None
    assert [a["ok"] for a in walk["attempts"]] == [False, False]


def test_a_candidate_is_read_against_its_own_measured_cut():
    """The cut belongs to the model it was fitted on. 0.7 under a 0.9 ruler is HUMAN_LIKELY and under
    its own 0.6 ruler is AI_GENERATED, so a walk that carries the old cut would silently re-decide
    every substituted leg."""
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": completion(0.7, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second", "candidates": [
            {"base_url": NVIDIA_URL, "model": "pool/third",
             "secondary_decision_threshold": 0.6}]}, calls)
    assert resp["classification"] == "AI_GENERATED"
    assert resp["config_used"]["thresholds"]["secondary_decision"] == 0.6

    calls2: list = []
    _, resp2 = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": completion(0.7, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second",
         "candidates": [{"base_url": NVIDIA_URL, "model": "pool/third"}]}, calls2)
    assert resp2["classification"] == "HUMAN_LIKELY", "the lane's own 0.9 cut still applies"


def test_a_walk_never_starts_a_leg_that_cannot_finish():
    """The count of candidates is uncapped because the user chose that; the clock is not. A leg
    started with three seconds left cannot complete, and losing the request to a gateway timeout is
    worse than the missing second opinion it was bought for."""
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": completion(0.95, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second",
         "candidates": [{"base_url": NVIDIA_URL, "model": "pool/third"}]}, calls, timeout=5.0)
    assert [m for lane, m in calls if lane == "secondary"] == ["pool/second"], \
        "the skipped candidate must not be called"
    walk = resp["secondary_walk"]
    assert walk["switched_to"] is None
    assert walk["attempts"][1]["model"] == "pool/third"
    assert "time" in walk["attempts"][1]["skipped"]


def test_a_candidate_that_is_the_primary_is_never_bought():
    """The same rule the collision guard applies to the head lane: a model cannot corroborate
    itself, and here it is cheaper still -- the primary just answered this text."""
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/third": completion(0.95, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second", "candidates": [
            {"base_url": NVIDIA_URL, "model": engine.DEFAULT_PRIMARY_MODEL},
            {"base_url": NVIDIA_URL, "model": "pool/third"}]}, calls)
    assert [m for lane, m in calls if lane == "secondary"] == ["pool/second", "pool/third"]
    assert resp["secondary_walk"]["switched_to"] == "pool/third"


def test_a_lane_with_no_candidates_behaves_exactly_as_it_did():
    """The walk is additive: a request that names no list must produce the same body it produced
    before, because that is the shape every measured band in the ranking was captured with."""
    calls: list = []
    _, resp = walk_detect({"__primary__": completion(0.5, 0.9), "pool/second": completion(0.95, 0.8)},
                          {"base_url": NVIDIA_URL, "model": "pool/second"}, calls)
    assert calls == [("primary", engine.DEFAULT_PRIMARY_MODEL), ("secondary", "pool/second")]
    assert resp["method"] == "TIE_BREAK_AI"
    assert "secondary_walk" not in resp


def test_a_candidate_that_is_not_a_lane_is_refused_before_anything_is_called():
    """One ruler: a lane accepted at request time that the engine later cannot dial is the
    `LANE_CONFIG_INVALID` this repo has been bitten by twice."""
    for bad in ([{"base_url": "not-a-url", "model": "pool/x"}],
                [{"base_url": NVIDIA_URL}],
                [{"base_url": NVIDIA_URL, "model": "pool x"}],
                "pool/x",
                ["not even an object"],
                [{"base_url": NVIDIA_URL, "model": "pool/x", "temperature": 4}]):
        with pytest.raises(engine.Reject) as e:
            engine.normalize({"text": TEXT, "lane_config": {"secondary": {"candidates": bad}}},
                             {}, env=ENV)
        assert e.value.code == "LANE_CONFIG_INVALID", bad


def test_a_candidate_key_is_honored_only_when_the_caller_may_bring_keys():
    """A hosted deploy must not let a request swap the operator's credential -- the same guard the
    `x-secondary-api-key` header runs under, applied to every credential a candidate carries."""
    key = "nvapi-CANDIDATEkey-123456"
    with pytest.raises(engine.Reject) as e:
        engine.normalize({"text": TEXT, "lane_config": {"secondary": {"candidates": [
            {"base_url": NVIDIA_URL, "model": "pool/x", "api_key": key}]}}},
                         {}, env={**ENV, "ALLOW_BYOK": "false"})
    assert e.value.code == "BYOK_DISABLED"

    seen: list = []
    calls: list = []
    _, resp = walk_detect(
        {"__primary__": completion(0.5, 0.9), "pool/second": REFUSED,
         "pool/third": completion(0.95, 0.8)},
        {"base_url": NVIDIA_URL, "model": "pool/second", "candidates": [
            {"base_url": NVIDIA_URL, "model": "pool/third", "api_key": key}]},
        calls, cfgs=seen)
    assert seen[-1]["secondary_api_key"] == key, \
        "the substituted leg is paid for with its own credential"
    assert PLAINTEXT not in json.dumps(resp) and key not in json.dumps(resp)
    assert resp["config_used"]["secondary"]["key_source"] == "request"


# ------------------------------------------------------------------ the round trip

class _Response:
    def __init__(self, raw: bytes, status: int):
        self._raw, self.status = raw, status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int, body: bytes):
    return urllib.error.HTTPError("http://g/v1/chat/completions", code, "err", {}, io.BytesIO(body))


def test_the_gateway_call_posts_to_the_api_root_and_carries_a_bearer_header(monkeypatch):
    seen = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        return _Response(b'{"choices":[{"message":{"content":"{}"},"finish_reason":"stop"}]}', 200)

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake)
    out = engine.call_gateway("https://integrate.api.nvidia.com/v1", {"model": "m"}, "k-1", 90.0)
    assert seen["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert seen["auth"] == "Bearer k-1" and seen["timeout"] == 90.0
    assert out["choices"][0]["finish_reason"] == "stop"


def test_a_keyless_loopback_call_sends_an_empty_bearer_header(monkeypatch):
    seen = {}
    monkeypatch.setattr(engine.urllib.request, "urlopen",
                        lambda req, timeout=None: seen.update(auth=req.get_header("Authorization"))
                        or _Response(b"{}", 200))
    engine.call_gateway("http://127.0.0.1:20128/v1", {}, "", 5.0)
    assert seen["auth"] == "Bearer", "('Bearer ' + key).trim() is what the HTTP node sends"


def test_a_non_2xx_is_returned_as_data_never_raised(monkeypatch):
    """`neverError: true`: a 4xx body with an `error` object is the provider's own diagnosis and the
    ensemble has to see it, not a transport death."""
    def raise_429(req, timeout=None):
        raise _http_error(429, b'{"error": {"code": 429, "message": "capped"}}')

    monkeypatch.setattr(engine.urllib.request, "urlopen", raise_429)
    out = engine.call_gateway("https://integrate.api.nvidia.com/v1", {}, "k", 5.0)
    assert out["error"]["code"] == 429


def test_a_non_2xx_with_a_plain_text_body_becomes_a_status_wrapper(monkeypatch):
    def raise_404(req, timeout=None):
        raise _http_error(404, b"page not found")

    monkeypatch.setattr(engine.urllib.request, "urlopen", raise_404)
    out = engine.call_gateway("https://integrate.api.nvidia.com/v1", {}, "k", 5.0)
    assert out == {"_status": 404, "_body": "page not found"}


def test_a_transport_death_is_retried_like_the_http_node_then_named(monkeypatch):
    waits: list[float] = []
    tries = []

    def fake(req, timeout=None):
        tries.append(timeout)
        raise urllib.error.URLError(OSError("timed out"))

    monkeypatch.setattr(engine.urllib.request, "urlopen", fake)
    monkeypatch.setattr(engine.time, "sleep", waits.append)
    out = engine.call_gateway("https://integrate.api.nvidia.com/v1", {}, "k", 90.0)
    assert out["_transport"] == "timeout"
    assert len(tries) == engine.MAX_TRIES and waits == [engine.RETRY_WAIT_S] * 3, \
        "retryOnFail / maxTries=4 / waitBetweenTries=3000"


def test_a_lane_stops_when_the_request_runs_out_of_time_divergence_4(monkeypatch):
    """Through n8n this overrun killed the whole request (ENGINE_TIMEOUT, deliberately no failover).
    In-process it is one failed lane inside a request that can still answer from the other.

    Two ways to run out, both tested: a lane that has nothing left before it starts, and a lane that
    failed once and cannot fit a 3 s wait plus a call into what remains.
    """
    def never(req, timeout=None):
        raise AssertionError("no request may be made once the deadline has passed")

    monkeypatch.setattr(engine.urllib.request, "urlopen", never)
    out = engine.call_gateway("https://integrate.api.nvidia.com/v1", {}, "k", 90.0,
                              deadline=time.time() - 1)
    assert out["_transport"] == "unreachable"
    assert "stopped after 0 attempt(s), the time available to this request ran out" \
        in out["_transport_detail"]

    def slow_death(req, timeout=None):
        raise urllib.error.URLError(OSError("The read operation timed out"))

    monkeypatch.setattr(engine.urllib.request, "urlopen", slow_death)
    waits: list[float] = []
    monkeypatch.setattr(engine.time, "sleep", waits.append)
    out = engine.call_gateway("https://integrate.api.nvidia.com/v1", {}, "k", 90.0,
                              deadline=time.time() + engine.RETRY_WAIT_S + 1)
    assert out["_transport"] == "timeout"
    assert "stopped after 1 attempt(s), no time left for another" in out["_transport_detail"]
    assert waits == [], "a retry that cannot land must not even be waited for"


# ------------------------------------------------------------------ which engine answers

@pytest.fixture()
def client():
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db._fernet = None
    db.init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _lane_rows(client, with_key=True):
    """One provider and the one row that owns the primary lane.

    The lane is exclusive (`clear_other_lane`), so a second `lane: "primary"` row would silently
    un-seat the first rather than sit beside it. `timeout_ms` is a number the engine does NOT ship
    with, so a cfg that carries it proves the stored row reached the request.
    """
    pid = client.post("/providers", json={"name": "nvidia",
                                          "base_url": "https://integrate.api.nvidia.com/v1"}
                      ).get_json()["id"]
    r = client.post("/models", json={"provider_id": pid, "model_id": "z-ai/glm-5.3-flash",
                                     "lane": "primary", "timeout_ms": 77000})
    assert r.status_code == 201, r.get_json()
    if with_key:
        assert client.post(f"/providers/{pid}/keys", json={"api_key": PLAINTEXT}).status_code == 201
    return pid


def test_the_choice_is_derived_and_only_a_hosted_bind_can_be_local(monkeypatch):
    for name in ("DETECTION_ENGINE", "LOCAL_SINGLE_USER", "BACKEND_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert engine.native_enabled() is False, "a container must keep answering through the workflow"
    monkeypatch.setenv("LOCAL_SINGLE_USER", "1")
    monkeypatch.setenv("BACKEND_HOST", "127.0.0.1")
    assert engine.native_enabled() is True, "the packaged install is the one that must stand alone"
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    assert engine.native_enabled() is False, "a loopback-only promise cannot hold on 0.0.0.0"
    monkeypatch.setenv("DETECTION_ENGINE", "n8n")
    assert engine.native_enabled() is False, "the override wins in both directions"


def test_local_mode_never_contacts_the_workflow(client, monkeypatch):
    """"The local app shouldn't be based on n8n" is this test. Not merely that a local detect
    succeeds -- that the forwarder is unreachable code, so a machine with no n8n still scores text."""
    def no_forward(*a, **kw):
        raise AssertionError("the n8n forwarder must not be reached in local mode")

    monkeypatch.setenv("DETECTION_ENGINE", "python")
    calls: list[str] = []
    monkeypatch.setattr(engine, "run_lane", runner_for({"primary": completion(0.95, 0.9)}, calls))
    _lane_rows(client)
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "forward", no_forward)

    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert calls == ["primary"] and body["versions"]["engine"] == "python-native"
    assert body["classification"] == "AI_GENERATED" and body["lane_source"] == "deployed-default"
    row = client.get(f"/detections/{body['backend_detection_id']}").get_json()
    assert row["payload"]["versions"]["engine"] == "python-native", \
        "a verdict re-read months later has to say which engine produced it"


def test_local_mode_still_walks_the_lane_config_and_the_stored_key(client, monkeypatch):
    """The dispatch must not re-implement lane resolution: the model row, the decrypted key and the
    stored band arrive at normalize exactly as they did at the workflow.

    The request names only a model, so every other field the engine sees has to have come from the
    row -- an engine that read the deployed lane instead would report 90000 (its own baked ceiling)
    and no base_url at all."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    seen: dict = {}

    def spy(cfg, lane, text, deadline=None):
        seen["cfg"] = cfg
        seen["headers_key"] = cfg[f"{lane}_api_key"]
        return engine.parse_lane(cfg, lane, completion(0.95, 0.9))

    calls: list[str] = []
    monkeypatch.setattr(engine, "run_lane", lambda cfg, lane, text, deadline=None:
                        (calls.append(lane), spy(cfg, lane, text, deadline))[1])
    _lane_rows(client)
    r = client.post("/detect", json={"text": TEXT,
                                     "lane_config": {"primary": {"model": "z-ai/glm-5.3-flash"}}})
    assert r.status_code == 200, r.get_json()
    cfg = seen["cfg"]
    assert seen["headers_key"] == PLAINTEXT, "the stored key still pays for an explicit lane"
    assert cfg["primary_model"] == "z-ai/glm-5.3-flash"
    assert calls == ["primary"]
    assert cfg["primary_base_url"] == "https://integrate.api.nvidia.com/v1", \
        "a named model is sent to the gateway its row belongs to, not the env default"
    assert cfg["request_timeout_ms"] == 77000, "the named row supplies the ceiling, not the default"
    assert calls == ["primary"], "a decisive primary does not buy a second opinion"
    assert r.get_json()["evidence"]["models"][0]["model"] == "z-ai/glm-5.3-flash"


def test_a_hosted_deploy_still_forwards_to_the_webhook(client, monkeypatch):
    captured: dict = {}

    def fake(url, payload, headers, timeout):
        captured.update(url=url, payload=payload)
        return 200, {"classification": "AI_GENERATED", "method": "PRIMARY_DECIDED_AI",
                     "review_required": False, "config_used": {}}, 12.0

    for name in ("DETECTION_ENGINE", "LOCAL_SINGLE_USER"):
        monkeypatch.delenv(name, raising=False)
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "forward", fake)
    _lane_rows(client)
    assert client.post("/detect", json={"text": TEXT}).status_code == 200
    assert captured["url"].endswith("/webhook/ai-detect-ai")
    assert captured["payload"]["lane_config"]["primary"]["model"] == "z-ai/glm-5.3-flash"


def test_a_keyless_local_detect_names_the_page_not_the_port(client, monkeypatch):
    """Before this engine existed, the same install answered 502 ENGINE_UNREACHABLE about a port.
    Now it says the one true thing: there is no credential to pay for the call."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    _lane_rows(client, with_key=False)
    r = client.post("/detect", json={"text": TEXT})
    assert r.status_code == 400, r.get_json()
    body = r.get_json()
    assert body["error_code"] == "PROVIDER_KEY_MISSING"
    assert "Configuration page" in (body.get("hint") or "")
    assert "n8n" not in json.dumps(body)


def test_a_missing_key_is_not_read_as_a_model_failure(client):
    """"No credential" is fixed on the Configuration page; another model in the pool would answer
    the same way, so the walk must not spend the request's budget reproducing it."""
    import backend.app as app_mod
    assert app_mod.engine_level_failure({"error_code": "PROVIDER_KEY_MISSING"}) is True
    assert app_mod.engine_level_failure({"error_code": "BYOK_DISABLED"}) is True
    assert app_mod.engine_level_failure({"error_code": "http_503"}) is False


def test_gateway_health_names_the_engine_and_skips_the_workflow_probe(client, monkeypatch):
    """A red `n8n.reachable: false` beside a detect that just answered is the confusion this
    closes: in local mode that server is not on the path, so it is not even dialled."""
    monkeypatch.setenv("DETECTION_ENGINE", "python")
    import backend.app as app_mod

    def no_socket(*a, **kw):
        raise AssertionError("the workflow's /healthz must not be probed in local mode")

    monkeypatch.setattr(app_mod.urllib.request, "urlopen", no_socket)
    body = client.get("/gateway-health").get_json()
    assert body["detection_engine"] == "python-native"
    assert body["n8n"]["on_request_path"] is False
    assert "python-native" in body["n8n"]["note"]
