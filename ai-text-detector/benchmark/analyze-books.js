// False-positive report for the Gutenberg corpus. Every text in corpus-books.json is
// real published human prose, so there is no accuracy number to compute here -- the only
// question is how often a model calls a book passage AI under the shipped cuts.
//
//   node analyze-books.js [results-*.jsonl ...]     (default: every results-book*.jsonl)
//
// A passage in the 0.40-0.69 band is not a verdict: it is a deferral that the tie-break
// lane answers on its own scale, so the band column matters as much as the AI column.

const fs = require('fs');
const path = require('path');

const dir = __dirname;
const AI_CUT = 0.69;
const HUMAN_CUT = 0.40;

function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  if (!s.length) return NaN;
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
}

const files = process.argv.length > 2
  ? process.argv.slice(2)
  : fs.readdirSync(dir).filter((f) => /^results-book.*\.jsonl$/.test(f)).sort();

if (!files.length) {
  console.log('no results-book*.jsonl yet');
  process.exit(0);
}

const rowsOf = (f) => fs.readFileSync(path.join(dir, f), 'utf8')
  .split('\n').filter(Boolean).map((l) => JSON.parse(l));

const pad = (v, n) => String(v).padEnd(n);

// The results rows carry no model field -- provenance lives in runs-manifest.log, one
// params line per run, keyed by tag. Same file backend/seed.py reads.
const MANIFEST = path.join(dir, 'runs-manifest.log');
const byTag = {};
if (fs.existsSync(MANIFEST)) {
  for (const line of fs.readFileSync(MANIFEST, 'utf8').split(/\r?\n/)) {
    if (!line.startsWith('params: ')) continue;
    try {
      const p = JSON.parse(line.slice('params: '.length));
      byTag[p.tag] = p;
    } catch (e) { /* an unparseable line is not worth losing the rest of the log over */ }
  }
}
// A run captured before the harness appended its manifest line still names its model on the
// first line of its own run log, so fall back to that instead of printing '?' -- the same
// source backend/seed.py uses, so the two can never attribute one file to two models.
const modelFromRunLog = (tag) => {
  const log = path.join(dir, 'run-' + tag + '.log');
  if (!fs.existsSync(log)) return null;
  const head = fs.readFileSync(log, 'utf8').split(/\r?\n/).slice(0, 3).join('\n');
  return (head.match(/\bmodel=(\S+)/) || [])[1] || null;
};
const modelOf = (f) => {
  const tag = path.basename(f).replace(/^results-/, '').replace(/\.jsonl$/, '');
  return byTag[tag]?.model || modelFromRunLog(tag) || '?';
};

console.log(pad('file', 32) + pad('model', 44) + 'usable med   max   <=0.40  band  >=0.69  FP%');
for (const f of files) {
  const rows = rowsOf(f);
  const ok = rows.filter((r) => r.ok && typeof r.ai_probability === 'number');
  const model = modelOf(f);
  if (!ok.length) {
    console.log(pad(f, 32) + pad(model, 44) + '0 usable of ' + rows.length);
    continue;
  }
  const p = ok.map((r) => r.ai_probability);
  const under = ok.filter((r) => r.ai_probability <= HUMAN_CUT).length;
  const band = ok.filter((r) => r.ai_probability > HUMAN_CUT && r.ai_probability < AI_CUT).length;
  const over = ok.filter((r) => r.ai_probability >= AI_CUT).length;
  console.log(
    pad(f, 32)
    + pad(model, 44)
    + pad(`${ok.length}/${rows.length}`, 7)
    + pad(median(p).toFixed(3), 7)
    + pad(Math.max(...p).toFixed(2), 6)
    + pad(under, 7)
    + pad(band, 6)
    + pad(over, 7)
    + (100 * over / ok.length).toFixed(1) + '%',
  );
  const deferred = ok.filter((r) => r.ai_probability > HUMAN_CUT && r.ai_probability < AI_CUT);
  if (deferred.length) {
    console.log('    in band (tie-break decides): ' +
      deferred.map((r) => `${r.text_id} ${r.ai_probability}`).join(', '));
  }
  const fps = ok.filter((r) => r.ai_probability >= AI_CUT);
  if (fps.length) {
    console.log('    called AI: ' + fps.map((r) => `${r.text_id} ${r.ai_probability} conf ${r.confidence}`).join(', '));
  }
}
