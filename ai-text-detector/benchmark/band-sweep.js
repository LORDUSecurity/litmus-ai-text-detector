#!/usr/bin/env node
/* Grid a model's band over train+val+stress and report each zone separately.
   Question this answers: does ANY (ai,hum,gate) make stress wrongs vanish while the
   old corpora stay >=95% acc/decided? If none, the failure is the model, not the band.
   usage: node band-sweep.js --tag <stress-tag> --row <1|22|23|30> */
const fs = require('fs');
const path = require('path');
const B = __dirname;
const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? process.argv[i + 1] : d; };

const ROWS = {
  1:  { train: ['ultra550', 'clean550-hc3'], val: ['clean550-holdout', 'bookultra550', 'freshai-550'] },
  22: { train: ['nvfull-glm-5-3'], val: ['oos-nv-glm-5-3', 'book-nv-glm-5-3'] },
  23: { train: ['nvfull-glm-5-3-flash', 'clean-nvfull-glm-5-3-flash'], val: ['ooslong-glm-5-3-flash', 'oosshort-glm-5-3-flash'] },
  30: { train: ['nocap-clean-omkiro-sonnet-4-5'],
        val: ['nocap-holdout-omkiro-sonnet-4-5'] },
};
function load(tag) {
  const f = path.join(B, `results-${tag}.jsonl`);
  if (!fs.existsSync(f)) return null;
  const out = [];
  for (const line of fs.readFileSync(f, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    let r; try { r = JSON.parse(line); } catch { continue; }
    if (!r || typeof r.ai_probability !== 'number' || !r.label) continue;
    if (!r.ok || r.salvaged || r.truncated) continue;
    out.push({ p: r.ai_probability, c: typeof r.confidence === 'number' ? r.confidence : 1, label: r.label });
  }
  return out.length >= 12 ? out : null;
}
function score(rows, ai, hum, gate) {
  let correct = 0, wrong = 0, review = 0, fp = 0, fn = 0;
  for (const r of rows) {
    const sure = r.c >= gate;
    const v = sure && r.p >= ai ? 'AI' : sure && r.p <= hum ? 'HUMAN' : null;
    if (!v) { review++; continue; }
    if (v === r.label) correct++; else { wrong++; if (r.label === 'HUMAN') fp++; else fn++; }
  }
  return { correct, wrong, review, fp, fn, decided: correct + wrong, n: rows.length };
}

const stressTag = arg('tag', 'oosstress-nemotron-ultra');
const row = parseInt(arg('row', '1'), 10);
const spec = ROWS[row];
const OLD = spec.train.map(load).filter(Boolean).flat().concat(spec.val.map(load).filter(Boolean));
const S = load(stressTag);
if (!S) { console.error('no stress capture for ' + stressTag); process.exit(2); }
console.log(`row ${row}: old fit data=${OLD.length} rows, stress=${S.length} rows`);
const cands = [];
for (let aq = 0.10; aq <= 0.90 + 1e-9; aq += 0.05)
  for (let hq = 0.05; hq <= Math.min(0.60, aq - 0.05) + 1e-9; hq += 0.05) {
    const ai = +aq.toFixed(2), hum = +hq.toFixed(2);
    for (const gate of [0.0, 0.5, 0.55, 0.6, 0.65, 0.7]) {
      const o = score(OLD, ai, hum, gate), s = score(S, ai, hum, gate);
      cands.push({ ai, hum, gate, o, s });
    }
  }
// the ask: stress wrongs == 0, old acc/decided >= 0.95, maximize (old decided + stress decided)
const clean = cands.filter(c => c.s.wrong === 0 && c.o.decided >= 24 && c.s.decided >= 8 &&
  c.o.correct / c.o.decided >= 0.95 && c.s.correct / c.s.decided >= 0.95);
clean.sort((a, b) => (b.o.decided + b.s.decided) - (a.o.decided + a.s.decided) || a.o.wrong - b.o.wrong);
if (!clean.length) {
  console.log('NO band gets stress wrong=0 while keeping old acc/decided >= 0.95.');
  // what is the best achievable on stress alone, and what does it cost the old data?
  const best = cands.slice().sort((a, b) => a.s.wrong - b.s.wrong || (b.s.decided) - (a.s.decided))[0];
  console.log(`best-on-stress band ${best.ai}/${best.hum}/g${best.gate}: stress w=${best.s.wrong} d=${best.s.decided}/${best.s.n} | old w=${best.o.wrong} d=${best.o.decided}/${best.o.n} acc=${(100 * best.o.correct / best.o.decided).toFixed(1)}%`);
} else {
  console.log(`${clean.length} band(s) satisfy the ask. top by combined decided:`);
  for (const c of clean.slice(0, 6))
    console.log(`  ${c.ai}/${c.hum}/g${c.gate}: old d=${c.o.decided}/${c.o.n} w=${c.o.wrong}(${c.o.fp}FP/${c.o.fn}FN) acc=${(100 * c.o.correct / c.o.decided).toFixed(1)}% | stress d=${c.s.decided}/${c.s.n} w=${c.s.wrong}`);
}
// also report the shipped band for reference
const shipped = { 1: [0.70, 0.45, 0.65], 22: [0.65, 0.30, 0.55], 23: [0.80, 0.55, 0.50], 30: [0.80, 0.30, 0.0] }[row];
const o = score(OLD, ...shipped), s = score(S, ...shipped);
console.log(`shipped ${shipped.join('/')}: old d=${o.decided}/${o.n} w=${o.wrong} | stress d=${s.decided}/${s.n} w=${s.wrong}(${s.fp}FP/${s.fn}FN)`);
