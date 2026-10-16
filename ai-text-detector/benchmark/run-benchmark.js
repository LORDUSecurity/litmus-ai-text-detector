// Benchmark harness: calls the configured NVIDIA NIM model with the EXACT prompt and
// the EXACT defensive parser used by ai-only-workflow.json, so measured scores are the
// same numbers the workflow would produce.
//
// Usage:
//   node run-benchmark.js --tag baseline [--temperature 0.1] [--top_p 0.9]
//                         [--top_p null]  omit top_p: some upstreams reject the parameter
//                         [--max_tokens 0]  0 (default) omits the cap entirely [--model nvidia/...] [--limit 48]
//                         [--concurrency 4] [--corpus corpus.json]
//                         [--base_env OPENROUTER_BASE_URL
//                          --key_env OPENROUTER_API_KEY,OPENROUTER_API_KEY_2]
//                         [--resume]   keep rows already scored ok, call only the rest
//
// Writes results-<tag>.jsonl. Never prints or logs an API key.

const fs = require('fs');
const path = require('path');

// ---- config from the project .env (last key wins, same as dotenv) ----
function loadEnv(p) {
  const env = {};
  for (const line of fs.readFileSync(p, 'utf8').split(/\r?\n/)) {
    if (line.trim().startsWith('#')) continue;
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return env;
}
const ENV = loadEnv(path.join(__dirname, '..', '.env'));

// ---- CLI args ----
const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf('--' + name);
  // An empty value (--base_url "") must fall back to the default, not override it:
  // an empty base URL silently turns every request into a relative-path fetch failure.
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const KNOWN_FLAGS = ['tag', 'model', 'base_env', 'key_env', 'base_url', 'temperature', 'top_p',
  'max_tokens', 'limit', 'concurrency', 'corpus', 'resume', 'prompt', 'tries', 'wait_ms', 'timeout'];
// arg() returns the default for any name it does not read, so a mistyped flag is worse
// than a crash: `--wait 15000` (the real name is wait_ms) once ran a full rate-limit retry
// pass at the 3s default while the log claimed a slow backoff. Reject before measuring.
for (const f of argv.filter((a) => a.startsWith('--')).map((a) => a.slice(2))) {
  if (!KNOWN_FLAGS.includes(f)) {
    console.error('unknown flag --' + f + ' (known: ' + KNOWN_FLAGS.join(', ') + ')');
    process.exit(2);
  }
}
const TAG = arg('tag', 'run');
const MODEL = arg('model', ENV.PRIMARY_MODEL);
const BASE_ENV = arg('base_env', 'PRIMARY_BASE_URL');
const KEY_ENV = arg('key_env', 'PRIMARY_API_KEY');
// Both host and key are named by .env variable, never passed on the command line, so a
// second provider can be benchmarked without the secret reaching shell history.
const BASE_URL = arg('base_url', ENV[BASE_ENV]);
// --key_env accepts a comma-separated list. Free tiers rate-limit per key and per
// upstream pool, so rotating keys across retries gives a 429 a second chance to land.
const API_KEYS = KEY_ENV.split(',').map((n) => ENV[n.trim()]).filter(Boolean);
if (!API_KEYS.length) { console.error(KEY_ENV + ' missing from .env'); process.exit(1); }
const TEMPERATURE = parseFloat(arg('temperature', '0.1'));
// `--top_p null` omits the parameter entirely instead of sending a number. Some free-tier
// upstreams reject it outright -- liquid/lfm-2.5-2.6b:free answers 200 with temperature
// alone and 400 "The `top_p` parameter is not supported." with it, so a harness that always
// sends top_p reports that model as dead when it is only incompatible.
const TOP_P_RAW = arg('top_p', '0.9');
const TOP_P = /^(null|none|omit)$/i.test(TOP_P_RAW) ? null : parseFloat(TOP_P_RAW);
if (TOP_P !== null && !isFinite(TOP_P)) {
  console.error('--top_p must be a number or the literal null (got ' + TOP_P_RAW + ')');
  process.exit(2);
}
const MAX_TOKENS = parseInt(arg('max_tokens', '0'), 10);   // 0 = send no cap at all
const LIMIT = parseInt(arg('limit', '48'), 10);
const CONCURRENCY = parseInt(arg('concurrency', '4'), 10);
const CORPUS = path.join(__dirname, arg('corpus', 'corpus.json'));
const RESUME = argv.includes('--resume');
const OUT = path.join(__dirname, 'results-' + TAG + '.jsonl');
const MANIFEST = path.join(__dirname, 'runs-manifest.log');

// ---- prompts ----
// 'direct' is copied verbatim from Build Primary Request in ai-only-workflow.json.
// The variants are experimental levers; only 'direct' reflects the shipped workflow.
const PROMPTS = {
  direct: {
    system: 'You are an AI text detection classifier. Return ONLY a compact JSON object. No markdown, no code fences, no commentary, no reasoning text. Schema: {"ai_probability": number between 0 and 1, "confidence": number between 0 and 1, "signals": up to 3 objects of {"name": string, "direction": "AI" or "HUMAN", "strength": number between 0 and 1}, "phrases": up to 4 objects of {"quote": string copied VERBATIM from the text, at most 120 characters, exact substring with no ellipsis and no edits, "direction": "AI" or "HUMAN", "strength": number between 0 and 1}}. ai_probability means the probability the text was generated by an AI rather than written by a human. For each phrase quote the passage exactly as written, then judge that passage; pick the strongest evidence in each direction.',
    user: (t) => 'Was the following text likely written by an AI? Judge the text itself.\n\n"""\n' + t + '\n"""\n\nReturn ONLY the JSON object.'
  },
  evidence: {
    system: 'You are a forensic stylistic analyst. First identify concrete, quotable stylistic evidence, THEN assign a probability. Return ONLY a compact JSON object. No markdown, no code fences. Schema: {"signals": up to 4 objects of {"name": string, "direction": "AI" or "HUMAN", "strength": number between 0 and 1, "quote": string}, "ai_probability": number between 0 and 1, "confidence": number between 0 and 1}. Base ai_probability only on the evidence you actually found. If the evidence is weak or mixed, ai_probability must be near 0.5 and confidence low. Do not guess.',
    user: (t) => 'Analyse the stylistic evidence in this text and decide whether an AI wrote it.\n\n"""\n' + t + '\n"""\n\nQuote the specific phrases that drove your decision. Return ONLY the JSON object.'
  },
  contrastive: {
    system: 'You are an AI text detection classifier. Decide by asking what a typical human writer would have done differently in this exact context, then judge whether the text shows those human traits. Return ONLY a compact JSON object. No markdown, no code fences, no commentary. Schema: {"ai_probability": number between 0 and 1, "confidence": number between 0 and 1, "signals": up to 3 objects of {"name": string, "direction": "AI" or "HUMAN", "strength": number between 0 and 1}}. Be decisive: reserve 0.4-0.6 only for genuinely ambiguous text.',
    user: (t) => 'Would a typical human writing this same answer have produced this text? Compare and decide.\n\n"""\n' + t + '\n"""\n\nReturn ONLY the JSON object.'
  }
};
const PROMPT_STYLE = arg('prompt', 'direct');
if (!PROMPTS[PROMPT_STYLE]) { console.error('unknown --prompt ' + PROMPT_STYLE); process.exit(1); }
const PROMPT = PROMPTS[PROMPT_STYLE];

function buildBody(text) {
  return {
    model: MODEL,
    messages: [
      { role: 'system', content: PROMPT.system },
      { role: 'user', content: PROMPT.user(text) }
    ],
    temperature: TEMPERATURE,
    ...(TOP_P === null ? {} : { top_p: TOP_P }),
    // 0 = no cap sent, matching the deployed workflow: the answer must be allowed to close its
    // own JSON, because a truncated answer costs the score's evidence (phrases) not just prose.
    ...(MAX_TOKENS > 0 ? { max_tokens: MAX_TOKENS } : {}),
    stream: false
  };
}

// ---- parser: copied verbatim from Parse Primary ----
function toUnit(v) {
  const n = typeof v === 'string' ? parseFloat(v) : v;
  if (typeof n !== 'number' || !isFinite(n)) return null;
  return Math.min(1, Math.max(0, n));
}

// Mirrors the shipped `Parse Primary` node (n8n/generate-workflow.py, tested by
// n8n/test-parse.js): a truncated number is only salvageable if a JSON delimiter follows it,
// because a cut inside `0.78` otherwise reads back as `0` -- an invented verdict, not a missing
// one. Change one copy, change the other.
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
  let depth = 0, end = -1, inStr = false, esc = false;
  for (let i = start; i < stripped.length; i++) {
    const ch = stripped[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '\\') esc = true;
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
    const ap = wholeNumber(candidate, /"ai_probability"\s*:\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)/);
    const cf = wholeNumber(candidate, /"confidence"\s*:\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)/);
    if (ap !== null || cf !== null) {
      parsed = { ai_probability: ap === null ? null : parseFloat(ap), confidence: cf === null ? null : parseFloat(cf), signals: [], phrases: [] };
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
  // phrases: verbatim evidence spans, same sanitizer as the deployed workflow parser.
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
    result: { ai_probability: ai_probability, confidence: confidenceRaw === null ? 0.5 : confidenceRaw, signals: signals, phrases: phrases }
  };
}

