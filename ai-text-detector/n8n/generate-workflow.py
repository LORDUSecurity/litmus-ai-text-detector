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
  primary_model:      env.PRIMARY_MODEL      || 'nvidia/nemotron-3-ultra-550b-a55b',
  secondary_base_url: env.SECONDARY_BASE_URL || 'https://integrate.api.nvidia.com/v1',
  secondary_api_key:  env.SECONDARY_API_KEY  || '',
  secondary_model:    env.SECONDARY_MODEL    || 'nvidia/nemotron-3-super-120b-a12b',

  min_text_length: Math.max(1, Math.floor(num(env.MIN_TEXT_LENGTH, 10))),
  max_text_length: Math.floor(num(env.MAX_TEXT_LENGTH, 50000)),

  // Defaults are the values calibrated against the HC3 benchmark; see .env for
  // the measured evidence behind each one.
  ai_prob_threshold:    num(env.AI_PROBABILITY_THRESHOLD, 0.69),
  ai_conf_threshold:    num(env.AI_CONFIDENCE_THRESHOLD, 0.60),
  human_prob_threshold: num(env.HUMAN_PROBABILITY_THRESHOLD, 0.45),
  human_conf_threshold: num(env.HUMAN_CONFIDENCE_THRESHOLD, 0.60),
  max_disagreement:     num(env.MAX_MODEL_DISAGREEMENT, 0.30),

  // Applied to confidence when only one lane answered. 1.0 disables it. The old
  // hardcoded 0.85 turned a typical 0.82 into 0.697, just under the old 0.70
  // gate, which is why clear-cut AI text kept landing in REVIEW_REQUIRED.
  single_model_penalty: num(env.SINGLE_MODEL_CONFIDENCE_PENALTY, 1.0),

  gray_low:  num(env.GRAY_ZONE_LOW, 0.45),
  gray_high: num(env.GRAY_ZONE_HIGH, 0.69),

  primary_weight:   num(env.PRIMARY_MODEL_WEIGHT, 0.9),
  secondary_weight: num(env.SECONDARY_MODEL_WEIGHT, 0.1),

  request_timeout_ms: Math.floor(num(env.LLM_TIMEOUT_MS, 90000)),

  detector_version:   env.DETECTOR_VERSION   || '2.0.0',
  classifier_version: env.CLASSIFIER_VERSION || '2.0.0',
  threshold_version:  env.THRESHOLD_VERSION  || '2.0.0'
};

// The two lane weights must sum to 1.0. Leftover values from the old
// three-component ensemble (statistics 0.60 + primary 0.25 + secondary 0.15)
// sum to 0.40 and would silently deflate every probability by 60%, so
// renormalise and flag it rather than trusting the raw numbers.
const weightSum = cfg.primary_weight + cfg.secondary_weight;
if (weightSum > 0) {
  cfg.weights_renormalized = Math.abs(weightSum - 1) > 0.001;
  cfg.primary_weight = cfg.primary_weight / weightSum;
  cfg.secondary_weight = cfg.secondary_weight / weightSum;
} else {
  cfg.primary_weight = 0.9;
  cfg.secondary_weight = 0.1;
  cfg.weights_renormalized = true;
}

