// Score screen lanes (corpus-screen12: 6 AI / 6 HUMAN) at the deployed band.
// Tag→model from the last manifest params line per tag. Prefix via argv[2], default om2screen-.
const fs = require('fs');
const PRE = process.argv[2] || 'om2screen-';
const med = (a) => { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const lines = fs.readFileSync('benchmark/runs-manifest.log', 'utf8').trim().split('\n');
const byTag = {};
for (const l of lines) {
  if (!l.startsWith('params: ')) continue;
  try { const p = JSON.parse(l.slice(8)); if (p.tag && p.tag.startsWith(PRE)) byTag[p.tag] = p; } catch {}
}
for (const tag of Object.keys(byTag).sort()) {
  const file = `benchmark/results-${tag}.jsonl`;
  if (!fs.existsSync(file)) { console.log(`${tag}\tNOFILE`); continue; }
  const rows = fs.readFileSync(file, 'utf8').trim().split('\n').map(JSON.parse);
  const ok = rows.filter((r) => r.ok && typeof r.ai_probability === 'number');
  const ai = ok.filter((r) => r.label === 'AI').map((r) => r.ai_probability);
  const hu = ok.filter((r) => r.label === 'HUMAN').map((r) => r.ai_probability);
  let w = 0, t = 0;
  for (const a of ai) for (const h of hu) { if (a > h) w++; else if (a === h) t++; }
  const auc = ai.length && hu.length ? (w + 0.5 * t) / (ai.length * hu.length) : NaN;
  let hAsAI = 0, aAsH = 0, defer = 0, correct = 0;
  for (const r of ok) {
    const gate = r.confidence >= 0.6;
    const v = gate && r.ai_probability >= 0.69 ? 'AI' : gate && r.ai_probability <= 0.40 ? 'HUMAN' : 'REVIEW';
    if (v === 'REVIEW') defer++; else if (v === r.label) correct++; else if (r.label === 'HUMAN') hAsAI++; else aAsH++;
  }
  const lat = ok.map((r) => r.latency_ms).sort((a, b) => a - b);
  const p50 = lat.length ? (lat[lat.length >> 1] / 1000).toFixed(1) : 'n/a';
  const f = (x) => Number.isFinite(x) ? x.toFixed(3) : 'n/a';
  console.log(`${byTag[tag].model}\tusable=${ok.length}/12\tauc=${f(auc)}\twrong=${hAsAI + aAsH}(hAsAI=${hAsAI},aAsH=${aAsH})\tdefer=${defer}\tacc=${f(correct / 12)}\tmedAI=${f(med(ai))}\tmedHU=${f(med(hu))}\tp50s=${p50}`);
}
