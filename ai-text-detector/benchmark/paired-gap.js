/*
 * Paired decision-layer gap between two captures of the SAME corpus.
 *
 * AUC and the panel table both say something, and neither answers the question that matters
 * for shipping: on the texts both models answered, does one decision rule actually beat the
 * other, or is the difference 1-2 rows out of 48 (which is noise -- and on this pipeline the
 * run-to-run jitter alone flips 8 of 12 borderline texts)?
 *
 * So: join by text id, apply ONE rule to both sides, and test the only pairs that disagree.
 * That is McNemar's test, which ignores the texts both got right and both got wrong -- the
 * agreement carries no information about which model is better.
 *
 *   node benchmark/paired-gap.js results-ultra550.jsonl results-orfull-ling-3-1-flash.jsonl
 *   node benchmark/paired-gap.js <a> <b> [--ai 0.63 --human 0.52]   (one rule applied to both
 *      sides; default is the band the shipped workflow bakes)
 */
const fs = require('fs');
const path = require('path');

function arg(flag, dflt) {
  const i = process.argv.indexOf(flag);
  return i > -1 ? process.argv[i + 1] : dflt;
}

// Positional args only: a flag's value ("--ai 0.85") must not be read as a third capture.
const files = [];
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i].startsWith('--')) { i++; continue; }
  files.push(process.argv[i]);
}
if (files.length !== 2) {
  console.error('usage: node paired-gap.js <results-a.jsonl> <results-b.jsonl> [--ai 0.69 --human 0.40]');
  process.exit(1);
}
const AI_T = parseFloat(arg('--ai', 0.69));
const HU_T = parseFloat(arg('--human', 0.40));
const tag = (f) => path.basename(f).replace(/^results-/, '').replace(/\.jsonl$/, '');

function load(f) {
  const byId = new Map();
  for (const line of fs.readFileSync(path.join(__dirname, f), 'utf8').split('\n').filter(Boolean)) {
    const r = JSON.parse(line);
    if (r.ok && r.ai_probability !== null && r.ai_probability !== undefined) byId.set(r.id, r);
  }
  return byId;
}

const A = load(files[0]), B = load(files[1]);
const shared = [...A.keys()].filter((id) => B.has(id));
if (!shared.length) { console.log('no text id is scored by both captures'); process.exit(2); }

// 0 = decided correctly, 1 = decided wrongly, 2 = deferred by the band
const verdict = (r) => {
  const p = r.ai_probability;
  const isAI = r.label === 'AI';
  if (p >= AI_T) return isAI ? 0 : 1;
  if (p <= HU_T) return isAI ? 1 : 0;
  return 2;
};

let aa = 0, bb = 0, cc = 0, dd = 0, other = 0;
const discordant = [];
const deferOnly = { A: [], B: [] };
for (const id of shared) {
  const va = verdict(A.get(id)), vb = verdict(B.get(id));
  if (va === 0 && vb === 0) aa++;
  else if (va === 1 && vb === 1) bb++;
  else if (va === 0 && vb === 1) { cc++; discordant.push([id, 'A right / B wrong', A.get(id).ai_probability.toFixed(2), B.get(id).ai_probability.toFixed(2)]); }
  else if (va === 1 && vb === 0) { dd++; discordant.push([id, 'B right / A wrong', A.get(id).ai_probability.toFixed(2), B.get(id).ai_probability.toFixed(2)]); }
  else if (va === 2 && vb !== 2) deferOnly.A.push(id);
  else if (vb === 2 && va !== 2) deferOnly.B.push(id);
  else other++; // both deferred
}

const n = cc + dd;
// exact two-sided binomial under the null "each discordant text is 50/50"
function c(k) { let r = 1; for (let i = 0; i < k; i++) r = r * (n - i) / (i + 1); return r; }
const k = Math.min(cc, dd);
let p = 0;
for (let i = 0; i <= k; i++) p += c(i);
p = Math.min(1, 2 * p * Math.pow(0.5, n));

const score = (map, side) => {
  let correct = 0, wrong = 0, deferred = 0;
  for (const id of shared) {
    const v = verdict(map.get(id));
    if (v === 0) correct++; else if (v === 1) wrong++; else deferred++;
  }
  const decided = correct + wrong;
  return {
    correct, wrong, deferred,
    acc_all: (correct / shared.length),
    acc_decided: decided ? correct / decided : NaN,
  };
};
const sa = score(A, 'A'), sb = score(B, 'B');

console.log(`PAIRED GAP  ${tag(files[0])}  vs  ${tag(files[1])}`);
console.log(`rule: AI if p>=${AI_T}, HUMAN if p<=${HU_T}, else deferred   |   texts scored by both: ${shared.length}`);
console.log('');
console.log(`${'side'.padEnd(26)} correct  wrong  deferred  acc/all  acc/decided`);
[[tag(files[0]), sa], [tag(files[1]), sb]].forEach(([t, s]) => {
  console.log(`${t.padEnd(26)} ${String(s.correct).padEnd(9)} ${String(s.wrong).padEnd(7)} ${String(s.deferred).padEnd(10)} ${s.acc_all.toFixed(3).padEnd(9)} ${(isNaN(s.acc_decided) ? '—' : s.acc_decided.toFixed(3))}`);
});
console.log('');
console.log(`both right ${aa}   both wrong ${bb}   A right/B wrong ${cc}   B right/A wrong ${dd}   both deferred ${other}`);
console.log(`McNemar exact two-sided p = ${p.toFixed(4)} on ${n} discordant pair(s)` +
  (p >= 0.05 ? '  -> indistinguishable on this corpus' : '  -> survives the paired test'));
console.log('');
console.log('who the band costs (one side deferred, the other decided):');
console.log(`  only ${tag(files[0])} deferred: ${deferOnly.A.length} ${deferOnly.A.slice(0, 8).join(' ')}`);
console.log(`  only ${tag(files[1])} deferred: ${deferOnly.B.length} ${deferOnly.B.slice(0, 8).join(' ')}`);
if (discordant.length) {
  console.log('');
  console.log('the deciding disagreements (pA vs pB):');
  for (const d of discordant) console.log(`  ${d[0].padEnd(8)} ${d[1].padEnd(18)} ${d[2]} vs ${d[3]}`);
}
