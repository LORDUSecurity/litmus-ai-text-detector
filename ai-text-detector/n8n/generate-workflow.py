"""Generate n8n/ai-only-workflow.json (AI-only detection pipeline)."""
import json, os

OUT = r"C:\Users\adam\Desktop\humanizer ultra\ai-text-detector\n8n\ai-only-workflow.json"

BS = "\\"  # single backslash, used to build JS escape sequences

# ---------------------------------------------------------------- Normalize
NORMALIZE_JS = r"""
const raw = $input.first().json;
const payload = (raw && raw.body) ? raw.body : raw;
const env = $env || {};

const num = (v, d) => { const n = parseFloat(v); return isFinite(n) ? n : d; };

const cfg = {
  primary_base_url:   env.PRIMARY_BASE_URL   || 'https://integrate.api.nvidia.com/v1',
  primary_api_key:    env.PRIMARY_API_KEY    || '',
  primary_model:      env.PRIMARY_MODEL      || 'z-ai/glm-5.3-flash',
  secondary_base_url: env.SECONDARY_BASE_URL || 'https://integrate.api.nvidia.com/v1',
  secondary_api_key:  env.SECONDARY_API_KEY  || '',
  secondary_model:    env.SECONDARY_MODEL    || 'google/diffusiongemma-26b-a4b-it',

  // The second-opinion lane is OPTIONAL. Disabled (env SECONDARY_ENABLED=false, or a
  // request sending lane_config.secondary.enabled=false) means a primary deferral becomes
  // REVIEW_REQUIRED instead of buying a second call. Measured reason: strong single lanes
  // (kimi-k3, glm-5.3-flash) are 0-wrong with only 2-3 deferrals of 48, so the tie-break's
  // remaining value is pool-death insurance, not accuracy.
  secondary_enabled: String(env.SECONDARY_ENABLED === undefined ? 'true' : env.SECONDARY_ENABLED).toLowerCase() !== 'false',

  min_text_length: Math.max(1, Math.floor(num(env.MIN_TEXT_LENGTH, 10))),
  max_text_length: Math.floor(num(env.MAX_TEXT_LENGTH, 50000)),

  // Defaults are the values calibrated against the HC3 benchmark; see .env for
  // the measured evidence behind each one.
  ai_prob_threshold:    num(env.AI_PROBABILITY_THRESHOLD, 0.69),
  ai_conf_threshold:    num(env.AI_CONFIDENCE_THRESHOLD, 0.60),
  human_prob_threshold: num(env.HUMAN_PROBABILITY_THRESHOLD, 0.40),
  human_conf_threshold: num(env.HUMAN_CONFIDENCE_THRESHOLD, 0.60),

  // The secondary lane is a tie-breaker, not a weighted input: it only answers when
  // the primary defers, and its probability is read against THIS number, never against
  // the primary thresholds. 0.90 is the value the 48-text holdout picked for
  // google/diffusiongemma-26b-a4b-it — that model sits at median 0.75 on true-human
  // text and 0.98 on AI text, so its usable AI signal starts where its scale tops out.
  secondary_decision_threshold: num(env.SECONDARY_DECISION_THRESHOLD, 0.90),

  // Applied to confidence when only one lane answered. 1.0 disables it. The old
  // hardcoded 0.85 turned a typical 0.82 into 0.697, just under the old 0.70
  // gate, which is why clear-cut AI text kept landing in REVIEW_REQUIRED.
  single_model_penalty: num(env.SINGLE_MODEL_CONFIDENCE_PENALTY, 1.0),

  request_timeout_ms: Math.floor(num(env.LLM_TIMEOUT_MS, 90000)),

  // Sampling shape of the classifier call. Low temperature is deliberate: this is
  // a measurement, not a generation, so run-to-run jitter has to be minimised.
  temperature: num(env.LLM_TEMPERATURE, 0.1),
  top_p: num(env.LLM_TOP_P, 0.9),
  // 0 (the default) means DO NOT SEND a cap at all: the answer is allowed to be as long as
  // the model wants and the provider's own ceiling is the only limit. A positive number still
  // caps. This exists because a 700-token cap cut nemotron's answer off before its JSON closed,
  // so the lane that decides the verdict returned no evidence while the free tier charges
  // nothing per token -- the cap was buying nothing.
  max_tokens: Math.floor(num(env.LLM_MAX_TOKENS, 0)),

  detector_version:   env.DETECTOR_VERSION   || '2.0.0',
  classifier_version: env.CLASSIFIER_VERSION || '2.0.0',
  threshold_version:  env.THRESHOLD_VERSION  || '2.0.0',

  // Per-request overrides (lane_config + BYOK headers) are the contract the UI
  // talks to, so their guards live here rather than in the client. Exact-host
  // allowlist, no suffix matching: 'openrouter.ai.evil.com' must not pass.
  allowed_hosts: String(env.ALLOWED_LLM_HOSTS ||
      'localhost,127.0.0.1,::1,integrate.api.nvidia.com,openrouter.ai')
    .split(',')
    .map(function (s) { return s.trim().toLowerCase(); })
    .filter(Boolean),
  allow_byok: String(env.ALLOW_BYOK || 'false').toLowerCase() === 'true'
};

const fail = (message, hint, code) => [{ json: {
  valid: false,
  error_message: message,
  error_hint: hint || null,
  error_code: code || 'INPUT_REJECTED'
} }];

if (!payload || typeof payload !== 'object') {
  return fail('Request body must be a JSON object.', 'POST {"text": "..."} with Content-Type: application/json');
}
if (typeof payload.text !== 'string' || payload.text.trim() === '') {
  return fail("Field 'text' is required and must be a non-empty string.");
}

const text = payload.text.trim();
if (text.length < cfg.min_text_length) {
  return fail(`Text too short: ${text.length} characters (minimum ${cfg.min_text_length}).`);
}
if (text.length > cfg.max_text_length) {
  return fail(`Text too long: ${text.length} characters (maximum ${cfg.max_text_length}).`);
}

// --- per-request lane overrides -----------------------------------------
// The HTTP nodes read cfg.<lane>_base_url / _model / _api_key, so overriding a
// lane means resolving the final values here and only here. Anything rejected
// never reaches an outbound request.
const LOOPBACK = ['localhost', '127.0.0.1', '::1'];

function checkBaseUrl(raw) {
  const value = String(raw || '').trim();
  if (!value) return { ok: false, error: 'base_url must be a non-empty string.' };
  if (value.length > 500) return { ok: false, error: 'base_url is too long (max 500 characters).' };

  // Parsed by hand, not with new URL(): the n8n Code node sandbox has no URL
  // global, and a ReferenceError caught as "invalid URL" made every correct
  // base_url look broken. Keep this dependency-free so the unit harness and the
  // sandbox execute literally the same logic.
  const m = value.match(/^(https?):\/\/([^/?#]*)([\s\S]*)$/i);
  if (!m) return { ok: false, error: 'base_url must be an absolute http(s) URL, for example http://localhost:20128/v1' };
  const scheme = m[1].toLowerCase();
  const authority = m[2];
  const rest = m[3] || '';

  if (authority === '') return { ok: false, error: 'base_url has no hostname.' };
  if (authority.indexOf('@') !== -1) return { ok: false, error: 'base_url must not embed credentials.' };

  let host = '';
  let portStr = '';
  let bracketed = false;
  if (authority.charAt(0) === '[') {
    const close = authority.indexOf(']');
    if (close === -1) return { ok: false, error: 'base_url has an unterminated IPv6 hostname.' };
    host = authority.slice(1, close);
    bracketed = true;
    if (!/^[0-9A-Fa-f:.]+$/.test(host)) return { ok: false, error: 'base_url has an invalid IPv6 hostname.' };
    const after = authority.slice(close + 1);
    if (after !== '' && after.charAt(0) !== ':') return { ok: false, error: 'base_url has unexpected characters after the hostname.' };
    portStr = after.slice(1);
  } else {
    const idx = authority.lastIndexOf(':');
    if (idx !== -1 && /^\d+$/.test(authority.slice(idx + 1))) {
      portStr = authority.slice(idx + 1);
      host = authority.slice(0, idx);
    } else {
      host = authority;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(host)) return { ok: false, error: 'base_url has an invalid hostname.' };
  }
  host = host.toLowerCase();

  if (portStr !== '') {
    if (!/^\d{1,5}$/.test(portStr)) return { ok: false, error: 'base_url port is not a number.' };
    const p = parseInt(portStr, 10);
    if (p < 1 || p > 65535) return { ok: false, error: 'base_url port is out of range (1-65535).' };
  }

  // Exact match, never a suffix: 'openrouter.ai.evil.com' must not pass because
  // 'openrouter.ai' is listed.
  if (cfg.allowed_hosts.indexOf(host) === -1) {
    return { ok: false, error: 'base_url host "' + host + '" is not in ALLOWED_LLM_HOSTS (' + cfg.allowed_hosts.join(', ') + ').' };
  }

  if (rest.indexOf('?') !== -1) return { ok: false, error: 'base_url must not contain a query string.' };
  if (rest.indexOf('#') !== -1) return { ok: false, error: 'base_url must not contain a fragment.' };
  if (rest !== '' && rest.charAt(0) !== '/') return { ok: false, error: 'base_url has a malformed path.' };
  if (!/^[\/A-Za-z0-9._~%-]*$/.test(rest)) return { ok: false, error: 'base_url path contains unsupported characters.' };
  if (/\/chat\/completions\/?$/i.test(rest)) {
    return { ok: false, error: 'base_url must be the API root (for example http://localhost:20128/v1), not the /chat/completions endpoint.' };
  }

  // The HTTP nodes append /chat/completions, so trailing slashes are stripped
  // here rather than rejected — a trailing slash is a typo, not an attack.
  const origin = scheme + '://' + (bracketed ? '[' + host + ']' : host) + (portStr === '' ? '' : ':' + portStr);
  return { ok: true, value: origin + rest.replace(/\/+$/g, ''), host: host };
}

function checkModel(raw) {
  const value = String(raw || '').trim();
  if (!value) return { ok: false, error: 'model must be a non-empty string.' };
  if (value.length > 200) return { ok: false, error: 'model is too long (max 200 characters).' };
  if (!/^[A-Za-z0-9._:/@+-]+$/.test(value)) {
    return { ok: false, error: 'model contains unsupported characters (allowed: letters, digits and . _ : / @ + -).' };
  }
  return { ok: true, value: value };
}

function checkKey(raw) {
  const value = String(raw || '').trim();
  if (value.length > 512) return { ok: false, error: 'API key is too long (max 512 characters).' };
  // Covers nvapi-, sk-or-v1-, bearer JWTs and base64 blobs, and excludes
  // whitespace, quotes and CR/LF that could break out of the header.
  if (!/^[A-Za-z0-9._:+/-]+$/.test(value)) {
    return { ok: false, error: 'API key contains unsupported characters (allowed: letters, digits and . _ : + / -).' };
  }
  return { ok: true, value: value };
}

const hdrs = (raw && raw.headers) || {};
const hdr = (name) => {
  const v = hdrs[name];
  return typeof v === 'string' ? v.trim() : '';
};

const errors = [];
const laneWarnings = [];
const laneMeta = {};
let byokRejected = false;

const lcIsBad = payload.lane_config !== undefined && payload.lane_config !== null &&
  (typeof payload.lane_config !== 'object' || Array.isArray(payload.lane_config));
if (lcIsBad) {
  errors.push('lane_config must be an object with optional primary and secondary entries.');
}
const lc = (!lcIsBad && payload.lane_config) ? payload.lane_config : {};

for (const lane of ['primary', 'secondary']) {
  const rawLane = lc[lane];
  const laneBad = rawLane !== undefined && rawLane !== null &&
    (typeof rawLane !== 'object' || Array.isArray(rawLane));
  if (laneBad) errors.push('lane_config.' + lane + ' must be an object.');
  const o = (!laneBad && rawLane) ? rawLane : {};
  const meta = { base_url_source: 'env', model_source: 'env', key_source: 'env', base_url: cfg[lane + '_base_url'], host: null };

  const allowed = lane === 'secondary' ? ['base_url', 'model', 'enabled'] : ['base_url', 'model'];
  Object.keys(o).forEach(function (k) {
    if (allowed.indexOf(k) === -1) {
      errors.push('lane_config.' + lane + ' has unsupported field "' + k + '" (accepted: ' + allowed.join(', ') + ').');
    }
  });

  // Only the secondary lane can be switched off per request: the primary IS the detector.
  if (lane === 'secondary' && o.enabled !== undefined && o.enabled !== null && String(o.enabled).trim() !== '') {
    const e = typeof o.enabled === 'boolean' ? String(o.enabled) : String(o.enabled).trim().toLowerCase();
    if (e === 'true' || e === 'false') cfg.secondary_enabled = e === 'true';
    else errors.push('lane_config.secondary.enabled must be true or false (got ' + String(o.enabled) + ').');
  }

  // A field the UI left untouched arrives as '' or '   '. That means "not
  // specified, use the environment", not "invalid" — rejecting it would make
  // every form submit fail on the lanes the user did not touch.
  const given = (v) => v !== undefined && v !== null && String(v).trim() !== '';

  if (given(o.base_url)) {
    const r = checkBaseUrl(o.base_url);
    if (!r.ok) errors.push('lane_config.' + lane + '.base_url: ' + r.error);
    else { cfg[lane + '_base_url'] = r.value; meta.base_url = r.value; meta.host = r.host; meta.base_url_source = 'request'; }
  }
  if (given(o.model)) {
    const r = checkModel(o.model);
    if (!r.ok) errors.push('lane_config.' + lane + '.model: ' + r.error);
    else { cfg[lane + '_model'] = r.value; meta.model_source = 'request'; }
  }

  const supplied = hdr('x-' + lane + '-api-key');
  if (supplied !== '') {
    if (!cfg.allow_byok) {
      byokRejected = true;
      errors.push('An ' + lane + ' API key was sent in a header but bring-your-own-key is disabled. Set ALLOW_BYOK=true to enable it.');
      laneMeta[lane] = meta;
      continue;
    }
    const r = checkKey(supplied);
    if (!r.ok) errors.push('x-' + lane + '-api-key: ' + r.error);
    else { cfg[lane + '_api_key'] = r.value; meta.key_source = 'header'; }
  } else if (cfg[lane + '_api_key']) {
    meta.key_source = 'env';
  } else {
    meta.key_source = 'none';
  }

  // Re-parse the (possibly env-only) URL so the loopback rule applies to both.
  const resolved = checkBaseUrl(cfg[lane + '_base_url']);
  if (!resolved.ok) {
    errors.push(lane + ' base_url from configuration is invalid: ' + resolved.error);
  } else {
    meta.host = resolved.host;
    meta.base_url = resolved.value;
    cfg[lane + '_base_url'] = resolved.value;
  }
  laneMeta[lane] = meta;
}

// Per-request tuning overrides. The Flask backend stores these per model row in
// SQLite and sends them here, which is what makes a parameter edited in the UI
// actually change the call instead of being decorative. Out-of-range numbers are
// errors rather than silent clamps: a clamp would hide a corrupt DB row and the
// caller would see a verdict produced by thresholds it never asked for.
const TUNING_FIELDS = {
  temperature:                  { min: 0,    max: 2,      integer: false },
  top_p:                        { min: 0,    max: 1,      integer: false },
  max_tokens:                   { min: 0,    max: 4096,   integer: true  },
  request_timeout_ms:           { min: 1000, max: 300000, integer: true  },
  ai_prob_threshold:            { min: 0,    max: 1,      integer: false },
  ai_conf_threshold:            { min: 0,    max: 1,      integer: false },
  human_prob_threshold:         { min: 0,    max: 1,      integer: false },
  human_conf_threshold:         { min: 0,    max: 1,      integer: false },
  secondary_decision_threshold: { min: 0,    max: 1,      integer: false },
  single_model_penalty:         { min: 0,    max: 1,      integer: false }
};

const tnIsBad = payload.tuning !== undefined && payload.tuning !== null &&
  (typeof payload.tuning !== 'object' || Array.isArray(payload.tuning));
if (tnIsBad) {
  errors.push('tuning must be an object of numeric overrides.');
}
const tn = (!tnIsBad && payload.tuning) ? payload.tuning : {};
const tuningApplied = {};
Object.keys(tn).forEach(function (k) {
  if (!Object.prototype.hasOwnProperty.call(TUNING_FIELDS, k)) {
    errors.push('tuning has unsupported field "' + k + '" (accepted: ' +
      Object.keys(TUNING_FIELDS).join(', ') + ').');
    return;
  }
  const v = tn[k];
  // A column the DB left NULL arrives as null: "not tuned, keep the default".
  if (v === null || v === undefined || v === '') return;
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!isFinite(n)) { errors.push('tuning.' + k + ' must be a number.'); return; }
  const spec = TUNING_FIELDS[k];
  if (n < spec.min || n > spec.max) {
    errors.push('tuning.' + k + ' must be between ' + spec.min + ' and ' + spec.max + ' (got ' + n + ').');
    return;
  }
  cfg[k] = spec.integer ? Math.floor(n) : n;
  tuningApplied[k] = cfg[k];
});

// The two cuts must bracket a real gray zone. An inverted pair classifies every
// text as both AI and HUMAN at once and the primary branch wins by code order,
// which reads as a calibrated verdict while being pure accident.
if (cfg.human_prob_threshold >= cfg.ai_prob_threshold) {
  errors.push('human_prob_threshold (' + cfg.human_prob_threshold +
    ') must be below ai_prob_threshold (' + cfg.ai_prob_threshold + ').');
}

// A keyless lane is only acceptable on a loopback gateway (self-hosted OmniRoute
// with REQUIRE_API_KEY=false). Anything off-box without a key is a hard reject —
// silently firing an unauthenticated request at a remote provider is not a
// fallback, it is a bug the caller would have to discover from a 401.
const primaryHost = (laneMeta.primary && laneMeta.primary.host) || '';
if (!cfg.primary_api_key && LOOPBACK.indexOf(primaryHost) === -1) {
  return fail(
    'No primary API key available: PRIMARY_API_KEY is unset and no x-primary-api-key header was supplied.',
    'Export PRIMARY_API_KEY, send the key in the x-primary-api-key header with ALLOW_BYOK=true, or point PRIMARY_BASE_URL at a loopback gateway.',
    'PROVIDER_KEY_MISSING');
}
if (!cfg.primary_api_key) laneWarnings.push('Primary lane is running without an API key because the host is loopback.');
if (cfg.secondary_enabled && !cfg.secondary_api_key) laneWarnings.push('Secondary lane has no API key; the gray-zone second opinion will fail if it is called.');

if (errors.length) {
  return fail(errors.join(' '),
    'lane_config accepts base_url and model (plus enabled for the secondary lane); keys go in x-primary-api-key / x-secondary-api-key headers; tuning accepts numbers only.',
    byokRejected ? 'BYOK_DISABLED' : 'LANE_CONFIG_INVALID');
}

// FNV-1a derived fingerprint. Non-cryptographic: used only to correlate logs,
// never as a security boundary or a cache key against adversarial input.
const normalized = text.replace(/\s+/g, ' ').trim();
let h1 = 0x811c9dc5 >>> 0;
let h2 = 0x9e3779b9 >>> 0;
for (let i = 0; i < normalized.length; i++) {
  const c = normalized.charCodeAt(i);
  h1 = ((h1 ^ c) * 0x01000193) >>> 0;
  h2 = ((h2 + c + i) * 0x85ebca6b) >>> 0;
}
const text_fingerprint = h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0');

const document_id = (typeof payload.document_id === 'string' && payload.document_id.trim())
  ? payload.document_id.trim().slice(0, 200)
  : `doc_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;

return [{ json: {
  valid: true,
  document_id,
  text,
  text_length: text.length,
  text_fingerprint,
  source_url: typeof payload.source_url === 'string' ? payload.source_url.slice(0, 2000) : null,
  content_type: typeof payload.content_type === 'string' ? payload.content_type.slice(0, 200) : null,
  metadata: (payload.metadata && typeof payload.metadata === 'object') ? payload.metadata : {},
  requested_by: typeof payload.requested_by === 'string' ? payload.requested_by.slice(0, 200) : null,
  timestamp: new Date().toISOString(),
  lane_meta: laneMeta,
  lane_warnings: laneWarnings,
  tuning_applied: tuningApplied,
  config: cfg
} }];
""".strip()

