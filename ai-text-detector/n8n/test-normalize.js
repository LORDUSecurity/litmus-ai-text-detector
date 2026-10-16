/*
 * Runs the shipped `Normalize Input` code node against hostile payloads.
 * The JS is read out of ai-only-workflow.json, so this tests the artefact that
 * actually gets imported into n8n rather than a copy that can drift.
 *
 *   node n8n/test-normalize.js
 */
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

const WF = JSON.parse(readFileSync(join(__dirname, 'ai-only-workflow.json'), 'utf8'));
const NODE = WF.nodes.find((n) => n.name === 'Normalize Input');
if (!NODE) throw new Error('Normalize Input node not found');

const run = new Function('$input', '$env', NODE.parameters.jsCode);

function invoke(payload, env) {
  const out = run({ first: () => ({ json: payload }) }, env || {});
  return out[0].json;
}

const BASE_ENV = {
  PRIMARY_API_KEY: 'nvapi-environmentkey',
  SECONDARY_API_KEY: 'nvapi-environmentkey',
};

// Each case: [name, payload, env, expect, check]
// expect is 'ok' or a substring that must appear in error_message.
const cases = [
  ['plain text with env config', { text: 'this is a sentence with some length to it' }, BASE_ENV, 'ok',
    (r) => eq(r.config.primary_base_url, 'https://integrate.api.nvidia.com/v1', 'primary_base_url')],

  ['host suffix trick (openrouter.ai.evil.com)',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'https://openrouter.ai.evil.com/v1' } } },
    BASE_ENV, 'not in ALLOWED_LLM_HOSTS'],

  ['query string smuggling a good host',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'https://evil.com/x?h=openrouter.ai' } } },
    BASE_ENV, ['not in ALLOWED_LLM_HOSTS', 'query string']],

  ['credentials embedded in the URL',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'https://u:p@localhost/v1' } } },
    BASE_ENV, 'must not embed credentials'],

  ['non-http scheme',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'ftp://localhost/v1' } } },
    BASE_ENV, 'absolute http(s) URL'],

  ['file scheme (SSRF to disk)',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'file:///etc/passwd' } } },
    BASE_ENV, 'absolute http(s) URL'],

  ['cloud metadata endpoint',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://169.254.169.254/latest/meta-data' } } },
    BASE_ENV, 'not in ALLOWED_LLM_HOSTS'],

  ['port 0',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost:0/v1' } } },
    BASE_ENV, ['port', 'invalid hostname'], null, 'LANE_CONFIG_INVALID'],

  ['port out of range',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost:99999/v1' } } },
    BASE_ENV, ['port', 'invalid hostname'], null, 'LANE_CONFIG_INVALID'],

  ['path already includes /chat/completions',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost:20128/v1/chat/completions' } } },
    BASE_ENV, 'not the /chat/completions endpoint'],

  ['fragment',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'https://localhost/v1#frag' } } },
    BASE_ENV, 'fragment'],

  ['trailing slash is normalised, not rejected',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost:20128/v1///' } } },
    { ...BASE_ENV, ALLOW_BYOK: 'true', PRIMARY_API_KEY: '' }, 'ok',
    (r) => eq(r.config.primary_base_url, 'http://localhost:20128/v1', 'normalised base_url')],

  ['non-default port preserved',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://127.0.0.1:19999/v1' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.primary_base_url, 'http://127.0.0.1:19999/v1', 'port kept')],

  ['model name with a shell-ish payload',
    { text: 'a long enough sentence for detection', lane_config: { primary: { model: 'foo; rm -rf /' } } },
    BASE_ENV, 'unsupported characters'],

  ['model name with a quote and brace',
    { text: 'a long enough sentence for detection', lane_config: { primary: { model: '{"x":1}' } } },
    BASE_ENV, 'unsupported characters'],

  ['model name with traversal',
    { text: 'a long enough sentence for detection', lane_config: { primary: { model: '../../etc/passwd' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.primary_model, '../../etc/passwd', 'traversal-shaped model')],

  ['empty model',
    { text: 'a long enough sentence for detection', lane_config: { primary: { model: '   ' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.primary_model, 'z-ai/glm-5.3-flash', 'blank model falls back to env')],

  ['empty base_url',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: '' } } },
    BASE_ENV, 'ok',
    (r) => {
      eq(r.config.primary_base_url, 'https://integrate.api.nvidia.com/v1', 'blank base_url falls back to env');
      eq(r.lane_meta.primary.base_url_source, 'env', 'blank override is not counted as a request override');
    }],

  ['oversized model name',
    { text: 'a long enough sentence for detection', lane_config: { primary: { model: 'a'.repeat(201) } } },
    BASE_ENV, 'too long'],

  ['valid model override lands in cfg',
    { text: 'a long enough sentence for detection', lane_config: { primary: { model: 'nvidia/nemotron-3-super-120b-a12b:free' } } },
    BASE_ENV, 'ok',
    (r) => {
      eq(r.config.primary_model, 'nvidia/nemotron-3-super-120b-a12b:free', 'model override');
      eq(r.lane_meta.primary.model_source, 'request', 'model_source');
    }],

  ['unknown lane_config field',
    { text: 'a long enough sentence for detection', lane_config: { primary: { baseUrl: 'http://localhost/v1' } } },
    BASE_ENV, 'unsupported field "baseUrl"'],

  ['unknown lane name',
    { text: 'a long enough sentence for detection', lane_config: { tertiary: { model: 'x' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.valid, true, 'an unrecognised lane entry is ignored')],

  ['lane_config is an array',
    { text: 'a long enough sentence for detection', lane_config: ['primary'] },
    BASE_ENV, 'lane_config must be an object'],

  ['lane_config.primary is a string',
    { text: 'a long enough sentence for detection', lane_config: { primary: 'localhost' } },
    BASE_ENV, 'must be an object'],

  ['BYOK header while BYOK is disabled',
    { text: 'a long enough sentence for detection', headers: { 'x-primary-api-key': 'sk-or-v1-userkey' } },
    BASE_ENV, 'bring-your-own-key is disabled', null, 'BYOK_DISABLED'],

  ['BYOK header accepted when enabled',
    { text: 'a long enough sentence for detection', headers: { 'x-primary-api-key': 'sk-or-v1-userkey' } },
    { ...BASE_ENV, ALLOW_BYOK: 'true' }, 'ok',
    (r) => {
      eq(r.config.primary_api_key, 'sk-or-v1-userkey', 'header key wins over env');
      eq(r.lane_meta.primary.key_source, 'header', 'key_source');
    }],

  ['BYOK header with CRLF injection',
    { text: 'a long enough sentence for detection', headers: { 'x-primary-api-key': 'abc\r\nX-Evil: 1' } },
    { ...BASE_ENV, ALLOW_BYOK: 'true' }, 'unsupported characters'],

  ['BYOK header with a space',
    { text: 'a long enough sentence for detection', headers: { 'x-primary-api-key': 'Bearer abc def' } },
    { ...BASE_ENV, ALLOW_BYOK: 'true' }, 'unsupported characters'],

  ['BYOK header oversized',
    { text: 'a long enough sentence for detection', headers: { 'x-primary-api-key': 'k'.repeat(513) } },
    { ...BASE_ENV, ALLOW_BYOK: 'true' }, 'too long'],

  ['secondary key via header only',
    { text: 'a long enough sentence for detection', headers: { 'x-secondary-api-key': 'sk-or-v1-second' } },
    { ...BASE_ENV, ALLOW_BYOK: 'true', SECONDARY_API_KEY: '' }, 'ok',
    (r) => eq(r.config.secondary_api_key, 'sk-or-v1-second', 'secondary header key')],

  ['no key at all against a remote host',
    { text: 'a long enough sentence for detection' },
    { PRIMARY_API_KEY: '', SECONDARY_API_KEY: '' },
    'No primary API key available'],

  ['no key against loopback is allowed with a warning',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost:20128/v1' } } },
    { PRIMARY_API_KEY: '', SECONDARY_API_KEY: '' },
    'ok',
    (r) => {
      eq(r.lane_meta.primary.key_source, 'none', 'key_source none');
      eq((r.lane_warnings || []).some((w) => /loopback/.test(w)), true, 'loopback warning present');
    }],

  ['env allowlist is respected when widened',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'https://gateway.internal:8443/v1' } } },
    { ...BASE_ENV, SECONDARY_BASE_URL: 'https://gateway.internal:8443/v1', ALLOWED_LLM_HOSTS: 'localhost, gateway.internal , 127.0.0.1' }, 'ok',
    (r) => {
      eq(r.config.primary_base_url, 'https://gateway.internal:8443/v1', 'widened allowlist');
      eq(r.config.secondary_base_url, 'https://gateway.internal:8443/v1', 'env lane also validated against the same allowlist');
    }],

  ['narrowing the allowlist invalidates a default env lane too',
    { text: 'a long enough sentence for detection' },
    { ...BASE_ENV, ALLOWED_LLM_HOSTS: 'localhost' },
    'not in ALLOWED_LLM_HOSTS'],

  ['text too short', { text: 'hi' }, BASE_ENV, 'Text too short'],
  ['text missing', { note: 'no text' }, BASE_ENV, "Field 'text' is required"],
  ['body is an array', ['a', 'b'], BASE_ENV, "Field 'text' is required"],
  ['body is a bare string', 'just text', BASE_ENV, 'Request body must be a JSON object'],

  ['oversized text',
    { text: 'x'.repeat(50001) }, BASE_ENV, 'Text too long'],

  ['ipv6 loopback without brackets',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://::1:20128/v1' } } },
    BASE_ENV, 'invalid hostname'],

  ['uppercase scheme and host',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'HTTP://LOCALHOST:20128/V1' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.primary_base_url, 'http://localhost:20128/V1', 'scheme lowered, host lowered, path case kept')],

  ['host with no path at all',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.primary_base_url, 'http://localhost', 'bare host is a valid API root')],

  ['scheme-less host',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'localhost:20128/v1' } } },
    BASE_ENV, 'absolute http(s) URL'],

  ['control characters in the URL (header splitting)',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://localhost:20128/v1' + String.fromCharCode(13, 10) + 'X-Evil: 1' } } },
    BASE_ENV, 'unsupported characters'],

  ['ipv6 loopback with brackets',
    { text: 'a long enough sentence for detection', lane_config: { primary: { base_url: 'http://[::1]:20128/v1' } } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.primary_base_url, 'http://[::1]:20128/v1', 'bracketed ipv6 kept')],

  // ---- per-request tuning (the DB-stored parameters) ---------------------
  ['tuning overrides sampling and thresholds',
    { text: 'a long enough sentence for detection',
      tuning: { temperature: 0.4, top_p: 0.7, max_tokens: 300, request_timeout_ms: 45000,
                ai_prob_threshold: 0.55, human_prob_threshold: 0.30,
                secondary_decision_threshold: 0.95 } },
    BASE_ENV, 'ok',
    (r) => {
      eq(r.config.temperature, 0.4, 'temperature');
      eq(r.config.top_p, 0.7, 'top_p');
      eq(r.config.max_tokens, 300, 'max_tokens');
      eq(r.config.request_timeout_ms, 45000, 'request_timeout_ms');
      eq(r.config.ai_prob_threshold, 0.55, 'ai_prob_threshold');
      eq(r.config.human_prob_threshold, 0.30, 'human_prob_threshold');
      eq(r.config.secondary_decision_threshold, 0.95, 'secondary_decision_threshold');
      eq(r.tuning_applied.max_tokens, 300, 'tuning_applied records what the request changed');
      eq(r.tuning_applied.temperature, 0.4, 'tuning_applied temperature');
    }],

  ['env defaults survive when tuning is absent',
    { text: 'a long enough sentence for detection' },
    { ...BASE_ENV, LLM_TEMPERATURE: '0.25', LLM_MAX_TOKENS: '900' }, 'ok',
    (r) => {
      eq(r.config.temperature, 0.25, 'env temperature');
      eq(r.config.max_tokens, 900, 'env max_tokens');
      eq(Object.keys(r.tuning_applied).length, 0, 'nothing applied');
    }],

  ['NULL tuning columns mean "keep the default"',
    { text: 'a long enough sentence for detection',
      tuning: { temperature: 0.3, ai_prob_threshold: null, top_p: '', max_tokens: undefined } },
    BASE_ENV, 'ok',
    (r) => {
      eq(r.config.temperature, 0.3, 'temperature applied');
      eq(r.config.ai_prob_threshold, 0.69, 'null left the default alone');
      eq(r.config.top_p, 0.9, 'empty string left the default alone');
      eq(r.config.max_tokens, 0, 'undefined left the default alone (0 = no cap sent)');
      eq(Object.keys(r.tuning_applied).join(','), 'temperature', 'only the real value is recorded');
    }],

  ['max_tokens 0 passes the validator: it means "send no cap"',
    { text: 'a long enough sentence for detection', tuning: { max_tokens: 0 } },
    BASE_ENV, 'ok',
    (r) => {
      eq(r.config.max_tokens, 0, 'a stored zero is a value, not a missing one');
      eq(r.tuning_applied.max_tokens, 0, 'the caller is told the zero came from it');
    }],

  ['numeric strings from SQLite are accepted',
    { text: 'a long enough sentence for detection', tuning: { temperature: '0.15', max_tokens: '512' } },
    BASE_ENV, 'ok',
    (r) => {
      eq(r.config.temperature, 0.15, 'string temperature parsed');
      eq(r.config.max_tokens, 512, 'string max_tokens parsed and floored');
    }],

  ['max_tokens is floored, not rounded up',
    { text: 'a long enough sentence for detection', tuning: { max_tokens: 512.9 } },
    BASE_ENV, 'ok',
    (r) => eq(r.config.max_tokens, 512, 'floor')],

  ['tuning temperature out of range',
    { text: 'a long enough sentence for detection', tuning: { temperature: 2.5 } },
    BASE_ENV, 'must be between 0 and 2'],

  ['tuning max_tokens absurdly large',
    { text: 'a long enough sentence for detection', tuning: { max_tokens: 1000000 } },
    BASE_ENV, 'must be between 0 and 4096'],

  ['tuning max_tokens negative is still refused',
    { text: 'a long enough sentence for detection', tuning: { max_tokens: -1 } },
    BASE_ENV, 'must be between 0 and 4096'],

  ['tuning threshold above 1',
    { text: 'a long enough sentence for detection', tuning: { ai_prob_threshold: 1.4 } },
    BASE_ENV, 'tuning.ai_prob_threshold must be between 0 and 1'],

  ['tuning is not a place to smuggle a model',
    { text: 'a long enough sentence for detection', tuning: { primary_model: 'evil/model' } },
    BASE_ENV, 'tuning has unsupported field "primary_model"'],

  ['tuning is not a place to smuggle a key',
    { text: 'a long enough sentence for detection', tuning: { primary_api_key: 'nvapi-x' } },
    BASE_ENV, 'tuning has unsupported field "primary_api_key"'],

  ['tuning non-numeric value',
    { text: 'a long enough sentence for detection', tuning: { top_p: 'high' } },
    BASE_ENV, 'tuning.top_p must be a number'],

  ['tuning must be an object',
    { text: 'a long enough sentence for detection', tuning: [0.5] },
    BASE_ENV, 'tuning must be an object'],

  ['inverted thresholds are rejected',
    { text: 'a long enough sentence for detection',
      tuning: { ai_prob_threshold: 0.30, human_prob_threshold: 0.80 } },
    BASE_ENV, 'must be below ai_prob_threshold'],

  ['a request cannot invert the env-baked pair either',
    { text: 'a long enough sentence for detection', tuning: { human_prob_threshold: 0.99 } },
    BASE_ENV, 'must be below ai_prob_threshold'],

  // ---- the optional second-opinion lane ----
  ['lane_config.secondary.enabled=false disables the tie-break',
    { text: 'a long enough sentence for detection',
      lane_config: { secondary: { enabled: false } } },
    BASE_ENV, 'ok', (r) => eq(r.config.secondary_enabled, false, 'secondary_enabled')],

  ['enabled arrives as the string "false" from form fields',
    { text: 'a long enough sentence for detection',
      lane_config: { secondary: { enabled: 'false' } } },
    BASE_ENV, 'ok', (r) => eq(r.config.secondary_enabled, false, 'secondary_enabled')],

  ['enabled:"true" keeps the lane on',
    { text: 'a long enough sentence for detection',
      lane_config: { secondary: { enabled: 'true' } } },
    BASE_ENV, 'ok', (r) => eq(r.config.secondary_enabled, true, 'secondary_enabled')],

  ['a cfg with no enabled key stays ON (defensive default)',
    { text: 'a long enough sentence for detection' },
    BASE_ENV, 'ok', (r) => eq(r.config.secondary_enabled, true, 'secondary_enabled')],

  ['env SECONDARY_ENABLED=false is the operator-level switch',
    { text: 'a long enough sentence for detection' },
    { ...BASE_ENV, SECONDARY_ENABLED: 'false' }, 'ok',
    (r) => eq(r.config.secondary_enabled, false, 'secondary_enabled')],

  ['enabled=maybe is rejected, never guessed',
    { text: 'a long enough sentence for detection',
      lane_config: { secondary: { enabled: 'maybe' } } },
    BASE_ENV, 'must be true or false'],

  ['the primary lane has no enabled field',
    { text: 'a long enough sentence for detection',
      lane_config: { primary: { enabled: false } } },
    BASE_ENV, ['unsupported field', 'enabled']],

  ['a disabled lane does not warn about its missing key',
    { text: 'a long enough sentence for detection' },
    { ...BASE_ENV, SECONDARY_API_KEY: '', SECONDARY_ENABLED: 'false' }, 'ok',
    (r) => eq((r.lane_warnings || []).some((w) => /Secondary lane has no API key/.test(w)),
      false, 'no key warning while the lane is off')],

  ['the same empty key DOES warn while the lane is on',
    { text: 'a long enough sentence for detection' },
    { ...BASE_ENV, SECONDARY_API_KEY: '' }, 'ok',
    (r) => eq((r.lane_warnings || []).some((w) => /Secondary lane has no API key/.test(w)),
      true, 'key warning present when the lane is on')],
];

