r"""The detection engine, in Python.

Why this file exists
--------------------
`n8n/ai-only-workflow.json` has always been the authority for what a verdict means: it owns the
prompt, the tolerant parser, the decision bands and the tie-break rule, and the backend only chose
WHICH model and WHICH credential paid for the call. That made the packaged local app a thin client
of a second program: to score a paragraph on one's own machine the user had to run n8n as well, and
a missing workflow answered about an HTTP port (`ENGINE_UNREACHABLE`) instead of about their text.
This module is the same engine inside the service that owns the request, so a local install answers
on its own and needs nothing but a provider key.

The container deployment is unchanged and still runs the workflow. Two engines answering one
question is only safe while they are provably the same arithmetic, which is what
`tools/engine_parity.py` enforces: it executes the shipped `Normalize Input`, `Parse Primary` and
`Ensemble & Build Result` nodes out of `ai-only-workflow.json` under node and diffs their output
against this module's on the same fixtures. `backend/tests/test_engine_parity.py` runs the same
comparison against the golden file that tool records, so the suite stays hermetic.

Faithfulness rules
------------------
Everything below is a transcription, not a reinterpretation: the same defaults, the same validation
order (including which check returns first and therefore preempts the others), the same salvage
conditions, the same verdict arithmetic and the same rounding. The JS-specific semantics are matched
rather than approximated, because a verdict must not depend on which runtime bought it:

* string length and slicing count UTF-16 code units (`js_len`, `js_slice`), so `text_length`, the
  max-length gate and every truncated quote are the same numbers as in the workflow;
* `Math.round` is half-up (`round4`), not Python's round-half-to-even;
* `parseFloat` reads the longest numeric prefix and clamps nothing (`js_float`);
* the FNV-1a fingerprint runs on IEEE-754 doubles with JS's `>>> 0` (`to_uint32`), which reproduces
  the float64 rounding of the workflow's product;
* `\s` and `String.trim()` get JS's exact character set -- Python's `\s` also matches `\x1c`-`\x1f`
  and `str.strip()` also removes them, so a text ending in a record separator would otherwise reach
  the gateway with different bytes and a different length;
* `_js_str` renders numbers and booleans the way JS string interpolation does, because those strings
  are the validation messages users read.

Deliberate divergences, each masked in `tools/engine_parity.py` (see `masks_for`) rather than
waived -- the first two are about what a FAILED lane says about itself, and none of them can change
a verdict:

1. A non-2xx gateway response that carries no `error` object is reported as `http_<status>`. The
   n8n HTTP node runs with `neverError: true`, so it hands its parser the response body and never
   the status: a `404 page not found` from a mis-pinned gateway became `no_choices`, which reads as
   "the model answered badly" rather than "the id was posted to the wrong host". The benchmark
   harness made this same change on 2026-10-17 for the same reason. A body that does carry an
   `error` object is still reported exactly as the workflow reports it.
2. A lane that dies on the transport says `timeout` or `unreachable` instead of the workflow's
   `provider_error`. Which phase failed is the difference between waiting for a pool and fixing
   Settings, the distinction task #123 made at the request level; the in-process engine is the only
   place that can still see it.
3. `ALLOW_BYOK` defaults to true here while the workflow defaults it to false, and the
   `PROVIDER_KEY_MISSING` text names the Configuration page rather than `PRIMARY_API_KEY`. Both are
   about who the caller is, not about the verdict: a hosted n8n must not let a visitor swap the
   operator's credential, whereas this engine only ever answers the single-owner loopback install,
   where the caller IS the operator and BYOK is the feature they are looking at. The parity harness
   therefore compares a rejection's status and `error_code`, not its prose.
4. A lane stops when the REQUEST runs out of time, not only when its own `request_timeout_ms` does.
   Through n8n the backend gives up on the workflow and answers `ENGINE_TIMEOUT` (which is
   deliberately a no-failover code), so a slow primary took the whole request down with it and no
   second candidate was ever tried. In-process, the same overrun is a failed lane inside the
   request, which is what the failover walk in `/detect` was built to handle.
5. The tie-break lane can walk down the pool (`lane_config.secondary.candidates`), and three
   provenance fields exist only because of that port: `config.secondary_candidates`, which the
   workflow's validator has no concept of, and `lane_meta.<lane>.model`, a copy of the
   already-compared `config.<lane>_model` that the walk re-points at the leg it dialled. No fixture
   sends `candidates` -- the workflow would reject it -- so the two masked model fields are equal
   strings in every recorded case, and the walk's own `secondary_walk` block never appears.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENGINE_NAME = "python-native"

# ------------------------------------------------------------------ JS primitives

# JS's `\s` / `String.trim()` set: WhiteSpace + LineTerminator, and nothing else.
_JS_WS = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
          "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
_JS_WS_RUN = re.compile("[" + re.escape(_JS_WS) + "]+")
_JS_WS_CHARS = frozenset(_JS_WS)
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def js_trim(value: str) -> str:
    start, end = 0, len(value)
    while start < end and value[start] in _JS_WS_CHARS:
        start += 1
    while end > start and value[end - 1] in _JS_WS_CHARS:
        end -= 1
    return value[start:end]


def _utf16_units(value: str) -> list[int]:
    """JS's view of a string: every non-BMP character as its surrogate pair."""
    units: list[int] = []
    for ch in value:
        code = ord(ch)
        if code > 0xFFFF:
            code -= 0x10000
            units.append(0xD800 + (code >> 10))
            units.append(0xDC00 + (code & 0x3FF))
        else:
            units.append(code)
    return units


def js_len(value: str) -> int:
    return len(_utf16_units(value))


def js_slice(value: str, start: int, end: int | None = None) -> str:
    """String.prototype.slice over code units. A cut that would leave a lone surrogate behind drops
    it: Python can hold one but UTF-8 cannot encode it, and no quote is worth an encoder error."""
    units = _utf16_units(value)
    n = len(units)
    s = start if start >= 0 else max(n + start, 0)
    e = n if end is None else (end if end >= 0 else max(n + end, 0))
    if s >= e:
        return ""
    import array
    buf = array.array("H", units[s:e])
    return buf.tobytes().decode("utf-16-le", errors="ignore")


def to_uint32(value: float) -> int:
    """JS's ToUint32 applied to an already-rounded double."""
    if math.isnan(value) or math.isinf(value):
        return 0
    return int(value) & 0xFFFFFFFF


def to_int32(value: float) -> int:
    """JS's ToInt32: truncate, take modulo 2**32, read the low word as signed. A uint32 seed enters
    a `^` as a NEGATIVE operand, which is what the fingerprint below needs."""
    u = to_uint32(value)
    return u - 0x100000000 if u >= 0x80000000 else u


def round4(value: float) -> float:
    """Math.round(n * 10000) / 10000 -- half-up, not Python's round-half-to-even."""
    return math.floor(value * 10000 + 0.5) / 10000


def _js_number(x: float | int) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return repr(x) if isinstance(x, float) else str(x)


def _js_str(value) -> str:
    """String(value) as JS sees it -- the difference shows up in error text and in `given()`."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return _js_number(value)
    return str(value)


def _js_str_or(value) -> str:
    """`String(raw || '')`, which is what the workflow's three check functions start with. A falsy
    argument -- null, undefined, '', 0, false -- becomes the empty string, so `0` never reaches the
    parser as the text "0"."""
    return "" if not js_truthy(value) else _js_str(value)


def js_truthy(value) -> bool:
    """JS truthiness, which is not Python's: an empty array or object is TRUE in JS, and the
    workflow tests `payload.error`, `choices[0]` and `choice.message` that way. Using Python's
    `if x:` on those would send a JSON error envelope of `{}` down the no_choices branch."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0 and value == value
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (dict, list, tuple)):
        return True
    return bool(value)