# ------------------------------------------------------------ Build request
def build_request_js(lane):
    return r"""
const input = $('Normalize Input').first().json;
const cfg = input.config;

// response_format / json_object is deliberately NOT sent: on free-tier upstreams it
// provably triggered 400s, reasoning preambles and duplicated-key garbage. A strict
// prompt plus tolerant parsing on the far side is more reliable.
const SYSTEM_PROMPT = 'You are an AI text detection classifier. Return ONLY a compact JSON object. No markdown, no code fences, no commentary, no reasoning text. Schema: {"ai_probability": number between 0 and 1, "confidence": number between 0 and 1, "signals": up to 3 objects of {"name": string, "direction": "AI" or "HUMAN", "strength": number between 0 and 1}, "phrases": up to 4 objects of {"quote": string copied VERBATIM from the text, at most 120 characters, exact substring with no ellipsis and no edits, "direction": "AI" or "HUMAN", "strength": number between 0 and 1}}. ai_probability means the probability the text was generated by an AI rather than written by a human. For each phrase quote the passage exactly as written, then judge that passage; pick the strongest evidence in each direction.';

const body = {
  model: cfg.__LANE___model,
  messages: [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: 'Was the following text likely written by an AI? Judge the text itself.\n\n\"\"\"\n' + input.text + '\n\"\"\"\n\nReturn ONLY the JSON object.' }
  ],
  temperature: cfg.temperature,
  top_p: cfg.top_p,
  // No cap unless a positive one is configured: 0 sends no max_tokens field at all.
  ...(cfg.max_tokens > 0 ? { max_tokens: cfg.max_tokens } : {}),
  stream: false
};

return [{ json: {
  lane: '__LANE__',
  model: cfg.__LANE___model,
  base_url: cfg.__LANE___base_url,
  api_key_present: !!cfg.__LANE___api_key,
  body_json: JSON.stringify(body)
} }];
""".replace('__LANE__', lane).strip()

