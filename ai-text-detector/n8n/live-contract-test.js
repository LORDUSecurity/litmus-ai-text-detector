/*
 * Live contract test for the per-request lane overrides and BYOK headers.
 * Hits the real webhook; nothing here is simulated.
 *
 *   node n8n/live-contract-test.js
 *   node n8n/live-contract-test.js   # via the local gateway:
 *     DETECT_URL=http://127.0.0.1:8787/detect node n8n/live-contract-test.js
 *
 * Expects n8n to be running with the workflow imported and published.
 */
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

const DETECT_URL = process.env.DETECT_URL || 'http://localhost:5678/webhook/ai-detect-ai';

// Read the real key out of the gitignored .env so it is never hardcoded here.
const env = {};
for (const line of readFileSync(join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
  if (m) env[m[1]] = m[2];
}
const REAL_KEY = env.PRIMARY_API_KEY;
const CANARY = 'CANARYkey-donotpersist-0123456789';
if (!REAL_KEY) throw new Error('PRIMARY_API_KEY missing from .env');

const AI_TEXT = [
  'In today\u2019s fast-paced digital landscape, it is essential to leverage the power of',
  'artificial intelligence to streamline your workflows and unlock unprecedented',
  'productivity. By seamlessly integrating cutting-edge solutions, organisations can',
  'not only optimise their operations but also empower their teams to focus on what',
  'truly matters. Moreover, the transformative potential of these technologies extends',
  'beyond mere efficiency, fostering a culture of continuous innovation and growth.',
].join(' ');

async function post(body, headers) {
  const t0 = Date.now();
  const res = await fetch(DETECT_URL, {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {}),
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let json;
  try { json = JSON.parse(text); } catch (e) { json = null; }
  return { status: res.status, text, json, ms: Date.now() - t0 };
}

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail });
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : '\n        ' + detail}`);
}

(async () => {
  console.log('\n[1] baseline: env config only');
  let r = await post({ text: AI_TEXT });
  check('HTTP 200', r.status === 200, `got ${r.status}: ${r.text.slice(0, 300)}`);
  check('verdict produced', r.json && r.json.classification, r.text.slice(0, 300));
  check('config_used present', r.json && r.json.config_used, 'missing');
  check('both lanes report env sources',
    r.json && r.json.config_used.primary.base_url_source === 'env' &&
    r.json.config_used.secondary.key_source === 'env',
    JSON.stringify(r.json && r.json.config_used));
  console.log(`        ${r.json.classification} p=${r.json.ai_probability} in ${r.ms}ms`);
  console.log('        config_used = ' + JSON.stringify(r.json.config_used));

  console.log('\n[2] lane_config model override');
  r = await post({
    text: AI_TEXT,
    lane_config: {
      primary: { model: 'nvidia/nemotron-3-super-120b-a12b' },
      secondary: { model: 'nvidia/nemotron-3-super-120b-a12b' },
    },
  });
  check('HTTP 200', r.status === 200, `got ${r.status}: ${r.text.slice(0, 300)}`);
  check('primary model override reported',
    r.json && r.json.config_used.primary.model === 'nvidia/nemotron-3-super-120b-a12b',
    JSON.stringify(r.json && r.json.config_used));
  check('override source marked as request',
    r.json && r.json.config_used.primary.model_source === 'request', 'not marked');
  check('lane evidence used the overridden model',
    r.json && r.json.evidence.models[0].model === 'nvidia/nemotron-3-super-120b-a12b',
    JSON.stringify(r.json && r.json.evidence.models));
  console.log(`        ${r.json.classification} p=${r.json.ai_probability} in ${r.ms}ms`);

  console.log('\n[3] SSRF: host suffix trick must be refused before any outbound call');
  r = await post({
    text: AI_TEXT,
    lane_config: { primary: { base_url: 'https://openrouter.ai.evil.com/v1' } },
  });
  check('HTTP 400', r.status === 400, `got ${r.status}`);
  check('error_code LANE_CONFIG_INVALID',
    r.json && r.json.error_code === 'LANE_CONFIG_INVALID', r.text.slice(0, 300));
  check('names the offending host',
    r.text.includes('openrouter.ai.evil.com'), r.text.slice(0, 300));

  console.log('\n[4] invalid model charset');
  r = await post({ text: AI_TEXT, lane_config: { primary: { model: 'bad model name' } } });
  check('HTTP 400', r.status === 400, `got ${r.status}`);
  check('mentions unsupported characters', r.text.includes('unsupported characters'), r.text.slice(0, 300));

  console.log('\n[5] BYOK: a wrong header key must actually be used (proves the header wins)');
  r = await post({ text: AI_TEXT }, { 'x-primary-api-key': CANARY, 'x-secondary-api-key': CANARY });
  check('HTTP 200 (the run completes and reports the failure)',
    r.status === 200, `got ${r.status}: ${r.text.slice(0, 400)}`);
  check('key_source is header',
    r.json && r.json.config_used.primary.key_source === 'header',
    JSON.stringify(r.json && r.json.config_used));
  check('all models failed, as a bogus key must cause',
    r.json && r.json.classification === 'DETECTION_UNAVAILABLE',
    r.json && r.json.classification);
  check('the canary key is NOT echoed anywhere in the response',
    !r.text.includes(CANARY), 'RESPONSE LEAKED THE KEY');
  const errSnippet = r.json && r.json.evidence && r.json.evidence.models.map((m) => m.error_code + '/' + (m.error_detail || '')).join(' | ');
  console.log(`        lane errors: ${String(errSnippet).slice(0, 200)}`);

  console.log('\n[6] BYOK: the real key in a header produces a verdict');
  r = await post({ text: AI_TEXT }, { 'x-primary-api-key': REAL_KEY });
  check('HTTP 200', r.status === 200, `got ${r.status}`);
  check('verdict produced with the caller-supplied key',
    r.json && r.json.classification === 'AI_GENERATED',
    r.json && `${r.json.classification} p=${r.json.ai_probability} ${r.json.method}`);
  check('key_source is header',
    r.json && r.json.config_used.primary.key_source === 'header', 'not marked');
  check('the real key is NOT echoed anywhere in the response',
    !r.text.includes(REAL_KEY), 'RESPONSE LEAKED THE KEY');
  console.log(`        ${r.json.classification} p=${r.json.ai_probability} c=${r.json.confidence} in ${r.ms}ms`);

  const failed = results.filter((x) => !x.ok);
  console.log('\n' + '='.repeat(60));
  console.log(`${results.length - failed.length}/${results.length} assertions passed`);
  process.exit(failed.length ? 1 : 0);
})();
