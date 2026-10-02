// End-to-end policy simulation: replays the workflow's real decision path using the
// measured per-text scores, for the shipped config vs the proposed tuned config.
//
// The adaptive design means the secondary only fires inside the gray zone, so the
// effective score is the primary alone most of the time and a blend sometimes.
//
// Usage: node policy-sim.js results-ultra550.jsonl results-baseline.jsonl

const fs = require('fs');
const path = require('path');

const [filePrimary, fileSecondary] = process.argv.slice(2);
if (!filePrimary || !fileSecondary) { console.error('usage: node policy-sim.js <primary.jsonl> <secondary.jsonl>'); process.exit(1); }

function load(f) {
  const m = new Map();
  for (const l of fs.readFileSync(path.join(__dirname, f), 'utf8').split('\n').filter(Boolean)) {
    const r = JSON.parse(l);
    if (r.ok && r.ai_probability !== null) m.set(r.id, r);
  }
  return m;
}
const P = load(filePrimary);
const S = load(fileSecondary);
const ids = [...P.keys()].filter((id) => S.has(id));

// A policy fully describes the workflow's configurable behaviour.
function classify(p, s, pol) {
  // Parse Primary: need_secondary when inside gray zone, low confidence, or failed.
  const needSecondary = (p.ai_probability >= pol.grayLow && p.ai_probability <= pol.grayHigh) || p.confidence < 0.5;

  let prob, conf, secondaryCalled = false;
  if (needSecondary && s) {
    secondaryCalled = true;
    prob = pol.wPrimary * p.ai_probability + pol.wSecondary * s.ai_probability;
    conf = (p.confidence + s.confidence) / 2;
    const disagreement = Math.abs(p.ai_probability - s.ai_probability);
    if (prob >= pol.aiT && conf >= pol.aiConfT && disagreement <= pol.maxDisagreement) return { c: 'AI_GENERATED', prob, conf, secondaryCalled };
    if (prob <= pol.huT && conf >= pol.huConfT && disagreement <= pol.maxDisagreement) return { c: 'HUMAN_LIKELY', prob, conf, secondaryCalled };
    return { c: 'REVIEW_REQUIRED', prob, conf, secondaryCalled };
  }
  prob = p.ai_probability;
  conf = p.confidence * pol.penalty;
  if (prob >= pol.aiT && conf >= pol.aiConfT) return { c: 'AI_GENERATED', prob, conf, secondaryCalled };
  if (prob <= pol.huT && conf >= pol.huConfT) return { c: 'HUMAN_LIKELY', prob, conf, secondaryCalled };
  return { c: 'REVIEW_REQUIRED', prob, conf, secondaryCalled };
}

function runPolicy(name, pol, primaryMap, secondaryMap) {
  const tally = { AI_GENERATED: 0, HUMAN_LIKELY: 0, REVIEW_REQUIRED: 0 };
  let tp = 0, tn = 0, fp = 0, fn = 0, rev = 0, secCalls = 0;
  const mistakes = [];
  for (const id of ids) {
    const p = primaryMap.get(id), s = secondaryMap.get(id);
    const isAI = p.label === 'AI';
    const r = classify(p, s, pol);
    tally[r.c]++;
    if (r.secondaryCalled) secCalls++;
    if (r.c === 'REVIEW_REQUIRED') { rev++; continue; }
    if (r.c === 'AI_GENERATED' && isAI) tp++;
    else if (r.c === 'AI_GENERATED' && !isAI) { fp++; mistakes.push({ id, label: p.label, got: r.c, prob: r.prob, conf: r.conf }); }
    else if (r.c === 'HUMAN_LIKELY' && !isAI) tn++;
    else { fn++; mistakes.push({ id, label: p.label, got: r.c, prob: r.prob, conf: r.conf }); }
  }
  const n = ids.length, dec = tp + tn + fp + fn;
  return {
    name, tally, tp, tn, fp, fn, rev, secCalls, n,
    accuracy: (tp + tn) / n,
    precision: dec ? (tp + tn) / dec : 0,
    review_rate: rev / n,
    ai_recall: tp + fn ? tp / (tp + fn) : 0,
    human_spec: tn + fp ? tn / (tn + fp) : 0,
    mistakes
  };
}

const p3 = (x) => (Math.round(x * 1000) / 1000).toFixed(3);
function report(r) {
  console.log('\n' + r.name);
  console.log('  accuracy=' + p3(r.accuracy) + '  precision_of_decided=' + p3(r.precision) +
    '  review_rate=' + p3(r.review_rate));
  console.log('  AI_recall=' + p3(r.ai_recall) + '  HUMAN_specificity=' + p3(r.human_spec) +
    '  false_positives=' + r.fp + '  false_negatives=' + r.fn);
  console.log('  outcomes: ' + JSON.stringify(r.tally) + '  secondary_calls=' + r.secCalls + '/' + r.n);
  if (r.mistakes.length) {
    console.log('  wrong verdicts:');
    for (const m of r.mistakes) console.log('    ' + m.id + ' true=' + m.label + ' got=' + m.got + ' p=' + m.prob.toFixed(2) + ' conf=' + m.conf.toFixed(2));
  }
}

console.log('='.repeat(76));
console.log('POLICY SIMULATION over ' + ids.length + ' texts (primary=550B, secondary=120B)');
console.log('='.repeat(76));

// Exactly what is in .env / the workflow today.
const shipped = {
  aiT: 0.85, huT: 0.20, aiConfT: 0.70, huConfT: 0.70, penalty: 0.85,
  grayLow: 0.35, grayHigh: 0.65, wPrimary: 0.6, wSecondary: 0.4, maxDisagreement: 0.30
};
// Proposed from the measured data.
const proposed = {
  aiT: 0.69, huT: 0.45, aiConfT: 0.60, huConfT: 0.60, penalty: 1.00,
  grayLow: 0.45, grayHigh: 0.69, wPrimary: 0.9, wSecondary: 0.1, maxDisagreement: 0.30
};
// Same thresholds but primary decides alone (no secondary at all).
const solo = { ...proposed, grayLow: 1.1, grayHigh: -1.1 };

report(runPolicy('SHIPPED (120B primary, ai>=0.85 human<=0.20 conf>=0.70 penalty 0.85, w 0.6/0.4)',
  shipped, S, P));   // note: shipped had the 120B as primary
report(runPolicy('PROPOSED (550B primary, ai>=0.69 human<=0.45 conf>=0.60 penalty 1.0, w 0.9/0.1)',
  proposed, P, S));
report(runPolicy('PROPOSED-SOLO (550B decides alone, secondary never called)',
  solo, P, S));