// ---- HTTP with retries mirroring the workflow node (maxTries 4, wait 3s) ----
// --tries/--wait override the mirror only to push through a lossy link; the manifest
// records what was used, and neither lever can change a score, only whether you get one.
const MAX_TRIES = parseInt(arg('tries', '4'), 10);
const WAIT_MS = parseInt(arg('wait_ms', '3000'), 10);
// Per-request wall-clock ceiling, in SECONDS. NVIDIA's free-tier pool is often minutes-slow on a
// cold queue (measured: z-ai/glm-5.3 on corpus-clean exceeded the 90s default on all four tries of
// every row on 2026-10-11, so the harness reported a live model as dead -- the exact failure class
// documented for a 30s probe hiding the best free models. Slow != dead.) Raise it for the free
// lanes; it can only change whether a score is obtained, never the score, so the manifest records
// it alongside tries/wait_ms for the same reason.
const TIMEOUT_S = parseInt(arg('timeout', '90'), 10);
if (!isFinite(TIMEOUT_S) || TIMEOUT_S <= 0) {
  console.error('--timeout must be a positive integer of seconds (got ' + arg('timeout', '90') + ')');
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function callOnce(text, key) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT_S * 1000);
  try {
    const res = await fetch(BASE_URL + '/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(buildBody(text)),
      signal: ctrl.signal
    });
    const raw = await res.text();
    let payload;
    try { payload = JSON.parse(raw); } catch (e) { payload = { error: { code: res.status, message: raw.slice(0, 300) } }; }
    // The `x-ratelimit-*` numbers are the only place the gateway states the free-tier cap, and
    // they arrive ONLY in the 429 body's echoed headers -- a 200 response carries none (verified
    // 2026-10-07 with all three keys served). Storing them makes "how much budget is left, and is
    // it per key or per account" answerable from a capture instead of a dedicated probe.
    const limits = {};
    res.headers.forEach((v, k) => { if (k.startsWith('x-ratelimit-')) limits[k] = v; });
    const echoed = payload && payload.error && payload.error.metadata && payload.error.metadata.headers;
    if (echoed && typeof echoed === 'object') {
      for (const [k, v] of Object.entries(echoed)) {
        if (k.toLowerCase().startsWith('x-ratelimit-')) limits['body_' + k.toLowerCase()] = v;
      }
    }
    return { status: res.status, payload, limits };
  } finally {
    clearTimeout(t);
  }
}

