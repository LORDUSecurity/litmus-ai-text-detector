// Which OpenRouter keys still have free-tier headroom? Prints only the env var NAME, the HTTP
// status, the `x-ratelimit-*` response headers and the error's first words -- never a key value,
// never any other header, and never more of the body than the error kind. Run:
//   node tools/probe-or-headroom.js
// The rate-limit headers are why this exists: they are the only place the gateway states the cap
// numerically, and a captured 429 body buries them past the harness's 200-char error truncation.
const fs = require('node:fs');
const path = require('node:path');

const env = {};
for (const line of fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
}

const names = Object.keys(env).filter((k) => /^OPENROUTER_API_KEY(_\d+)?$/.test(k)).sort();
const MODEL = process.argv[2] || 'nvidia/nemotron-3-ultra-550b-a55b:free';

// Only ever these header names -- they carry numbers, not identity.
function limitHeaders(headers) {
  const out = [];
  headers.forEach((value, name) => {
    if (name.startsWith('x-ratelimit-')) out.push(name + '=' + value);
  });
  return out;
}

// Exit code is part of the contract: benchmark scripts gate on it instead of grepping prose.
//   0 = at least one key served a request today
//   1 = every key returned the account-level free-models-per-day cap (waiting will not help)
//   2 = no key could be tested (transport error, or no key configured)
(async () => {
  if (!names.length) { console.log('no OPENROUTER_API_KEY* in .env'); process.exitCode = 2; return; }
  let served = 0, capped = 0, broken = 0;
  for (const name of names) {
    const key = env[name];
    if (!key) { console.log(name + ': not set'); broken++; continue; }
    let status = 'request-failed', detail = '', limits = [];
    try {
      const r = await fetch('https://openrouter.ai/api/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + key },
        body: JSON.stringify({ model: MODEL, messages: [{ role: 'user', content: 'Reply with the single word OK' }], max_tokens: 8 }),
      });
      status = r.status;
      limits = limitHeaders(r.headers);
      if (status !== 200) {
        const body = await r.text();
        let parsed = null;
        try { parsed = JSON.parse(body); } catch (e) { /* non-JSON body: fall back to its prefix */ }
        // Only the error's leading words: a body can echo request data, and a length plus a
        // prefix is enough to tell free-models-per-day from a transient upstream limit.
        detail = (parsed && parsed.error && parsed.error.message ? parsed.error.message : body).slice(0, 64);
        // The gateway also mirrors the upstream limiter's headers into the error body, which is
        // where the cap shows up even when the HTTP response itself carries none.
        const echoed = parsed && parsed.error && parsed.error.metadata && parsed.error.metadata.headers;
        if (echoed && typeof echoed === 'object') {
          for (const [name, value] of Object.entries(echoed)) {
            if (name.toLowerCase().startsWith('x-ratelimit-')) limits.push('body:' + name + '=' + value);
          }
        }
      }
    } catch (e) { detail = e.name; }
    console.log(`${name} (len=${key.length}) -> ${status}${detail ? '  ' + detail : '  usable'}${limits.length ? '  [' + limits.join(' ') + ']' : ''}`);
    if (status === 200) served++;
    else if (status === 429) capped++;
    else broken++;
  }
  process.exitCode = served > 0 ? 0 : (capped > 0 && broken === 0 ? 1 : 2);
})();
