// Simulator for the RETIRED weighted-blend policy (score = w*primary + (1-w)*secondary).
// The AI-only workflow stopped blending across lanes on 2026-10-06; run
// `node ../n8n/test-ensemble.js replay <primary> <secondary>` to replay the policy that
// actually ships. This file is kept because the blend's failure is the evidence the
// tie-break design rests on, and it needs no API calls.
//
// Usage: node ensemble-sim.js results-ultra550.jsonl results-baseline.jsonl

const fs = require('fs');
const path = require('path');

const [fileA, fileB] = process.argv.slice(2);
if (!fileA || !fileB) { console.error('usage: node ensemble-sim.js <primary.jsonl> <secondary.jsonl>'); process.exit(1); }

function load(f) {
  const m = new Map();
  for (const l of fs.readFileSync(path.join(__dirname, f), 'utf8').split('\n').filter(Boolean)) {
    const r = JSON.parse(l);
    if (r.ok && r.ai_probability !== null) m.set(r.id, r);
  }
  return m;
}
const A = load(fileA);   // primary lane
const B = load(fileB);   // secondary lane
const ids = [...A.keys()].filter((id) => B.has(id));

console.log('='.repeat(76));
console.log('ENSEMBLE SIMULATION on ' + ids.length + ' texts scored by both models');
console.log('  primary  (A) = ' + fileA);
console.log('  secondary(B) = ' + fileB);
console.log('='.repeat(76));

const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
function auc(items) {
  const pos = items.filter((i) => i.isAI).map((i) => i.score);
  const neg = items.filter((i) => !i.isAI).map((i) => i.score);
  if (!pos.length || !neg.length) return null;
  let s = 0;
  for (const p of pos) for (const n of neg) s += p > n ? 1 : (p === n ? 0.5 : 0);
  return +(s / (pos.length * neg.length)).toFixed(4);
}
function bestAcc(items) {
  let best = { acc: 0, t: 0 };
  for (let t = 0; t <= 1.0001; t += 0.01) {
    let c = 0;
    for (const i of items) if ((i.score >= t) === i.isAI) c++;
    if (c / items.length > best.acc) best = { acc: c / items.length, t: +t.toFixed(2) };
  }
  return best;
}

const base = ids.map((id) => ({ id, isAI: A.get(id).label === 'AI', a: A.get(id).ai_probability, b: B.get(id).ai_probability }));

console.log('\nSingle-model reference:');
for (const [nm, key] of [['A ' + fileA, 'a'], ['B ' + fileB, 'b']]) {
  const items = base.map((x) => ({ isAI: x.isAI, score: x[key] }));
  const ba = bestAcc(items);
  console.log('  ' + nm.padEnd(9) + ' AUC=' + String(auc(items)).padEnd(8) + ' bestAcc=' + ba.acc.toFixed(3) + ' @t=' + ba.t.toFixed(2) +
    '  meanAI=' + mean(items.filter((i) => i.isAI).map((i) => i.score)).toFixed(3) +
    ' meanHUMAN=' + mean(items.filter((i) => !i.isAI).map((i) => i.score)).toFixed(3));
}

console.log('\nWeighted blend  score = w*A + (1-w)*B   (w was PRIMARY_MODEL_WEIGHT, now retired):');
console.log('  w      AUC      bestAcc  @t     meanAI  meanHUMAN  gap');
for (const w of [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.0]) {
  const items = base.map((x) => ({ isAI: x.isAI, score: w * x.a + (1 - w) * x.b }));
  const ba = bestAcc(items);
  const ai = mean(items.filter((i) => i.isAI).map((i) => i.score));
  const hu = mean(items.filter((i) => !i.isAI).map((i) => i.score));
  const mark = Math.abs(w - 0.9) < 1e-9 ? "   <- last shipped weight (0.9/0.1)" : "";
  console.log('  ' + w.toFixed(1) + '    ' + String(auc(items)).padEnd(9) + ba.acc.toFixed(3).padEnd(9) +
    ba.t.toFixed(2).padEnd(7) + ai.toFixed(3).padEnd(8) + hu.toFixed(3).padEnd(11) + (ai - hu).toFixed(3) + mark);
}

// Agreement / complementarity: does B ever rescue a text A got wrong?
console.log('\nComplementarity at each model\'s own best threshold:');
const tA = bestAcc(base.map((x) => ({ isAI: x.isAI, score: x.a }))).t;
const tB = bestAcc(base.map((x) => ({ isAI: x.isAI, score: x.b }))).t;
let aRightBWrong = 0, bRightARight = 0, bRightAWrong = 0, bothWrong = 0;
for (const x of base) {
  const ra = (x.a >= tA) === x.isAI;
  const rb = (x.b >= tB) === x.isAI;
  if (ra && rb) bRightARight++;
  else if (ra && !rb) aRightBWrong++;
  else if (!ra && rb) bRightAWrong++;
  else bothWrong++;
}
console.log('  A right & B right : ' + bRightARight);
console.log('  A right & B wrong : ' + aRightBWrong + '   (B would only add noise here)');
console.log('  A wrong & B right : ' + bRightAWrong + '   (the only texts B can rescue)');
console.log('  both wrong        : ' + bothWrong);

// Simulate the adaptive policy: call B only when A lands in the gray zone.
console.log('\nAdaptive policy simulation (call B only when A is inside the gray zone):');
console.log('  gray zone        B calls  acc(A alone, tuned)  acc(adaptive)  review-free');
for (const [lo, hi] of [[0.45, 0.69], [0.35, 0.65], [0.30, 0.75], [0.0, 1.0]]) {
  let calls = 0, correctSolo = 0, correctAdaptive = 0;
  const tSolo = 0.69; // tuned AI threshold for A
  for (const x of base) {
    const solo = x.a >= tSolo;
    if (solo === x.isAI) correctSolo++;
    let score = x.a;
    if (x.a >= lo && x.a <= hi) { calls++; score = 0.6 * x.a + 0.4 * x.b; }
    if ((score >= tSolo) === x.isAI) correctAdaptive++;
  }
  console.log('  [' + lo.toFixed(2) + ',' + hi.toFixed(2) + ']'.padEnd(10) +
    String(calls + '/' + base.length).padEnd(9) +
    (correctSolo / base.length).toFixed(3).padEnd(21) +
    (correctAdaptive / base.length).toFixed(3));
}
