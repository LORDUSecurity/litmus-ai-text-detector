// OmniRoute multiplexes many *provider accounts* behind one /v1 endpoint, and the accounts
// fail independently: one can be live (kiro -> HTTP 200) while another is a dead key
// (lmarena -> 401 "User not found"), a paid-only route (opencode -> 402), or blocked on a
// missing browser binary (gemini-web -> 503 "requires the Playwright Chromium browser").
// GET /v1/models lists all of them together, so a catalogue count is NOT a usable-model
// count. This script answers "which provider prefixes actually serve?" before any budget is
// spent probing 1600 models.
//
//   node omniroute-providers.js [--per 2] [--concurrency 2] [--timeout 90] [--tag omni]
//   node omniroute-providers.js --filter '^(kiro|mistral)/'
//
// Uses the SAME system prompt and user template as catalog-probe.js, verbatim: a shortened
// prompt moves a model's score enough to cross a decision band, so numbers from a probe that
// paraphrases the prompt are not comparable to results-*.jsonl.
//
// Writes omniroute-providers-<tag>.json. Never prints the API key.

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
const PER = parseInt(arg('per', '2'), 10);
const CONCURRENCY = parseInt(arg('concurrency', '2'), 10);
const TIMEOUT_MS = parseInt(arg('timeout', '90'), 10) * 1000;
const FILTER = arg('filter', null);
const TAG = arg('tag', 'omni');
const BASE_URL = env[arg('base_env', 'OMNIROUTE_BASE_URL')];
const API_KEY = env[arg('key_env', 'OMNIROUTE_API_KEY')];

if (!BASE_URL || !API_KEY) {
  console.error('OMNIROUTE_BASE_URL / OMNIROUTE_API_KEY must be set (uncommented) in ../.env');
  process.exit(2);
}

const SYSTEM = 'You are an AI text detection classifier. Return ONLY a compact JSON object. No markdown, no code fences, no reasoning text. Schema: {"ai_probability": number between 0 and 1, "confidence": number between 0 and 1}. Under 180 tokens.';

// Same grading as catalog-probe.js so the two tools' verdicts mean the same thing.
function grade(content) {
  if (typeof content !== 'string' || !content) return { verdict: 'EMPTY', detail: 'no string content' };
  const stripped = content.replace(/```json/gi, '```').replace(/```/g, '').trim();
  const m = stripped.match(/"ai_probability"\s*:\s*(-?\d*\.?\d+)/);
  if (m) return { verdict: 'SCORED', detail: 'p=' + m[1] };
  if (/thinking process|let me analyze|\*\*Analyze/i.test(stripped)) return { verdict: 'REASONING_PREAMBLE', detail: stripped.slice(0, 70).replace(/\s+/g, ' ') };
  return { verdict: 'NO_JSON', detail: stripped.slice(0, 70).replace(/\s+/g, ' ') };
}

async function listModels() {
  const res = await fetch(BASE_URL + '/models', { headers: { Authorization: 'Bearer ' + API_KEY } });
  if (!res.ok) throw new Error('GET /models -> HTTP ' + res.status);
  const j = await res.json();
  return (j.data || []).map((d) => d.id);
}

async function probe(model, text) {
  const rec = { model, started: new Date().toISOString() };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  const t0 = Date.now();
  try {
    const res = await fetch(BASE_URL + '/chat/completions', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, messages: [{ role: 'system', content: SYSTEM }, { role: 'user', content: 'Judge this text:\n"""\n' + text + '\n"""' }], temperature: 0.1, max_tokens: 500 }),
      signal: ctrl.signal
    });
    rec.http = res.status;
    const raw = await res.text();
    let j = null;
    try { j = JSON.parse(raw); } catch (e) { /* non-JSON body */ }
    if (j && j.error) {
      rec.verdict = 'PROVIDER_ERROR';
      rec.detail = String(j.error.message || raw).slice(0, 160);
    } else if (res.ok && j) {
      const msg = (j.choices || [])[0] && (j.choices || [])[0].message;
      const g = grade(msg && msg.content);
      rec.verdict = g.verdict;
      rec.detail = g.detail;
      rec.finish = (j.choices || [])[0] && (j.choices || [])[0].finish_reason;
    } else {
      rec.verdict = 'HTTP_' + res.status;
      rec.detail = raw.slice(0, 160);
    }
  } catch (e) {
    rec.verdict = e.name === 'AbortError' ? 'TIMEOUT' : 'EXCEPTION';
    rec.detail = (e.message || String(e)).slice(0, 160);
  } finally {
    clearTimeout(timer);
  }
  rec.ms = Date.now() - t0;
  return rec;
}