def _js_json_numbers(value):
    """Walk a JSON tree replacing `1.0` with `1`. Python keeps the float where JS never had one,
    and `body_json` is compared byte for byte against the node's `JSON.stringify` output."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _js_json_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_js_json_numbers(v) for v in value]
    return value


def _json_str(value) -> str:
    """JSON.stringify: no spaces, real characters, JS-shaped numbers."""
    try:
        return json.dumps(_js_json_numbers(value), ensure_ascii=False, separators=(",", ":"),
                          default=str)
    except (TypeError, ValueError):
        return _js_str(value)


def js_float(value) -> float | None:
    """parseFloat: skip leading whitespace, read the longest numeric prefix, else NaN -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = js_trim(_js_str(value if value is not None else ""))
    m = re.match(r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def num(value, default: float) -> float:
    """The workflow's `num(env.X, default)`: a finite parse, else the default."""
    n = js_float(value)
    return default if n is None or not math.isfinite(n) else n


def iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# -------------------------------------------------------------------- the prompt
# Byte-identical to SYSTEM_PROMPT in n8n/generate-workflow.py's build_request_js(). Every band in
# the benchmark was fitted through this text, so changing it here without changing it there -- or
# re-measuring -- invalidates the numbers on the ranking page. tools/engine_parity.py compares the
# two copies literally.

SYSTEM_PROMPT = (
    'You are an AI text detection classifier. Return ONLY a compact JSON object. No markdown, '
    'no code fences, no commentary, no reasoning text. Schema: {"ai_probability": number between '
    '0 and 1, "confidence": number between 0 and 1, "signals": up to 3 objects of {"name": string, '
    '"direction": "AI" or "HUMAN", "strength": number between 0 and 1}, "phrases": up to 4 objects '
    'of {"quote": string copied VERBATIM from the text, at most 120 characters, exact substring '
    'with no ellipsis and no edits, "direction": "AI" or "HUMAN", "strength": number between 0 and '
    '1}}. ai_probability means the probability the text was generated by an AI rather than written '
    'by a human. For each phrase quote the passage exactly as written, then judge that passage; '
    'pick the strongest evidence in each direction.'
)

USER_PROMPT_HEAD = 'Was the following text likely written by an AI? Judge the text itself.\n\n"""\n'
USER_PROMPT_TAIL = '\n"""\n\nReturn ONLY the JSON object.'

# ------------------------------------------------------------------ baked defaults
# The workflow's own fallbacks (the literals in NORMALIZE_JS), not new numbers. Here they are only
# reached when a model row carries no column value; the `tuning` block built from the DB normally
# supplies them. backend/seed.py keeps a second copy for the rows themselves, and
# tests/test_seed.py asserts neither copy drifts from the generator.

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_PRIMARY_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_SECONDARY_MODEL = "google/diffusiongemma-26b-a4b-it"
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,::1,integrate.api.nvidia.com,openrouter.ai"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# cfg key -> (env name, default, integer)
ENV_DEFAULTS = {
    "min_text_length": ("MIN_TEXT_LENGTH", 10, True),
    "max_text_length": ("MAX_TEXT_LENGTH", 50000, True),
    "ai_prob_threshold": ("AI_PROBABILITY_THRESHOLD", 0.69, False),
    "ai_conf_threshold": ("AI_CONFIDENCE_THRESHOLD", 0.60, False),
    "human_prob_threshold": ("HUMAN_PROBABILITY_THRESHOLD", 0.40, False),
    "human_conf_threshold": ("HUMAN_CONFIDENCE_THRESHOLD", 0.60, False),
    "secondary_decision_threshold": ("SECONDARY_DECISION_THRESHOLD", 0.90, False),
    "single_model_penalty": ("SINGLE_MODEL_CONFIDENCE_PENALTY", 1.0, False),
    "request_timeout_ms": ("LLM_TIMEOUT_MS", 90000, True),
    "temperature": ("LLM_TEMPERATURE", 0.1, False),
    "top_p": ("LLM_TOP_P", 0.9, False),
    "max_tokens": ("LLM_MAX_TOKENS", 0, True),
}
VERSION_DEFAULTS = {"detector_version": "2.0.0", "classifier_version": "2.0.0",
                    "threshold_version": "2.0.0"}


def max_text_length() -> int:
    """The ceiling `normalize` will enforce, read the same way it reads it.

    The app needs this before it forwards a body: on the hosted deploy the engine is not on the
    request path at all, so a 4 MB text would go to the workflow, get refused there, and cost the
    user a round trip to learn what one integer could have said. Reading the table rather than
    restating the number is what keeps the two deploys from disagreeing about what a detection is
    allowed to be sent -- and an operator who raises MAX_TEXT_LENGTH in the environment raises the
    front door with it.
    """
    name, default, is_int = ENV_DEFAULTS["max_text_length"]
    value = num(os.environ.get(name), default)
    return math.floor(value) if is_int else value

# Same table, same ranges, same messages as the workflow's TUNING_FIELDS.
TUNING_FIELDS = {
    "temperature": (0.0, 2.0, False),
    "top_p": (0.0, 1.0, False),
    "max_tokens": (0, 4096, True),
    "request_timeout_ms": (1000, 300000, True),
    "ai_prob_threshold": (0.0, 1.0, False),
    "ai_conf_threshold": (0.0, 1.0, False),
    "human_prob_threshold": (0.0, 1.0, False),
    "human_conf_threshold": (0.0, 1.0, False),
    "secondary_decision_threshold": (0.0, 1.0, False),
    "single_model_penalty": (0.0, 1.0, False),
}

# The n8n HTTP node is configured retryOnFail / maxTries=4 / waitBetweenTries=3000.
MAX_TRIES = 4
RETRY_WAIT_S = 3.0

# The tie-break walk's floor for starting another leg. Below this a substituted model cannot
# finish, and the request would trade the answer one lane already gave for a gateway timeout that
# carries nothing. The primary walk keeps the same twenty seconds (`FAILOVER_RESERVE_S` in app.py):
# one lane call is shorter than a whole detection, but the rule is identical -- never start what
# you cannot finish.
SECONDARY_MIN_ATTEMPT_S = 20.0

# One entry of `lane_config.secondary.candidates`: a lane the tie-break may be moved to. An
# endpoint and a model are required, because "which box" and "which model" are the two questions a
# lane has to answer for itself; the other two fields say how that leg is paid for and which ruler
# it is read on.
CANDIDATE_FIELDS = ("base_url", "model", "api_key", "secondary_decision_threshold")


class Reject(Exception):
    """A request the engine will not spend a gateway call on. Mirrors the workflow's `fail()`."""

    def __init__(self, message: str, hint: str | None = None, code: str = "INPUT_REJECTED"):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


# ------------------------------------------------------------------ lane validation

def check_base_url(raw, allowed_hosts: tuple[str, ...]) -> dict:
    """Port of checkBaseUrl: exact-host allowlist, no credentials, no query/fragment, API root."""
    value = js_trim(_js_str_or(raw))
    if not value:
        return {"ok": False, "error": "base_url must be a non-empty string."}
    if js_len(value) > 500:
        return {"ok": False, "error": "base_url is too long (max 500 characters)."}

    m = re.match(r"(?i)^(https?)://([^/?#]*)([\s\S]*)$", value)
    if not m:
        return {"ok": False, "error": "base_url must be an absolute http(s) URL, for example "
                                      "http://localhost:20128/v1"}
    scheme = m.group(1).lower()
    authority = m.group(2)
    rest = m.group(3) or ""

    if authority == "":
        return {"ok": False, "error": "base_url has no hostname."}
    if "@" in authority:
        return {"ok": False, "error": "base_url must not embed credentials."}

    bracketed = authority.startswith("[")
    port_str = ""
    if bracketed:
        close = authority.find("]")
        if close == -1:
            return {"ok": False, "error": "base_url has an unterminated IPv6 hostname."}
        host = authority[1:close]
        if not re.fullmatch(r"[0-9A-Fa-f:.]+", host):
            return {"ok": False, "error": "base_url has an invalid IPv6 hostname."}
        after = authority[close + 1:]
        if after and after[0] != ":":
            return {"ok": False, "error": "base_url has unexpected characters after the hostname."}
        port_str = after[1:]
    else:
        idx = authority.rfind(":")
        tail = authority[idx + 1:] if idx != -1 else ""
        if idx != -1 and re.fullmatch(r"[0-9]+", tail):
            port_str = tail
            host = authority[:idx]
        else:
            host = authority
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
            return {"ok": False, "error": "base_url has an invalid hostname."}
    host = host.lower()

    if port_str != "":
        if not re.fullmatch(r"[0-9]{1,5}", port_str):
            return {"ok": False, "error": "base_url port is not a number."}
        port = int(port_str)
        if port < 1 or port > 65535:
            return {"ok": False, "error": "base_url port is out of range (1-65535)."}

    # Exact match, never a suffix: 'openrouter.ai.evil.com' must not pass because
    # 'openrouter.ai' is listed.
    if host not in allowed_hosts:
        return {"ok": False, "error": 'base_url host "' + host + '" is not in ALLOWED_LLM_HOSTS ('
                                      + ", ".join(allowed_hosts) + ")."}

    if "?" in rest:
        return {"ok": False, "error": "base_url must not contain a query string."}
    if "#" in rest:
        return {"ok": False, "error": "base_url must not contain a fragment."}
    if rest and rest[0] != "/":
        return {"ok": False, "error": "base_url has a malformed path."}
    if not re.fullmatch(r"[/A-Za-z0-9._~%-]*", rest):
        return {"ok": False, "error": "base_url path contains unsupported characters."}
    if re.search(r"/chat/completions/?$", rest, re.I):
        return {"ok": False, "error": "base_url must be the API root (for example "
                                      "http://localhost:20128/v1), not the /chat/completions "
                                      "endpoint."}

    origin = scheme + "://" + (f"[{host}]" if bracketed else host) + \
        ("" if port_str == "" else ":" + port_str)
    return {"ok": True, "value": origin + re.sub(r"/+$", "", rest), "host": host}


def check_model(raw) -> dict:
    value = js_trim(_js_str_or(raw))
    if not value:
        return {"ok": False, "error": "model must be a non-empty string."}
    if js_len(value) > 200:
        return {"ok": False, "error": "model is too long (max 200 characters)."}
    if not re.fullmatch(r"[A-Za-z0-9._:/@+-]+", value):
        return {"ok": False, "error": "model contains unsupported characters (allowed: letters, "
                                      "digits and . _ : / @ + -)."}
    return {"ok": True, "value": value}


def check_key(raw) -> dict:
    value = js_trim(_js_str_or(raw))
    if js_len(value) > 512:
        return {"ok": False, "error": "API key is too long (max 512 characters)."}
    # Covers nvapi-, sk-or-v1-, bearer JWTs and base64 blobs, and excludes
    # whitespace, quotes and CR/LF that could break out of the header.
    if not re.fullmatch(r"[A-Za-z0-9._:+/-]+", value):
        return {"ok": False, "error": "API key contains unsupported characters (allowed: letters, "
                                      "digits and . _ : + / -)."}
    return {"ok": True, "value": value}


def fingerprint(text: str) -> str:
    """FNV-1a derived, computed exactly the way the workflow does it: non-cryptographic, used only
    to correlate a verdict with the call that produced it."""
    normalized = js_trim(_JS_WS_RUN.sub(" ", text))
    h1 = 0x811C9DC5
    h2 = 0x9E3779B9
    for i, code in enumerate(_utf16_units(normalized)):
        # `h1 ^ c` runs through ToInt32 in JS, so the operand entering the multiply is signed once
        # the seed's high bit is set. A Python-int XOR keeps the same low 32 bits but a different
        # magnitude, and past 2**53 the product's rounding decides those bits -- so the fingerprint
        # would drift from the workflow's.
        h1 = to_uint32(float(to_int32(h1) ^ code) * 16777619.0)
        h2 = to_uint32(float(h2 + code + i) * 2246822507.0)
    return f"{h1:08x}{h2:08x}"


# ------------------------------------------------------------------- normalize

def _given(value) -> bool:
    """A field the UI left untouched arrives as '' or '   '. That means "not specified, use the
    environment", not "invalid"."""
    return value is not None and js_trim(_js_str(value)) != ""


def secondary_candidates(raw, hosts: tuple[str, ...],
                         cfg: dict) -> tuple[list[dict], list[str], bool]:
    """Read the pool legs the tie-break may be moved to, with the request's own validators.

    Every entry is checked by `check_base_url` and `check_model` -- the same two functions that
    gate the head lane -- because a candidate the app accepted at request time and the engine then
    refuses to dial is the `LANE_CONFIG_INVALID` this repo has been bitten by twice. A credential
    on a candidate is a credential *in the request*, so it goes through the `ALLOW_BYOK` guard
    exactly as an `x-secondary-api-key` header does: on a hosted deploy a caller may not name the
    key the operator pays with, whoever the caller is.

    The key of a candidate that names none is not baked in here: it is inherited from the lane at
    dial time, which is after the header has been read.
    """
    out: list[dict] = []
    errors: list[str] = []
    byok_rejected = False
    if not isinstance(raw, list):
        return out, ["lane_config.secondary.candidates must be a list of lanes, each "
                     "{" + ", ".join(CANDIDATE_FIELDS[:2]) + ", ...}."], False
    for i, item in enumerate(raw):
        where = f"lane_config.secondary.candidates[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object with base_url and model.")
            continue
        for k in item:
            if k not in CANDIDATE_FIELDS:
                errors.append(f'{where} has unsupported field "{k}" '
                              f"(accepted: {', '.join(CANDIDATE_FIELDS)}).")
        url = check_base_url(item.get("base_url"), hosts)
        if not url.get("ok"):
            errors.append(f"{where}.base_url: {url['error']}")
        model = check_model(item.get("model"))
        if not model.get("ok"):
            errors.append(f"{where}.model: {model['error']}")
        if not url.get("ok") or not model.get("ok"):
            continue
        entry = {"base_url": url["value"], "model": model["value"]}

        supplied = item.get("api_key")
        if _given(supplied):
            checked = check_key(supplied)
            if not checked.get("ok"):
                errors.append(f"{where}.api_key: {checked['error']}")
                continue
            if not cfg["allow_byok"]:
                byok_rejected = True
                errors.append(f"{where}.api_key was sent in the body but bring-your-own-key is "
                              "disabled. Set ALLOW_BYOK=true to enable it.")
                continue
            entry["api_key"] = checked["value"]
            entry["key_source"] = "request"

        cut = item.get("secondary_decision_threshold")
        if _given(cut):
            n = js_float(cut)
            if n is None or not math.isfinite(n) or n < 0 or n > 1:
                errors.append(f"{where}.secondary_decision_threshold must be a number between 0 "
                              f"and 1 (got {_js_str(cut)}).")
                continue
            entry["secondary_decision_threshold"] = n
        out.append(entry)
    return out, errors, byok_rejected


