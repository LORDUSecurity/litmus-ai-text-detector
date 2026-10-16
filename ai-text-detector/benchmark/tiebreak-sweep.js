/*
 * Offline sweep: every measured model as primary x every other as tie-break secondary,
 * scored through the SHIPPED Ensemble & Build Result node (n8n/test-ensemble.js reads the
 * jsCode out of ai-only-workflow.json, so these are the deployed decision rules, not a
 * reimplementation).
 *
 *   node benchmark/tiebreak-sweep.js [--thresholds 0.70,0.80,0.85,0.90,0.95] [--top 25]
 *
 * Corpus-bound: 48 HC3 texts, one run per model. Run-to-run jitter on borderline texts is
 * large, so treat single-row differences of 1-2 texts as noise, not signal. The two
 * muse-glimmer runs are the only repeated model and give a rough noise scale.
 */
const { join } = require('node:path');
const B = __dirname;
const { replay, config, evaluate, laneRecord, needSecondary, readJsonl } = require(join(B, '..', 'n8n', 'test-ensemble.js'));

// Model ids: tags after 2026-10-06 are read from runs-manifest.log; the four legacy tags
// were run before the manifest existed and are labelled from the session record.
const LEGACY = {
  ultra550: 'nvidia/nemotron-3-ultra-550b-a55b',
  gptoss20b: 'openai/gpt-oss-20b',
  baseline: 'nvidia/nemotron-3-super-120b-a12b',
  repeat01: 'nvidia/nemotron-3-super-120b-a12b (repeat)',
  t0: 'nvidia/nemotron-3-super-120b-a12b (temperature 0)',
};
function modelsFromManifest() {
  const out = {};
  try {
    const { readFileSync } = require('node:fs');
    for (const line of readFileSync(join(B, 'runs-manifest.log'), 'utf8').split(/\r?\n/)) {
      const m = line.match(/^params: (\{.*\})$/);
      if (!m) continue;
      const p = JSON.parse(m[1]);
      if (!out[p.tag]) out[p.tag] = p.model;
    }
  } catch (e) { /* manifest optional */ }
  return Object.assign({}, LEGACY, out);
}

const TAGS = ['ultra550', 'museglimmer30b', 'diffgemma26b', 'nano30b', 'lagunaxs', 'llama11v', 'gptoss20b', 'baseline', 'repeat01', 't0', 'museglimmer02'];
const MODELS = modelsFromManifest();

function load(tag) {
  const rows = readJsonl(join(B, 'results-' + tag + '.jsonl'));
  const byId = {};
  for (const r of rows) byId[r.id] = r;
  return { rows: rows, byId: byId };
}

// Primary-only: the secondary branch is never taken, so a deferring primary goes to review.
function alone(tag) {
  const cfg = config();
  const d = data[tag];
  const t = { correct: 0, wrong: 0, review: 0 };
  for (const p of d.rows) {
    const pRec = laneRecord(p, 'primary', cfg.primary_model);
    const out = evaluate(cfg, pRec, null);
    const truth = p.label === 'AI' ? 'AI_GENERATED' : 'HUMAN_LIKELY';
    if (out.classification === 'REVIEW_REQUIRED' || out.classification === 'DETECTION_UNAVAILABLE') t.review++;
    else if (out.classification === truth) t.correct++;
    else t.wrong++;
  }
  return t;
}

function pair(primary, secondary, threshold) {
  return replay(join(B, 'results-' + primary + '.jsonl'), join(B, 'results-' + secondary + '.jsonl'), threshold, true);
}

// How many texts the primary defers on: the number of second calls the pair costs.
// Independent of the secondary's cut, so it is the latency column of the table.
function deferrals(tag) {
  const cfg = config();
  let n = 0;
  for (const p of data[tag].rows) if (needSecondary(laneRecord(p, 'primary', cfg.primary_model), cfg)) n++;
  return n;
}

const argv = process.argv.slice(2);
const ti = argv.indexOf('--thresholds');
const THRESHOLDS = ti >= 0 ? argv[ti + 1].split(',').map(parseFloat) : [0.70, 0.75, 0.80, 0.85, 0.90, 0.95];
const gi = argv.indexOf('--top');
const TOP = gi >= 0 ? parseInt(argv[gi + 1], 10) : 30;

const data = {};
for (const t of TAGS) data[t] = load(t);

const pad = (s, n) => String(s).padEnd(n);
const score = (t) => t.correct * 100 - t.review * 2; // ranking key: correct first, then fewer reviews

console.log('Models measured on the 48-text HC3 corpus (n=' + data.ultra550.rows.length + ' each):');
for (const t of TAGS) {
  const a = alone(t);
  console.log('  ' + pad(t, 15) + pad(MODELS[t] || '?', 52) + ' alone: correct=' + a.correct + ' wrong=' + a.wrong + ' review=' + a.review);
}

