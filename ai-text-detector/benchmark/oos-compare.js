// In-sample vs out-of-sample comparison for ONE model at the shipped decision band.
//
// compare.js joins runs by text id, which is exactly what cannot be done here: the holdout corpus
// has different texts, so the two captures share no ids. This reports each side separately and then
// the delta, which is the only honest way to read a generalisation check.
//
//   node oos-compare.js <in-sample.jsonl> <out-of-sample.jsonl> [--ai 0.69] [--human 0.40]
//
// The out-of-sample corpus may be one-sided (e.g. corpus-books.json is 46 HUMAN texts and no AI),
// in which case only the side that is present is reported -- and the script says so, rather than
// silently printing an accuracy over a class that was never tested.

const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const arg = (n, d) => {
  const i = argv.indexOf('--' + n);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d;
};
const files = argv.filter((a) => !a.startsWith('--') && argv[argv.indexOf(a) - 1] !== '--ai' && argv[argv.indexOf(a) - 1] !== '--human');
if (files.length < 2) { console.error('pass two result files: <in-sample.jsonl> <out-of-sample.jsonl>'); process.exit(1); }

const AI_CUT = parseFloat(arg('ai', '0.69'));
const HU_CUT = parseFloat(arg('human', '0.40'));

function band(p) {
  if (p === null || p === undefined) return 'DEFER';
  if (p >= AI_CUT) return 'AI';
  if (p <= HU_CUT) return 'HUMAN';
  return 'DEFER';
}

function load(f) {
  return fs.readFileSync(path.join(__dirname, f), 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

function side(rows, label) {
  const usable = rows.filter((r) => r.ok && r.ai_probability !== null && r.label === label);
  if (!usable.length) return null;
  const wrong = usable.filter((r) => band(r.ai_probability) !== label && band(r.ai_probability) !== 'DEFER');
  const def = usable.filter((r) => band(r.ai_probability) === 'DEFER');
  const right = usable.filter((r) => band(r.ai_probability) === label);
  const ps = usable.map((r) => r.ai_probability).sort((a, b) => a - b);
  const median = ps.length % 2 ? ps[(ps.length - 1) / 2] : (ps[ps.length / 2 - 1] + ps[ps.length / 2]) / 2;
  return {
    n: usable.length, right: right.length, wrong: wrong.length, defer: def.length,
    wrongIds: wrong.map((r) => r.id + ' p=' + r.ai_probability),
    min: ps[0], median, max: ps[ps.length - 1],
    distinct: new Set(ps).size,
    // accuracy over every usable text of this class: a deferral is not a correct answer.
    acc: right.length / usable.length,
  };
}

function report(tag, rows) {
  const noAnswer = rows.filter((r) => !r.ok || r.ai_probability === null).length;
  const ai = side(rows, 'AI');
  const hu = side(rows, 'HUMAN');
  console.log('\n' + tag);
  console.log('-'.repeat(tag.length));
  console.log('  rows=' + rows.length + '  no-answer=' + noAnswer);
  if (!ai && !hu) { console.log('  nothing usable'); return { ai: null, hu: null }; }
  for (const [name, s] of [['AI    ', ai], ['HUMAN ', hu]]) {
    if (!s) { console.log('  ' + name + ': NOT PRESENT in this corpus -- untested, not passed'); continue; }
    console.log('  ' + name + ': n=' + s.n + '  right=' + s.right + '  WRONG=' + s.wrong
      + '  deferred=' + s.defer + '  acc=' + s.acc.toFixed(3)
      + '  p min/med/max=' + s.min + '/' + s.median + '/' + s.max + '  distinct=' + s.distinct);
    if (s.wrongIds.length) console.log('          wrong: ' + s.wrongIds.join(', '));
  }
  return { ai, hu };
}

const A = load(files[0]);
const B = load(files[1]);
console.log('='.repeat(74));
console.log('BAND ai>=' + AI_CUT + ' hu<=' + HU_CUT + '  (a deferral counts as not-correct)');
console.log('='.repeat(74));
const a = report('IN-SAMPLE      ' + files[0], A);
const b = report('OUT-OF-SAMPLE  ' + files[1], B);

console.log('\nDELTA (out-of-sample minus in-sample)');
for (const [name, k] of [['AI', 'ai'], ['HUMAN', 'hu']]) {
  if (!a[k] || !b[k]) { console.log('  ' + name.padEnd(6) + ': cannot compare -- ' + (!a[k] ? 'absent in-sample' : 'absent out-of-sample')); continue; }
  const d = b[k].acc - a[k].acc;
  console.log('  ' + name.padEnd(6) + ': acc ' + a[k].acc.toFixed(3) + ' -> ' + b[k].acc.toFixed(3)
    + '  (' + (d >= 0 ? '+' : '') + d.toFixed(3) + ')   wrong ' + a[k].wrong + ' -> ' + b[k].wrong
    + '   deferred ' + a[k].defer + ' -> ' + b[k].defer);
}
console.log('\nRead this as a generalisation check, not a re-ranking: the two corpora are different texts,');
console.log('and HC3\'s AI class is GPT-3.5-era ChatGPT output on both sides.');