def normalize(payload: dict, headers: dict, env: dict | None = None,
              allowed_hosts: tuple[str, ...] | None = None) -> dict:
    """The workflow's `Normalize Input` node: config, lane guards, tuning merge, text identity."""
    env = env if env is not None else os.environ
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else hosts_from_env(env)

    cfg: dict = {
        "primary_base_url": _js_str(env.get("PRIMARY_BASE_URL") or "") or DEFAULT_BASE_URL,
        "primary_api_key": _js_str(env.get("PRIMARY_API_KEY") or ""),
        "primary_model": _js_str(env.get("PRIMARY_MODEL") or "") or DEFAULT_PRIMARY_MODEL,
        "secondary_base_url": _js_str(env.get("SECONDARY_BASE_URL") or "") or DEFAULT_BASE_URL,
        "secondary_api_key": _js_str(env.get("SECONDARY_API_KEY") or ""),
        "secondary_model": _js_str(env.get("SECONDARY_MODEL") or "") or DEFAULT_SECONDARY_MODEL,
        "secondary_enabled": _js_str(env.get("SECONDARY_ENABLED", "true")).lower() != "false",
        # The pool legs the tie-break may walk to, in the order the caller ranked them. Empty means
        # no walk: the lane gets one call and the answer is the one this engine has always given.
        "secondary_candidates": [],
        "allowed_hosts": hosts,
        # The workflow's default is false because a hosted n8n must not let a caller swap the
        # operator's credential for a browser-supplied one. This engine only ever answers the
        # single-owner loopback install, where the caller IS the operator and the BYOK feature
        # exists for them, so that flag's safe direction inverts here.
        "allow_byok": _js_str(env.get("ALLOW_BYOK", "true")).lower() == "true",
    }
    for key, (name, default, is_int) in ENV_DEFAULTS.items():
        value = num(env.get(name), default)
        cfg[key] = math.floor(value) if is_int else value
    if "min_text_length" in cfg:
        cfg["min_text_length"] = max(1, cfg["min_text_length"])
    for key, default in VERSION_DEFAULTS.items():
        cfg[key] = _js_str(env.get(key.upper()) or "") or default

    # JS `!payload || typeof payload !== 'object'`: a JSON array body IS an object to typeof, so it
    # gets as far as the text check and fails there -- which is what the workflow answers, and the
    # difference is visible in /detect's 400 body.
    if not isinstance(payload, (dict, list)) or not js_truthy(payload):
        raise Reject("Request body must be a JSON object.",
                     'POST {"text": "..."} with Content-Type: application/json')
    text_in = _prop(payload, "text")
    if not isinstance(text_in, str) or js_trim(text_in) == "":
        raise Reject("Field 'text' is required and must be a non-empty string.")

    text = js_trim(text_in)
    if js_len(text) < cfg["min_text_length"]:
        raise Reject(f"Text too short: {js_len(text)} characters "
                     f"(minimum {_js_number(cfg['min_text_length'])}).")
    if js_len(text) > cfg["max_text_length"]:
        raise Reject(f"Text too long: {js_len(text)} characters "
                     f"(maximum {_js_number(cfg['max_text_length'])}).")

    errors: list[str] = []
    lane_warnings: list[str] = []
    lane_meta: dict[str, dict] = {}
    byok_rejected = False

    lc_raw = _prop(payload, "lane_config")
    lc_is_bad = lc_raw is not None and not isinstance(lc_raw, dict)
    if lc_is_bad:
        errors.append("lane_config must be an object with optional primary and secondary entries.")
    lc = lc_raw if (not lc_is_bad and isinstance(lc_raw, dict)) else {}

    hdrs = {str(k).lower(): v for k, v in (headers or {}).items()}

    def hdr(name: str) -> str:
        v = hdrs.get(name)
        return js_trim(v) if isinstance(v, str) else ""

    for lane in ("primary", "secondary"):
        raw_lane = lc.get(lane)
        lane_bad = raw_lane is not None and not isinstance(raw_lane, dict)
        if lane_bad:
            errors.append(f"lane_config.{lane} must be an object.")
        o = raw_lane if (not lane_bad and isinstance(raw_lane, dict)) else {}
        meta = {"base_url_source": "env", "model_source": "env", "key_source": "env",
                "base_url": cfg[lane + "_base_url"], "host": None}

        allowed = (["base_url", "model", "enabled", "candidates"] if lane == "secondary"
                   else ["base_url", "model"])
        for k in o:
            if k not in allowed:
                errors.append(f'lane_config.{lane} has unsupported field "{k}" '
                              f"(accepted: {', '.join(allowed)}).")

        if lane == "secondary" and "candidates" in o:
            entries, cand_errors, cand_byok = secondary_candidates(o["candidates"], hosts, cfg)
            errors.extend(cand_errors)
            byok_rejected = byok_rejected or cand_byok
            cfg["secondary_candidates"] = entries

        # Only the secondary lane can be switched off per request: the primary IS the detector.
        enabled = o.get("enabled")
        if (lane == "secondary" and enabled is not None
                and js_trim(_js_str(enabled)) != ""):
            flag = _js_str(enabled) if isinstance(enabled, bool) else _js_str(enabled).strip().lower()
            if flag == "true":
                cfg["secondary_enabled"] = True
            elif flag == "false":
                cfg["secondary_enabled"] = False
            else:
                errors.append("lane_config.secondary.enabled must be true or false "
                              f"(got {_js_str(enabled)}).")

        if _given(o.get("base_url")):
            r = check_base_url(o["base_url"], hosts)
            if not r.get("ok"):
                errors.append(f"lane_config.{lane}.base_url: {r['error']}")
            else:
                cfg[lane + "_base_url"] = r["value"]
                meta["base_url"] = r["value"]
                meta["host"] = r["host"]
                meta["base_url_source"] = "request"
        if _given(o.get("model")):
            r = check_model(o["model"])
            if not r.get("ok"):
                errors.append(f"lane_config.{lane}.model: {r['error']}")
            else:
                cfg[lane + "_model"] = r["value"]
                meta["model_source"] = "request"

        supplied = hdr("x-" + lane + "-api-key")
        if supplied != "":
            if not cfg["allow_byok"]:
                byok_rejected = True
                errors.append(f"An {lane} API key was sent in a header but bring-your-own-key is "
                              "disabled. Set ALLOW_BYOK=true to enable it.")
                lane_meta[lane] = meta
                continue
            r = check_key(supplied)
            if not r.get("ok"):
                errors.append(f"x-{lane}-api-key: {r['error']}")
            else:
                cfg[lane + "_api_key"] = r["value"]
                meta["key_source"] = "header"
        elif cfg[lane + "_api_key"]:
            meta["key_source"] = "env"
        else:
            meta["key_source"] = "none"

        # Re-parse the (possibly env-only) URL so the loopback rule applies to both.
        resolved = check_base_url(cfg[lane + "_base_url"], hosts)
        if not resolved.get("ok"):
            errors.append(f"{lane} base_url from configuration is invalid: {resolved['error']}")
        else:
            meta["host"] = resolved["host"]
            meta["base_url"] = resolved["value"]
            cfg[lane + "_base_url"] = resolved["value"]
        meta["model"] = cfg[lane + "_model"]
        lane_meta[lane] = meta

    tn_raw = _prop(payload, "tuning")
    tn_is_bad = tn_raw is not None and not isinstance(tn_raw, dict)
    if tn_is_bad:
        errors.append("tuning must be an object of numeric overrides.")
    tn = tn_raw if (not tn_is_bad and isinstance(tn_raw, dict)) else {}
    tuning_applied: dict[str, object] = {}
    for k, v in tn.items():
        if k not in TUNING_FIELDS:
            errors.append(f'tuning has unsupported field "{k}" (accepted: '
                          f"{', '.join(TUNING_FIELDS)}).")
            continue
        if v is None or v == "":
            continue
        n = js_float(v)
        if n is None or not math.isfinite(n):
            errors.append(f"tuning.{k} must be a number.")
            continue
        lo, hi, is_int = TUNING_FIELDS[k]
        if n < lo or n > hi:
            errors.append(f"tuning.{k} must be between {_js_number(lo)} and {_js_number(hi)} "
                          f"(got {_js_number(n)}).")
            continue
        cfg[k] = math.floor(n) if is_int else n
        tuning_applied[k] = cfg[k]

    # The two cuts must bracket a real gray zone. An inverted pair classifies every text as both
    # AI and HUMAN at once and the primary branch wins by code order.
    if cfg["human_prob_threshold"] >= cfg["ai_prob_threshold"]:
        errors.append(f"human_prob_threshold ({_js_number(cfg['human_prob_threshold'])}) must be "
                      f"below ai_prob_threshold ({_js_number(cfg['ai_prob_threshold'])}).")

    # A keyless lane is only acceptable on a loopback gateway (a self-hosted gateway running with
    # no key requirement). Anything off-box without a key is a hard reject. This returns before the
    # accumulated lane errors are reported, exactly as the workflow's does.
    primary_host = (lane_meta.get("primary") or {}).get("host") or ""
    if not cfg["primary_api_key"] and primary_host not in LOOPBACK_HOSTS:
        raise Reject(
            "No primary API key available: no credential is configured for this endpoint and none "
            "was supplied.",
            "Add a key for the provider on the Configuration page, or set PRIMARY_API_KEY in the "
            "environment this app runs in, or point the primary lane at a loopback gateway. This "
            "install scores text itself and holds no credential of its own, so there is nothing "
            "else to pay for the call with.",
            "PROVIDER_KEY_MISSING")
    if not cfg["primary_api_key"]:
        lane_warnings.append("Primary lane is running without an API key because the host is "
                             "loopback.")
    if cfg["secondary_enabled"] and not cfg["secondary_api_key"]:
        lane_warnings.append("Secondary lane has no API key; the gray-zone second opinion will "
                             "fail if it is called.")

    if errors:
        # Word-for-word the workflow's hint. `parity-golden.json` compares it, and the workflow
        # cannot produce a candidates error, so the string stays as the shipped one.
        raise Reject(" ".join(errors),
                     "lane_config accepts base_url and model (plus enabled for the secondary "
                     "lane); keys go in x-primary-api-key / x-secondary-api-key headers; tuning "
                     "accepts numbers only.",
                     "BYOK_DISABLED" if byok_rejected else "LANE_CONFIG_INVALID")

    doc_id_raw = _prop(payload, "document_id")
    given_id = js_trim(doc_id_raw) if isinstance(doc_id_raw, str) else ""
    document_id = (js_slice(given_id, 0, 200) if given_id
                   else "doc_" + str(int(time.time() * 1000)) + "_"
                   + "".join(random.choice(_B36) for _ in range(8)))

    source_url_raw = _prop(payload, "source_url")
    content_type_raw = _prop(payload, "content_type")
    metadata_raw = _prop(payload, "metadata")
    requested_by_raw = _prop(payload, "requested_by")

    return {
        "valid": True,
        "document_id": document_id,
        "text": text,
        "text_length": js_len(text),
        "text_fingerprint": fingerprint(text),
        "source_url": (js_slice(_js_str(source_url_raw), 0, 2000)
                       if isinstance(source_url_raw, str) else None),
        "content_type": (js_slice(_js_str(content_type_raw), 0, 200)
                         if isinstance(content_type_raw, str) else None),
        # JS `payload.metadata && typeof payload.metadata === 'object'` -- and an array IS an object
        # to typeof, so a list metadata passes here too rather than collapsing to {}.
        "metadata": (metadata_raw if js_truthy(metadata_raw)
                     and isinstance(metadata_raw, (dict, list)) else {}),
        "requested_by": (js_slice(_js_str(requested_by_raw), 0, 200)
                         if isinstance(requested_by_raw, str) else None),
        "timestamp": iso_now(),
        "lane_meta": lane_meta,
        "lane_warnings": lane_warnings,
        "tuning_applied": tuning_applied,
        "config": cfg,
    }


