// Probe EVERY model the gateway lists, with one real request each, and record why it
// does or does not work. GET /v1/models is a catalogue of what the account *might* be
// entitled to, not a promise: entries here come back 404 ("Function ... Not found for
// account"), 503 ("Worker local total request limit reached"), or never answer at all.
//
//   node catalog-probe.js [--concurrency 3] [--timeout 30] [--text ai-000]
//   node catalog-probe.js --base_env OPENROUTER_BASE_URL --key_env OPENROUTER_API_KEY --filter ':free$'
//
// Writes catalog-probe.json. Never prints the API key.

const fs = require('fs');
const path = require('path');

const env = {};
for (const l of fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  if (l.trim().startsWith('#')) continue;
  const m = l.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
}

const argv = process.argv.slice(2);
const arg = (n, d) => { const i = argv.indexOf('--' + n); return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d; };
const CONCURRENCY = parseInt(arg('concurrency', '3'), 10);
const TIMEOUT_MS = parseInt(arg('timeout', '30'), 10) * 1000;
const TEXT_ID = arg('text', 'ai-000');
const BASE_URL = env[arg('base_env', 'PRIMARY_BASE_URL')];
const API_KEY = env[arg('key_env', 'PRIMARY_API_KEY')];
const FILTER = arg('filter', null);

const SYSTEM = 'You are an AI text detection classifier. Return ONLY a compact JSON object. No markdown, no code fences, no reasoning text. Schema: {"ai_probability": number between 0 and 1, "confidence": number between 0 and 1}. Under 180 tokens.';

async function listModels() {
  const res = await fetch(BASE_URL + '/models', { headers: { Authorization: 'Bearer ' + API_KEY } });
  const j = await res.json();
  let ids = (j.data || []).map((d) => d.id);
  if (FILTER) {
    const re = new RegExp(FILTER);
    ids = ids.filter((id) => re.test(id));
  }
  return ids.sort();
}

function grade(content) {
  if (typeof content !== 'string' || !content) return { verdict: 'EMPTY', detail: 'no string content' };
  const stripped = content.replace(/```json/gi, '```').replace(/```/g, '').trim();
  const m = stripped.match(/"ai_probability"\s*:\s*(-?\d*\.?\d+)/);
  if (m) return { verdict: 'SCORED', detail: 'p=' + m[1] };
  // A thinking preamble that ran out of budget is a different failure from a model that
  // answers in prose: the first is fixable with max_tokens, the second is not.
  if (/thinking process|let me analyze|\*\*Analyze/i.test(stripped)) return { verdict: 'REASONING_PREAMBLE', detail: stripped.slice(0, 70).replace(/\s+/g, ' ') };
  return { verdict: 'NO_JSON', detail: stripped.slice(0, 70).replace(/\s+/g, ' ') };
}

(async () => {
  const models = await listModels();
  const TAG = arg('tag', 'primary');
  const corpus = JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus.json'), 'utf8'));
  const text = (corpus.find((c) => c.id === TEXT_ID) || corpus[0]).text;
  console.log('probing ' + models.length + ' listed models on ' + BASE_URL + ' (text ' + TEXT_ID + '), concurrency ' + CONCURRENCY);

  const results = new Array(models.length);
  let next = 0, done = 0;
  async function worker() {
    while (true) {
      const i = next++;
      if (i >= models.length) return;
      const model = models[i];
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
      const t0 = Date.now();
      let rec = { model, http: null, ms: null, verdict: null, detail: null };
      try {
        const res = await fetch(BASE_URL + '/chat/completions', {
          method: 'POST',
          headers: { Authorization: 'Bearer ' + API_KEY, 'Content-Type': 'application/json' },
          body: JSON.stringify({ model, messages: [{ role: 'system', content: SYSTEM }, { role: 'user', content: 'Judge this text:\n"""\n' + text + '\n"""' }], temperature: 0.1, max_tokens: 500 }),
          signal: ctrl.signal
        });
        const raw = await res.text();
        rec.http = res.status;
        let j = null;
        try { j = JSON.parse(raw); } catch (e) { /* non-JSON body, keep the text */ }
        if (j && j.error) {
          rec.verdict = 'PROVIDER_ERROR';
          // OpenRouter hides the real reason in error.metadata.raw; the top-level message is
          // the same generic "Provider returned error" for every failure class. limit_source
          // is what distinguishes "this account is capped" from "that upstream is busy".
          const md = (j.error.metadata) || {};
          rec.detail = String(md.raw || j.error.message || JSON.stringify(j.error)).slice(0, 160);
          rec.limit_source = md.limit_source || null;
          rec.provider = md.provider_name || null;
        } else if (j && j.choices && j.choices[0]) {
          const g = grade(j.choices[0].message && j.choices[0].message.content);
          rec.verdict = g.verdict; rec.detail = g.detail + (j.choices[0].finish_reason ? ' finish=' + j.choices[0].finish_reason : '');
        } else {
          rec.verdict = 'UNEXPECTED_BODY'; rec.detail = (raw || JSON.stringify(j)).slice(0, 120);
        }
      } catch (e) {
        rec.verdict = e.name === 'AbortError' ? 'TIMEOUT' : 'THREW';
        rec.detail = String(e.message).slice(0, 100);
      } finally {
        clearTimeout(timer);
        rec.ms = Date.now() - t0;
      }
      results[i] = rec;
      console.log('  [' + String(++done).padStart(3) + '/' + models.length + '] ' + model.padEnd(46) + String(rec.http || '-').padStart(4) + ' ' + String(rec.ms).padStart(6) + 'ms  ' + String(rec.verdict).padEnd(17) + String(rec.detail).slice(0, 60));
    }
  }
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, models.length) }, worker));

  fs.writeFileSync(path.join(__dirname, 'catalog-probe-' + TAG + '.json'), JSON.stringify(results, null, 1) + '\n');
  const tally = {};
  for (const r of results) tally[r.verdict + '/' + (r.http || '-')] = (tally[r.verdict + '/' + (r.http || '-')] || 0) + 1;
  console.log('\nwrote catalog-probe-' + TAG + '.json');
  console.log('tally: ' + JSON.stringify(tally));
  console.log('\nUSABLE (returned a score):');
  for (const r of results.filter((x) => x.verdict === 'SCORED')) console.log('  ' + r.model.padEnd(46) + r.detail.padEnd(12) + r.ms + 'ms');
  console.log('\nother non-404 behaviour worth a second look:');
  for (const r of results.filter((x) => x.verdict !== 'SCORED' && x.http !== 404 && x.verdict !== 'PROVIDER_ERROR')) console.log('  ' + r.model.padEnd(46) + String(r.http) + ' ' + r.verdict + ' ' + String(r.detail).slice(0, 60));
})();