# ------------------------------------------------------------------- Parser
def parse_js(lane):
    return r"""
const raw = $input.first().json;
const cfg = $('Normalize Input').first().json.config;

function toUnit(v) {
  const n = typeof v === 'string' ? parseFloat(v) : v;
  if (typeof n !== 'number' || !isFinite(n)) return null;
  return Math.min(1, Math.max(0, n));
}

// The digits a truncation left behind, but only if the truncation left ALL of them: the match must
// be followed by a JSON delimiter, otherwise the number may have been cut mid-digit.
function wholeNumber(text, re) {
  const m = text.match(re);
  if (!m) return null;
  return /^\s*[,}\]]/.test(text.slice(m.index + m[0].length)) ? m[1] : null;
}

function extract(payload) {
  if (!payload || typeof payload !== 'object') {
    return { ok: false, error_code: 'empty_response', error_detail: 'HTTP node produced no object' };
  }
  if (payload.error) {
    const e = payload.error;
    return {
      ok: false,
      error_code: String(e.code || e.status || 'provider_error'),
      error_detail: String(e.message || JSON.stringify(e)).slice(0, 300)
    };
  }

  const choice = (Array.isArray(payload.choices) && payload.choices[0]) || null;
  if (!choice) {
    return { ok: false, error_code: 'no_choices', error_detail: JSON.stringify(payload).slice(0, 300) };
  }

  const msg = choice.message || {};
  let content = msg.content;
  if (content === null || content === undefined || content === '') content = msg.reasoning || '';
  if (typeof content !== 'string') {
    try { content = JSON.stringify(content); } catch (e) { content = ''; }
  }
  if (!content) {
    return { ok: false, error_code: 'null_content', error_detail: 'message keys: ' + Object.keys(msg).join(',') };
  }

  const finish = choice.finish_reason || null;
  const stripped = content.replace(/```json/gi, '```').replace(/```/g, '').trim();
  const start = stripped.indexOf('{');
  if (start === -1) {
    return { ok: false, error_code: 'no_json_object', error_detail: stripped.slice(0, 200), finish_reason: finish };
  }

  // Scan for the first balanced object, honouring string escapes.
  let depth = 0, end = -1, inStr = false, esc = false;
  for (let i = start; i < stripped.length; i++) {
    const ch = stripped[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '__BSLASH__') esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') { inStr = true; continue; }
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { end = i; break; } }
  }
  const candidate = end > start ? stripped.slice(start, end + 1) : stripped.slice(start);

  let parsed = null;
  let salvaged = false;
  try {
    parsed = JSON.parse(candidate);
  } catch (e) {
    // Truncated output (finish_reason=length) still usually carries the two numbers -- but only
    // those the cut left COMPLETE. A number that runs to the end of the string may have lost
    // digits, and the regex cannot tell: `0.78` cut at `0.` matches as 0, which is not a missing
    // answer but an invented one (confidently HUMAN, and ok:true, so it would decide alone when
    // the other lane failed). Require a JSON delimiter after the number, or refuse it.
    const ap = wholeNumber(candidate, /"ai_probability"\s*:\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)/);
    const cf = wholeNumber(candidate, /"confidence"\s*:\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)/);
    if (ap !== null || cf !== null) {
      parsed = {
        ai_probability: ap === null ? null : parseFloat(ap),
        confidence: cf === null ? null : parseFloat(cf),
        signals: [],
        phrases: []
      };
      salvaged = true;
    }
  }
  if (!parsed || typeof parsed !== 'object') {
    return { ok: false, error_code: 'json_parse_failed', error_detail: candidate.slice(0, 200), finish_reason: finish };
  }

  const ai_probability = toUnit(parsed.ai_probability);
  if (ai_probability === null) {
    return { ok: false, error_code: 'missing_ai_probability', error_detail: candidate.slice(0, 200) };
  }
  const confidenceRaw = toUnit(parsed.confidence);

  const signals = Array.isArray(parsed.signals)
    ? parsed.signals.slice(0, 5).map(function (s) {
        s = s || {};
        const st = toUnit(s.strength);
        return {
          name: String(s.name || 'unknown').slice(0, 80),
          direction: String(s.direction || '').toUpperCase() === 'HUMAN' ? 'HUMAN' : 'AI',
          strength: st === null ? 0.5 : st
        };
      })
    : [];

  // phrases: verbatim evidence spans for the UI's phrase highlighting. A quote that is
  // missing or not a string is dropped -- the UI can only highlight what it can match.
  // Junk direction defaults to AI, same policy as signals; strength defaults to 0.5.
  const phrases = Array.isArray(parsed.phrases)
    ? parsed.phrases.slice(0, 6).map(function (q) {
        q = q || {};
        const quote = typeof q.quote === 'string' ? q.quote.trim() : '';
        if (!quote) return null;
        const st = toUnit(q.strength);
        return {
          quote: quote.slice(0, 200),
          direction: String(q.direction || '').toUpperCase() === 'HUMAN' ? 'HUMAN' : 'AI',
          strength: st === null ? 0.5 : st
        };
      }).filter(function (q) { return q !== null; })
    : [];

  return {
    ok: true,
    salvaged: salvaged,
    truncated: finish === 'length',
    result: {
      ai_probability: ai_probability,
      confidence: confidenceRaw === null ? 0.5 : confidenceRaw,
      signals: signals,
      phrases: phrases
    }
  };
}

const out = extract(raw);
out.lane = '__LANE__';
out.model = cfg.__LANE___model;
out.at = new Date().toISOString();

// A verdict of exactly 0 must NOT be treated as a failure; only ok=false is.
if (out.ok) {
  const p = out.result.ai_probability;
  const c = out.result.confidence;
  // Buy the second opinion exactly when the ensemble would use it: this is the same
  // decisive test the tie-break applies, so a call is never bought and then ignored
  // (the old gray-zone band overlapped the decision band at p = 0.45 and p = 0.69).
  const decisive = (p >= cfg.ai_prob_threshold && c >= cfg.ai_conf_threshold)
                || (p <= cfg.human_prob_threshold && c >= cfg.human_conf_threshold);
  out.need_secondary = cfg.secondary_enabled !== false && (!decisive || out.salvaged);
} else {
  out.need_secondary = cfg.secondary_enabled !== false;
}

return [{ json: out }];
""".replace('__LANE__', lane).replace('__BSLASH__', BS + BS).strip()