def hosts_from_env(env: dict) -> tuple[str, ...]:
    raw = _js_str(env.get("ALLOWED_LLM_HOSTS") or "") or DEFAULT_ALLOWED_HOSTS
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


# ------------------------------------------------------------------ build request

def build_chat_body(cfg: dict, lane: str, text: str) -> dict:
    """`Build <Lane> Request`. response_format/json_object is deliberately NOT sent: on free-tier
    upstreams it provably triggered 400s, reasoning preambles and duplicated-key garbage. A strict
    prompt plus tolerant parsing on the far side is more reliable."""
    body = {
        "model": cfg[lane + "_model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_HEAD + text + USER_PROMPT_TAIL},
        ],
        "temperature": cfg["temperature"],
        "top_p": cfg["top_p"],
    }
    # Key order follows the workflow's object literal -- which puts max_tokens ahead of stream --
    # so `body_json` from the shipped node and this dict serialise to the same bytes.
    if cfg["max_tokens"] > 0:
        body["max_tokens"] = cfg["max_tokens"]
    body["stream"] = False
    return body


# ------------------------------------------------------------------------ parser

def _prop(obj, name):
    """Property access as JS reads it: an array or a string has no `.choices`, and asking for one
    is undefined rather than an error."""
    return obj.get(name) if isinstance(obj, dict) else None


