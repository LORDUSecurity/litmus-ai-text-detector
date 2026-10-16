// Unit tests for the benchmark harness's retry classification. No network: global.fetch is
// stubbed, and requiring run-benchmark.js only exports helpers (it runs a real benchmark only
// when invoked directly). Run: node test-retry.js
'use strict';

// Set the CLI before the require so the backoff is instant and two keys rotate. Values are
// .env variable NAMES; the secrets stay in loadEnv and never reach this process's output.
process.argv = [process.argv[0], process.argv[1],
  '--tries', '3', '--wait_ms', '1',
  '--key_env', 'OPENROUTER_API_KEY,OPENROUTER_API_KEY_2'];

const rb = require('./run-benchmark.js');
const { extract, transientInBody, callWithRetry } = rb;

let pass = 0, fail = 0;
function check(name, cond, got) {
  if (cond) { pass++; console.log('ok   ' + name); }
  else { fail++; console.log('FAIL ' + name + (got === undefined ? '' : '  got: ' + JSON.stringify(got))); }
}

const OVERLOAD = { error: { status: 503, message: 'Upstream error from Nvidia: Service temporarily overloaded' } };
const ANSWER = { choices: [{ message: { content: '{"ai_probability":0.82,"confidence":0.9,"signals":[]}' }, finish_reason: 'stop' }] };

// ---- classification: which failures are worth a second request ----
check('in-body 503 (http 200) is transient', transientInBody(extract(OVERLOAD)) === true);
check('in-body 429 via error.code is transient',
  transientInBody(extract({ error: { code: '429', message: 'Rate limit exceeded' } })) === true);
check('a permanent provider error is not transient',
  transientInBody(extract({ error: { code: 'content_policy_violation', message: 'blocked' } })) === false,
  extract({ error: { code: 'content_policy_violation', message: 'blocked' } }));
check('no_json_object is not transient (the model did answer)',
  transientInBody(extract({ choices: [{ message: { content: 'I cannot help with that.' }, finish_reason: 'stop' }] })) === false);
check('a clean score is not transient', transientInBody(extract(ANSWER)) === false);
check('a good row still parses', extract(ANSWER).ok === true);
// The defect this fixes: before, the harness returned the OVERLOAD row at attempt 1 and the
// summary counted it as a JSON parse failure.
check('the overload row carries error_code 503', extract(OVERLOAD).error_code === '503',
  extract(OVERLOAD).error_code);

// ---- behaviour: does it actually re-request, and rotate? ----
function fakeFetch(replies) {
  const seen = [];
  global.fetch = async (url, opts) => {
    const reply = replies[Math.min(seen.length, replies.length - 1)];
    seen.push({ auth: opts.headers.Authorization, url });
    return {
      status: reply.status,
      headers: { forEach() {} },
      text: async () => typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body)
    };
  };
  return seen;
}

(async () => {
  const item = { id: 'x', label: 'AI', config: 'open_qa', text: 'some text' };

  let seen = fakeFetch([{ status: 200, body: OVERLOAD }, { status: 200, body: ANSWER }]);
  let r = await callWithRetry(item);
  check('200-with-503-then-answer: retried and scored', r.ok === true && r.attempt === 2,
    { ok: r.ok, attempt: r.attempt, error_code: r.error_code });
  check('retries rotate to the next key', rb.keyCount > 1 ? seen[0].auth !== seen[1].auth : seen.length === 2,
    { requests: seen.length, keyCount: rb.keyCount });
  check('the transient attempt is not reported as http_200 success', r.http_status === 200);

  seen = fakeFetch([{ status: 200, body: OVERLOAD }]);
  r = await callWithRetry(item);
  check('persistent overload gives up after --tries', r.ok === false && r.attempt === 3 && seen.length === 3,
    { attempt: r.attempt, requests: seen.length });
  check('the failed row keeps its real error_code', r.error_code === '503');

  seen = fakeFetch([{ status: 200, body: { error: { code: 'content_policy_violation', message: 'no' } } }]);
  r = await callWithRetry(item);
  check('a permanent error is not retried (no hammering)', seen.length === 1 && r.ok === false,
    { requests: seen.length });

  seen = fakeFetch([{ status: 200, body: { choices: [{ message: { content: 'prose, no json' }, finish_reason: 'stop' }] } }]);
  r = await callWithRetry(item);
  check('a bad-format answer is not retried', seen.length === 1 && r.error_code === 'no_json_object',
    { requests: seen.length, error_code: r.error_code });

  seen = fakeFetch([{ status: 429, body: { error: { message: 'up keep' } } }, { status: 200, body: ANSWER }]);
  r = await callWithRetry(item);
  check('transport 429 still recovers', r.ok === true && r.attempt === 2, { attempt: r.attempt });

  // Measured 2026-10-16: nvidia/nemotron-3-ultra-550b-a55b answered HTTP 404 with a *flat* body
  // ({status,title,detail} -- no `error` key), so extract() fell to its "no choices" branch and the
  // whole 48-row run was recorded as `no_choices`. The label hid "this model is not deployed for
  // this account" behind a parse-level complaint, and it cost a background leg to find out. Any
  // non-2xx must be reported as the HTTP code it is, whatever shape the body happens to be in.
  const FLAT_404 = { status: 404, title: 'Not Found', detail: "Function id '948fe171' version 'null': Specified function in account 'x' is not found" };
  seen = fakeFetch([{ status: 404, body: FLAT_404 }]);
  r = await callWithRetry(item);
  check('a transport 404 is reported as http_404, not no_choices',
    r.error_code === 'http_404' && r.ok === false, { error_code: r.error_code });
  check('a dead model is not hammered: one request only', seen.length === 1, { requests: seen.length });
  check('the failed row carries its http_status', r.http_status === 404, { http_status: r.http_status });
  check('the provider detail survives into error_detail',
    /Function id/.test(String(r.error_detail || '')), String(r.error_detail || '').slice(0, 60));

  seen = fakeFetch([{ status: 410, body: { status: 410, title: 'Gone', detail: 'model retired' } }]);
  r = await callWithRetry(item);
  check('a 410 EOL is reported as http_410 (kimi-k3 class), not a parse failure',
    r.error_code === 'http_410' && seen.length === 1, { error_code: r.error_code, requests: seen.length });

  seen = fakeFetch([{ status: 400, body: { error: { message: 'max_tokens must be >= 1' } } }]);
  r = await callWithRetry(item);
  check('a 400 from a bad request shape is http_400 and not retried',
    r.error_code === 'http_400' && seen.length === 1, { error_code: r.error_code, requests: seen.length });

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
