#!/usr/bin/env node
/*
 * Paired drift check for the phrase-evidence prompt change.
 *
 * The prompt was edited to also ask for verbatim `phrases`. That must not move the
 * score the calibrated bands were fit on. This joins two captures of the SAME corpus
 * by id -- the old prompt (oosstress-*) and the new prompt (stressprompt-*) -- and
 * reports, at the SAVED band, every verdict that changed, whether the wrong-count
 * moved, how far the probability drifted, and whether the new prompt started
 * truncating/salvaging rows it used to answer cleanly.
 *
 * It also validates the feature itself: of the phrases the new prompt returned, how
 * many quotes are actually findable in the source text (exact, then whitespace-
 * normalized) -- a quote the UI cannot locate is a quote the user never sees painted.
 *
 * usage: node compare-drift.js --old oosstress-omkiro-sonnet-4-5 \
 *                              --new stressprompt-omkiro-sonnet-4-5 \
 *                              [--ai .75 --hum .30 --gate 0] [--corpus corpus-stress.json]
 */
const fs = require('fs');
const path = require('path');
const B = __dirname;
const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? process.argv[i + 1] : d; };

const OLD = arg('old', 'oosstress-omkiro-sonnet-4-5');
const NEW = arg('new', 'stressprompt-omkiro-sonnet-4-5');
const AI = parseFloat(arg('ai', '0.75'));
const HUM = parseFloat(arg('hum', '0.30'));
const GATE = parseFloat(arg('gate', '0'));
const CORPUS = arg('corpus', 'corpus-stress.json');

function load(tag) {
  const p = path.join(B, 'results-' + tag + '.jsonl');
  if (!fs.existsSync(p)) { console.error('missing ' + p); process.exit(2); }
  const m = new Map();
  for (const line of fs.readFileSync(p, 'utf8').split(/\r?\n/)) {
    if (!line.trim()) continue;
    let r; try { r = JSON.parse(line); } catch { continue; }
    if (r && r.id !== undefined) m.set(String(r.id), r);
  }
  return m;
}
const oldM = load(OLD);
const newM = load(NEW);

// Same clean-row filter score-band.js / fit-bands.js use.
const clean = (r) => r && r.ok && !r.salvaged && !r.truncated
  && typeof r.ai_probability === 'number' && !!r.label;
const verdict = (r) => {
  const sure = (typeof r.confidence === 'number' ? r.confidence : 1) >= GATE;
  return sure && r.ai_probability >= AI ? 'AI' : sure && r.ai_probability <= HUM ? 'HUMAN' : 'REVIEW';
};

const ids = [...new Set([...oldM.keys(), ...newM.keys()])].sort();
let paired = 0, flips = 0;
const flipKinds = {};
const decisiveFlips = [];
let oldCorrect = 0, oldWrong = 0, newCorrect = 0, newWrong = 0;
let sumDp = 0, maxDp = 0, maxDpId = '', sumDc = 0;
const oldCleanNotNew = [], newCleanNotOld = [];

for (const id of ids) {
  const o = oldM.get(id), n = newM.get(id);
  const oc = clean(o), nc = clean(n);
  if (oc && !nc) oldCleanNotNew.push(id + (n ? ' (' + (n.error_code || (n.truncated ? 'truncated' : n.salvaged ? 'salvaged' : 'unclean')) + ')' : ' (absent)'));
  if (nc && !oc) newCleanNotOld.push(id);
  if (!oc || !nc) continue; // paired comparison needs both sides usable
  paired++;
  const vo = verdict(o), vn = verdict(n);
  const dp = Math.abs(n.ai_probability - o.ai_probability);
  const dc = Math.abs((typeof n.confidence === 'number' ? n.confidence : 1) - (typeof o.confidence === 'number' ? o.confidence : 1));
  sumDp += dp; sumDc += dc;
  if (dp > maxDp) { maxDp = dp; maxDpId = id; }
  if (vo !== vn) {
    flips++;
    const k = vo + '->' + vn;
    flipKinds[k] = (flipKinds[k] || 0) + 1;
    // a flip between two decisive verdicts is the dangerous kind: it changes the answer
    if (vo !== 'REVIEW' && vn !== 'REVIEW') decisiveFlips.push(id + ' ' + o.label + ' ' + vo + '(' + o.ai_probability + ')->' + vn + '(' + n.ai_probability + ')');
  }
  if (vo === o.label) oldCorrect++; else oldWrong++;
  if (vn === n.label) newCorrect++; else newWrong++;
}