# ----------------------------------------------------------------- Ensemble
ENSEMBLE_JS = r"""
const input = $('Normalize Input').first().json;
const cfg = input.config;
const primary = $('Parse Primary').first().json;

// The secondary lane only runs on the gray-zone branch, so its node may not exist
// in this execution. Absence means "not called", not "failed".
let secondary = { ok: false, skipped: true, error_code: 'not_called', lane: 'secondary', model: cfg.secondary_model };
try {
  const s = $('Parse Secondary').first();
  if (s && s.json) secondary = s.json;
} catch (e) { /* secondary branch was not taken */ }

const round = (n) => Math.round(n * 10000) / 10000;
const pOK = !!(primary && primary.ok);
const sOK = !!(secondary && secondary.ok);

const attempt = (a) => ({
  lane: a.lane,
  model: a.model || null,
  ok: !!a.ok,
  skipped: !!a.skipped,
  error_code: a.error_code || null,
  error_detail: a.error_detail || null,
  salvaged: !!a.salvaged,
  truncated: !!a.truncated,
  result: a.ok ? a.result : null
});

const models = [attempt(primary)];
if (secondary && !secondary.skipped) models.push(attempt(secondary));

let ai_probability = null;
let confidence = null;
let disagreement = null;
let classification, method;
let deciding_lane = null;
let review_required = false;
const warnings = [];

(input.lane_warnings || []).forEach(function (w) { warnings.push(w); });
if (cfg.secondary_enabled === false) {
  warnings.push('Second-opinion lane is disabled: gray-zone texts answer as REVIEW_REQUIRED; no tie-break call was bought.');
}

// Each lane is read against its own scale, then the VERDICTS combine. Blending the raw
// probabilities was measured net-negative on the 48-text corpus: the 550B puts true-human
// text at median 0.25 and diffusiongemma-26b at 0.75, so 0.9*0.25 + 0.1*0.75 is not a
// statistic anyone calibrated. For the same reason |p - s| is reported as lane_gap but
// never used as a gate — those two models differ by ~0.5 on essentially every text.
const primaryVerdict = (prob, conf) => {
  if (prob >= cfg.ai_prob_threshold && conf >= cfg.ai_conf_threshold) return 'AI_GENERATED';
  if (prob <= cfg.human_prob_threshold && conf >= cfg.human_conf_threshold) return 'HUMAN_LIKELY';
  return null;
};
const secondaryVerdict = (prob) => (prob >= cfg.secondary_decision_threshold ? 'AI_GENERATED' : 'HUMAN_LIKELY');

if (pOK && sOK) {
  const p = primary.result.ai_probability;
  const s = secondary.result.ai_probability;
  disagreement = Math.abs(p - s);
  const pv = primaryVerdict(p, primary.result.confidence);

  if (pv) {
    ai_probability = p;
    confidence = primary.result.confidence;
    classification = pv;
    deciding_lane = 'primary';
    method = 'PRIMARY_DECIDED_' + (pv === 'AI_GENERATED' ? 'AI' : 'HUMAN');
    warnings.push('The primary was decisive on its own, so the tie-break lane was ignored rather than blended in.');
  } else {
    // The primary deferred (gray zone, low confidence, or salvaged JSON) — the second
    // lane exists for exactly this case, so it answers and its own threshold decides.
    ai_probability = s;
    confidence = secondary.result.confidence;
    classification = secondaryVerdict(s);
    deciding_lane = 'secondary';
    method = 'TIE_BREAK_' + (classification === 'AI_GENERATED' ? 'AI' : 'HUMAN');
  }
} else if (pOK || sOK) {
  const solo = pOK ? primary : secondary;
  ai_probability = solo.result.ai_probability;
  confidence = solo.result.confidence * cfg.single_model_penalty;
  method = pOK ? 'SINGLE_MODEL_PRIMARY' : 'SINGLE_MODEL_SECONDARY';
  deciding_lane = pOK ? 'primary' : 'secondary';
  if (cfg.single_model_penalty !== 1) {
    warnings.push('Only one model returned a usable verdict; confidence scaled by ' + cfg.single_model_penalty + '.');
  } else {
    warnings.push('Only one model returned a usable verdict.');
  }

  const v = pOK ? primaryVerdict(ai_probability, confidence) : secondaryVerdict(ai_probability);
  if (v) {
    classification = v;
    method += v === 'AI_GENERATED' ? '_AI' : '_HUMAN';
  } else {
    classification = 'REVIEW_REQUIRED';
    method += '_UNCERTAIN';
    review_required = true;
  }
  if (!pOK) {
    warnings.push('The primary model failed; this verdict comes from the fallback lane, read against SECONDARY_DECISION_THRESHOLD.');
  }
} else {
  classification = 'DETECTION_UNAVAILABLE';
  method = 'ALL_MODELS_FAILED';
  review_required = true;
  warnings.push('Every configured model failed. See evidence.models for per-lane reasons.');
}

const laneMeta = input.lane_meta || {};
const configView = (lane) => {
  const m = laneMeta[lane] || {};
  return {
    base_url: m.base_url || cfg[lane + '_base_url'],
    model: cfg[lane + '_model'],
    base_url_source: m.base_url_source || 'env',
    model_source: m.model_source || 'env',
    // 'env', 'header' or 'none' — the key value itself is never echoed anywhere.
    key_source: m.key_source || (cfg[lane + '_api_key'] ? 'env' : 'none'),
    enabled: lane !== 'secondary' || cfg.secondary_enabled !== false
  };
};

const out = {
  document_id: input.document_id,
  classification: classification,
  ai_probability: ai_probability === null ? null : round(ai_probability),
  confidence: confidence === null ? null : round(confidence),
  method: method,
  review_required: review_required,
  config_used: {
    byok_enabled: cfg.allow_byok,
    primary: configView('primary'),
    secondary: configView('secondary'),
    // Effective values after any per-request tuning was merged, plus exactly which
    // keys came from the request. Without this a caller cannot tell whether the
    // verdict was produced by its own stored parameters or by the baked defaults.
    thresholds: {
      ai_prob: round(cfg.ai_prob_threshold),
      ai_conf: round(cfg.ai_conf_threshold),
      human_prob: round(cfg.human_prob_threshold),
      human_conf: round(cfg.human_conf_threshold),
      secondary_decision: round(cfg.secondary_decision_threshold),
      single_model_penalty: round(cfg.single_model_penalty)
    },
    sampling: {
      temperature: cfg.temperature,
      top_p: cfg.top_p,
      max_tokens: cfg.max_tokens > 0 ? cfg.max_tokens : 'auto'
    },
    tuning_applied: input.tuning_applied || {}
  },
  evidence: {
    provenance: { checked: false, reason: 'unavailable in ai-only mode' },
    watermark: { checked: false, reason: 'unavailable in ai-only mode' },
    statistics: { checked: false, reason: 'unavailable in ai-only mode' },
    models: models
  },
  component_scores: {
    primary_probability: pOK ? round(primary.result.ai_probability) : null,
    primary_confidence: pOK ? round(primary.result.confidence) : null,
    secondary_probability: sOK ? round(secondary.result.ai_probability) : null,
    secondary_confidence: sOK ? round(secondary.result.confidence) : null,
    // Descriptive only. |p - s| between differently-scaled models carries no decision.
    lane_gap: disagreement === null ? null : round(disagreement),
    secondary_called: !!(secondary && !secondary.skipped),
    decision_mode: 'tie_break',
    deciding_lane: deciding_lane,
    secondary_decision_threshold: round(cfg.secondary_decision_threshold)
  },
  warning: 'Human-likely means no strong AI-generation signal was detected; it does not prove human authorship.',
  versions: {
    detector: cfg.detector_version,
    classifier: cfg.classifier_version,
    thresholds: cfg.threshold_version,
    mode: 'ai-only'
  },
  _internal: {
    text_fingerprint: input.text_fingerprint,
    text_length: input.text_length,
    received_at: input.timestamp,
    completed_at: new Date().toISOString()
  }
};

if (warnings.length) out.warnings = warnings;
if (classification === 'DETECTION_UNAVAILABLE') {
  out.error = 'All configured models failed to return a usable verdict.';
}

return [{ json: out }];
""".strip()