// OpenRouter does not relay its upstream's failures as HTTP codes: it answers 200 and puts the
// real status in the body ({error:{status:503}, "Upstream error from Nvidia: Service temporarily
// overloaded"}). The transport check in callWithRetry therefore never sees it, and the harness
// gave up at attempt 1 with no key rotation -- which is how the nemotron:free hc3-48 run lost
// 19 of its 48 texts (measured 2026-10-07: every one of those rows has http_status 200,
// error_code "503" and attempt 1). Same transient class, same treatment as a 429.
const TRANSIENT_CODES = ['429', '500', '502', '503', '504'];
function transientInBody(out) {
  return !!(out && !out.ok && TRANSIENT_CODES.includes(String(out.error_code)));
}

async function callWithRetry(item) {
  let last = null;
  for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
    const t0 = Date.now();
    // Rotate to the next key on every attempt. A :free 429 is usually one account
    // hitting the shared upstream pool, so the same request from the other key often
    // goes through.
    const key = API_KEYS[(attempt - 1) % API_KEYS.length];
    try {
      const { status, payload, limits } = await callOnce(item.text, key);
      const latency = Date.now() - t0;
      const rl = Object.keys(limits).length ? limits : null;
      // Retry transport-level / rate-limit problems, and in-body provider errors (below): this
      // harness measures the model, so it must not charge the gateway's mood to the model. The
      // shipped workflow already does both -- measured 2026-10-07 in tools/verify-lane-retry.js, a
      // real 503 and a 200-carrying-an-upstream-503 each cost 4 attempts 3s apart -- so the harness
      // is now the same shape as the thing under test instead of harsher than it.
      if (status >= 400) {
        // Classify a failed request by the HTTP code the gateway answered with, before the body
        // reaches extract(). Providers do not agree on an error shape: NVIDIA returns a flat
        // {status,title,detail} with no `error` key, so a 404 "Function id ... is not found"
        // (measured 2026-10-16 for the deployed primary id) used to be recorded as `no_choices` --
        // a parse-level label that hid "this model is not deployed for this account" and made a
        // whole 48-row leg look like a formatting problem. Only the transient codes re-request.
        const transient = TRANSIENT_CODES.includes(String(status));
        last = {
          ok: false, error_code: 'http_' + status, http_status: status,
          error_detail: JSON.stringify(payload).slice(0, 200),
          latency_ms: latency, attempt, rate_limit: rl
        };
        if (transient && attempt < MAX_TRIES) { await sleep(WAIT_MS * attempt); continue; }
        return last;
      }
      const out = extract(payload);
      out.latency_ms = latency;
      out.attempt = attempt;
      out.http_status = status;
      out.rate_limit = rl;
      if (transientInBody(out)) {
        last = out;
        if (attempt < MAX_TRIES) { await sleep(WAIT_MS * attempt); continue; }
        return last;
      }
      return out;
    } catch (e) {
      last = { ok: false, error_code: 'exception', error_detail: String(e.message).slice(0, 200), latency_ms: Date.now() - t0, attempt };
      if (attempt < MAX_TRIES) { await sleep(WAIT_MS * attempt); }
    }
  }
  return last;
}

