/*
 * Merge the two stress halves into corpus-stress.json and prove it is a NEW corpus.
 *
 * Guards (all fatal, not warnings):
 *  - every row inside --min/--max char band (default 3000-6000, matching corpus-long's
 *    ceiling and above it);
 *  - id uniqueness;
 *  - normalized-text hash overlap with ANY other corpus passed via --against
 *    (default set) must be zero — a stress row that equals an already-benchmarked row
 *    would silently double-count in scoring.
 *
 *   node merge-stress-corpus.js [--human corpus-stress-human.json] [--ai corpus-stress-ai.json]
 *                               [--out corpus-stress.json] [--min 3000] [--max 6000]
 *                               [--against f1,f2,...]
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const B = __dirname;
const HUMAN = path.join(B, arg('human', 'corpus-stress-human.json'));
const AI = path.join(B, arg('ai', 'corpus-stress-ai.json'));
const OUT = path.join(B, arg('out', 'corpus-stress.json'));
const MIN = parseInt(arg('min', '3000'), 10);
const MAX = parseInt(arg('max', '6000'), 10);
const AGAINST = arg('against', 'corpus-books.json,corpus.json,corpus-holdout.json,corpus-long.json,corpus-short.json,corpus-clean.json')
  .split(',').map((s) => s.trim()).filter(Boolean);

const norm = (s) => s.replace(/\s+/g, ' ').trim().toLowerCase();
const h = (s) => crypto.createHash('sha256').update(norm(s)).digest('hex');

const human = JSON.parse(fs.readFileSync(HUMAN, 'utf8'));
const ai = JSON.parse(fs.readFileSync(AI, 'utf8'));
const merged = human.concat(ai);

const fail = [];
for (const r of merged) {
  if (!r.text || !r.label || !r.id) fail.push(`${r.id || '?'}: missing id/text/label`);
  if (r.text && (r.text.length < MIN || r.text.length > MAX)) fail.push(`${r.id}: ${r.text.length} chars outside ${MIN}-${MAX}`);
}
const ids = new Set(merged.map((r) => r.id));
if (ids.size !== merged.length) fail.push('duplicate ids across halves');

const foreign = new Set();
for (const f of AGAINST) {
  const p = path.join(B, f);
  if (!fs.existsSync(p)) { console.log(`  (skip --against ${f}: not present)`); continue; }
  for (const row of JSON.parse(fs.readFileSync(p, 'utf8'))) foreign.add(h(row.text));
}
let collisions = 0;
for (const r of merged) if (foreign.has(h(r.text))) { collisions++; fail.push(`${r.id}: text hash collides with an existing corpus row`); }

console.log(`human=${human.length} ai=${ai.length} merged=${merged.length}`);
for (const lbl of ['HUMAN', 'AI']) {
  const lens = merged.filter((r) => r.label === lbl).map((r) => r.text.length);
  if (lens.length) console.log(`  ${lbl}: ${lens.length} rows, ${Math.min(...lens)}-${Math.max(...lens)} chars, avg ${Math.round(lens.reduce((a, b) => a + b, 0) / lens.length)}`);
}
console.log(`compared against ${foreign.size} hashes from: ${AGAINST.join(', ')}`);

if (fail.length) {
  console.log(`FAILED (${fail.length}):`);
  for (const f of fail) console.log('  ' + f);
  process.exit(1);
}
fs.writeFileSync(OUT, JSON.stringify(merged, null, 2));
console.log(`OK — wrote ${path.basename(OUT)} (${collisions} collisions, zero tolerated)`);
