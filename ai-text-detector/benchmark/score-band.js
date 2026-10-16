#!/usr/bin/env node
/* Score one results-<tag>.jsonl capture against the SAVED band (fit-bands CURRENT),
   mirroring the deployed decision rule and the same clean-row filter the fitter uses.
   usage: node score-band.js --tag <results-tag> [--ai .75 --hum .30 --gate 0]
   Defaults read CURRENT for the row whose MAP val/train contains the tag. */
const fs = require('fs');
const path = require('path');
const B = __dirname;
const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? process.argv[i + 1] : d; };

const TAG = arg('tag', null);
if (!TAG) { console.error('usage: node score-band.js --tag <tag> [--ai A --hum H --gate G]'); process.exit(2); }

// bands from fit-bands.js CURRENT (v3)
const BANDS = {
  'oosstress-omkiro-sonnet-4-5': [0.75, 0.30, 0.0],
  'oosstress-nemotron-ultra': [0.70, 0.45, 0.65],
  'oosstress-nv-glm-5-3': [0.65, 0.30, 0.55],
  'oosstress-nv-glm-5-3-flash': [0.80, 0.55, 0.50],
};
let band = BANDS[TAG];
const a = arg('ai', null), h = arg('hum', null), g = arg('gate', null);
if (a !== null && h !== null) band = [parseFloat(a), parseFloat(h), g === null ? 0 : parseFloat(g)];
if (!band) { console.error('no saved band for tag ' + TAG + ' — pass --ai/--hum/--gate'); process.exit(2); }

const rows = [];
for (const line of fs.readFileSync(path.join(B, 'results-' + TAG + '.jsonl'), 'utf8').split(/\r?\n/)) {
  if (!line.trim()) continue;
  let r; try { r = JSON.parse(line); } catch { continue; }
  if (!r || typeof r.ai_probability !== 'number' || !r.label) continue;
  if (!r.ok || r.salvaged || r.truncated) continue;
  rows.push({ p: r.ai_probability, c: typeof r.confidence === 'number' ? r.confidence : 1, label: r.label, id: r.id });
}
const [AI, HUM, GATE] = band;
let correct = 0, wrong = 0, review = 0, fp = 0, fn = 0;
const wrongs = [];
for (const r of rows) {
  const sure = r.c >= GATE;
  const v = sure && r.p >= AI ? 'AI' : sure && r.p <= HUM ? 'HUMAN' : null;
  if (!v) { review++; continue; }
  if (v === r.label) correct++;
  else { wrong++; if (r.label === 'HUMAN') fp++; else fn++; wrongs.push(r.id + ' ' + r.label + ' p=' + r.p + ' conf=' + r.c + ' -> ' + v); }
}
const decided = correct + wrong;
console.log('band ' + band.join('/') + '  rows=' + rows.length +
  '  decided=' + decided + ' correct=' + correct + ' wrong=' + wrong + ' (' + fp + 'FP/' + fn + 'FN)' +
  '  review=' + review +
  (decided ? '  acc/decided=' + (100 * correct / decided).toFixed(1) + '%' : '') +
  '  coverage=' + (100 * decided / rows.length).toFixed(0) + '%');
for (const w of wrongs) console.log('  WRONG ' + w);
// per-class score distribution for eyeball
const by = {};
for (const r of rows) (by[r.label] = by[r.label] || []).push(r.p);
for (const k of Object.keys(by).sort()) {
  const s = by[k].slice().sort((x, y) => x - y);
  console.log('  ' + k + ' p: min ' + s[0] + ' med ' + s[Math.floor(s.length / 2)] + ' max ' + s[s.length - 1] + ' n=' + s.length);
}
