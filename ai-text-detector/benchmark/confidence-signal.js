// Does the model's self-reported `confidence` carry any usable signal?
// Tests two things:
//   1. discrimination - does confidence differ between AI and HUMAN texts? (if not, it is
//      not evidence about the class)
//   2. calibration    - does confidence predict whether the verdict was CORRECT?
//      This is the only reason to gate on confidence.
//
// Usage: node confidence-signal.js results-<tag>.jsonl [...]

const fs = require('fs');
const path = require('path');

const files = process.argv.slice(2);
if (!files.length) { console.error('pass result files'); process.exit(1); }

function mean(xs) { return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0; }
function sd(xs) { const m = mean(xs); return xs.length > 1 ? Math.sqrt(xs.reduce((a, b) => a + (b - m) ** 2, 0) / (xs.length - 1)) : 0; }

function auc(pairs) {
  // pairs: [{score, positive:boolean}]
  const pos = pairs.filter((p) => p.positive).map((p) => p.score);
  const neg = pairs.filter((p) => !p.positive).map((p) => p.score);
  if (!pos.length || !neg.length) return null;
  let s = 0;
  for (const p of pos) for (const n of neg) s += p > n ? 1 : (p === n ? 0.5 : 0);
  return +(s / (pos.length * neg.length)).toFixed(4);
}

for (const f of files) {
  const rows = fs.readFileSync(path.join(__dirname, f), 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l))
    .filter((r) => r.ok && r.ai_probability !== null && r.confidence !== null);

  console.log('\n' + '='.repeat(74));
  console.log(f + '  (' + rows.length + ' usable)');
  console.log('='.repeat(74));

  const aiC = rows.filter((r) => r.label === 'AI').map((r) => r.confidence);
  const huC = rows.filter((r) => r.label === 'HUMAN').map((r) => r.confidence);
  console.log('1) DISCRIMINATION  confidence by true class');
  console.log('   AI    mean=' + mean(aiC).toFixed(3) + ' sd=' + sd(aiC).toFixed(3));
  console.log('   HUMAN mean=' + mean(huC).toFixed(3) + ' sd=' + sd(huC).toFixed(3));
  console.log('   AUC(confidence -> class) = ' + auc(rows.map((r) => ({ score: r.confidence, positive: r.label === 'AI' }))));
  console.log('   (near 0.50 means confidence says nothing about which class the text is)');

  // Correctness at the model's own best binary threshold (0.41 for 550B, 0.46 for 120B).
  for (const t of [0.41, 0.46, 0.50]) {
    const scored = rows.map((r) => ({ ...r, predAI: r.ai_probability >= t }));
    const correct = scored.filter((r) => r.predAI === (r.label === 'AI'));
    const wrong = scored.filter((r) => r.predAI !== (r.label === 'AI'));
    const cC = correct.map((r) => r.confidence), wC = wrong.map((r) => r.confidence);
    const a = auc(scored.map((r) => ({ score: r.confidence, positive: r.predAI === (r.label === 'AI') })));
    console.log('\n2) CALIBRATION at decision threshold ' + t.toFixed(2));
    console.log('   correct verdicts (n=' + cC.length + ') mean confidence=' + mean(cC).toFixed(3));
    console.log('   wrong   verdicts (n=' + wC.length + ') mean confidence=' + mean(wC).toFixed(3));
    console.log('   AUC(confidence -> was correct) = ' + a);
    console.log('   separation = ' + (mean(cC) - mean(wC)).toFixed(3));
  }

  // Reliability buckets: within each confidence band, what fraction of verdicts were right?
  console.log('\n3) RELIABILITY by confidence band (threshold 0.41)');
  const bands = [[0, 0.65], [0.65, 0.72], [0.72, 0.80], [0.80, 0.86], [0.86, 1.01]];
  for (const [lo, hi] of bands) {
    const b = rows.filter((r) => r.confidence >= lo && r.confidence < hi);
    if (!b.length) { console.log('   [' + lo.toFixed(2) + ',' + hi.toFixed(2) + ')  n=0'); continue; }
    const right = b.filter((r) => (r.ai_probability >= 0.41) === (r.label === 'AI')).length;
    console.log('   [' + lo.toFixed(2) + ',' + hi.toFixed(2) + ')  n=' + String(b.length).padStart(2) +
      '  accuracy=' + (right / b.length).toFixed(3));
  }
}
