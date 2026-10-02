// Fire real webhook requests at the live n8n workflow using labeled corpus texts,
// so the tuned thresholds can be checked end-to-end rather than in simulation.
// Usage: node live-webhook-test.js ai-004 hu-002 ai-000 hu-015
const fs = require('fs');
const path = require('path');

const URL = 'http://localhost:5678/webhook/ai-detect-ai';
const CONCURRENCY = parseInt(process.env.CONCURRENCY || '1', 10);
const QUIET = process.env.QUIET === '1';
const corpus = new Map(JSON.parse(fs.readFileSync(path.join(__dirname, 'corpus.json'), 'utf8')).map((c) => [c.id, c]));
let wanted = process.argv.slice(2);
if (wanted[0] === '--all') wanted = [...corpus.keys()];
if (!wanted.length) { console.error('usage: node live-webhook-test.js <id> ... | --all'); process.exit(1); }

async function post(id) {
  const item = corpus.get(id);
  if (!item) throw new Error('no corpus entry for ' + id);
  const t0 = Date.now();
  const res = await fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: item.text, source: 'hc3-live-test', text_id: id }),
  });
  const body = await res.json();
  const ms = Date.now() - t0;
  const e = body.evidence || {};
  const cs = body.component_scores || {};
  if (QUIET) {
    return { id, label: item.label, cls: body.classification, method: body.method, ms,
      prob: body.ai_probability, conf: body.confidence, sec: cs.secondary_called, verdict: verdictFor(item.label, body.classification) };
  }
  console.log('\n=== ' + id + '  true_label=' + item.label + '  (' + item.text.length + ' chars, ' + ms + 'ms, HTTP ' + res.status + ')');
  console.log('  classification = ' + body.classification + '   method = ' + body.method);
  console.log('  ai_probability = ' + body.ai_probability + '   confidence = ' + body.confidence +
    '   disagreement = ' + (cs.model_disagreement === null || cs.model_disagreement === undefined ? 'n/a' : cs.model_disagreement));
  console.log('  secondary_called = ' + cs.secondary_called + '   weights = ' + JSON.stringify(cs.weights_used) +
    '   threshold_version = ' + ((body.versions || {}).thresholds));
  console.log('  review_required = ' + body.review_required);
  const models = e.models || [];
  console.log('  lanes: ' + models.map((m) => m.lane + '/' + (m.model || '-').split('/').pop() +
    ' ok=' + m.ok + (m.skipped ? ' SKIPPED' : '') + (m.result ? ' p=' + m.result.ai_probability + ' c=' + m.result.confidence : '')).join('  |  '));
  if (body.warnings && body.warnings.length) console.log('  warnings: ' + JSON.stringify(body.warnings));
  const verdict = verdictFor(item.label, body.classification);
  console.log('  --> ' + verdict);
  return { id, label: item.label, cls: body.classification, method: body.method, verdict };
}

function verdictFor(label, cls) {
  if (cls === 'REVIEW_REQUIRED') return 'DEFERRED';
  if ((label === 'AI' && cls === 'AI_GENERATED') || (label === 'HUMAN' && cls === 'HUMAN_LIKELY')) return 'CORRECT';
  return 'WRONG';
}

async function main() {
  const out = [];
  const queue = [...wanted];
  async function worker() {
    while (queue.length) {
      const id = queue.shift();
      try { out.push(await post(id)); }
      catch (err) {
        if (!QUIET) console.log('\n=== ' + id + '  FAILED: ' + err.message);
        out.push({ id, label: (corpus.get(id) || {}).label, cls: 'ERROR', verdict: 'ERROR', err: err.message });
      }
    }
  }
  const t0 = Date.now();
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  const secs = ((Date.now() - t0) / 1000).toFixed(1);

  out.sort((a, b) => a.id.localeCompare(b.id));
  console.log('\n' + '='.repeat(64));
  for (const r of out) console.log('  ' + r.id + '  true=' + (r.label || '?') + '  ' + (r.cls || '-') + '  ' + r.verdict);

  const n = out.length;
  const rev = out.filter((r) => r.cls === 'REVIEW_REQUIRED').length;
  const err = out.filter((r) => r.verdict === 'ERROR').length;
  const dec = out.filter((r) => r.verdict === 'CORRECT' || r.verdict === 'WRONG');
  const correct = out.filter((r) => r.verdict === 'CORRECT').length;
  const wrong = out.filter((r) => r.verdict === 'WRONG').length;
  const ai = out.filter((r) => r.label === 'AI');
  const hu = out.filter((r) => r.label === 'HUMAN');
  const secCalls = out.filter((r) => r.sec === true).length;
  console.log('\nLIVE END-TO-END over ' + n + ' texts in ' + secs + 's (concurrency ' + CONCURRENCY + ')');
  console.log('  accuracy (all)        = ' + (correct / n).toFixed(3) + '   (' + correct + '/' + n + ')');
  console.log('  precision (decided)   = ' + (dec.length ? (correct / dec.length).toFixed(3) : 'n/a') + '   (' + correct + '/' + dec.length + ')');
  console.log('  review rate           = ' + (rev / n).toFixed(3) + '   (' + rev + '/' + n + ')');
  console.log('  errors                = ' + err);
  console.log('  wrong verdicts        = ' + wrong + '  -> ' + (out.filter((r) => r.verdict === 'WRONG').map((r) => r.id + '(true ' + r.label + ' got ' + r.cls + ')').join(', ') || 'none'));
  console.log('  AI texts   decided/correct = ' + ai.filter((r) => r.verdict !== 'DEFERRED').length + '/' + ai.filter((r) => r.verdict === 'CORRECT').length + '  (of ' + ai.length + ')');
  console.log('  HUMAN texts decided/correct = ' + hu.filter((r) => r.verdict !== 'DEFERRED').length + '/' + hu.filter((r) => r.verdict === 'CORRECT').length + '  (of ' + hu.length + ')');
  if (QUIET) console.log('  secondary lane fired on ' + secCalls + '/' + n);
}

main();
