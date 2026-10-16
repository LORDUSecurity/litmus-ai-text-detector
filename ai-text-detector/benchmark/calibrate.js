// Calibrate the workflow's three-way decision thresholds against measured benchmark scores.
// Grid-searches the same logic Ensemble & Build Result uses on the single-model path.
//
// Usage: node calibrate.js results-<tag>.jsonl [--max-review 0.20] [--top 10]

const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const FILE = argv.find((a) => !a.startsWith('--'));
function num(name, dflt) {
  const i = argv.indexOf('--' + name);
  return i >= 0 ? parseFloat(argv[i + 1]) : dflt;
}
const MAX_REVIEW = num('max-review', 0.20);
const TOP = num('top', 8);

const rows = fs.readFileSync(path.join(__dirname, FILE), 'utf8')
  .split('\n').filter(Boolean).map((l) => JSON.parse(l))
  .filter((r) => r.ok && r.ai_probability !== null && r.confidence !== null);

if (!rows.length) { console.error('no usable rows in ' + FILE); process.exit(1); }

const P = rows.map((r) => r.ai_probability);
const C = rows.map((r) => r.confidence);
const IS_AI = rows.map((r) => r.label === 'AI');

function evaluate(aiT, huT, confT, penalty) {
  let tp = 0, tn = 0, fp = 0, fn = 0, review = 0;
  for (let i = 0; i < rows.length; i++) {
    const c = C[i] * penalty;
    let pred;
    if (P[i] >= aiT && c >= confT) pred = 'AI';
    else if (P[i] <= huT && c >= confT) pred = 'HUMAN';
    else pred = 'REVIEW';
    if (pred === 'REVIEW') { review++; continue; }
    if (pred === 'AI' && IS_AI[i]) tp++;
    else if (pred === 'AI' && !IS_AI[i]) fp++;
    else if (pred === 'HUMAN' && !IS_AI[i]) tn++;
    else fn++;
  }
  const n = rows.length;
  const decided = tp + tn + fp + fn;
  return {
    aiT, huT, confT, penalty,
    tp, tn, fp, fn, review,
    accuracy: (tp + tn) / n,                        // REVIEW counts as wrong
    precision_decided: decided ? (tp + tn) / decided : 0,
    review_rate: review / n,
    tpr: tp + fn ? tp / (tp + fn) : 0,               // recall on AI
    tnr: tn + fp ? tn / (tn + fp) : 0                // specificity on HUMAN
  };
}

const grid = [];
for (let aiT = 0.40; aiT <= 0.951; aiT += 0.01) {
  for (let huT = 0.05; huT <= 0.501; huT += 0.01) {
    if (huT >= aiT) continue;
    for (let confT = 0.0; confT <= 0.901; confT += 0.05) {
      for (let penalty = 0.70; penalty <= 1.001; penalty += 0.05) {
        grid.push(evaluate(+aiT.toFixed(2), +huT.toFixed(2), +confT.toFixed(2), +penalty.toFixed(2)));
      }
    }
  }
}

const r2 = (x) => Math.round(x * 10000) / 10000;
const fmt = (g) => 'ai>=' + g.aiT.toFixed(2) + ' human<=' + g.huT.toFixed(2) +
  ' conf>=' + g.confT.toFixed(2) + ' penalty=' + g.penalty.toFixed(2) +
  '  =>  acc=' + r2(g.accuracy) + ' decided_prec=' + r2(g.precision_decided) +
  ' review=' + r2(g.review_rate) + ' AI_recall=' + r2(g.tpr) + ' HUMAN_spec=' + r2(g.tnr) +
  '  [tp' + g.tp + ' tn' + g.tn + ' fp' + g.fp + ' fn' + g.fn + ' rev' + g.review + ']';

console.log('='.repeat(78));
console.log('CALIBRATION on ' + FILE + '  (' + rows.length + ' usable rows)');
console.log('grid size: ' + grid.length + ' threshold combinations');
console.log('='.repeat(78));

const shipped = evaluate(0.85, 0.20, 0.70, 0.85);
console.log('\nSHIPPED CONFIG  ' + fmt(shipped));

const bestAcc = grid.reduce((a, b) => (b.accuracy > a.accuracy ||
  (b.accuracy === a.accuracy && b.review_rate < a.review_rate)) ? b : a);
console.log('\nBEST RAW ACCURACY (REVIEW counted as wrong)');
console.log('  ' + fmt(bestAcc));

// Pareto: best decided-precision at each review-rate cap.
console.log('\nPARETO FRONTIER - best precision at each review-rate cap');
console.log('  (precision = of the calls it actually decided, how many were right)');
for (const cap of [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50]) {
  const pool = grid.filter((g) => g.review_rate <= cap + 1e-9 && g.tp + g.tn + g.fp + g.fn > 0);
  if (!pool.length) { console.log('  review<=' + cap.toFixed(2) + '  (no config satisfies)'); continue; }
  const best = pool.reduce((a, b) => (b.precision_decided > a.precision_decided ? b : a));
  console.log('  review<=' + cap.toFixed(2) + '  ' + fmt(best));
}