function eq(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

let pass = 0;
const failures = [];
for (const [name, payload, env, expect, extra, expectCode] of cases) {
  // The webhook node nests the parsed body under `body`; test both shapes.
  const wrapped = { headers: payload.headers || {}, body: payload };
  try {
    const r = invoke(wrapped, env);
    if (expect === 'ok') {
      if (r.valid !== true) throw new Error(`expected acceptance, got rejection: ${r.error_message}`);
      if (extra) extra(r);
    } else if (Array.isArray(expect)) {
      const [first, ...rest] = expect;
      if (r.valid !== false) throw new Error(`expected rejection containing ${first}, got acceptance`);
      const hit = [first, ...rest].filter((s) => r.error_message.includes(s));
      if (!hit.length) throw new Error(`expected one of ${JSON.stringify(expect)} in: ${r.error_message}`);
    } else {
      if (r.valid !== false) throw new Error(`expected rejection containing "${expect}", got acceptance`);
      if (!r.error_message.includes(expect)) throw new Error(`expected "${expect}" in: ${r.error_message}`);
      if (!r.error_code) throw new Error('rejection is missing error_code');
      if (expectCode && r.error_code !== expectCode) {
        throw new Error(`expected error_code ${expectCode}, got ${r.error_code}`);
      }
    }
    pass++;
  } catch (e) {
    failures.push(`${name}\n    ${e.message}`);
  }
}

console.log(`Normalize Input: ${pass}/${cases.length} cases pass`);
for (const f of failures) console.log('  FAIL ' + f);
process.exit(failures.length ? 1 : 0);
