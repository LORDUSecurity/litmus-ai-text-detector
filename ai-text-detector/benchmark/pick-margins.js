/* Margin audit for the K=5 knee picks: how close do decided calls sit to their cut,
   and which of those near-cut calls are WRONG (a 0.00-margin correct call can only
   jitter to REVIEW, a near-cut wrong call means the cut is inside the wrong class). */
const fs = require('fs');
const path = require('path');
const B = __dirname;
const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? process.argv[i + 1] : d; };

const PICKS = JSON.parse(fs.readFileSync(path.join(B, 'knee-picks.json'), 'utf8'));

// MAP lives in fit-bands.js; re-declare the subset needed here rather than scraping the source.
const MAP = {
  1:  { train: ['ultra550', 'clean550-hc3'], val: ['clean550-holdout', 'bookultra550', 'freshai-550'] },
  2:  { train: ['diffgemma26b', 'cleandiff-hc3'], val: ['cleandiff-holdout', 'bookdiffgemma'] },
  4:  { train: ['orfull-nvidia-nemotron-3-ultra-550b-a55b-free', 'clean-orfull-nemotron-ultra-free'], val: [] },
  10: { train: ['orfull-ling-3-1-flash', 'clean-orfull-ling-3-1-flash'], val: [] },
  21: { train: ['nvfull-gemma-4-31b-it', 'clean-nvfull-gemma-4-31b-it'], val: [] },
  22: { train: ['nvfull-glm-5-3'], val: ['oos-nv-glm-5-3', 'book-nv-glm-5-3'] },
  23: { train: ['nvfull-glm-5-3-flash', 'clean-nvfull-glm-5-3-flash'], val: ['ooslong-glm-5-3-flash', 'oosshort-glm-5-3-flash'] },
  24: { train: ['nvfull-nemotron-3-5-lightning-30b', 'clean-nvfull-nemotron-3-5-lightning'], val: [] },
  25: { train: ['nvfull-deepseek-v4-1-flash'], val: [] },
  26: { train: ['nvfull-kimi-k3'], val: ['nvholdout-kimi-k3', 'nvholdout2-kimi-k3'] },
  30: { train: ['omfull-kiro-claude-sonnet-4-5', 'clean-omfull-kiro-claude-sonnet-4-5'],
        val: ['oos-omkiro-sonnet-4-5', 'clean-oos-omkiro-sonnet-4-5', 'book-omkiro-sonnet-4-5'] },
  31: { train: ['omfull-kiro-claude-haiku-4-5', 'clean-omfull-kiro-claude-haiku-4-5'], val: [] },
};

function load(tag) {
  const f = path.join(B, `results-${tag}.jsonl`);
  if (!fs.existsSync(f)) return null;
  const rows = [];
  for (const line of fs.readFileSync(f, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    let r; try { r = JSON.parse(line); } catch { continue; }
    if (!r || typeof r.ai_probability !== 'number' || !r.label) continue;
    if (!r.ok || r.salvaged || r.truncated) continue;
    rows.push({ p: r.ai_probability, c: typeof r.confidence === 'number' ? r.confidence : 1, label: r.label, id: r.id });
  }
  return rows.length >= 12 ? rows : null;
}

for (const pk of PICKS) {
  if (!pk.changed || pk.ai === null) continue;
  const spec = MAP[pk.row_id];
  const rows = spec.train.map(load).filter(Boolean).concat(spec.val.map(load).filter(Boolean)).flat();
  let minM = 9, minId = '', near = 0, nearWrong = 0, n00 = 0;
  for (const r of rows) {
    if (!(r.c >= pk.gate)) continue;
    let cut = null;
    if (r.p >= pk.ai) cut = pk.ai; else if (r.p <= pk.hum) cut = pk.hum; else continue;
    const m = Math.abs(r.p - cut);
    if (m < minM - 1e-9) { minM = m; minId = r.id; }
    if (m <= 0.0201) { near++; if ((r.p >= pk.ai ? 'AI' : 'HUMAN') !== r.label) nearWrong++; }
    if (m <= 1e-9) n00++;
  }
  const prov = spec.val.map(load).filter(Boolean).length ? '' : '  [TRAIN-ONLY: in-sample, provisional]';
  console.log(`row ${pk.row_id} ${pk.model} @ ${pk.ai}/${pk.hum}/g${pk.gate}: ` +
    `tightest margin ${minM.toFixed(2)} (${minId}), ${n00} exact-on-cut, ${near} within 0.02 (${nearWrong} of them WRONG)${prov}`);
}
