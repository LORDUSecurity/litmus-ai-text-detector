/*
 * Does the shipped workflow retry a lane whose gateway fails? Measured end to end through the real
 * n8n webhook against tools/mock-gateway.js on loopback: zero API budget, and the mock's own request
 * log is the evidence (a retry nobody counted is a retry nobody has).
 *
 *   node tools/verify-lane-retry.js            # needs n8n running with the workflow active
 *
 * Six cases, chosen so each count means something:
 *   ok          a healthy lane is called once        (the floor every other count reads against)
 *   emptyjson   a 200 carrying {} is called once     (proves the retries below are error-driven, not
 *                                                     the node simply running four times)
 *   http503x3   a real 503 that clears is retried    (NVIDIA's "worker pool full" failure)
 *   wrap503x3   a 200 whose body is an upstream 503  (OpenRouter's relay: the HTTP code says fine)
 *   http503     a lane that never recovers: bounded retries, then a graceful ALL_MODELS_FAILED
 *   wrap503     same, and the recorded reason must blame the gateway, not the model's formatting
 */
const { spawn } = require('node:child_process');
const { join } = require('node:path');

const WEBHOOK = process.env.WEBHOOK_URL || 'http://localhost:5678/webhook/ai-detect-ai';
const PORT = Number(process.env.MOCK_PORT || 20777);
const BASE = 'http://127.0.0.1:' + PORT + '/v1';
const MAX_TRIES = 4;  // the HTTP node's own maxTries, as set in the workflow definition

// What a failed lane may be blamed on. A gateway code means "the lane never answered"; a parse code
// means "the model answered in a form with no score". Reporting the second for the first is how a
// rate limit ends up published as a model defect.
const GATEWAY_CODES = ['503', '502', '500', '504', '429', 'empty_response', 'no_choices', 'null_content', 'exception'];
const MODEL_CODES = ['no_json_object', 'json_parse_failed', 'missing_ai_probability'];

const AI_TEXT = [
  'In today\u2019s fast-paced digital landscape, it is essential to leverage the power of',
  'artificial intelligence to streamline your workflows and unlock unprecedented',
  'productivity. By seamlessly integrating cutting-edge solutions, organisations can',
  'not only optimise their operations but also empower their teams to focus on what',
  'truly matters. Moreover, the transformative potential of these technologies extends',
  'beyond mere efficiency, fostering a culture of continuous innovation and growth.',
].join(' ');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function json(url, init) {
  const res = await fetch(url, init);
  const text = await res.text();
  let body = null;
  try { body = JSON.parse(text); } catch (e) { /* keep the raw text for the report */ }
  return { status: res.status, body, text };
}

async function startMock(scenario) {
  const child = spawn(process.execPath,
    [join(__dirname, 'mock-gateway.js'), '--port', String(PORT), '--scenario', scenario],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  const errs = [];
  child.stderr.on('data', (d) => errs.push(String(d)));
  for (let i = 0; i < 40; i++) {
    try {
      const s = await json('http://127.0.0.1:' + PORT + '/state');
      if (s.status === 200) break;
    } catch (e) { await sleep(250); }
    if (i === 39) throw new Error('mock did not come up: ' + errs.join(''));
  }
  return child;
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok });
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + '  ' + name + (ok ? '' : '\n        ' + detail));
}