const results = [];
for (const p of TAGS) {
  for (const s of TAGS) {
    if (p === s) continue;
    for (const th of THRESHOLDS) {
      const r = pair(p, s, th);
      results.push({ p: p, s: s, th: th, t: r.tally, rows: r.rows, key: score(r.tally) });
    }
  }
}

results.sort((a, b) => b.key - a.key || b.t.correct - a.t.correct || a.t.review - b.t.review);

const deferCache = {};
for (const t of TAGS) deferCache[t] = deferrals(t);

console.log('\nTop ' + TOP + ' of ' + results.length + ' combinations (shipped tie-break rules, deferral derived from the primary):');
console.log('  ' + pad('primary', 15) + pad('secondary', 15) + 'cut   correct wrong review  defer  acc(answered)');
for (const r of results.slice(0, TOP)) {
  const answered = r.t.correct + r.t.wrong;
  console.log('  ' + pad(r.p, 15) + pad(r.s, 15) + r.th.toFixed(2) + '   '
    + pad(r.t.correct, 8) + pad(r.t.wrong, 6) + pad(r.t.review, 7)
    + pad(deferCache[r.p], 7) + (r.t.correct / Math.max(1, answered)).toFixed(3));
}

console.log('\nBest secondary per primary (at each cut, vs that primary alone):');
console.log('  ' + pad('primary', 15) + 'alone  ' + THRESHOLDS.map((t) => pad('@' + t.toFixed(2), 10)).join(''));
for (const p of TAGS) {
  const a = alone(p);
  const cells = THRESHOLDS.map((th) => {
    let best = null;
    for (const r of results) {
      if (r.p !== p || r.th !== th) continue;
      if (!best || score(r.t) > score(best.t)) best = r;
    }
    return pad(best ? best.t.correct + '/' + best.s.slice(0, 4) : '-', 10);
  });
  console.log('  ' + pad(p, 15) + pad(a.correct, 7) + cells.join(''));
}

// Noise scale: the same model run twice, as primary and as secondary, must not change the
// ranking by much. It does, which is why the table above is read in bands, not row by row.
const dupA = 'museglimmer30b', dupB = 'museglimmer02';
console.log('\nRun-to-run noise check: ' + dupA + ' vs ' + dupB + ' (same model, two runs), as pairs against each other:');
for (const th of [0.8, 0.9]) {
  for (const [a, b] of [[dupA, dupB], [dupB, dupA]]) {
    const t = pair(a, b, th).tally;
    console.log('  ' + pad(a, 15) + ' + ' + pad(b, 15) + ' @' + th.toFixed(2)
      + ': correct=' + t.correct + ' wrong=' + t.wrong + ' review=' + t.review);
  }
}

// Per-text detail for one combination, and what it changes vs the deployed pair.
//   node tiebreak-sweep.js --detail ultra550,diffgemma26b,0.95
const di = argv.indexOf('--detail');
if (di >= 0 && argv[di + 1]) {
  const [dp, ds, dth] = argv[di + 1].split(',');
  const deployed = { p: 'ultra550', s: 'diffgemma26b', th: 0.9 };
  const ref = pair(deployed.p, deployed.s, deployed.th);
  const detail = pair(dp, ds, parseFloat(dth));
  const byId = {};
  for (const r of ref.rows) byId[r.id] = r;
  console.log('\nDetail ' + dp + ' + ' + ds + ' @' + parseFloat(dth).toFixed(2) + ' vs deployed '
    + deployed.p + ' + ' + deployed.s + ' @' + deployed.th.toFixed(2) + ':');
  for (const r of detail.rows) {
    const o = byId[r.id];
    const flip = o && o.outcome !== r.outcome ? '   <== deployed: ' + o.outcome + '/' + o.method : '';
    if (r.outcome !== 'correct' || flip) {
      console.log('  ' + pad(r.id, 9) + pad(r.label, 7) + pad(r.outcome, 9) + pad(r.method, 30)
        + 'p=' + pad(r.p, 7) + 'lane=' + pad(r.lane, 11) + (o ? o.outcome : '-') + flip);
    }
  }
  const dt = detail.tally, rt = ref.tally;
  console.log('  ' + dp + '+@' + parseFloat(dth).toFixed(2) + ': ' + dt.correct + '/' + dt.wrong + '/' + dt.review
    + '   deployed: ' + rt.correct + '/' + rt.wrong + '/' + rt.review
    + '   defer ' + deferCache[dp] + ' vs ' + deferCache[deployed.p]);
}
