// One-off scoring of the 7 resumed OpenRouter lanes at the deployed band.
// AI verdict: p>=0.69 && conf>=0.6 ; HUMAN: p<=0.40 && conf>=0.6 ; else defer.
// AUC = Mann-Whitney over answered rows, ties 0.5. Prints one line per lane.
const fs = require('fs');
const LANES = [
  ['ling-3.1 dirty', 'benchmark/results-orfull-ling-3-1-flash.jsonl'],
  ['ling-3.1 clean', 'benchmark/results-clean-orfull-ling-3-1-flash.jsonl'],
  ['or/free dirty', 'benchmark/results-orfull-openrouter-free.jsonl'],
  ['or/free clean', 'benchmark/results-clean-orfull-openrouter-free.jsonl'],
  ['lfm-2.5 dirty', 'benchmark/results-orfull-lfm-2-5-2-6b-free.jsonl'],
  ['lfm-2.5 clean', 'benchmark/results-clean-orfull-lfm-2-5.jsonl'],
  ['ling-3.0-sante dirty(31)', 'benchmark/results-orfull-inclusionai-ling-3-0-flash-sante-free.jsonl'],
];
const med = (a) => { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
for (const [name, file] of LANES) {
  const rows = fs.readFileSync(file, 'utf8').trim().split('\n').map(JSON.parse);
  const ok = rows.filter((r) => r.ok && typeof r.ai_probability === 'number');
  const ai = ok.filter((r) => r.label === 'AI').map((r) => r.ai_probability);
  const hu = ok.filter((r) => r.label === 'HUMAN').map((r) => r.ai_probability);
  let wins = 0, ties = 0;
  for (const a of ai) for (const h of hu) { if (a > h) wins++; else if (a === h) ties++; }
  const auc = (wins + 0.5 * ties) / (ai.length * hu.length);
  let hAsAI = 0, aAsH = 0, defer = 0, correct = 0;
  for (const r of ok) {
    const gate = r.confidence >= 0.6;
    const v = gate && r.ai_probability >= 0.69 ? 'AI' : gate && r.ai_probability <= 0.40 ? 'HUMAN' : 'REVIEW';
    if (v === 'REVIEW') defer++;
    else if (v === r.label) correct++;
    else if (r.label === 'HUMAN') hAsAI++;
    else aAsH++;
  }
  const f = (x) => Number.isFinite(x) ? x.toFixed(3) : 'n/a';
  console.log(`${name}\tusable=${ok.length}/48\tauc=${f(auc)}\twrong=${hAsAI + aAsH}(hAsAI=${hAsAI},aAsH=${aAsH})\tdefer=${defer}\taccCorpus=${f(correct / 48)}\tmedAI=${f(med(ai))}\tmedHU=${f(med(hu))}`);
}
