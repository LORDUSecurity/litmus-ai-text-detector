// Offline dirty-vs-clean confound table. Reads matched (dirty-file, clean-file) result pairs for the
// SAME model+params and reports how removing the tokenizer confound moves each separation statistic.
// AUC / Cohen's d / distributions use the exact formulas in analyze.js (verified against its 550b row
// so this table and the published analysis cannot disagree). The verdict column applies the DEPLOYED
// single-model primary cut from .env (AI>=AI_PROBABILITY_THRESHOLD & conf>=AI_CONFIDENCE_THRESHOLD;
// HUMAN<=HUMAN_PROBABILITY_THRESHOLD), which is the thing that actually misjudges text in production.
//
//   node confound-table.js "label,dirty.jsonl,clean.jsonl" [...]      (each triple = one model)
//   node confound-table.js --pair 550B,results-ultra550.jsonl,results-clean550-hc3.jsonl
//
// No network. Never reads a key except the numeric thresholds.

const fs = require('fs');
const path = require('path');

const env = {};
for (const l of fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  if (l.trim().startsWith('#')) continue;
  const m = l.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
}
const AI_CUT = parseFloat(env.AI_PROBABILITY_THRESHOLD);
const HU_CUT = parseFloat(env.HUMAN_PROBABILITY_THRESHOLD);
const CONF = parseFloat(env.AI_CONFIDENCE_THRESHOLD);

function load(f) {
  return fs.readFileSync(path.join(__dirname, f), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}
const scored = (rows) => rows.filter((r) => r.ok && typeof r.ai_probability === 'number');

// --- analyze.js-identical stats ---
function auc(rows) {
  const s = scored(rows);
  const pos = s.filter((r) => r.label === 'AI').map((r) => r.ai_probability);
  const neg = s.filter((r) => r.label === 'HUMAN').map((r) => r.ai_probability);
  if (!pos.length || !neg.length) return NaN;
  const all = [...pos, ...neg].map((v, i) => ({ v, p: i < pos.length })).sort((a, b) => a.v - b.v);
  let rank = 0, i = 0, sumPosRanks = 0;
  while (i < all.length) {
    let j = i; while (j < all.length && all[j].v === all[i].v) j++;
    const avg = (rank + 1 + rank + (j - i)) / 2;
    for (let k = i; k < j; k++) if (all[k].p) sumPosRanks += avg;
    rank += (j - i); i = j;
  }
  const U = sumPosRanks - pos.length * (pos.length + 1) / 2;
  return +(U / (pos.length * neg.length)).toFixed(4);
}
function cohend(rows) {
  const s = scored(rows);
  const a = s.filter((r) => r.label === 'AI').map((r) => r.ai_probability);
  const h = s.filter((r) => r.label === 'HUMAN').map((r) => r.ai_probability);
  if (a.length < 2 || h.length < 2) return NaN;
  const ma = a.reduce((x, y) => x + y, 0) / a.length, mh = h.reduce((x, y) => x + y, 0) / h.length;
  const va = a.reduce((x, y) => x + (y - ma) ** 2, 0) / (a.length - 1);
  const vh = h.reduce((x, y) => x + (y - mh) ** 2, 0) / (h.length - 1);
  return +((ma - mh) / Math.sqrt((va + vh) / 2)).toFixed(3);
}
function means(rows) {
  const s = scored(rows);
  const m = (lab) => { const v = s.filter((r) => r.label === lab).map((r) => r.ai_probability); return v.reduce((a, b) => a + b, 0) / (v.length || 1); };
  return { ai: +m('AI').toFixed(3), hu: +m('HUMAN').toFixed(3) };
}
function bestAcc(rows) {
  const s = scored(rows);
  let best = { t: null, acc: -1 };
  for (let t = 0; t <= 1.0001; t += 0.01) {
    let c = 0; for (const r of s) if ((r.ai_probability >= t) === (r.label === 'AI')) c++;
    const acc = c / s.length;
    if (acc > best.acc + 1e-9) best = { t: +t.toFixed(2), acc: +acc.toFixed(4) };
  }
  return best;
}
// deployed single-model primary verdict at the shipped cut
function verdictCounts(rows) {
  let humanAsAI = 0, aiAsHuman = 0, review = 0, decidedCorrect = 0, decided = 0;
  for (const r of scored(rows)) {
    const p = r.ai_probability, c = r.confidence;
    let v = 'REVIEW';
    if (c >= CONF && p >= AI_CUT) v = 'AI';
    else if (c >= CONF && p <= HU_CUT) v = 'HUMAN';
    if (v === 'REVIEW') { review++; continue; }
    decided++;
    if (r.label === 'HUMAN' && v === 'AI') humanAsAI++;
    if (r.label === 'AI' && v === 'HUMAN') aiAsHuman++;
    if ((r.label === 'AI' && v === 'AI') || (r.label === 'HUMAN' && v === 'HUMAN')) decidedCorrect++;
  }
  return { humanAsAI, aiAsHuman, review, accDecided: decided ? +(decidedCorrect / decided).toFixed(3) : NaN };
}

function parsePairs(argv) {
  const out = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--pair') { const p = argv[++i].split(','); out.push({ label: p[0], dirty: p[1], clean: p[2] }); }
    else { const p = argv[i].split(','); if (p.length === 3) out.push({ label: p[0], dirty: p[1], clean: p[2] }); }
  }
  return out;
}

const pairs = parsePairs(process.argv.slice(2));
if (!pairs.length) {
  console.error('usage: node confound-table.js "label,dirty.jsonl,clean.jsonl" ...   (or --pair L,d,c)');
  console.error('deployed cut read from .env: AI>=' + AI_CUT + ' (conf>=' + CONF + '), HUMAN<=' + HU_CUT);
  process.exit(1);
}

const F = (x) => (Number.isFinite(x) ? x.toFixed(3) : '  --  ');
console.log('confound table  |  deployed single-model cut: AI>=' + AI_CUT + ' &conf>=' + CONF + ', HUMAN<=' + HU_CUT);
console.log('model           corpus-usable      AUC  dirty>clean        d  dirty>clean     gap(d->c)   humanAsAI d>c   aiAsHuman d>c');
console.log('-'.repeat(112));
for (const p of pairs) {
  let dR, cR;
  try { dR = load(p.dirty); cR = load(p.clean); }
  catch (e) { console.log(p.label.padEnd(16) + ' LOAD ERROR: ' + e.message); continue; }
  const dA = auc(dR), cA = auc(cR), dD = cohend(dR), cD = cohend(cR);
  const dm = means(dR), cm = means(cR);
  const dV = verdictCounts(dR), cV = verdictCounts(cR);
  const dGap = +(dm.ai - dm.hu).toFixed(3), cGap = +(cm.ai - cm.hu).toFixed(3);
  console.log(
    p.label.slice(0, 16).padEnd(16) +
    ' ' + scored(dR).length + '/' + scored(cR).length + ' ok      ' +
    F(dA) + '>' + F(cA) + (cA - dA >= 0 ? ' +' : ' ') + (cA - dA).toFixed(3) + '   ' +
    F(dD) + '>' + F(cD) + (cD - dD >= 0 ? ' +' : ' ') + (cD - dD).toFixed(3) + '   ' +
    dGap.toFixed(3) + '->' + cGap.toFixed(3) + '  ' +
    '   ' + dV.humanAsAI + '->' + cV.humanAsAI + (cV.humanAsAI - dV.humanAsAI > 0 ? '  +' : '  ') +
    '   ' + dV.aiAsHuman + '->' + cV.aiAsHuman
  );
}
