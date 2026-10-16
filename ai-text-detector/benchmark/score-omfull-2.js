// Score the two OmniRoute full captures at the deployed band (same logic as score-resume-7).
const fs = require('fs');
const LANES = [
  ['omfull kiro/glm-5', 'benchmark/results-omfull-kiro-glm-5.jsonl'],
  ['omfull no-think sonnet', 'benchmark/results-omfull-nothink-kiro-claude-sonnet-4-5.jsonl'],
];
const med = (a) => { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
for (const [name, file] of LANES) {
  const rows = fs.readFileSync(file, 'utf8').trim().split('\n').map(JSON.parse);
  const ok = rows.filter((r) => r.ok && typeof r.ai_probability === 'number');
  const ai = ok.filter((r) => r.label === 'AI').map((r) => r.ai_probability);
  const hu = ok.filter((r) => r.label === 'HUMAN').map((r) => r.ai_probability);
  let w = 0, t = 0;
  for (const a of ai) for (const h of hu) { if (a > h) w++; else if (a === h) t++; }
  const auc = (w + 0.5 * t) / (ai.length * hu.length);
  let hAsAI = 0, aAsH = 0, defer = 0, correct = 0;
  const misses = [];
  for (const r of ok) {
    const gate = r.confidence >= 0.6;
    const v = gate && r.ai_probability >= 0.69 ? 'AI' : gate && r.ai_probability <= 0.40 ? 'HUMAN' : 'REVIEW';
    if (v === 'REVIEW') defer++; else if (v === r.label) correct++;
    else { if (r.label === 'HUMAN') hAsAI++; else aAsH++; misses.push(`${r.id}:${r.label}->${v}@p=${r.ai_probability},conf=${r.confidence}`); }
  }
  const f = (x) => Number.isFinite(x) ? x.toFixed(3) : 'n/a';
  console.log(`${name}\tusable=${ok.length}/48\tauc=${f(auc)}\twrong=${hAsAI + aAsH}(hAsAI=${hAsAI},aAsH=${aAsH})\tdefer=${defer}\taccCorpus=${f(correct / 48)}\tmedAI=${f(med(ai))}\tmedHU=${f(med(hu))}`);
  console.log(`  misses: ${misses.join(' ') || 'none'}`);
}
