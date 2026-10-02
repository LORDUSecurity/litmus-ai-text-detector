// Holdout validation: calibrate thresholds on one half of the corpus, evaluate on the
// other half, both directions. Guards against overfitting thresholds to 48 texts.
//
// Usage: node validate-holdout.js results-<tag>.jsonl

const fs = require('fs');
const path = require('path');

const FILE = process.argv[2];
if (!FILE) { console.error('pass a result file'); process.exit(1); }

const rows = fs.readFileSync(path.join(__dirname, FILE), 'utf8')
  .split('\n').filter(Boolean).map((l) => JSON.parse(l))
  .filter((r) => r.ok && r.ai_probability !== null && r.confidence !== null);

function evaluate(set, aiT, huT, confT, penalty) {
  let tp = 0, tn = 0, fp = 0, fn = 0, review = 0;
  for (const r of set) {
    const c = r.confidence * penalty;
    let pred;
    if (r.ai_probability >= aiT && c >= confT) pred = 'AI';
    else if (r.ai_probability <= huT && c >= confT) pred = 'HUMAN';
    else pred = 'REVIEW';
    if (pred === 'REVIEW') { review++; continue; }
    const isAI = r.label === 'AI';
    if (pred === 'AI' && isAI) tp++;
    else if (pred === 'AI' && !isAI) fp++;
    else if (pred === 'HUMAN' && !isAI) tn++;
    else fn++;
  }
  const n = set.length;
  const decided = tp + tn + fp + fn;
  return {
    accuracy: (tp + tn) / n,
    precision: decided ? (tp + tn) / decided : 0,
    review_rate: review / n,
    tpr: tp + fn ? tp / (tp + fn) : 0,
    tnr: tn + fp ? tn / (tn + fp) : 0,
    tp, tn, fp, fn, review
  };
}

// Search the same grid as calibrate.js, optimising accuracy with a review-rate cap.
function bestConfig(train, maxReview) {
  let best = null;
  for (let aiT = 0.30; aiT <= 0.95; aiT += 0.01) {
    for (let huT = 0.05; huT <= 0.60; huT += 0.01) {
      if (huT >= aiT) continue;
      for (const confT of [0.0, 0.5, 0.6, 0.65, 0.7]) {
        for (const penalty of [0.7, 0.85, 1.0]) {
          const m = evaluate(train, +aiT.toFixed(2), +huT.toFixed(2), confT, penalty);
          if (m.review_rate > maxReview) continue;
          const obj = m.accuracy + Math.min(m.tpr, m.tnr) * 0.5;
          if (!best || obj > best.obj) best = { aiT: +aiT.toFixed(2), huT: +huT.toFixed(2), confT, penalty, obj, train: m };
        }
      }
    }
  }
  return best;
}

const r3 = (x) => (Math.round(x * 1000) / 1000).toFixed(3);
const show = (m) => 'acc=' + r3(m.accuracy) + ' prec=' + r3(m.precision) +
  ' review=' + r3(m.review_rate) + ' AI_recall=' + r3(m.tpr) + ' HUMAN_spec=' + r3(m.tnr) +
  ' [tp' + m.tp + ' tn' + m.tn + ' fp' + m.fp + ' fn' + m.fn + ' rev' + m.review + ']';

console.log('='.repeat(76));
console.log('HOLDOUT VALIDATION  ' + FILE + '  (' + rows.length + ' usable rows)');
console.log('='.repeat(76));

// Class-stratified split. The corpus alternates AI/HUMAN by construction, so splitting
// on index parity would put every AI text in one fold and every HUMAN text in the other.
// Instead, deal alternately within each class so both folds are balanced.
const A = [], B = [];
for (const label of ['AI', 'HUMAN']) {
  const group = rows.filter((r) => r.label === label);
  group.forEach((r, i) => (i % 2 === 0 ? A : B).push(r));
}
console.log('fold A n=' + A.length + ' (AI ' + A.filter((r) => r.label === 'AI').length + ')');
console.log('fold B n=' + B.length + ' (AI ' + B.filter((r) => r.label === 'AI').length + ')');

for (const maxReview of [0.0, 0.15, 0.30]) {
  console.log('\n--- review-rate cap ' + maxReview.toFixed(2) + ' ---');
  for (const [trName, tr, teName, te] of [['A', A, 'B', B], ['B', B, 'A', A]]) {
    const cfg = bestConfig(tr, maxReview);
    if (!cfg) { console.log('  train ' + trName + ': no config satisfies cap'); continue; }
    const held = evaluate(te, cfg.aiT, cfg.huT, cfg.confT, cfg.penalty);
    console.log('  tuned on ' + trName + ' -> ai>=' + cfg.aiT.toFixed(2) + ' human<=' + cfg.huT.toFixed(2) +
      ' conf>=' + cfg.confT.toFixed(2) + ' penalty=' + cfg.penalty.toFixed(2));
    console.log('    train ' + trName + ': ' + show(cfg.train));
    console.log('    HELDOUT ' + teName + ': ' + show(held));
  }
}

// In-sample vs out-of-sample gap for the shipped config, for reference.
const shipped = { aiT: 0.85, huT: 0.20, confT: 0.70, penalty: 0.85 };
console.log('\n--- shipped config on each fold (for reference) ---');
console.log('  fold A: ' + show(evaluate(A, shipped.aiT, shipped.huT, shipped.confT, shipped.penalty)));
console.log('  fold B: ' + show(evaluate(B, shipped.aiT, shipped.huT, shipped.confT, shipped.penalty)));
console.log('  all   : ' + show(evaluate(rows, shipped.aiT, shipped.huT, shipped.confT, shipped.penalty)));
