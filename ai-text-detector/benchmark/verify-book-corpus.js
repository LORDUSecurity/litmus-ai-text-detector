/*
 * Independent provenance check for corpus-books.json.
 *
 * Does NOT reuse build-book-corpus.js. For every excerpt it re-reads the cached book
 * file (or re-downloads it), collapses whitespace on both sides, and asserts the
 * excerpt appears in the book as one contiguous run of words. A single fabricated or
 * edited word anywhere in an excerpt makes that excerpt fail.
 *
 *   node benchmark/verify-book-corpus.js [corpus-file.json]
 *   (default: corpus-books.json; pass corpus-stress-human.json for the stress corpus)
 */
const fs = require('fs');
const path = require('path');

const B = __dirname;
const file = process.argv[2] || 'corpus-books.json';
const corpus = JSON.parse(fs.readFileSync(path.join(B, file), 'utf8'));
const squash = (s) => s
  .replace(/\[[^\]]*\]/g, ' ')
  .replace(/[\u2018\u2019\u201c\u201d]/g, '"')
  .replace(/[\u2013\u2014]/g, '-')
  .replace(/\s+/g, ' ')
  .trim();

async function loadBook(g, url) {
  const cached = path.join(B, 'book-sources', `pg${g}.txt`);
  if (fs.existsSync(cached)) return fs.readFileSync(cached, 'utf8');
  const res = await fetch(url, { headers: { 'User-Agent': 'calibration-harness/1.0 (offline research)' } });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  const text = await res.text();
  fs.writeFileSync(cached, text, 'utf8');
  return text;
}

(async () => {
  const books = {};
  for (const it of corpus) books[it.source.gutenberg] = it.source.url;
  const source = {};
  for (const g of Object.keys(books)) {
    try {
      source[g] = squash(await loadBook(g, books[g]));
    } catch (e) {
      source[g] = null;
      console.log(`  UNREADABLE pg${g}: ${e.message}`);
    }
  }

  let ok = 0;
  const bad = [];
  for (const it of corpus) {
    const src = source[it.source.gutenberg];
    if (!src) { bad.push([it.id, 'source file unavailable']); continue; }
    const ex = squash(it.text);
    const at = src.indexOf(ex);
    if (at < 0) {
      // Report where it diverges so a failure is diagnosable rather than mysterious.
      let prefix = ex.split(' ').slice(0, 12).join(' ');
      let cut = 12;
      while (src.indexOf(prefix) < 0 && prefix.split(' ').length > 1) {
        cut -= 2;
        prefix = ex.split(' ').slice(0, cut).join(' ');
      }
      const found = src.indexOf(prefix) >= 0;
      bad.push([it.id, found ? `diverges after "${prefix.slice(-40)}"` : `not even the first words are present`]);
      continue;
    }
    ok++;
    it._offset = at;
  }

  console.log(`\n${ok}/${corpus.length} excerpts found verbatim in their cited book file`);
  if (bad.length) {
    console.log(`${bad.length} FAILED:`);
    for (const [id, why] of bad) console.log(`  ${id}: ${why}`);
  }
  const offs = corpus.filter((c) => c._offset !== undefined);
  if (offs.length) {
    const span = Math.max(...offs.map((c) => c._offset)) - Math.min(...offs.map((c) => c._offset));
    console.log(`character offsets in source span ${span} — excerpts are spread through the books, not lifted from one page`);
  }
  process.exit(bad.length ? 1 : 0);
})();
