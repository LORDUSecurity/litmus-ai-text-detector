// join the 3 response-time legs per model x text (task 91)
const fs = require('fs');
const path = require('path');
const files = {
  'nemotron-3-ultra': 'results-appgw-nemotron-ultra.jsonl',
  'glm-5.3-flash': 'results-appgw-glm53flash.jsonl',
  'gemma-4-31b': 'results-appgw-gemma4-31b.jsonl',
  'deepseek-v4.1': 'results-appgw-deepseek-v41.jsonl',
};
const gw = {};
for (const [m, f] of Object.entries(files)) {
  gw[m] = {};
  for (const l of fs.readFileSync(path.join(__dirname, f), 'utf8').split('\n').filter((x) => x.trim())) {
    const r = JSON.parse(l);
    (gw[m][r.id] = gw[m][r.id] || []).push({ ms: r.latency_ms, err: r.http_status >= 400 || r.error_code === 'exception' });
  }
}
const rows = fs.readFileSync(path.join(__dirname, 'results-appbench.jsonl'), 'utf8')
  .split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
const good = rows.filter((r) => r.http === 200 && r.wall_ms > 500);
for (const m of Object.keys(files)) {
  for (const t of ['short-hu', 'med-ai', 'long-hu', 'defer-ai']) {
    const g = (gw[m][t] || []).map((x) => x.err ? 'ERR' + Math.round(x.ms / 1000) + 's' : Math.round(x.ms / 1000) + 's').join('/');
    const w = good.filter((r) => r.leg === 'webhook' && r.model.includes(m) && r.text === t).map((r) => Math.round(r.wall_ms / 1000) + 's').join('/');
    const d = good.filter((r) => r.leg === 'detect' && r.model.includes(m) && r.text === t).map((r) => Math.round(r.wall_ms / 1000) + 's').join('/');
    const b = good.filter((r) => r.leg === 'detect' && r.model.includes(m) && r.text === t && r.backend_ms > 500).map((r) => Math.round(r.backend_ms / 1000) + 's').join('/');
    console.log(`${m.padEnd(16)} ${t.padEnd(9)} gw=[${g}] webhook=[${w}] detect=[${d}] backend=[${b}]`);
  }
}
