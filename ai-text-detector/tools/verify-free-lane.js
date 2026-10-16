// Prove the shipped stack can verify an arbitrary text on the FREE tier, end to end:
// backend -> n8n webhook -> OpenRouter `:free` model, with the tuned parameters taken from that
// model's own DB row. This is the clause the benchmark numbers cannot cover: a capture proves a
// model scores a corpus, this proves a real request through the real pipeline returns a verdict.
//
//   node tools/verify-free-lane.js                      # two known-label texts from the corpus
//   node tools/verify-free-lane.js --text "my own text"
//   node tools/verify-free-lane.js --model some/model:free --key_env OPENROUTER_API_KEY_2
//
// The lane is overridden per request rather than assigned in the DB, so the shipped paid lanes
// are untouched. An explicit `lane_config` means the backend does NOT pull a key from the store
// (that only happens on the default path), so this tool supplies one from .env as a BYOK header.
// It never prints a key: only its length. Exit 0 = every text produced a verdict, 1 = any failed.
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const arg = (name, dflt) => {
  const i = process.argv.indexOf('--' + name);
  return i === -1 ? dflt : (process.argv[i + 1] && !process.argv[i + 1].startsWith('--') ? process.argv[i + 1] : true);
};

const BACKEND = arg('backend', 'http://127.0.0.1:8787');
const MODEL = arg('model', 'nvidia/nemotron-3-ultra-550b-a55b:free');
const KEY_ENV = arg('key_env', 'OPENROUTER_API_KEY');

function envValue(name) {
  const file = path.join(ROOT, '.env');
  if (!fs.existsSync(file)) return null;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m && m[1] === name) return m[2].replace(/^["']|["']$/g, '');
  }
  return null;
}

const texts = [];
if (typeof arg('text', null) === 'string') {
  texts.push({ id: 'inline', label: 'UNKNOWN', text: arg('text') });
} else {
  const corpus = JSON.parse(fs.readFileSync(path.join(ROOT, 'benchmark', 'corpus.json'), 'utf8'));
  const pick = (id) => corpus.find((t) => t.id === id);
  texts.push({ ...pick('ai-000'), note: 'expected AI' }, { ...pick('hu-000'), note: 'expected HUMAN' });
}

(async () => {
  const key = envValue(KEY_ENV);
  if (!key) { console.log(`FAIL  ${KEY_ENV} is not set in .env`); process.exitCode = 1; return; }
  // The endpoint comes from the running backend's own catalogue, so this never hardcodes a URL the
  // database might have moved on from.
  let baseUrl;
  try {
    const list = await (await fetch(BACKEND + '/providers')).json();
    const p = (list.providers || list).find((x) => /openrouter/i.test(x.name || ''));
    baseUrl = p && p.base_url;
  } catch (e) { console.log(`FAIL  could not read ${BACKEND}/providers: ${e.message}`); process.exitCode = 1; return; }
  if (!baseUrl) { console.log('FAIL  no openrouter provider row in the database'); process.exitCode = 1; return; }

  console.log(`backend=${BACKEND}  lane primary -> ${baseUrl}  model=${MODEL}  key=${KEY_ENV} (len ${key.length}, not printed)`);

  let allVerdicts = true;
  for (const t of texts) {
    const body = {
      text: t.text,
      document_id: 'verify-free-lane-' + t.id,
      lane_config: { primary: { base_url: baseUrl, model: MODEL } }
    };
    const t0 = Date.now();
    let resp, status;
    try {
      resp = await fetch(BACKEND + '/detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-primary-api-key': key },
        body: JSON.stringify(body),
      });
      status = resp.status;
      resp = await resp.json();
    } catch (e) {
      console.log(`${t.id.padEnd(8)} FAIL  request threw: ${e.name} ${e.message}`);
      allVerdicts = false;
      continue;
    }
    const ms = Date.now() - t0;
    const cfg = resp.config_used || {};
    const got = resp.classification || 'NONE';
    const ok = got !== 'NONE' && got !== 'DETECTION_UNAVAILABLE';
    if (!ok) allVerdicts = false;
    console.log(
      `${t.id.padEnd(8)} ${ok ? 'verdict' : 'FAIL '}  ${got.padEnd(16)}` +
      ` p=${resp.ai_probability ?? '-'}` +
      ` conf=${resp.confidence ?? '-'}` +
      ` review=${resp.review_required}` +
      ` method=${resp.method || resp.decision_method || '-'}` +
      ` model=${(cfg.primary && cfg.primary.model) || resp.model || '-'}` +
      ` id=${resp.backend_detection_id ?? '-'}` +
      ` (${ms}ms, http ${status})` +
      (t.note ? `   [${t.note}]` : '')
    );
    if (!ok && (resp.error || resp.error_code)) console.log(`         error: ${resp.error_code || ''} ${String(resp.error || '').slice(0, 160)}`);
    // The whole point of the DB-driven params clause is that the called model's row supplies the
    // cuts, so print them and let a human see they are not the workflow's baked-in defaults.
    if (resp.tuning_used || cfg.tuning) console.log(`         tuning from row: ${JSON.stringify(resp.tuning_used || cfg.tuning)}`);
  }
  process.exitCode = allVerdicts ? 0 : 1;
  console.log(allVerdicts ? 'PASS  every text came back with a verdict from the free lane' : 'FAIL  at least one text produced no verdict');
})();
