/*
 * Scratch check for run-benchmark.js --resume: fabricate an all-ok results file for the
 * current book corpus, then run with --resume and confirm zero provider calls are made
 * and the file round-trips unchanged. Deletes its own scratch artefacts.
 */
const fs = require('fs');
const { join } = require('node:path');
const B = __dirname;

const corpus = JSON.parse(fs.readFileSync(join(B, 'corpus-books.json'), 'utf8'));
const tag = 'resumetest';
const out = join(B, 'results-' + tag + '.jsonl');
const rows = corpus.map((it) => ({
  id: it.id, label: it.label, config: it.config, text_length: it.text.length,
  ok: true, error_code: null, error_detail: null, salvaged: false, truncated: false,
  ai_probability: 0.02, confidence: 0.99, signals: [], latency_ms: 1, attempt: 1, http_status: 200,
}));
const before = rows.map((r) => JSON.stringify(r)).join('\n') + '\n';
fs.writeFileSync(out, before);

const t0 = Date.now();
require('child_process').execFileSync(process.execPath, [
  join(B, 'run-benchmark.js'), '--tag', tag, '--model', 'google/diffusiongemma-26b-a4b-it',
  '--corpus', 'corpus-books.json', '--limit', '60', '--resume', '--concurrency', '1',
], { cwd: B, stdio: 'inherit', timeout: 120000 });
const after = fs.readFileSync(out, 'utf8');

console.log('\nresume check');
console.log('  file identical after resume-with-nothing-to-do : ' + (after === before));
console.log('  rows                                           : ' + after.trim().split(/\r?\n/).length + '/' + corpus.length);
console.log('  wall clock                                     : ' + (Date.now() - t0) + 'ms (no calls expected)');
fs.unlinkSync(out);
console.log('  scratch removed                                : ' + !fs.existsSync(out));