(async () => {
  const cases = [
    { scenario: 'ok', wantPrimary: 1, wantVerdict: true,
      note: 'a healthy lane must be called exactly once' },
    { scenario: 'emptyjson', wantPrimary: 1,
      note: 'a 200 with an unusable body is not a gateway failure, so nothing is retried' },
    { scenario: 'http503x3', wantPrimary: MAX_TRIES, wantVerdict: true,
      note: 'three real 503s then an answer: the retry must reach the answer' },
    { scenario: 'wrap503x3', wantPrimary: MAX_TRIES, wantVerdict: true,
      note: 'three HTTP 200s carrying an upstream 503 in the body: treated like a real 503' },
    { scenario: 'http503', wantPrimary: MAX_TRIES, wantDead: true,
      note: 'a lane that never recovers: bounded retries, then graceful degradation' },
    { scenario: 'wrap503', wantPrimary: MAX_TRIES, wantDead: true, wantGatewayReason: true,
      note: 'a permanently wrapped upstream error: the recorded reason must blame the gateway' },
  ];

  for (const tc of cases) {
    const child = await startMock(tc.scenario);
    try {
      await json('http://127.0.0.1:' + PORT + '/reset', { method: 'POST' });
      const t0 = Date.now();
      const r = await json(WEBHOOK, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: AI_TEXT,
          // Both lanes point at the mock: a failing primary must not buy a real secondary call, and
          // the mock is the only thing this test is allowed to talk to.
          lane_config: { primary: { base_url: BASE }, secondary: { base_url: BASE } },
        }),
      });
      const state = await json('http://127.0.0.1:' + PORT + '/state');
      const rows = (state.body && state.body.requests) || [];
      // Both lanes hit the same mock, so separate them by the model in the request body. The lane
      // that asked first is the primary: the secondary is only bought after the primary defers.
      const primary = rows.length ? rows[0].model : null;
      const tries = rows.filter((x) => x.model === primary).length;
      const trace = rows.map((x) => x.model.split('/').pop() + ':' + x.served).join(', ') || 'none';

      console.log('\n[' + tc.scenario + '] ' + tc.note);
      console.log('  webhook http=' + r.status + ' classification=' + (r.body && r.body.classification) +
        ' method=' + (r.body && r.body.method) + ' (' + ((Date.now() - t0) / 1000).toFixed(1) + 's)');
      console.log('  mock saw ' + rows.length + ' request(s): ' + trace);

      check(tc.scenario + ': webhook answered 200', r.status === 200, r.text.slice(0, 300));
      check(tc.scenario + ': primary attempted ' + tc.wantPrimary + ' time(s)',
        tries === tc.wantPrimary, 'attempted ' + tries + ' (trace: ' + trace + ')');
      if (tc.wantVerdict) {
        check(tc.scenario + ': a verdict came out', !!(r.body && r.body.classification), r.text.slice(0, 300));
      }
      if (tc.wantDead) {
        check(tc.scenario + ': dead lanes degrade instead of crashing',
          r.body && r.body.classification === 'DETECTION_UNAVAILABLE' && r.body.method === 'ALL_MODELS_FAILED',
          'got ' + (r.body && r.body.classification) + ' / ' + (r.body && r.body.method));
        const second = rows.filter((x) => x.model !== primary).length;
        check(tc.scenario + ': the secondary lane was tried and also gave up',
          second === MAX_TRIES, 'secondary attempted ' + second + ' (trace: ' + trace + ')');
      }
      if (tc.wantGatewayReason) {
        const models = (r.body && r.body.evidence && r.body.evidence.models) || [];
        const codes = models.map((m) => String(m.error_code));
        const blamedGateway = codes.length > 0 &&
          codes.some((c) => GATEWAY_CODES.includes(c)) &&
          !codes.some((c) => MODEL_CODES.includes(c));
        check(tc.scenario + ': reasons are gateway codes, not model parse codes',
          blamedGateway, 'codes: ' + JSON.stringify(codes) + ' details: ' +
            JSON.stringify(models.map((m) => String(m.error_detail || '').slice(0, 70))));
      }
    } catch (e) {
      check(tc.scenario + ': case ran', false, String((e && e.message) || e));
    } finally {
      child.kill();
      await sleep(400);
    }
  }

  const failed = results.filter((x) => !x.ok).length;
  console.log('\n' + (results.length - failed) + ' passed, ' + failed + ' failed');
  process.exit(failed ? 1 : 0);
})();
