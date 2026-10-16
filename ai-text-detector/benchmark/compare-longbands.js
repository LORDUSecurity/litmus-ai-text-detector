/* Quick old-vs-new band comparison on the long/short-band captures (no quota spent). */
const fs = require('fs');
const path = require('path');
const B = __dirname;

function score(rows, ai, hum, gate) {
  let c = 0, w = 0, r = 0, fp = 0, fn = 0;
  for (const x of rows) {
    if (!x.ok) continue;
    const p = x.ai_probability, cf = x.confidence ?? 1;
    let v = 'REVIEW';
    if (cf >= gate && p >= ai) v = 'AI';
    else if (cf >= gate && p <= hum) v = 'HUMAN';
    if (v === 'REVIEW') { r++; continue; }
    c++;
    const isAI = x.label === 'AI';
    if ((v === 'AI') !== isAI) { w++; if (v === 'AI') fp++; else fn++; }
  }
  return { c, w, r, fp, fn };
}
function fmt(n, s) {
  const cov = n ? ((s.c / n) * 100).toFixed(0) : '-';
  return s.c + '/' + n + ' decided (' + cov + '%) wrong=' + s.w +
         ' (' + s.fp + 'FP/' + s.fn + 'FN) review=' + s.r;
}
const load = (f) => fs.readFileSync(path.join(B, f), 'utf8').trim().split('\n').map(JSON.parse);

for (const [tag, f, rows] of [
  ['ooslong glm-5.3-flash', 'results-ooslong-glm-5-3-flash.jsonl', load('results-ooslong-glm-5-3-flash.jsonl')],
  ['oosshort glm-5.3-flash', 'results-oosshort-glm-5-3-flash.jsonl', load('results-oosshort-glm-5-3-flash.jsonl')],
  ['ooslong kimi-k3', 'results-ooslong-kimi-k3.jsonl', load('results-ooslong-kimi-k3.jsonl')],
]) {
  const n = rows.filter((x) => x.ok).length;
  console.log(tag + ' (n=' + n + ')');
  console.log('  old 0.69/0.40/gate0   : ' + fmt(n, score(rows, 0.69, 0.40, 0)));
  console.log('  old 0.69/0.40/gate0.6 : ' + fmt(n, score(rows, 0.69, 0.40, 0.6)));
}
const fl = load('results-ooslong-glm-5-3-flash.jsonl');
const ki = load('results-ooslong-kimi-k3.jsonl');
console.log('saved v2 bands:');
console.log('  flash 0.90/0.35/gate0.65: ' + fmt(fl.filter((x) => x.ok).length, score(fl, 0.90, 0.35, 0.65)));
console.log('  kimi  0.70/0.60/gate0   : ' + fmt(ki.filter((x) => x.ok).length, score(ki, 0.70, 0.60, 0)));
