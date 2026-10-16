/*
 * Decision margins: for a pair and a band, how far is each deciding score from the cut it
 * was read against? A band change that flips texts sitting 0.01 from the cut is fitting the
 * corpus (and the live jitter measured on this pipeline is far larger than that). A change
 * that flips texts with a wide margin is a real operating-point move.
 *
 *   node benchmark/band-margins.js <primaryTag> <secondaryTag> <AI_T> <HU_T> <SEC_T>
 */
const { join } = require('node:path');
const B = __dirname;
const { config, evaluate, laneRecord, needSecondary, readJsonl } = require(join(B, '..', 'n8n', 'test-ensemble.js'));

const [PTAG, STAG, AI_T, HU_T, SEC_T] = process.argv.slice(2);
if (!SEC_T) { console.error('usage: band-margins.js <primaryTag> <secondaryTag> <AI_T> <HU_T> <SEC_T>'); process.exit(2); }

const cfg = config({
  ai_prob_threshold: parseFloat(AI_T),
  human_prob_threshold: parseFloat(HU_T),
  secondary_decision_threshold: parseFloat(SEC_T),
});

const P = readJsonl(join(B, 'results-' + PTAG + '.jsonl'));
const S = {};
for (const r of readJsonl(join(B, 'results-' + STAG + '.jsonl'))) S[r.id] = r;

const pad = (s, n) => String(s).padEnd(n);
const rows = [];
for (const p of P) {
  const pRec = laneRecord(p, 'primary', cfg.primary_model);
  const call = pRec.ok ? needSecondary(pRec, cfg) : true;
  const s = S[p.id];
  const sRec = (s && call) ? laneRecord(s, 'secondary', cfg.secondary_model) : null;
  const out = evaluate(cfg, pRec, sRec);
  const truth = p.label === 'AI' ? 'AI_GENERATED' : 'HUMAN_LIKELY';
  const lane = out.component_scores.deciding_lane;
  // The deciding score and the cut it faced: that distance is the margin.
  const score = out.ai_probability;
  let cut = null;
  if (lane === 'secondary') cut = cfg.secondary_decision_threshold;
  else if (out.classification === 'AI_GENERATED') cut = cfg.ai_prob_threshold;
  else if (out.classification === 'HUMAN_LIKELY') cut = cfg.human_prob_threshold;
  const margin = cut === null ? null : Math.abs(score - cut);
  rows.push({
    id: p.id, label: p.label, cls: out.classification, method: out.method, lane: lane,
    score: score, cut: cut, margin: margin,
    outcome: (out.classification === 'REVIEW_REQUIRED' || out.classification === 'DETECTION_UNAVAILABLE') ? 'review'
      : out.classification === truth ? 'correct' : 'wrong',
  });
}

const near = (r, t) => r.margin !== null && r.margin <= t;
console.log(PTAG + ' + ' + STAG + '  band ' + AI_T + '/' + HU_T + '  tie-break cut ' + SEC_T);
console.log('  correct=' + rows.filter((r) => r.outcome === 'correct').length
  + ' wrong=' + rows.filter((r) => r.outcome === 'wrong').length
  + ' review=' + rows.filter((r) => r.outcome === 'review').length);
for (const t of [0.02, 0.05, 0.10]) {
  const dec = rows.filter((r) => r.outcome !== 'review');
  console.log('  decisions within ' + t.toFixed(2) + ' of their cut: '
    + dec.filter((r) => near(r, t)).length + '/' + dec.length
    + '   (of those, wrong: ' + dec.filter((r) => near(r, t) && r.outcome === 'wrong').length + ')');
}
console.log('\n  every non-primary-decided text, sorted by margin:');
for (const r of rows.slice().sort((a, b) => (a.margin === null ? 9 : a.margin) - (b.margin === null ? 9 : b.margin))) {
  if (r.outcome === 'review') continue;
  console.log('  ' + pad(r.id, 9) + pad(r.label, 7) + pad(r.outcome, 9) + pad(r.lane, 11)
    + 'score=' + pad(r.score, 6) + 'cut=' + pad(r.cut === null ? '-' : r.cut, 6)
    + 'margin=' + pad(r.margin === null ? '-' : r.margin.toFixed(3), 8) + r.method);
}