// ---- simple worker pool ----
async function pool(items, worker, n) {
  const results = new Array(items.length);
  let next = 0;
  async function run() {
    while (true) {
      const i = next++;
      if (i >= items.length) return;
      results[i] = await worker(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, run));
  return results;
}

// Requiring this file runs the unit tests (test-retry.js) instead of spending API budget.
if (require.main === module) {
(async () => {
  const corpusAll = JSON.parse(fs.readFileSync(CORPUS, 'utf8')).slice(0, LIMIT);
  // --resume keeps every row already scored ok and calls only the rest. This link drops
  // name resolution for minutes at a time, so a run that dies at 30/46 should not have to
  // start over (nor lose the rows it did get).
  const prior = {};
  if (RESUME && fs.existsSync(OUT)) {
    for (const line of fs.readFileSync(OUT, 'utf8').split(/\r?\n/)) {
      if (!line.trim()) continue;
      let r;
      try { r = JSON.parse(line); } catch (e) { continue; }
      if (r.ok) prior[r.id] = r;
    }
  }
  const corpus = corpusAll.filter((it) => !prior[it.id]);
  const kept = Object.keys(prior).length;
  if (kept) console.log('resume: keeping ' + kept + ' scored row(s), calling ' + corpus.length);
  console.log('tag=' + TAG, 'model=' + MODEL, 'prompt=' + PROMPT_STYLE, 'temp=' + TEMPERATURE, 'top_p=' + TOP_P,
    'max_tokens=' + (MAX_TOKENS > 0 ? MAX_TOKENS : 'auto'), 'timeout_s=' + TIMEOUT_S, 'n=' + corpus.length, 'concurrency=' + CONCURRENCY);
  // Never print a key, not even a prefix — log only how many keys are rotating.
  console.log('base_url=' + BASE_URL, 'keys=' + API_KEYS.length);

  const t0 = Date.now();
  let done = 0;
  const fresh = await pool(corpus, async (item, i) => {
    const r = await callWithRetry(item);
    done++;
    const score = r.ok ? r.result.ai_probability.toFixed(2) : 'ERR:' + r.error_code;
    console.log('  [' + String(done).padStart(2) + '/' + corpus.length + '] ' + item.id +
      // self-generated corpora carry config as an object ({model,...}) — print the model, not "[object Object]"
      ' ' + item.label.padEnd(5) + ' ' + String(item.config && item.config.model || item.config).padEnd(11) + ' -> ' + score +
      (r.ok ? ' conf=' + r.result.confidence.toFixed(2) : '') +
      (r.salvaged ? ' SALVAGED' : '') + (r.truncated ? ' TRUNCATED' : '') +
      ' (' + r.latency_ms + 'ms' + (r.attempt > 1 ? ', try' + r.attempt : '') + ')');
    return {
      id: item.id, label: item.label, config: item.config, text_length: item.text.length,
      ok: !!r.ok, error_code: r.error_code || null, error_detail: r.error_detail || null,
      salvaged: !!r.salvaged, truncated: !!r.truncated,
      ai_probability: r.ok ? r.result.ai_probability : null,
      confidence: r.ok ? r.result.confidence : null,
      signals: r.ok ? r.result.signals : null,
      phrases: r.ok ? r.result.phrases : null,
      latency_ms: r.latency_ms || null, attempt: r.attempt || null, http_status: r.http_status || null,
      rate_limit: r.rate_limit || null
    };
  }, CONCURRENCY);

  const byId = {};
  for (const r of fresh) byId[r.id] = r;
  const results = corpusAll.map((it) => byId[it.id] || prior[it.id]).filter(Boolean);
  fs.writeFileSync(OUT, results.map((r) => JSON.stringify(r)).join('\n') + '\n');

  const okc = results.filter((r) => r.ok);
  const errs = {};
  for (const r of results.filter((x) => !x.ok)) errs[r.error_code] = (errs[r.error_code] || 0) + 1;
  const lat = okc.map((r) => r.latency_ms).sort((a, b) => a - b);

  console.log('\nwrote ' + OUT);
  const paramsLine = 'params: ' + JSON.stringify({
    tag: TAG, model: MODEL, prompt: PROMPT_STYLE, temperature: TEMPERATURE, top_p: TOP_P,
    max_tokens: MAX_TOKENS, tries: MAX_TRIES, wait_ms: WAIT_MS, timeout_s: TIMEOUT_S,
    concurrency: CONCURRENCY, base_url: BASE_URL, corpus: path.basename(CORPUS), rows: results.length,
    usable: okc.length
  });
  // The manifest is what backend/seed.py reads to attribute a results file to a real
  // model id and provider. Without this line a new run imports as model=<tag>,
  // provider=nvidia -- a provenance lie, not just a missing label. Never write the key.
  fs.appendFileSync(MANIFEST, paramsLine + '\n');
  console.log(paramsLine);
  console.log('usable=' + okc.length + '/' + results.length +
    ' salvaged=' + results.filter((r) => r.salvaged).length +
    ' truncated=' + results.filter((r) => r.truncated).length +
    (Object.keys(errs).length ? ' errors=' + JSON.stringify(errs) : ' errors=none'));
  if (lat.length) console.log('latency ms p50/p90/max: ' + lat[Math.floor(lat.length / 2)] + '/' + lat[Math.floor(lat.length * 0.9)] + '/' + lat[lat.length - 1]);
  console.log('wall clock: ' + ((Date.now() - t0) / 1000).toFixed(1) + 's');
})();
}

// keyCount only, never the keys: test-retry.js asserts rotation happened without a secret
// reaching the test process's output.
module.exports = { extract, transientInBody, callWithRetry, keyCount: API_KEYS.length };