console.log('DRIFT  old=' + OLD + '  new=' + NEW);
console.log('band ai=' + AI + ' hum=' + HUM + ' gate=' + GATE + '   rows old=' + oldM.size + ' new=' + newM.size + '  paired&clean=' + paired);
console.log('verdict flips: ' + flips + '/' + paired + (paired ? ' (' + (100 * flips / paired).toFixed(1) + '%)' : ''));
for (const k of Object.keys(flipKinds).sort()) console.log('   ' + k + ': ' + flipKinds[k]);
console.log('decisive<->decisive flips (answer changed): ' + decisiveFlips.length);
for (const d of decisiveFlips) console.log('   FLIP ' + d);
console.log('correct/wrong  old: ' + oldCorrect + '/' + oldWrong + '   new: ' + newCorrect + '/' + newWrong +
  (oldWrong === newWrong ? '   (wrong-count unchanged)' : '   <-- WRONG-COUNT MOVED'));
console.log('p drift: mean|dp|=' + (paired ? (sumDp / paired).toFixed(3) : '0') + '  max|dp|=' + maxDp.toFixed(3) + (maxDpId ? ' @' + maxDpId : ''));
console.log('conf drift: mean|dc|=' + (paired ? (sumDc / paired).toFixed(3) : '0'));
console.log('was-clean-now-unclean (new prompt broke it): ' + oldCleanNotNew.length + (oldCleanNotNew.length ? ' -> ' + oldCleanNotNew.join(', ') : ''));
console.log('now-clean-was-unclean: ' + newCleanNotOld.length + (newCleanNotOld.length ? ' -> ' + newCleanNotOld.join(', ') : ''));

// ---- phrase locate-ability against the real source text ----
let corpusText = new Map();
const cp = path.join(B, CORPUS);
if (fs.existsSync(cp)) {
  const arr = JSON.parse(fs.readFileSync(cp, 'utf8'));
  const rows = Array.isArray(arr) ? arr : (arr.items || arr.rows || []);
  for (const r of rows) if (r && r.id !== undefined && typeof r.text === 'string') corpusText.set(String(r.id), r.text);
}
const norm = (s) => s.replace(/\s+/g, ' ').trim();
let rowsWithPhrases = 0, totalQuotes = 0, exact = 0, normOnly = 0, notFound = 0, phraseCounts = [];
if (corpusText.size) {
  for (const [id, n] of newM) {
    const ph = n && n.ok && Array.isArray(n.phrases) ? n.phrases : [];
    if (ph.length) rowsWithPhrases++;
    phraseCounts.push(ph.length);
    const text = corpusText.get(id);
    for (const q of ph) {
      totalQuotes++;
      if (!text || !q || typeof q.quote !== 'string') { notFound++; continue; }
      const raw = q.quote.trim();
      if (raw && text.indexOf(raw) >= 0) exact++;
      else if (raw && norm(text).indexOf(norm(raw)) >= 0) normOnly++;
      else notFound++;
    }
  }
  const avg = phraseCounts.length ? (phraseCounts.reduce((a, b) => a + b, 0) / phraseCounts.length) : 0;
  console.log('PHRASES (new capture): rows with >=1 phrase=' + rowsWithPhrases + '/' + newM.size +
    '  avg/row=' + avg.toFixed(2) + '  total quotes=' + totalQuotes);
  console.log('  locate-ability: exact=' + exact + '  normalized-only=' + normOnly +
    '  NOT FOUND=' + notFound + (totalQuotes ? '  (' + (100 * (exact + normOnly) / totalQuotes).toFixed(1) + '% paintable)' : ''));
} else {
  console.log('PHRASES: corpus ' + CORPUS + ' not found — skipped locate-ability check');
}