RESPOND_INVALID_JS = r"""
const rejected = $input.first().json;
const body = {
  classification: 'DETECTION_UNAVAILABLE',
  error: rejected.error_message,
  error_code: rejected.error_code || 'INPUT_REJECTED',
  review_required: false,
  method: rejected.error_code || 'INPUT_REJECTED'
};
if (rejected.error_hint) body.hint = rejected.error_hint;
return [{ json: body }];
""".strip()


def http_node(node_id, name, lane, x, y):
    # Built by concatenation, NOT f-string: n8n expressions need literal double
    # braces and an f-string would silently collapse "{{" to "{".
    expr = lambda e: '={{ ' + e + ' }}'
    cfgref = "$('Normalize Input').first().json.config." + lane
    buildref = "$('Build " + lane.title() + " Request').first().json.body_json"
    keyref = cfgref + "_api_key"
    return {
        "parameters": {
            "method": "POST",
            "url": expr(cfgref + "_base_url") + "/chat/completions",
            "authentication": "none",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    # Empty only for a loopback gateway running REQUIRE_API_KEY=false;
                    # every remote host is rejected upstream in Normalize Input.
                    {"name": "Authorization", "value": expr("('Bearer ' + " + keyref + ").trim()")},
                    {"name": "Content-Type", "value": "application/json"},
                    {"name": "Accept", "value": "application/json"},
                ]
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": expr(buildref),
            "options": {
                "timeout": expr("$('Normalize Input').first().json.config.request_timeout_ms"),
                "response": {"response": {"neverError": True}},
            },
        },
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [x, y],
        "retryOnFail": True,
        "maxTries": 4,
        "waitBetweenTries": 3000,
        "onError": "continueRegularOutput",
        "alwaysOutputData": True,
        "notesInFlow": False,
    }