async function mapLimit(items, n, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const k = i++; out[k] = await fn(items[k], k); }
  }));
  return out;
}

(async () => {
  const corpus = JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus.json'), 'utf8'));
  const text = (corpus.find((c) => c.id === 'ai-000') || corpus[0]).text;

  let ids = await listModels();
  const total = ids.length;
  if (FILTER) ids = ids.filter((id) => new RegExp(FILTER).test(id));

  const groups = new Map();
  for (const id of ids) {
    const p = id.includes('/') ? id.split('/')[0] : '(bare)';
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(id);
  }
  const prefixes = [...groups.keys()].sort((a, b) => groups.get(b).length - groups.get(a).length);
  console.log('catalogue total=' + total + ', in scope=' + ids.length + ', prefixes=' + prefixes.length +
              ', per prefix=' + PER + ', concurrency=' + CONCURRENCY + ', timeout=' + (TIMEOUT_MS / 1000) + 's');

  const jobs = [];
  for (const p of prefixes) for (const m of groups.get(p).slice(0, PER)) jobs.push({ prefix: p, model: m });

  const results = await mapLimit(jobs, CONCURRENCY, async (j) => {
    const r = await probe(j.model, text);
    r.prefix = j.prefix;
    console.log('  ' + String(r.prefix).padEnd(14) + String(r.model).padEnd(46) +
                String(r.verdict).padEnd(20) + String(r.http || '-').padEnd(5) +
                (r.ms / 1000).toFixed(1) + 's  ' + String(r.detail || '').slice(0, 90));
    return r;
  });

  // A prefix is SERVABLE if at least one probe produced a number. Everything else is
  // reported with the reason, because "dead" and "needs a browser binary" have different fixes.
  const summary = prefixes.map((p) => {
    const rs = results.filter((r) => r.prefix === p);
    const scored = rs.filter((r) => r.verdict === 'SCORED');
    return {
      prefix: p,
      models_listed: groups.get(p).length,
      probed: rs.length,
      scored: scored.length,
      status: scored.length ? 'SERVABLE' : (rs[0] ? rs[0].verdict : 'NOT_PROBED'),
      reason: scored.length ? null : (rs[0] ? rs[0].detail : null),
      sample_model: scored.length ? scored[0].model : (rs[0] ? rs[0].model : null)
    };
  }).sort((a, b) => (b.scored - a.scored) || (b.models_listed - a.models_listed));

  console.log('\nprefix         listed  probed  scored  status        reason');
  for (const s of summary) {
    console.log(String(s.prefix).padEnd(14) + String(s.models_listed).padEnd(8) + String(s.probed).padEnd(8) +
                String(s.scored).padEnd(8) + String(s.status).padEnd(14) + String(s.reason || '').slice(0, 100));
  }

  const out = path.join(__dirname, 'omniroute-providers-' + TAG + '.json');
  fs.writeFileSync(out, JSON.stringify({ generated: new Date().toISOString(), base_url: BASE_URL,
    catalogue_total: total, in_scope: ids.length, per_prefix: PER, summary, probes: results }, null, 2));
  console.log('\nwrote ' + out);
})();
