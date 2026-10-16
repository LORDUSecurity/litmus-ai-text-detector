/*
 * Boundary-rule test: model probabilities come back with two decimals, so a score can land
 * EXACTLY on a threshold. The shipped rules are inclusive (p >= AI is AI, p <= HU is human),
 * which means an exact hit ends the deferral band instead of being treated as uncertain.
 * Two ways to fix that: move the threshold (what the deployed value now does), or make an
 * exact hit defer. This compares them on the 48 texts through the shipped node.
 *
 *   node benchmark/boundary-rule-test.js [primaryTag] [secondaryTag]
 */
const { join } = require('node:path');
const B = __dirname;
const { config, evaluate, laneRecord, needSecondary, readJsonl } = require(join(B, '..', 'n8n', 'test-ensemble.js'));

const PTAG = process.argv[2] || 'ultra550';
const STAG = process.argv[3] || 'diffgemma26b';
const P = readJsonl(join(B, 'results-' + PTAG + '.jsonl'));
const S = {};
for (const r of readJsonl(join(B, 'results-' + STAG + '.jsonl'))) S[r.id] = r;

function run(cfg) {
  const t = { correct: 0, wrong: 0, review: 0, defer: 0, fp: 0, fn: 0 };
  for (const p of P) {
    const pR = laneRecord(p, 'primary', cfg.primary_model);
    const call = pR.ok ? needSecondary(pR, cfg) : true;
    if (call) t.defer++;
    const s = S[p.id];
    const sR = (s && call) ? laneRecord(s, 'secondary', cfg.secondary_model) : null;
    const o = evaluate(cfg, pR, sR);
    const truth = p.label === 'AI' ? 'AI_GENERATED' : 'HUMAN_LIKELY';
    if (o.classification === 'REVIEW_REQUIRED' || o.classification === 'DETECTION_UNAVAILABLE') t.review++;
    else if (o.classification === truth) t.correct++;
    else { t.wrong++; if (truth === 'HUMAN_LIKELY') t.fp++; else t.fn++; }
  }
  return t;
}

// An epsilon on the far side of a two-decimal score makes the comparison exclusive without
// changing any other text's outcome.
const EPS = 1e-7;
const variants = [
  ['0.69/0.45/0.90 inclusive (previous deploy)', 0.69, 0.45, 0.90],
  ['0.69/0.45/0.90 exact-hit defers', 0.69 + EPS, 0.45 - EPS, 0.90],
  ['0.69/0.40/0.90 inclusive (current deploy)', 0.69, 0.40, 0.90],
  ['0.69/0.40/0.90 exact-hit defers', 0.69 + EPS, 0.40 - EPS, 0.90],
];

console.log('primary=' + PTAG + '  secondary=' + STAG + '  n=' + P.length);
console.log('  ' + 'rule'.padEnd(46) + 'corr wrv rev defer fp/fn');
for (const [name, ai, hu, sec] of variants) {
  const r = run(config({ ai_prob_threshold: ai, human_prob_threshold: hu, secondary_decision_threshold: sec }));
  console.log('  ' + name.padEnd(46) + String(r.correct).padEnd(5) + String(r.wrong).padEnd(4)
    + String(r.review).padEnd(4) + String(r.defer).padEnd(6) + r.fp + '/' + r.fn);
}

// Which texts actually land on a boundary at two decimals - the population this rule touches.
const BANDS = [[0.69, 0.45], [0.69, 0.40]];
const hits = [];
for (const p of P) {
  if (!p.ok || p.ai_probability == null) continue;
  const on = new Set();
  for (const [ai, hu] of BANDS) {
    if (Math.abs(p.ai_probability - ai) < 1e-9) on.add('AI cut ' + ai);
    if (Math.abs(p.ai_probability - hu) < 1e-9) on.add('HUMAN cut ' + hu);
  }
  if (on.size) hits.push(p.id + ' p=' + p.ai_probability + '  ' + [...on].join(', ') + '  true=' + p.label);
}
console.log('\nprimary scores that land exactly on a two-decimal cut (the population this rule touches):');
for (const h of hits) console.log('  ' + h);
if (!hits.length) console.log('  none');