def code_node(node_id, name, js, x, y):
    return {
        "parameters": {"jsCode": js, "mode": "runOnceForAllItems"},
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [x, y],
    }


def if_node(node_id, name, expr, x, y):
    return {
        "parameters": {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "loose",
                    "version": 2,
                },
                "conditions": [
                    {
                        "id": node_id + "-cond",
                        "leftValue": expr,
                        "rightValue": True,
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": [x, y],
    }


nodes = [
    {
        "parameters": {
            "httpMethod": "POST",
            "path": "ai-detect-ai",
            "responseMode": "responseNode",
            "options": {},
        },
        "id": "webhook-ai-input",
        "name": "Webhook Input",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "webhookId": "7f3c1a20-6b41-4d5e-9a77-2c1e5d0b8a01",
        "position": [-400, 0],
    },
    code_node("normalize-ai-input", "Normalize Input", NORMALIZE_JS, -180, 0),
    if_node("if-valid-input", "Valid Input?", "={{ $json.valid }}", 40, 0),
    code_node("respond-invalid-body", "Build Invalid Response", RESPOND_INVALID_JS, 260, 220),
    {
        "parameters": {
            "respondWith": "json",
            "responseBody": "={{ JSON.stringify($json) }}",
            "options": {"responseCode": 400},
        },
        "id": "respond-invalid",
        "name": "Respond 400",
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": [480, 220],
    },
    code_node("build-primary-request", "Build Primary Request", build_request_js("primary"), 260, -100),
    http_node("primary-call", "Primary Call", "primary", 480, -100),
    code_node("parse-primary", "Parse Primary", parse_js("primary"), 700, -100),
    if_node("if-need-secondary", "Need Secondary?", "={{ $json.need_secondary }}", 920, -100),
    code_node("build-secondary-request", "Build Secondary Request", build_request_js("secondary"), 1140, 80),
    http_node("secondary-call", "Secondary Call", "secondary", 1360, 80),
    code_node("parse-secondary", "Parse Secondary", parse_js("secondary"), 1580, 80),
    code_node("ensemble-result", "Ensemble & Build Result", ENSEMBLE_JS, 1800, -100),
    {
        "parameters": {
            "respondWith": "json",
            "responseBody": "={{ JSON.stringify($json) }}",
            "options": {"responseCode": 200},
        },
        "id": "respond-ai-webhook",
        "name": "Respond to Webhook",
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": [2020, -100],
    },
]


def conn(src, out_index, dst):
    return {"node": dst, "type": "main", "index": out_index}


connections = {
    "Webhook Input": {"main": [[conn(None, 0, "Normalize Input")]]},
    "Normalize Input": {"main": [[conn(None, 0, "Valid Input?")]]},
    "Valid Input?": {"main": [
        [conn(None, 0, "Build Primary Request")],
        [conn(None, 0, "Build Invalid Response")],
    ]},
    "Build Invalid Response": {"main": [[conn(None, 0, "Respond 400")]]},
    "Build Primary Request": {"main": [[conn(None, 0, "Primary Call")]]},
    "Primary Call": {"main": [[conn(None, 0, "Parse Primary")]]},
    "Parse Primary": {"main": [[conn(None, 0, "Need Secondary?")]]},
    "Need Secondary?": {"main": [
        [conn(None, 0, "Build Secondary Request")],
        [conn(None, 0, "Ensemble & Build Result")],
    ]},
    "Build Secondary Request": {"main": [[conn(None, 0, "Secondary Call")]]},
    "Secondary Call": {"main": [[conn(None, 0, "Parse Secondary")]]},
    "Parse Secondary": {"main": [[conn(None, 0, "Ensemble & Build Result")]]},
    "Ensemble & Build Result": {"main": [[conn(None, 0, "Respond to Webhook")]]},
}

workflow = {
    "name": "AI Text Detection (AI-Only, OpenAI-compatible gateway)",
    "nodes": nodes,
    "connections": connections,
    # BYOK means a caller's API key transits this workflow in a request header.
    # n8n writes resolved node parameters and run bodies to SQLite when saving
    # is on, so the setting has to ship in the workflow itself: workflow-level
    # settings override the instance default, which protects the key even if the
    # operator forgets EXECUTIONS_DATA_SAVE_ON_SUCCESS=none in the environment.
    "settings": {
        "executionOrder": "v1",
        "saveExecutionProgress": False,
        "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "none",
        "saveManualExecutions": False,
    },
    "staticData": None,
    "tags": [],
    "pinData": {},
    "id": "ai-detection-ai-only",
    "active": False,
    "versionId": "3",
    "updatedAt": "2026-10-02T00:00:00.000Z",
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(workflow, f, indent=2, ensure_ascii=False)

# ---- sanity checks -------------------------------------------------------
with open(OUT, encoding="utf-8") as f:
    reloaded = json.load(f)
names = [n["name"] for n in reloaded["nodes"]]
assert len(names) == len(set(names)), "duplicate node names"
for src, outs in reloaded["connections"].items():
    assert src in names, f"connection source not a node: {src}"
    for branch in outs["main"]:
        for c in branch:
            assert c["node"] in names, f"connection target not a node: {c['node']}"

# Guard against the f-string brace-collapse bug: any n8n expression must use
# doubled braces. A lone "{" would be sent to the API as a literal string.
import re
def walk_strings(obj, where):
    if isinstance(obj, str):
        if obj.startswith("=") and ("{" in obj or "}" in obj):
            stripped = obj[1:].replace("{{", "").replace("}}", "")
            assert "{" not in stripped and "}" not in stripped, \
                f"single-brace expression in {where}: {obj!r}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            walk_strings(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_strings(v, f"{where}[{i}]")

for n in reloaded["nodes"]:
    walk_strings(n.get("parameters", {}), n["name"])

# Code nodes run as a function body (top-level `return` is legal in n8n but not
# in a script), so wrap before handing the JS to the parser. Without this a
# syntax error only surfaces at webhook time as a vague node failure.
import subprocess, tempfile, bisect
for n in reloaded["nodes"]:
    js = n.get("parameters", {}).get("jsCode")
    if not js:
        continue
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write("function __n8n_wrapped__() {\n" + js + "\n}\n")
        path = tmp.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    os.unlink(path)
    assert r.returncode == 0, f"JS syntax error in {n['name']}:\n{r.stderr}"
code_nodes = sum(1 for n in reloaded["nodes"] if n.get("parameters", {}).get("jsCode"))
print(f"js syntax check: OK ({code_nodes} code nodes)")

# A BYOK key must never be echoed back. Statically, the only place the response
# builder may reference a key is a boolean presence test, never an assignment.
for m in re.finditer(r"[:=]\s*cfg\[[^\]]*api_key[^\]]*\]|[:=]\s*cfg\.\w+_api_key", ENSEMBLE_JS):
    raise AssertionError("response builder echoes a key value: " + m.group(0))

# The n8n Code node sandbox does not expose Node/browser globals. A ReferenceError
# caught by a nearby try/catch turns into a plausible-looking validation failure,
# which is exactly how `new URL()` made every correct base_url look invalid in a
# live run while passing the unit harness. These must not appear in any node.
for node in reloaded["nodes"]:
    js = node.get("parameters", {}).get("jsCode") or ""
    lines = js.split("\n")
    offsets, acc = [], 0
    for line in lines:
        offsets.append(acc)
        acc += len(line) + 1
    for label, pattern in (
        ("URL", r"\bnew\s+URL\s*\("),
        ("fetch", r"\bfetch\s*\("),
        ("require", r"\brequire\s*\("),
        ("Buffer", r"\bBuffer\s*\("),
        ("process", r"\bprocess\."),
        ("setTimeout", r"\bsetTimeout\s*\("),
        ("globalThis", r"\bglobalThis\b"),
    ):
        for hit in re.finditer(pattern, js):
            # Only whole-line comments are skipped, so real code can never hide
            # behind a "//" that happens to sit inside a string literal.
            idx = bisect.bisect_right(offsets, hit.start()) - 1
            if re.match(r"\s*(//|\*|/\*)", lines[idx]):
                continue
            raise AssertionError(
                f"{node['name']} uses sandbox-unavailable {label}: {lines[idx].strip()}")
print("sandbox global check: OK")

print("brace-expression check: OK")
print("wrote", OUT)
print("nodes:", len(names))
print("bytes:", os.path.getsize(OUT))
print(" | ".join(names))