const fail = (message, hint) => [{ json: {
  valid: false,
  error_message: message,
  error_hint: hint || null
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
if (!cfg.primary_api_key) {
  return fail('PRIMARY_API_KEY is not set.', 'n8n must run with N8N_BLOCK_ENV_ACCESS_IN_NODE=false and PRIMARY_API_KEY exported.');
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
const SYSTEM_PROMPT = 'You are an AI text detection classifier. Return ONLY a compact JSON object. No markdown, no code fences, no commentary, no reasoning text. Schema: {"ai_probability": number between 0 and 1, "confidence": number between 0 and 1, "signals": up to 3 objects of {"name": string, "direction": "AI" or "HUMAN", "strength": number between 0 and 1}}. ai_probability means the probability the text was generated by an AI rather than written by a human. Keep the whole response under 180 tokens.';

const body = {
  model: cfg.__LANE___model,
  messages: [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: 'Was the following text likely written by an AI? Judge the text itself.\n\n\"\"\"\n' + input.text + '\n\"\"\"\n\nReturn ONLY the JSON object.' }
  ],
  temperature: 0.1,
  top_p: 0.9,
  max_tokens: 700,
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
    // Truncated output (finish_reason=length) still usually carries the two numbers.
    const ap = candidate.match(/"ai_probability"\s*:\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)/);
    const cf = candidate.match(/"confidence"\s*:\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)/);
    if (ap || cf) {
      parsed = {
        ai_probability: ap ? parseFloat(ap[1]) : null,
        confidence: cf ? parseFloat(cf[1]) : null,
        signals: []
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

  return {
    ok: true,
    salvaged: salvaged,
    truncated: finish === 'length',
    result: {
      ai_probability: ai_probability,
      confidence: confidenceRaw === null ? 0.5 : confidenceRaw,
      signals: signals
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
  out.need_secondary = (p >= cfg.gray_low && p <= cfg.gray_high) || c < 0.5 || out.salvaged;
} else {
  out.need_secondary = true;
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
let review_required = false;
const warnings = [];

if (cfg.weights_renormalized) {
  warnings.push('PRIMARY_MODEL_WEIGHT + SECONDARY_MODEL_WEIGHT did not sum to 1.0; renormalised to '
    + cfg.primary_weight.toFixed(3) + '/' + cfg.secondary_weight.toFixed(3)
    + '. Fix the .env to remove this warning.');
}

if (pOK && sOK) {
  const p = primary.result.ai_probability;
  const s = secondary.result.ai_probability;
  ai_probability = cfg.primary_weight * p + cfg.secondary_weight * s;
  confidence = (primary.result.confidence + secondary.result.confidence) / 2;
  disagreement = Math.abs(p - s);

  if (ai_probability >= cfg.ai_prob_threshold && confidence >= cfg.ai_conf_threshold && disagreement <= cfg.max_disagreement) {
    classification = 'AI_GENERATED'; method = 'ENSEMBLE_HIGH_CONFIDENCE';
  } else if (ai_probability <= cfg.human_prob_threshold && confidence >= cfg.human_conf_threshold && disagreement <= cfg.max_disagreement) {
    classification = 'HUMAN_LIKELY'; method = 'ENSEMBLE_LOW_AI_SIGNAL';
  } else if (disagreement > cfg.max_disagreement) {
    classification = 'REVIEW_REQUIRED'; method = 'HIGH_MODEL_DISAGREEMENT'; review_required = true;
  } else if (confidence < 0.5) {
    classification = 'REVIEW_REQUIRED'; method = 'LOW_CONFIDENCE'; review_required = true;
  } else {
    classification = 'REVIEW_REQUIRED'; method = 'UNCERTAIN_GRAY_ZONE'; review_required = true;
  }
} else if (pOK || sOK) {
  const solo = pOK ? primary.result : secondary.result;
  ai_probability = solo.ai_probability;
  confidence = solo.confidence * cfg.single_model_penalty;
  method = pOK ? 'SINGLE_MODEL_PRIMARY' : 'SINGLE_MODEL_SECONDARY';
  if (cfg.single_model_penalty !== 1) {
    warnings.push('Only one model returned a usable verdict; confidence scaled by ' + cfg.single_model_penalty + '.');
  } else {
    warnings.push('Only one model returned a usable verdict.');
  }
  if (!pOK) warnings.push('The primary model failed; this verdict comes from the fallback model.');

  if (ai_probability >= cfg.ai_prob_threshold && confidence >= cfg.ai_conf_threshold) {
    classification = 'AI_GENERATED'; method += '_HIGH_CONFIDENCE';
  } else if (ai_probability <= cfg.human_prob_threshold && confidence >= cfg.human_conf_threshold) {
    classification = 'HUMAN_LIKELY'; method += '_LOW_AI_SIGNAL';
  } else {
    classification = 'REVIEW_REQUIRED'; method += '_UNCERTAIN'; review_required = true;
  }
} else {
  classification = 'DETECTION_UNAVAILABLE';
  method = 'ALL_MODELS_FAILED';
  review_required = true;
  warnings.push('Every configured model failed. See evidence.models for per-lane reasons.');
}

const out = {
  document_id: input.document_id,
  classification: classification,
  ai_probability: ai_probability === null ? null : round(ai_probability),
  confidence: confidence === null ? null : round(confidence),
  method: method,
  review_required: review_required,
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
    model_disagreement: disagreement === null ? null : round(disagreement),
    secondary_called: !!(secondary && !secondary.skipped),
    weights_used: { primary: cfg.primary_weight, secondary: cfg.secondary_weight }
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
  review_required: false,
  method: 'INPUT_REJECTED'
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
    return {
        "parameters": {
            "method": "POST",
            "url": expr(cfgref + "_base_url") + "/chat/completions",
            "authentication": "none",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {
                        "name": "Authorization",
                        "value": "=Bearer {{ " + cfgref + "_api_key }}",
                    },
                    {"name": "Content-Type", "value": "application/json"},
                    {"name": "Accept", "value": "application/json"},
                ]
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": expr(buildref),
            "options": {"timeout": 90000, "response": {"response": {"neverError": True}}},
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
    "settings": {"executionOrder": "v1", "saveExecutionProgress": True},
    "staticData": None,
    "tags": [],
    "pinData": {},
    "id": "ai-detection-ai-only",
    "active": False,
    "versionId": "2",
    "updatedAt": "2026-09-28T00:00:00.000Z",
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

print("brace-expression check: OK")
print("wrote", OUT)
print("nodes:", len(names))
print("bytes:", os.path.getsize(OUT))
print(" | ".join(names))