def _js_keys(obj):
    """Object.keys: a dict gives its keys, an array or a string gives its indices, anything else
    gives nothing. Reached only by the `null_content` detail, but that detail is what tells an
    operator which fields their gateway actually returned."""
    if isinstance(obj, dict):
        return list(obj.keys())
    if isinstance(obj, (list, str)):
        return [str(i) for i in range(len(obj))]
    return []


def _whole_number(text: str, pattern: re.Pattern) -> str | None:
    """The digits a truncation left behind, but only if the truncation left ALL of them: the match
    must be followed by a JSON delimiter, otherwise the number may have been cut mid-digit."""
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1) if re.match("[" + re.escape(_JS_WS) + "]*[,}\\]]", text[m.end():]) else None


# Exactly the workflow's two salvage regexes, including its `-?` (a leading PLUS is not a number
# this parser will invent) and its `\s` set (Python's also covers \x1c-\x1f, which JS's does not).
_JS_S = "[" + re.escape(_JS_WS) + "]*"
_NUM = r"(-?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)"
_AI_PROB_RE = re.compile(r'"ai_probability"' + _JS_S + r":" + _JS_S + _NUM)
_CONFIDENCE_RE = re.compile(r'"confidence"' + _JS_S + r":" + _JS_S + _NUM)


def to_unit(value) -> float | None:
    n = js_float(value)
    if n is None or not math.isfinite(n):
        return None
    return min(1.0, max(0.0, n))


def extract(payload) -> dict:
    """The parse nodes' `extract()`, plus divergence #1 (http_<status>) and #2 (timeout/unreachable).

    `payload` is whatever the gateway round-trip produced: the response body, a `_transport` marker
    for a call that never completed, or a `_status`/`_body` wrapper for a non-2xx answer that
    carried no `error` object.
    """
    if isinstance(payload, dict) and payload.get("_transport"):
        return {"ok": False, "error_code": _js_str(payload["_transport"]),
                "error_detail": js_slice(_js_str(payload.get("_transport_detail") or ""), 0, 300)}
    if not isinstance(payload, (dict, list)):
        # JS `!payload || typeof payload !== 'object'`. Everything JSON can decode except an object
        # or an array takes this branch -- including a bare string a mis-pinned gateway answered
        # with. A list must NOT: it is typeof 'object', so it falls through to no_choices.
        return {"ok": False, "error_code": "empty_response",
                "error_detail": "HTTP node produced no object"}

    err = _prop(payload, "error")
    if js_truthy(err):
        code = _prop(err, "code")
        if not js_truthy(code):
            code = _prop(err, "status")
        if not js_truthy(code):
            code = "provider_error"
        message = _prop(err, "message")
        detail = message if js_truthy(message) else _json_str(err)
        return {"ok": False, "error_code": _js_str(code),
                "error_detail": js_slice(_js_str(detail), 0, 300)}

    choices = _prop(payload, "choices")
    choice = choices[0] if (isinstance(choices, list) and choices) else None
    if not js_truthy(choice):
        if isinstance(payload, dict) and isinstance(payload.get("_status"), int) \
                and payload["_status"] >= 400:
            return {"ok": False, "error_code": f"http_{payload['_status']}",
                    "error_detail": js_slice(_json_str(payload.get("_body")), 0, 300)}
        return {"ok": False, "error_code": "no_choices",
                "error_detail": js_slice(_json_str(payload), 0, 300)}

    msg = _prop(choice, "message")
    if not js_truthy(msg):
        msg = {}
    content = _prop(msg, "content")
    if content is None or content == "":
        reasoning = _prop(msg, "reasoning")
        content = reasoning if js_truthy(reasoning) else ""
    if not isinstance(content, str):
        content = _json_str(content)
    if not js_truthy(content):
        return {"ok": False, "error_code": "null_content",
                "error_detail": "message keys: " + ",".join(_js_keys(msg))}

    finish = _prop(choice, "finish_reason") or None
    stripped = js_trim(re.sub(r"```json", "```", content, flags=re.I).replace("```", ""))
    start = stripped.find("{")
    if start == -1:
        return {"ok": False, "error_code": "no_json_object",
                "error_detail": js_slice(stripped, 0, 200), "finish_reason": finish}

    # Scan for the first balanced object, honouring string escapes.
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    candidate = stripped[start:end + 1] if end > start else stripped[start:]

    parsed = None
    salvaged = False
    try:
        parsed = json.loads(candidate)
    except ValueError:
        # Truncated output (finish_reason=length) still usually carries the two numbers -- but only
        # those the cut left COMPLETE. A number that runs to the end of the string may have lost
        # digits, and the regex cannot tell: `0.78` cut at `0.` matches as 0, which is not a missing
        # answer but an invented one (confidently HUMAN, and ok:true, so it would decide alone when
        # the other lane failed). Require a JSON delimiter after the number, or refuse it.
        ap = _whole_number(candidate, _AI_PROB_RE)
        cf = _whole_number(candidate, _CONFIDENCE_RE)
        if ap is not None or cf is not None:
            parsed = {"ai_probability": js_float(ap) if ap is not None else None,
                      "confidence": js_float(cf) if cf is not None else None,
                      "signals": [], "phrases": []}
            salvaged = True
    if not isinstance(parsed, dict):
        return {"ok": False, "error_code": "json_parse_failed",
                "error_detail": js_slice(candidate, 0, 200), "finish_reason": finish}

    ai_probability = to_unit(parsed.get("ai_probability"))
    if ai_probability is None:
        return {"ok": False, "error_code": "missing_ai_probability",
                "error_detail": js_slice(candidate, 0, 200)}
    confidence_raw = to_unit(parsed.get("confidence"))

    signals = []
    raw_signals = parsed.get("signals")
    if isinstance(raw_signals, list):
        for s in raw_signals[:5]:
            s = s if isinstance(s, dict) else {}
            st = to_unit(s.get("strength"))
            signals.append({
                "name": js_slice(_js_str(s.get("name") or "unknown"), 0, 80),
                "direction": "HUMAN" if _js_str(s.get("direction") or "").upper() == "HUMAN"
                else "AI",
                "strength": 0.5 if st is None else st,
            })

    # phrases: verbatim evidence spans for the UI's phrase highlighting. A quote that is
    # missing or not a string is dropped -- the UI can only highlight what it can match.
    # Junk direction defaults to AI, same policy as signals; strength defaults to 0.5.
    phrases = []
    raw_phrases = parsed.get("phrases")
    if isinstance(raw_phrases, list):
        for q in raw_phrases[:6]:
            q = q if isinstance(q, dict) else {}
            quote = js_trim(q["quote"]) if isinstance(q.get("quote"), str) else ""
            if not quote:
                continue
            st = to_unit(q.get("strength"))
            phrases.append({
                "quote": js_slice(quote, 0, 200),
                "direction": "HUMAN" if _js_str(q.get("direction") or "").upper() == "HUMAN" else "AI",
                "strength": 0.5 if st is None else st,
            })

    return {
        "ok": True,
        "salvaged": salvaged,
        "truncated": finish == "length",
        "result": {
            "ai_probability": ai_probability,
            "confidence": 0.5 if confidence_raw is None else confidence_raw,
            "signals": signals,
            "phrases": phrases,
        },
    }


