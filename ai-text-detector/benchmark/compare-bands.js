// Does each newly saved band actually make BETTER DECISIONS than the old shipped band,
// measured ONLY on corpora the fit never saw? Also prints the tightest margin among the
// val decisions the new band makes (a 0-wrong claim with a 0.00 margin is luck, not a band).
//   node compare-bands.js            -> val-only, old vs new per model
// load/score semantics copied verbatim from fit-bands.js (same decision rule as deployment).
const fs = require('fs');
const path = require('path');
const B = __dirname;

const MAP = {
  1:  { name: 'nemotron-ultra',   old: [0.69, 0.40, 0.60], neu: [0.85, 0.40, 0.65], train: ['ultra550', 'clean550-hc3'], val: ['clean550-holdout', 'bookultra550', 'freshai-550'] },
  2:  { name: 'diffusiongemma',   old: [0.69, 0.40, 0.60], neu: [0.90, 0.30, 0.00], train: ['diffgemma26b', 'cleandiff-hc3'], val: ['cleandiff-holdout', 'bookdiffgemma'] },
  4:  { name: 'ultra:free mirror', old: [0.56, 0.45, 0.60], neu: [0.90, 0.30, 0.75], train: ['orfull-nvidia-nemotron-3-ultra-550b-a55b-free', 'clean-orfull-nemotron-ultra-free'], val: [] },
  10: { name: 'ling-3.1-flash',   old: [0.63, 0.52, 0.00], neu: [0.75, 0.50, 0.00], train: ['orfull-ling-3-1-flash', 'clean-orfull-ling-3-1-flash'], val: [] },
  21: { name: 'gemma-4-31b',      old: [0.69, 0.40, 0.00], neu: [0.90, 0.35, 0.00], train: ['nvfull-gemma-4-31b-it', 'clean-nvfull-gemma-4-31b-it'], val: [] },
  22: { name: 'glm-5.3',          old: [0.69, 0.40, 0.00], neu: [0.65, 0.30, 0.55], train: ['nvfull-glm-5-3'], val: ['oos-nv-glm-5-3', 'book-nv-glm-5-3'] },
  23: { name: 'glm-5.3-flash',    old: [0.69, 0.40, 0.00], neu: [0.90, 0.35, 0.65], train: ['nvfull-glm-5-3-flash', 'clean-nvfull-glm-5-3-flash'], val: ['ooslong-glm-5-3-flash', 'oosshort-glm-5-3-flash'] },
  26: { name: 'kimi-k3',          old: [0.69, 0.40, 0.60], neu: [0.70, 0.60, 0.00], train: ['nvfull-kimi-k3'], val: ['nvholdout-kimi-k3', 'nvholdout2-kimi-k3'] },
  30: { name: 'sonnet-4.5',       old: [0.69, 0.40, 0.00], neu: [0.90, 0.30, 0.00], train: ['omfull-kiro-claude-sonnet-4-5', 'clean-omfull-kiro-claude-sonnet-4-5'], val: ['oos-omkiro-sonnet-4-5', 'clean-oos-omkiro-sonnet-4-5', 'book-omkiro-sonnet-4-5'] },
  31: { name: 'haiku-4.5',        old: [0.69, 0.40, 0.00], neu: [0.85, 0.35, 0.00], train: ['omfull-kiro-claude-haiku-4-5', 'clean-omfull-kiro-claude-haiku-4-5'], val: [] },
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
    rows.push({ id: r.id, p: r.ai_probability, c: typeof r.confidence === 'number' ? r.confidence : 1, label: r.label, src: tag });
  }
  return rows.length >= 12 ? rows : null;
}

function score(rows, ai, hum, gate) {
  let correct = 0, wrong = 0, review = 0, fp = 0, fn = 0;
  const margins = [];
  for (const r of rows) {
    const sure = r.c >= gate;
    const v = sure && r.p >= ai ? 'AI' : sure && r.p <= hum ? 'HUMAN' : null;
    if (!v) { review++; continue; }
    const margin = v === 'AI' ? r.p - ai : hum - r.p;
    margins.push({ id: r.id, v, margin });
    if (v === r.label) correct++;
    else { wrong++; if (r.label === 'HUMAN') fp++; else fn++; }
  }
  margins.sort((a, b) => a.margin - b.margin);
  return { correct, wrong, review, fp, fn, tightest: margins[0] || null };
}

const fmt = (s, n) => `c${s.correct} w${s.wrong}(${s.fp}F/${s.fn}N) r${s.review} cov${n ? Math.round(100 * (s.correct + s.wrong) / n) : 0}%`;
const inS = (s) => s.tightest ? ` tightest ${s.tightest.id} ${s.tightest.v}@${s.tightest.margin.toFixed(2)}` : '';

for (const [idStr, m] of Object.entries(MAP)) {
  const val = m.val.map(load).filter(Boolean).flat();
  const train = m.train.map(load).filter(Boolean).flat();
  const set = val.length ? val : train;
  const kind = val.length ? 'VAL-only' : 'train-only (NO OUT-OF-SAMPLE DATA)';
  const o = score(set, ...m.old), n = score(set, ...m.neu);
  console.log(`${idStr} ${m.name.padEnd(18)} [${kind}, n=${set.length}]`);
  console.log(`   old ${m.old.join('/')} : ${fmt(o, set.length)}`);
  console.log(`   new ${m.neu.join('/')} : ${fmt(n, set.length)}${inS(n)}`);
  const better = n.wrong < o.wrong || (n.wrong === o.wrong && (n.correct > o.correct));
  console.log(`   => ${n.wrong < o.wrong ? 'fewer wrong' : n.wrong > o.wrong ? 'MORE wrong' : 'same wrong'}; ${n.correct > o.correct ? 'more correct decided' : n.correct < o.correct ? 'fewer decided' : 'same decided'}; deferrals ${o.review} -> ${n.review}`);
}