// Balanced: require both AI recall and HUMAN specificity to be decent.
const balanced = grid
  .filter((g) => g.review_rate <= MAX_REVIEW && g.tp + g.tn + g.fp + g.fn > 0)
  .map((g) => ({ ...g, obj: Math.min(g.tpr, g.tnr) + g.accuracy }))
  .sort((a, b) => b.obj - a.obj);

console.log('\nRECOMMENDED (review_rate <= ' + MAX_REVIEW + ', optimises min(AI_recall, HUMAN_spec) + accuracy)');
for (const g of balanced.slice(0, TOP)) {
  console.log('  ' + fmt(g) + '   obj=' + r2(g.obj));
}

// ---- named operating points, evaluated on the full set AND on stratified halves ----
const CANDIDATES = [
  { name: 'F shipped        ', aiT: 0.85, huT: 0.20, confT: 0.70, penalty: 0.85 },
  { name: 'A balanced       ', aiT: 0.45, huT: 0.42, confT: 0.00, penalty: 1.00 },
  { name: 'B balanced+conf  ', aiT: 0.45, huT: 0.42, confT: 0.60, penalty: 1.00 },
  { name: 'C high-precision ', aiT: 0.69, huT: 0.45, confT: 0.00, penalty: 1.00 },
  { name: 'D highprec+conf  ', aiT: 0.69, huT: 0.35, confT: 0.70, penalty: 1.00 },
  { name: 'E max-precision  ', aiT: 0.79, huT: 0.38, confT: 0.00, penalty: 1.00 },
  // The band fitted to the nemotron-3-ultra-550b-a55b:free capture and stored on its own row in
  // `models` (2026-10-07). Listed as a named point so its fold swing is printed on every corpus,
  // not just the one it was fitted on -- a band chosen on 48 texts is in-sample by construction.
  { name: 'G free-mirror band', aiT: 0.56, huT: 0.45, confT: 0.00, penalty: 1.00 }
];

// Class-stratified halves (corpus alternates AI/HUMAN, so index parity would be degenerate).
const foldA = [], foldB = [];
for (const label of ['AI', 'HUMAN']) {
  const g = rows.filter((r) => r.label === label);
  g.forEach((r, i) => (i % 2 === 0 ? foldA : foldB).push(r));
}
function evalSet(set, c) {
  let tp = 0, tn = 0, fp = 0, fn = 0, rev = 0;
  for (const r of set) {
    const cf = r.confidence * c.penalty;
    let pred;
    if (r.ai_probability >= c.aiT && cf >= c.confT) pred = 'AI';
    else if (r.ai_probability <= c.huT && cf >= c.confT) pred = 'HUMAN';
    else pred = 'REVIEW';
    if (pred === 'REVIEW') { rev++; continue; }
    const isAI = r.label === 'AI';
    if (pred === 'AI' && isAI) tp++; else if (pred === 'AI') fp++;
    else if (pred === 'HUMAN' && !isAI) tn++; else fn++;
  }
  const dec = tp + tn + fp + fn;
  return {
    acc: (tp + tn) / set.length,
    prec: dec ? (tp + tn) / dec : 0,
    rev: rev / set.length,
    tpr: tp + fn ? tp / (tp + fn) : 0,
    tnr: tn + fp ? tn / (tn + fp) : 0,
    fp: fp, fn: fn
  };
}
const p3 = (x) => (Math.round(x * 1000) / 1000).toFixed(3);
console.log('\n' + '='.repeat(78));
console.log('NAMED OPERATING POINTS  (all = full set; A/B = stratified halves)');
console.log('='.repeat(78));
console.log('candidate          ai>=   human<= conf>= pen   | all: acc  prec  review AIrec HUMspec fp fn | foldA acc | foldB acc');
for (const c of CANDIDATES) {
  const all = evalSet(rows, c), a = evalSet(foldA, c), b = evalSet(foldB, c);
  console.log(c.name + ' ' + c.aiT.toFixed(2) + '   ' + c.huT.toFixed(2) + '    ' +
    c.confT.toFixed(2) + '  ' + c.penalty.toFixed(2) + '  |      ' +
    p3(all.acc) + ' ' + p3(all.prec) + ' ' + p3(all.rev) + '  ' + p3(all.tpr) + '  ' + p3(all.tnr) +
    '   ' + String(all.fp).padStart(2) + ' ' + String(all.fn).padStart(2) +
    '  |   ' + p3(a.acc) + '   |   ' + p3(b.acc));
}