def needs_secondary(cfg: dict, out: dict) -> bool:
    """Buy the second opinion exactly when the ensemble would use it: the same decisive test the
    tie-break applies, so a call is never bought and then ignored."""
    if not out.get("ok"):
        return cfg["secondary_enabled"] is not False
    p = out["result"]["ai_probability"]
    c = out["result"]["confidence"]
    decisive = ((p >= cfg["ai_prob_threshold"] and c >= cfg["ai_conf_threshold"])
                or (p <= cfg["human_prob_threshold"] and c >= cfg["human_conf_threshold"]))
    return cfg["secondary_enabled"] is not False and (not decisive or bool(out.get("salvaged")))


# ------------------------------------------------------ the tie-break lane's walk
#
# The primary model has been able to switch itself to the next pooled model since #135, and that
# walk lives in `/detect` because re-posting the request is how a NEW primary gets bought. A
# tie-break walk cannot be built that way: the second opinion is bought INSIDE one detection, so
# re-posting would bill the primary a second time for a leg the user already paid for. It happens
# here instead, with the primary's answer already in hand.

def secondary_variants(cfg: dict) -> list[dict]:
    """Each candidate as a lane config of its own, in the order it should be dialled.

    A candidate naming the endpoint and model the primary just answered is dropped before it is
    ever tried: a model cannot corroborate itself, and here the cost of discovering that is a full
    second call to the gateway that answered minutes ago. The head lane's collision guard in
    `/detect` applies the same rule to the model the request named.
    """
    seen = {(cfg["primary_base_url"], cfg["primary_model"])}
    out: list[dict] = []
    for c in cfg.get("secondary_candidates") or []:
        pair = (c["base_url"], c["model"])
        if pair in seen:
            continue
        seen.add(pair)
        out.append({**cfg,
                    "secondary_base_url": c["base_url"],
                    "secondary_model": c["model"],
                    # A candidate that names no credential is paid for with whatever the lane was
                    # going to be paid with -- resolved here, after the header has been read.
                    "secondary_api_key": c.get("api_key") or cfg["secondary_api_key"],
                    # Read on its own cut when its row has one: 0.7 under the previous model's 0.9
                    # ruler is HUMAN_LIKELY and under its own 0.6 ruler is AI_GENERATED, so carrying
                    # the old cut would silently re-decide every substituted leg.
                    "secondary_decision_threshold": c.get("secondary_decision_threshold",
                                                          cfg["secondary_decision_threshold"]),
                    **({"key_source_from_candidate": "request"}
                       if c.get("api_key") else {})})
    return out


def _leg(variant: dict, out: dict) -> dict:
    """One walked leg, described the way the answer describes a lane: what was dialled, whether it
    answered, and which ruler it would have been read on."""
    return {"model": variant["secondary_model"], "base_url": variant["secondary_base_url"],
            "ok": bool(out.get("ok")),
            "error_code": out.get("error_code") or None,
            "error_detail": out.get("error_detail") or None,
            "decision_threshold": round4(variant["secondary_decision_threshold"]),
            **({"key_source": variant.get("key_source_from_candidate")}
               if variant.get("key_source_from_candidate") else {})}


def run_secondary(cfg: dict, run, text: str, deadline: float | None = None):
    """The tie-break lane, walking down the pool while a leg does not answer.

    Returns `(result, attempts)`. `attempts` is None when no candidate list came with the request:
    the lane gets one call and the body is exactly the one this engine has always produced, which
    is the shape every measured band in the ranking was captured with.

    How far the walk goes is the owner's question, not ours -- a free-tier pool has no way to know
    which of its models is asleep, so every leg after the primary is offered. What limits it is the
    clock: a leg is never started that cannot finish inside the request's own deadline, because the
    alternative is trading the answer one lane already gave for a gateway timeout that carries
    nothing. Engine-level refusals are not reachable here at all: this function runs only after the
    primary has answered, so a dead engine or a rejected text never got this far.
    """
    cands = secondary_variants(cfg)
    if not cands:
        return run(cfg, "secondary", text, deadline), None

    attempts: list[dict] = []
    dialled: dict | None = None
    for variant in [cfg, *cands]:
        if dialled is not None:
            left = (deadline - time.time()) if deadline is not None else None
            if left is not None and left < SECONDARY_MIN_ATTEMPT_S:
                attempts.append({"model": variant["secondary_model"],
                                 "base_url": variant["secondary_base_url"], "ok": False,
                                 "skipped": (f"no time left in this request to finish another "
                                             f"tie-break call ({left:.0f}s < "
                                             f"{SECONDARY_MIN_ATTEMPT_S:.0f}s)")})
                break
        out = run(variant, "secondary", text, deadline)
        dialled = variant
        attempts.append(_leg(variant, out))
        if out.get("ok"):
            break

    # The lane's own config describes the leg that was actually dialled last, so `config_used`,
    # the stored History row and the ensemble's ruler all name the model that ran rather than the
    # one that was asked for and never answered.
    if dialled is not cfg:
        cfg["secondary_base_url"] = dialled["secondary_base_url"]
        cfg["secondary_model"] = dialled["secondary_model"]
        cfg["secondary_api_key"] = dialled["secondary_api_key"]
        cfg["secondary_decision_threshold"] = dialled["secondary_decision_threshold"]
    # Reported whenever a list was offered, including when the head leg answered on the first try:
    # "the pool was walked and the first one said yes" is the answer to the question the user asked.
    return out, (attempts if cands else None)


