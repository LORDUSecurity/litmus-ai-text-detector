// Holdout validation for a TIE-BREAK secondary lane: the secondary is consulted only on
// texts where the primary defers (REVIEW), and its own score is read against the
// SECONDARY's threshold rather than blended into the primary's probability.
//
// This matters because the measured lanes sit on different score scales: the 550B puts
// true-human text at median 0.25, muse-glimmer-30b at median 0.68. A weighted average of
// two differently-scaled numbers is not a coherent statistic, so blending is compared
// against deciding per lane.
//
// Usage: node tiebreak-holdout.js <primary.jsonl> <secondary.jsonl> [secondary2.jsonl ...]

const fs = require('fs');
const path = require('path');

const [fileP, ...fileS] = process.argv.slice(2);
if (!fileP || !fileS.length) { console.error('usage: node tiebreak-holdout.js primary.jsonl secondary.jsonl [...]'); process.exit(1); }

const AI_T = 0.69, HU_T = 0.45;                 // deployed thresholds, read from .env values
const CAND_T = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.72, 0.73, 0.75, 0.80, 0.85, 0.90];

function load(f) {
  const m = new Map();
  for (const l of fs.readFileSync(path.join(__dirname, f), 'utf8').split('\n').filter(Boolean)) {
    const r = JSON.parse(l);
    if (r.ok && r.ai_probability !== null) m.set(r.id, r);
  }
  return m;
}
const decide = (v) => (v >= AI_T ? 'AI' : v <= HU_T ? 'HUMAN' : 'REVIEW');

const P = load(fileP);
const ids = [...P.keys()].sort();
const byLabel = { AI: [], HUMAN: [] };
for (const id of ids) byLabel[P.get(id).label].push(id);
const foldA = [], foldB = [];
for (const k of Object.keys(byLabel)) byLabel[k].forEach((id, i) => (i % 2 ? foldB : foldA).push(id));

function fitThreshold(S, fitIds) {
  let best = { t: null, score: -Infinity };
  for (const t of CAND_T) {
    let res = 0, mis = 0;
    for (const id of fitIds) {
      const p = P.get(id);
      if (decide(p.ai_probability) !== 'REVIEW') continue;
      const s = S.get(id);
      if (!s) continue;
      ((s.ai_probability >= t ? 'AI' : 'HUMAN') === p.label) ? res++ : mis++;
    }
    if (res - mis > best.score) best = { t, score: res - mis };
  }
  return best;
}

// Accuracy of the whole pipeline on a fold: primary decides where it can, secondary
// decides the deferrals at threshold t. Unanswered deferrals count as not-correct.
function evaluate(S, foldIds, t) {
  let correct = 0, wrong = 0, unresolved = 0, consulted = 0;
  for (const id of foldIds) {
    const p = P.get(id);
    const d = decide(p.ai_probability);
    if (d !== 'REVIEW') { d === p.label ? correct++ : wrong++; continue; }
    consulted++;
    const s = S.get(id);
    if (t === null || !s) { unresolved++; continue; }
    ((s.ai_probability >= t ? 'AI' : 'HUMAN') === p.label) ? correct++ : wrong++;
  }
  const n = foldIds.length;
  return { n, correct, wrong, unresolved, consulted, acc: correct / n };
}

console.log('primary   = ' + fileP + '  (' + ids.length + ' scored texts)');
console.log('folds     = A:' + foldA.length + ' B:' + foldB.length + ' (stratified by label)');
const baseA = evaluate(P, foldA, null), baseB = evaluate(P, foldB, null);
console.log('primary alone: foldA acc=' + baseA.acc.toFixed(3) + ' (review ' + baseA.unresolved + ')   foldB acc=' + baseB.acc.toFixed(3) + ' (review ' + baseB.unresolved + ')');
console.log('');
console.log('secondary       fit-on-A t  heldout-B acc  wrong  unresolved | fit-on-B t  heldout-A acc  wrong  unresolved');
for (const f of fileS) {
  const S = load(f);
  const a = fitThreshold(S, foldA), b = fitThreshold(S, foldB);
  const evalB = evaluate(S, foldB, a.t), evalA = evaluate(S, foldA, b.t);
  console.log(
    path.basename(f).replace(/^results-|\.jsonl$/g, '').padEnd(16) +
    String(a.t).padEnd(11) + evalB.acc.toFixed(3).padStart(6) + String(evalB.wrong).padStart(8) + String(evalB.unresolved).padStart(13) + '   |' +
    String(b.t).padEnd(12) + evalA.acc.toFixed(3).padStart(6) + String(evalA.wrong).padStart(8) + String(evalA.unresolved).padStart(13));
}
