/*
 * Does the tie-break lane deserve to overturn the primary's lean?
 *
 * Question this answers: when the primary defers, it is not neutral -- a score at the low end
 * of the band leans human and one at the high end leans AI. The shipped rule lets the secondary
 * decide those texts at >= secondary_decision_threshold on its own scale, which means the
 * secondary can call AI on something the primary leaned human about. Is that authority earning
 * its keep, or is it where the false positives come from?
 *
 * WHY THE RULER IS READ, NOT TYPED. This tool used to hardcode 0.40/0.69/MID=0.545 --
 * nemotron-3-ultra's ruler -- and pointed at another model's captures it silently judged that
 * model against the old ruler, inventing deferrals out of texts the deployment calls a verdict
 * and reporting them as "shipped". It now drives the shipped Ensemble node itself
 * (n8n/test-ensemble.js -> evaluate()), and the band comes from the same config a real request
 * would use: .env, the workflow's fallbacks, and the model's own stored row all agree by
 * construction. Override only to ask "what if" -- every override is printed, so a number can
 * never be read as the deployed rule without seeing the rule that produced it.
 *
 *   node benchmark/tiebreak-authority.js
 *   BAND_LO=0.55 BAND_HI=0.80 PRIM_FILE=results-clean-nvfull-glm-5-3-flash.jsonl \
 *     SEC_FILE=results-nocap-clean-diffgemma.jsonl node benchmark/tiebreak-authority.js
 *
 * PRIM_FILE / SEC_FILE are relative to benchmark/; AI_CONF / HUMAN_CONF / SEC_CUT / BAND_LO /
 * BAND_HI override the corresponding thresholds; SECONDARY_ENABLED is honoured as shipped.
 */
const { join } = require('node:path');
const { readFileSync } = require('node:fs');
const t = require('../n8n/test-ensemble.js');

const DIR = __dirname;
const PRIM = join(DIR, process.env.PRIM_FILE || 'results-clean-nvfull-glm-5-3-flash.jsonl');
const SEC = join(DIR, process.env.SEC_FILE || 'results-nocap-clean-diffgemma.jsonl');

// Provenance, read from the run log that records how each capture was made. Two purposes: the
// header states which corpus and which token cap the scores came from (a capped-era capture is
// not evidence about uncapped behaviour), and the guard below stops the mistake this tool was
// built out of -- judging one model's scores against another model's ruler.
function runParams(file) {
  const tag = (file.match(/results-(.+)\.jsonl$/) || [, ''])[1];
  let last = null;
  try {
    for (const line of readFileSync(join(DIR, 'runs-manifest.log'), 'utf8').split(/\r?\n/)) {
      const m = line.match(/^params: (\{.*\})$/);
      if (!m) continue;
      const p = JSON.parse(m[1]);
      if (p.tag === tag) last = p;
    }
  } catch { return null; }
  return last;
}

const primRun = runParams(PRIM);
const secRun = runParams(SEC);

// Candidate-rule overrides only; the shipped measurement uses none, so it reads the band the
// deployment itself would apply to this model.
const ov = {};
if (process.env.BAND_LO) ov.human_prob_threshold = Number(process.env.BAND_LO);
if (process.env.BAND_HI) ov.ai_prob_threshold = Number(process.env.BAND_HI);
if (process.env.AI_CONF) ov.ai_conf_threshold = Number(process.env.AI_CONF);
if (process.env.HUMAN_CONF) ov.human_conf_threshold = Number(process.env.HUMAN_CONF);
if (process.env.SEC_CUT) ov.secondary_decision_threshold = Number(process.env.SEC_CUT);

const cfg = Object.assign(t.config(), ov);
const BAND_LO = cfg.human_prob_threshold;
const BAND_HI = cfg.ai_prob_threshold;
// The band's own midpoint: below it the primary leaned human. Derived from the band in play,
// never typed, so a lean cut cannot sit outside the range it is supposed to divide.
const MID = (BAND_LO + BAND_HI) / 2;

console.log('replaying the shipped Ensemble node over');
console.log('  primary   ' + PRIM);
console.log('  secondary ' + SEC);
console.log('  captured  primary=' + (primRun ? primRun.model + ' corpus=' + primRun.corpus
  + ' max_tokens=' + primRun.max_tokens + ' usable=' + primRun.usable + '/' + primRun.rows : 'UNATTRIBUTED (no manifest tag)'));
console.log('            secondary=' + (secRun ? secRun.model + ' corpus=' + secRun.corpus
  + ' max_tokens=' + secRun.max_tokens + ' usable=' + secRun.usable + '/' + secRun.rows : 'UNATTRIBUTED (no manifest tag)'));