# ------------------------------------------------------------- gateway round-trip

def call_gateway(base_url: str, body: dict, key: str, timeout_s: float,
                 deadline: float | None = None) -> dict:
    """One lane's chat/completions round-trip, retried like the HTTP node.

    Returns the response body for a 2xx, the provider's own `error` object untouched for a non-2xx
    that carried one, a `_status`/`_body` wrapper for a non-2xx that did not (divergence #1), or a
    `_transport` marker once every try has failed (divergence #2).
    """
    url = base_url + "/chat/completions"
    data = json.dumps(body).encode("utf-8")
    last: dict = {"_transport": "unreachable", "_transport_detail": "not attempted"}
    for attempt in range(1, MAX_TRIES + 1):
        per_try = timeout_s
        if deadline is not None:
            left = deadline - time.time()
            if left <= 1:
                last["_transport_detail"] = (
                    f"{last['_transport_detail']}; stopped after {attempt - 1} attempt(s), the "
                    "time available to this request ran out")
                return last
            per_try = min(timeout_s, left)
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", ("Bearer " + (key or "")).strip())
        try:
            with urllib.request.urlopen(req, timeout=per_try) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            timed_out = "timed out" in _js_str(exc).lower()
            last = {"_transport": "timeout" if timed_out else "unreachable",
                    "_transport_detail": _js_str(exc)}
            if attempt < MAX_TRIES:
                if deadline is not None and deadline - time.time() <= RETRY_WAIT_S + 1:
                    last["_transport_detail"] = (
                        f"{last['_transport_detail']}; stopped after {attempt} attempt(s), no time "
                        "left for another")
                    return last
                time.sleep(RETRY_WAIT_S)
                continue
            return last

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            text = raw.decode("utf-8", "replace")
            if status >= 400:
                return {"_status": status, "_body": js_slice(text, 0, 300)}
            return text
        if status >= 400 and not (isinstance(parsed, dict) and parsed.get("error")):
            return {"_status": status, "_body": parsed}
        return parsed
    return last


def parse_lane(cfg: dict, lane: str, raw) -> dict:
    """`Parse <lane>` alone: read a gateway body the caller already has.

    Split out of `run_lane` so the parity harness can feed the exact JSON bodies the workflow's
    HTTP node was given, without either side making a network call.
    """
    out = extract(raw)
    out["lane"] = lane
    out["model"] = cfg[lane + "_model"]
    out["at"] = iso_now()
    out["need_secondary"] = needs_secondary(cfg, out)
    return out


def run_lane(cfg: dict, lane: str, text: str, deadline: float | None = None) -> dict:
    """Build, call, parse and stamp one lane -- the workflow's three nodes for that branch."""
    raw = call_gateway(cfg[lane + "_base_url"], build_chat_body(cfg, lane, text),
                       cfg[lane + "_api_key"], cfg["request_timeout_ms"] / 1000.0, deadline)
    return parse_lane(cfg, lane, raw)


# ------------------------------------------------------------------- the ensemble

def primary_verdict(cfg: dict, prob: float, conf: float) -> str | None:
    if prob >= cfg["ai_prob_threshold"] and conf >= cfg["ai_conf_threshold"]:
        return "AI_GENERATED"
    if prob <= cfg["human_prob_threshold"] and conf >= cfg["human_conf_threshold"]:
        return "HUMAN_LIKELY"
    return None


def secondary_verdict(cfg: dict, prob: float) -> str:
    return "AI_GENERATED" if prob >= cfg["secondary_decision_threshold"] else "HUMAN_LIKELY"


def ensemble(norm: dict, primary: dict, secondary: dict | None) -> dict:
    """`Ensemble & Build Result`. Each lane is read against its own scale, then the VERDICTS
    combine -- blending the raw probabilities was measured net-negative on the 48-text corpus,
    because the two lanes sit ~0.5 apart on essentially every text."""
    cfg = norm["config"]
    # The secondary lane only runs on the gray-zone branch, so it may be absent. Absence means
    # "not called", not "failed".
    if secondary is None:
        secondary = {"ok": False, "skipped": True, "error_code": "not_called",
                     "lane": "secondary", "model": cfg["secondary_model"]}

    def attempt(a: dict) -> dict:
        return {
            "lane": a.get("lane"),
            "model": a.get("model") or None,
            "ok": bool(a.get("ok")),
            "skipped": bool(a.get("skipped")),
            "error_code": a.get("error_code") or None,
            "error_detail": a.get("error_detail") or None,
            "salvaged": bool(a.get("salvaged")),
            "truncated": bool(a.get("truncated")),
            "result": a["result"] if a.get("ok") else None,
        }

    p_ok = bool(primary and primary.get("ok"))
    s_ok = bool(secondary and secondary.get("ok"))
    models = [attempt(primary)]
    if not secondary.get("skipped"):
        models.append(attempt(secondary))

    ai_probability: float | None = None
    confidence: float | None = None
    disagreement: float | None = None
    classification: str | None = None
    method: str | None = None
    deciding_lane: str | None = None
    review_required = False
    warnings: list[str] = list(norm.get("lane_warnings") or [])
    if cfg["secondary_enabled"] is False:
        warnings.append("Second-opinion lane is disabled: gray-zone texts answer as "
                        "REVIEW_REQUIRED; no tie-break call was bought.")

    if p_ok and s_ok:
        p = primary["result"]["ai_probability"]
        s = secondary["result"]["ai_probability"]
        disagreement = abs(p - s)
        pv = primary_verdict(cfg, p, primary["result"]["confidence"])
        if pv:
            ai_probability, confidence = p, primary["result"]["confidence"]
            classification = pv
            deciding_lane = "primary"
            method = "PRIMARY_DECIDED_" + ("AI" if pv == "AI_GENERATED" else "HUMAN")
            warnings.append("The primary was decisive on its own, so the tie-break lane was "
                            "ignored rather than blended in.")
        else:
            # The primary deferred (gray zone, low confidence, or salvaged JSON) -- the second lane
            # exists for exactly this case, so it answers and its own threshold decides.
            ai_probability, confidence = s, secondary["result"]["confidence"]
            classification = secondary_verdict(cfg, s)
            deciding_lane = "secondary"
            method = "TIE_BREAK_" + ("AI" if classification == "AI_GENERATED" else "HUMAN")
    elif p_ok or s_ok:
        solo = primary if p_ok else secondary
        ai_probability = solo["result"]["ai_probability"]
        confidence = solo["result"]["confidence"] * cfg["single_model_penalty"]
        method = "SINGLE_MODEL_PRIMARY" if p_ok else "SINGLE_MODEL_SECONDARY"
        deciding_lane = "primary" if p_ok else "secondary"
        if cfg["single_model_penalty"] != 1:
            warnings.append("Only one model returned a usable verdict; confidence scaled by "
                            f"{_js_number(cfg['single_model_penalty'])}.")
        else:
            warnings.append("Only one model returned a usable verdict.")

        verdict = (primary_verdict(cfg, ai_probability, confidence) if p_ok
                   else secondary_verdict(cfg, ai_probability))
        if verdict:
            classification = verdict
            method += "_AI" if verdict == "AI_GENERATED" else "_HUMAN"
        else:
            classification = "REVIEW_REQUIRED"
            method += "_UNCERTAIN"
            review_required = True
        if not p_ok:
            warnings.append("The primary model failed; this verdict comes from the fallback lane, "
                            "read against SECONDARY_DECISION_THRESHOLD.")
    else:
        classification = "DETECTION_UNAVAILABLE"
        method = "ALL_MODELS_FAILED"
        review_required = True
        warnings.append("Every configured model failed. See evidence.models for per-lane reasons.")

    lane_meta = norm.get("lane_meta") or {}

    def config_view(lane: str) -> dict:
        m = lane_meta.get(lane) or {}
        return {
            "base_url": m.get("base_url") or cfg[lane + "_base_url"],
            # `meta["model"]` is the lane's own model, kept in step with the endpoint by the
            # tie-break walk below: a verdict decided on pool/fourth must not be stored, banded and
            # health-tracked against pool/second, which never answered.
            "model": m.get("model") or cfg[lane + "_model"],
            "base_url_source": m.get("base_url_source") or "env",
            "model_source": m.get("model_source") or "env",
            # 'env', 'header' or 'none' -- the key value itself is never echoed anywhere.
            "key_source": m.get("key_source") or ("env" if cfg[lane + "_api_key"] else "none"),
            "enabled": lane != "secondary" or cfg["secondary_enabled"] is not False,
        }

    out = {
        "document_id": norm["document_id"],
        "classification": classification,
        "ai_probability": None if ai_probability is None else round4(ai_probability),
        "confidence": None if confidence is None else round4(confidence),
        "method": method,
        "review_required": review_required,
        "config_used": {
            "byok_enabled": cfg["allow_byok"],
            "primary": config_view("primary"),
            "secondary": config_view("secondary"),
            # Effective values after any per-request tuning was merged, plus exactly which keys came
            # from the request. Without this a caller cannot tell whether the verdict was produced by
            # its own stored parameters or by the baked defaults.
            "thresholds": {
                "ai_prob": round4(cfg["ai_prob_threshold"]),
                "ai_conf": round4(cfg["ai_conf_threshold"]),
                "human_prob": round4(cfg["human_prob_threshold"]),
                "human_conf": round4(cfg["human_conf_threshold"]),
                "secondary_decision": round4(cfg["secondary_decision_threshold"]),
                "single_model_penalty": round4(cfg["single_model_penalty"]),
            },
            "sampling": {
                "temperature": cfg["temperature"],
                "top_p": cfg["top_p"],
                "max_tokens": cfg["max_tokens"] if cfg["max_tokens"] > 0 else "auto",
            },
            "tuning_applied": norm.get("tuning_applied") or {},
        },
        "evidence": {
            "provenance": {"checked": False, "reason": "unavailable in ai-only mode"},
            "watermark": {"checked": False, "reason": "unavailable in ai-only mode"},
            "statistics": {"checked": False, "reason": "unavailable in ai-only mode"},
            "models": models,
        },
        "component_scores": {
            "primary_probability": round4(primary["result"]["ai_probability"]) if p_ok else None,
            "primary_confidence": round4(primary["result"]["confidence"]) if p_ok else None,
            "secondary_probability": round4(secondary["result"]["ai_probability"]) if s_ok else None,
            "secondary_confidence": round4(secondary["result"]["confidence"]) if s_ok else None,
            # Descriptive only. |p - s| between differently-scaled models carries no decision.
            "lane_gap": None if disagreement is None else round4(disagreement),
            "secondary_called": not bool(secondary.get("skipped")),
            "decision_mode": "tie_break",
            "deciding_lane": deciding_lane,
            "secondary_decision_threshold": round4(cfg["secondary_decision_threshold"]),
        },
        "warning": ("Human-likely means no strong AI-generation signal was detected; it does not "
                    "prove human authorship."),
        "versions": {
            "detector": cfg["detector_version"],
            "classifier": cfg["classifier_version"],
            "thresholds": cfg["threshold_version"],
            "mode": "ai-only",
            # Which implementation produced the verdict. A hosted deploy and a local install that
            # score the same text alike should be provably the same engine, and the stored row is
            # where anyone re-reading a verdict months later looks.
            "engine": ENGINE_NAME,
        },
        "_internal": {
            "text_fingerprint": norm["text_fingerprint"],
            "text_length": norm["text_length"],
            "received_at": norm["timestamp"],
            "completed_at": iso_now(),
        },
    }
    if warnings:
        out["warnings"] = warnings
    if classification == "DETECTION_UNAVAILABLE":
        out["error"] = "All configured models failed to return a usable verdict."
    return out


