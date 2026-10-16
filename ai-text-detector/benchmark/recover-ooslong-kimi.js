// Recover the lost ooslong-kimi-k3 capture rows from its run log.
// run-benchmark writes results-<tag>.jsonl only at exit; the node process died with exit
// 1073807364 before writing, but every completed row was printed with id/label/config/
// p/conf/latency/attempt — enough to rebuild scorable rows (signals are not recoverable
// and are left null; rows are flagged recovered_from_log).
//   node recover-ooslong-kimi.js
const fs = require('fs');
const path = require('path');

const LOG = path.join(__dirname, 'log-ooslong-kimi.txt');
const OUT = path.join(__dirname, 'results-ooslong-kimi-k3.jsonl');
const re = /\[\s*(\d+)\/(\d+)\]\s+(\S+)\s+(\S+)\s+(\S+)\s+->\s+(ERR:\S+|[\d.]+)(?:\s+conf=([\d.]+))?(.*?)(?:try(\d+))?\)/;
const rows = [];
for (const line of fs.readFileSync(LOG, 'utf8').split(/\r?\n/)) {
  const m = line.match(re);
  if (!m) continue;
  const [, , , id, label, config, scoreTok, confTok, middle, tryTok] = m;
  const lat = (line.match(/\((\d+)ms/) || [])[1] || null;
  const err = scoreTok.startsWith('ERR:');
  rows.push({
    id, label, config, text_length: null,
    ok: !err,
    error_code: err ? scoreTok.slice(4) : null,
    error_detail: null,
    salvaged: (middle || '').includes('SALVAGED'),
    truncated: (middle || '').includes('TRUNCATED'),
    ai_probability: err ? null : parseFloat(scoreTok),
    confidence: err || confTok == null ? null : parseFloat(confTok),
    signals: null,
    latency_ms: lat ? parseInt(lat, 10) : null,
    attempt: tryTok ? parseInt(tryTok, 10) : null,
    http_status: null, rate_limit: null,
    recovered_from_log: true,
  });
}
const seen = new Set(rows.map((r) => r.id));
if (seen.size !== rows.length) console.log('WARN duplicate ids (later line wins):', rows.length - seen.size);
fs.writeFileSync(OUT, rows.map((r) => JSON.stringify(r)).join('\n') + '\n');
console.log(`recovered ${rows.length} rows (${rows.filter((r) => r.ok).length} ok, ${rows.filter((r) => !r.ok).length} error) to ${path.basename(OUT)}`);