console.log('  lanes     ' + cfg.primary_model + ' (primary) / ' + cfg.secondary_model + ' (secondary)'
  + '  secondary_enabled=' + cfg.secondary_enabled);
console.log('  ruler     defer between ' + BAND_LO + '..' + BAND_HI
  + '  conf floor ' + cfg.ai_conf_threshold + '/' + cfg.human_conf_threshold
  + '  secondary decides at >= ' + cfg.secondary_decision_threshold);
if (Object.keys(ov).length) {
  console.log('  OVERRIDDEN by this invocation: ' + JSON.stringify(ov)
    + '  <- these are candidates, not the deployed rule');
}
if (!(BAND_LO < MID && MID < BAND_HI)) {
  console.error('band/lean inconsistent: need ' + BAND_LO + ' < ' + MID + ' < ' + BAND_HI);
  process.exit(2);
}
// The guard that would have caught the mistake this tool was rewritten out of: the band in play
// belongs to whatever .env/DB names as the lane, so captures from a different model would be
// scored against the wrong ruler. Say so and stop, rather than printing a wrong tally.
if (primRun && primRun.model !== cfg.primary_model) {
  console.error('MISMATCH: the primary capture is ' + primRun.model + ' but the ruler in play belongs to '
    + cfg.primary_model + '. Point PRIM_FILE at that model\'s capture, or set the band deliberately'
    + ' and run with ALLOW_MISMATCH=1.');
  if (!process.env.ALLOW_MISMATCH) process.exit(2);
}

const secRows = {};
for (const r of t.readJsonl(SEC)) secRows[r.id] = r;
const primRows = t.readJsonl(PRIM);
const truth = (r) => (r.label === 'AI' ? 'AI_GENERATED' : 'HUMAN_LIKELY');
const lean = (p) => (p < MID ? 'lean-human' : 'lean-ai');

// ------------------------------------------------- the shipped rule, node-decided
const shipped = t.replay(PRIM, SEC, cfg.secondary_decision_threshold, true);
const byId = {};
for (const r of shipped.rows) byId[r.id] = r;

const buckets = {};
for (const p of primRows) {
  const row = byId[p.id];
  if (!row) continue;
  // Bucketed by which lane the NODE says decided it, never by this file's guess at the
  // decisiveness rule. The mirror predicate below (needSecondary) is for the deferral listing
  // only, and is stricter than the node -- it also defers salvaged readings.
  const who = /^TIE_BREAK_/.test(row.method || '') ? 'by-secondary'
    : row.outcome === 'review' ? 'unanswered' : 'by-primary';
  const key = who + '/' + (p.ok ? lean(p.ai_probability) : 'no-reading');
  const b = (buckets[key] = buckets[key] || { n: 0, by: {}, wrong: 0, kinds: {} });
  b.n++;
  b.by[row.method] = (b.by[row.method] || 0) + 1;
  if (row.outcome === 'wrong') {
    b.wrong++;
    const kind = row.p >= 0.5 ? 'called-AI-on-human-text' : 'called-HUMAN-on-AI-text';
    b.kinds[kind] = (b.kinds[kind] || 0) + 1;
  }
}

console.log('\nShipped rule as the node applies it');
console.log('bucket'.padEnd(24) + ' n    wrong  decided by');
for (const [k, b] of Object.entries(buckets)) {
  console.log(k.padEnd(24) + String(b.n).padEnd(5) + ' ' + String(b.wrong).padEnd(6)
    + ' ' + JSON.stringify(b.by)
    + (Object.keys(b.kinds).length ? '  wrongs=' + JSON.stringify(b.kinds) : ''));
}
const acc = (c, w) => (c / Math.max(1, c + w)).toFixed(3);
console.log('  -> correct=' + shipped.tally.correct + ' wrong=' + shipped.tally.wrong
  + ' review=' + shipped.tally.review + '  accuracy/answered=' + acc(shipped.tally.correct, shipped.tally.wrong));

// what the tie-break alone is responsible for
const tieRows = shipped.rows.filter((r) => /^TIE_BREAK_/.test(r.method || ''));
const tieWrong = tieRows.filter((r) => r.outcome === 'wrong');
console.log('\nTie-break lane decided ' + tieRows.length + ' of ' + primRows.length + ' texts, '
  + tieWrong.length + ' of them wrongly'
  + (tieRows.length ? ': ' + tieRows.map((r) => r.id + '(' + r.method + ')').join(' ') : ''));