# --------------------------------------------------------------------- entry point

def _walk_summary(norm: dict, head: tuple, attempts: list[dict]) -> dict:
    """Fold a tie-break walk into the lane's provenance and describe it for the caller.

    `config_used.secondary`, the stored History row, the model's band and `model_health` are all read
    out of this answer, so they must name the model that actually replied. A walk that ended on
    `pool/fourth` while the row still said `pool/second` would credit, blame and re-band the wrong
    model -- the same unattributable-number failure this repo keeps re-measuring.

    `model_source` becomes `pool-walk` once the lane has dialled past the model the request named,
    so a substituted leg is never reported as if the deployment had chosen it.
    """
    ran = [a for a in attempts if not a.get("skipped")]
    last = ran[-1] if ran else None
    meta = norm["lane_meta"].setdefault("secondary", {})
    if last is not None:
        meta["model"] = last["model"]
        meta["base_url"] = last["base_url"]
        if last.get("key_source"):
            meta["key_source"] = last["key_source"]
    if last is not None and (last["model"], last["base_url"]) != head:
        meta["model_source"] = meta["base_url_source"] = "pool-walk"
    # `switched_to` answers a different question from `config_used`: not "which endpoint did the lane
    # end up dialling" (a dead walk dials all of them) but "whose reply decided this". A walk that
    # bought three refusals switched nothing -- there is no second opinion to have switched to.
    answered = next((a for a in ran if a.get("ok")), None)
    switched = None
    if answered is not None and (answered["model"], answered["base_url"]) != head:
        switched = answered["model"]
    return {"from": head[0], "switched_to": switched, "attempts": attempts}


def detect(body, headers: dict | None = None, timeout: float | None = None,
           env: dict | None = None,
           allowed_hosts: tuple[str, ...] | None = None,
           lane_runner=None) -> tuple[int, dict]:
    """One detection, start to finish. `body` is the JSON the caller POSTed; the answer is the
    body the workflow's Respond node sent."""
    headers = headers or {}
    # `(raw && raw.body) ? raw.body : raw` is how the shipped node unwraps the webhook envelope, so
    # an empty body falls through to the envelope object -- which has no `text` -- and is rejected
    # as a missing field rather than as a non-object.
    payload = body if js_truthy(body) else {"body": body, "headers": headers}
    try:
        norm = normalize(payload, headers, env=env, allowed_hosts=allowed_hosts)
    except Reject as reject:
        resp = {
            "classification": "DETECTION_UNAVAILABLE",
            "error": reject.message,
            "error_code": reject.code,
            "review_required": False,
            "method": reject.code,
        }
        if reject.hint:
            resp["hint"] = reject.hint
        return 400, resp

    cfg = norm["config"]
    # The backend's `timeout` is the ceiling on the whole call, so a lane that would outlast it
    # stops at the ceiling instead of leaving the caller waiting on a retry that cannot land.
    deadline = (time.time() + timeout) if timeout else None
    run = lane_runner or run_lane
    primary = run(cfg, "primary", norm["text"], deadline)
    walk = None
    secondary = None
    if primary["need_secondary"]:
        head = (cfg["secondary_model"], cfg["secondary_base_url"])
        secondary, attempts = run_secondary(cfg, run, norm["text"], deadline)
        if attempts is not None:
            walk = _walk_summary(norm, head, attempts)
    out = ensemble(norm, primary, secondary)
    if walk:
        out["secondary_walk"] = walk
        if walk["switched_to"]:
            out.setdefault("warnings", []).append(
                f"Second-opinion lane walked the pool: {walk['from']} did not answer, so the "
                f"tie-break verdict comes from {walk['switched_to']} read against its own cut.")
    return 200, out


def native_enabled() -> bool:
    """Whether this process answers /detect itself instead of handing it to n8n.

    Local mode is the packaged single-owner install, which is exactly the build that must not
    depend on a workflow server. `DETECTION_ENGINE` overrides the choice: `n8n` keeps the workflow
    path on a dev machine that already runs one, `python` forces the in-process engine in a
    container. The default is derived, so a fresh install needs no configuration to be correct.
    """
    from . import auth

    forced = (os.environ.get("DETECTION_ENGINE") or "").strip().lower()
    if forced in ("python", "native", "in-process"):
        return True
    if forced in ("n8n", "workflow"):
        return False
    return auth.local_mode()
