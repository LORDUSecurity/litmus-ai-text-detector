// Paired comparison across benchmark runs: joins by text id so the SAME text is
// compared between models/prompts/params. Reports per-run AUC on the shared subset,
// test-retest drift, and where each configuration wins.
//
// Usage: node compare.js results-a.jsonl results-b.jsonl [results-c.jsonl ...]

const fs = require('fs');
const path = require('path');

const files = process.argv.slice(2);
if (files.length < 2) { console.error('pass at least two result files'); process.exit(1); }

const runs = files.map((f) => {
  const rows = fs.readFileSync(path.join(__dirname, f), 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l));
  const byId = new Map();
  for (const r of rows) byId.set(r.id, r);
  return { file: f, tag: f.replace(/^results-/, '').replace(/\.jsonl$/, ''), byId, rows };
});

// Shared ids where every run produced a usable score.
const allIds = [...runs[0].byId.keys()];
const shared = allIds.filter((id) => runs.every((r) => {
  const x = r.byId.get(id);
  return x && x.ok && x.ai_probability !== null;
}));

console.log('='.repeat(78));
console.log('PAIRED COMPARISON over ' + shared.length + ' texts scored by all ' + runs.length + ' runs');
console.log('='.repeat(78));

function aucOf(run, ids) {
  const pos = [], neg = [];
  for (const id of ids) {
    const r = run.byId.get(id);
    (r.label === 'AI' ? pos : neg).push(r.ai_probability);
  }
  if (!pos.length || !neg.length) return null;
  let s = 0;
  for (const p of pos) for (const n of neg) s += p > n ? 1 : (p === n ? 0.5 : 0);
  return +(s / (pos.length * neg.length)).toFixed(4);
}
function mean(xs) { return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0; }

console.log('\nRun           AUC(shared)  meanAI  meanHUMAN  gap     bestAcc  @t');
for (const run of runs) {
  const ai = shared.filter((id) => run.byId.get(id).label === 'AI').map((id) => run.byId.get(id).ai_probability);
  const hu = shared.filter((id) => run.byId.get(id).label === 'HUMAN').map((id) => run.byId.get(id).ai_probability);
  let bestAcc = 0, bestT = 0;
  for (let t = 0; t <= 1.0001; t += 0.01) {
    let c = 0;
    for (const id of shared) {
      const r = run.byId.get(id);
      if ((r.ai_probability >= t) === (r.label === 'AI')) c++;
    }
    if (c / shared.length > bestAcc) { bestAcc = c / shared.length; bestT = t; }
  }
  console.log(run.tag.padEnd(13) + String(aucOf(run, shared)).padEnd(12) +
    mean(ai).toFixed(3).padEnd(8) + mean(hu).toFixed(3).padEnd(11) +
    (mean(ai) - mean(hu)).toFixed(3).padEnd(8) + bestAcc.toFixed(3).padEnd(9) + bestT.toFixed(2));
}

// Pairwise: how often does each run beat the other per text?
console.log('\nPairwise per-text wins (correct vs incorrect on the same text):');
for (let i = 0; i < runs.length; i++) {
  for (let j = i + 1; j < runs.length; j++) {
    const A = runs[i], B = runs[j];
    let aWins = 0, bWins = 0, tie = 0;
    for (const id of shared) {
      const ra = A.byId.get(id), rb = B.byId.get(id);
      const isAI = ra.label === 'AI';
      // distance from the ideal score (1 for AI, 0 for HUMAN)
      const da = Math.abs(ra.ai_probability - (isAI ? 1 : 0));
      const db = Math.abs(rb.ai_probability - (isAI ? 1 : 0));
      if (Math.abs(da - db) < 1e-9) tie++;
      else if (da < db) aWins++; else bWins++;
    }
    console.log('  ' + A.tag + ' better on ' + aWins + ' | ' + B.tag + ' better on ' + bWins + ' | tie ' + tie);
  }
}

// Test-retest drift for runs sharing identical params (same model+prompt+temp).
console.log('\nScore drift per text (max - min across runs), distribution:');
const drifts = shared.map((id) => {
  const vals = runs.map((r) => r.byId.get(id).ai_probability);
  return { id, label: runs[0].byId.get(id).label, min: Math.min(...vals), max: Math.max(...vals), d: Math.max(...vals) - Math.min(...vals) };
}).sort((a, b) => b.d - a.d);
const dm = drifts.map((x) => x.d);
console.log('  mean drift=' + mean(dm).toFixed(3) + '  median=' + dm[Math.floor(dm.length / 2)].toFixed(3) +
  '  max=' + dm[0].toFixed(3) + '  texts with drift>0.30: ' + dm.filter((x) => x > 0.3).length + '/' + dm.length);
console.log('  worst 8:');
for (const x of drifts.slice(0, 8)) {
  console.log('    ' + x.id + ' ' + x.label.padEnd(5) + ' range ' + x.min.toFixed(2) + '-' + x.max.toFixed(2) +
    '  ' + runs.map((r) => r.tag + '=' + r.byId.get(x.id).ai_probability.toFixed(2)).join(' '));
}