const overturns = tieRows.filter((r) => {
  const p = primRows.find((x) => x.id === r.id);
  return p && p.ok && p.ai_probability < MID && r.method === 'TIE_BREAK_AI';
});
console.log('  of those, ' + overturns.length + ' overturned a human-leaning primary into an AI verdict'
  + (overturns.length ? ': ' + overturns.map((r) => r.id).join(' ') : '')
  + '. Errors the primary made on its own are not the tie-break\'s to fix.');

// ------------------------------------------------------------------- the deferrals
const deferrals = primRows.filter((p) => p.ok && t.needSecondary(t.laneRecord(p, 'primary', cfg.primary_model), cfg));
console.log('\nBand deferrals: ' + deferrals.length + ' of ' + primRows.length + ' texts'
  + ' (mirror of the parse node\'s need_secondary, which also defers a salvaged reading;'
  + ' the "->" column is what the node actually did)');
for (const d of deferrals.slice().sort((a, b) => a.ai_probability - b.ai_probability)) {
  const s = secRows[d.id];
  console.log('  ' + d.id.padEnd(9) + ' label=' + String(d.label).padEnd(6)
    + ' primary=' + String(d.ai_probability).padEnd(6) + 'conf=' + String(d.confidence).padEnd(6)
    + ' secondary=' + (s && s.ok ? s.ai_probability + ' conf=' + s.confidence : 'n/a')
    + '  ' + lean(d.ai_probability).toUpperCase()
    + '  -> ' + (byId[d.id] ? byId[d.id].method + '/' + byId[d.id].outcome : '?'));
}

// ------------------------------------------------------- candidate rules, measured
console.log('\nCandidate rules (shipped node re-run per secondary cut, then the AI-overturn block):');
const cuts = [cfg.secondary_decision_threshold, 0.8, 0.85, 0.9, 0.95, 1.01];
for (const cut of cuts.filter((c, i) => cuts.indexOf(c) === i)) {
  const r = t.replay(PRIM, SEC, cut, true);
  // the restriction under test: while the primary leans human, the secondary may confirm
  // HUMAN but never create an AI verdict -- those texts go to review instead.
  let blocked = 0, correct = r.tally.correct, wrong = r.tally.wrong, review = r.tally.review;
  for (const row of r.rows) {
    const p = primRows.find((x) => x.id === row.id);
    if (row.method === 'TIE_BREAK_AI' && p && p.ok && p.ai_probability < MID) {
      blocked++;
      if (row.outcome === 'wrong') { wrong--; correct += 0; } else if (row.outcome === 'correct') correct--;
      review++;
    }
  }
  console.log('  secondary cut ' + Number(cut).toFixed(2) + ': correct=' + correct + ' wrong=' + wrong
    + ' review=' + review + ' (shipped ' + r.tally.correct + '/' + r.tally.wrong + '/' + r.tally.review + ')'
    + ' human-lean-AI-overturns-blocked=' + blocked
    + '  accuracy/answered=' + acc(correct, wrong));
}
// Lane off, emulated the way the workflow really does it: the secondary branch is never
// executed, so $('Parse Secondary') throws and the node falls back to its skipped placeholder
// (a lone primary that is not decisive becomes REVIEW_REQUIRED). Passing cfg.secondary_enabled
// to the replay is NOT that: the flag only writes a warning there, the routing lives upstream.
let off = { correct: 0, wrong: 0, review: 0 };
for (const p of primRows) {
  const out = t.evaluate(cfg, t.laneRecord(p, 'primary', cfg.primary_model), null);
  if (out.classification === 'REVIEW_REQUIRED' || out.classification === 'DETECTION_UNAVAILABLE') off.review++;
  else if (out.classification === truth(p)) off.correct++;
  else off.wrong++;
}
console.log('  secondary lane OFF (deferrals -> review, lone primary decides only when decisive):'
  + ' correct=' + off.correct + ' wrong=' + off.wrong + ' review=' + off.review
  + '  accuracy/answered=' + acc(off.correct, off.wrong)
  + '   answered=' + (off.correct + off.wrong) + '/' + primRows.length);
console.log('\nCaution: secondary_decision_threshold is one-sided -- secondaryVerdict is');
console.log('p >= cut ? AI : HUMAN. Raising it above every score does not silence the lane, it');
console.log('makes the secondary answer HUMAN on everything it is asked about.');
console.log('\nNote: the block column is a post-filter over the node\'s rows, so it prices the rule');
console.log('change without shipping it; review rows are unanswered documents, not errors.');
