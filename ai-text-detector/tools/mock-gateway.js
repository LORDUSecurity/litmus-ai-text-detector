/*
 * Loopback stand-in for an OpenAI-compatible gateway, used to measure whether the shipped
 * workflow retries a lane that fails. It costs zero API budget, and it counts its own requests
 * so retry behaviour is observed instead of assumed.
 *
 *   node tools/mock-gateway.js --port 20777 --scenario http503x3
 *
 * Scenarios: the first N requests fail, the rest answer.
 *   ok          always answers
 *   http503x3   three real HTTP 503s (what NVIDIA returns when its worker pool is full), then answers
 *   http503     always 503, so the permanent-failure path can be watched
 *   wrap503x3   three HTTP 200 responses whose *body* is an error, the way OpenRouter relays an
 *               upstream failure, then answers
 *   wrap503     always that wrapped error
 *   emptyjson   always a 200 carrying {} (a successful request with an unusable body)
 *
 * GET /state returns the request log (models and statuses only: the Authorization header is
 * counted, never stored, so a real key cannot land in the output). POST /reset zeroes it.
 */
const http = require('node:http');

function answer(model) {
  return {
    id: 'mock-' + Math.random().toString(36).slice(2),
    model,
    object: 'chat.completion',
    choices: [{
      index: 0,
      finish_reason: 'stop',
      message: {
        role: 'assistant',
        // Confident AI on purpose: a decisive primary is never supposed to buy a second opinion,
        // so the request count stays a clean measurement of the primary's own retries.
        content: JSON.stringify({
          ai_probability: 0.95,
          confidence: 0.9,
          signals: [{ name: 'mock_signal', direction: 'AI', strength: 0.9 }],
          rationale: 'deterministic mock answer, no model involved',
        }),
      },
    }],
  };
}

// OpenRouter wraps its upstream's failure inside a 200: the status lives in the body, so a client
// that only inspects the HTTP code reads this as a successful call.
const WRAPPED = {
  error: {
    code: 503,
    message: 'Upstream error from Nvidia: Service temporarily overloaded',
    metadata: { provider_name: 'NVIDIA', raw: '{"error":{"message":"Worker local total request limit reached (16/16)"}}' },
  },
};

const SCENARIOS = {
  ok: { failures: 0, mode: 'answer' },
  http503x3: { failures: 3, mode: 'http503' },
  http503: { failures: Infinity, mode: 'http503' },
  wrap503x3: { failures: 3, mode: 'wrap503' },
  wrap503: { failures: Infinity, mode: 'wrap503' },
  // A 200 with a body that is neither an error nor a completion. If the workflow retried this too,
  // the retries above would be the node running four times regardless of what it saw, and the
  // measurement would mean nothing. It must be called exactly once.
  emptyjson: { failures: Infinity, mode: 'empty' },
};

const argv = process.argv.slice(2);
function flag(name, dflt) {
  const i = argv.indexOf('--' + name);
  return i === -1 ? dflt : argv[i + 1];
}
const PORT = Number(flag('port', 20777));
const scenarioName = flag('scenario', 'ok');
if (!SCENARIOS[scenarioName]) {
  console.error('unknown scenario "' + scenarioName + '", choose one of: ' + Object.keys(SCENARIOS).join(', '));
  process.exit(2);
}

const state = { scenario: scenarioName, requests: [] };

const server = http.createServer((req, res) => {
  const send = (code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
  };
  if (req.method === 'GET' && req.url === '/state') return send(200, {
    scenario: state.scenario,
    total: state.requests.length,
    byModel: state.requests.reduce((acc, r) => {
      const k = r.model + ' -> ' + r.served;
      acc[k] = (acc[k] || 0) + 1;
      return acc;
    }, {}),
    requests: state.requests,
  });
  if (req.method === 'POST' && req.url === '/reset') {
    state.requests = [];
    return send(200, { reset: true });
  }
  if (req.method === 'POST' && /chat\/completions$/.test(req.url)) {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      let model = 'unknown';
      try { model = JSON.parse(raw).model || 'unknown'; } catch (e) { /* a malformed body is still a request */ }
      const sc = SCENARIOS[state.scenario];
      const failing = state.requests.length < sc.failures;
      // Count authorization attempts without ever keeping the value.
      const hasKey = !!req.headers.authorization;
      if (!failing) {
        state.requests.push({ model, served: 'answer', hasKey, at: new Date().toISOString() });
        return send(200, answer(model));
      }
      if (sc.mode === 'empty') {
        state.requests.push({ model, served: 'empty200', hasKey, at: new Date().toISOString() });
        return send(200, {});
      }
      if (sc.mode === 'http503') {
        state.requests.push({ model, served: 'http503', hasKey, at: new Date().toISOString() });
        return send(503, { error: { message: 'Worker local total request limit reached (16/16)' } });
      }
      state.requests.push({ model, served: 'wrap503', hasKey, at: new Date().toISOString() });
      return send(200, WRAPPED);
    });
    return;
  }
  send(404, { error: { message: 'mock only serves /v1/chat/completions, /state, /reset' } });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('mock gateway on http://127.0.0.1:' + PORT + '/v1 scenario=' + scenarioName);
});
