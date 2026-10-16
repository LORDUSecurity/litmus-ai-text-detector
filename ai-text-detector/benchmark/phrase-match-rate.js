#!/usr/bin/env node
/**
 * How many of the model's own quotes can the evidence panel actually find in the text?
 *
 * `result-card.ts` paints a phrase by locating its quote in the scored text: an exact
 * `indexOf`, then a whitespace-collapsed fallback (internal runs → one space, both ends
 * trimmed) mapped back onto original offsets. A quote that matches neither is *counted* as
 * `unmatched` and the panel says so — the evidence silently shrinks. Before loosening the
 * shipped matcher (which risks painting a span the model never quoted), measure what the
 * current two steps miss and what shape the misses are.
 *
 * The strategies below mirror the shipped `locate()` for (a), (b) and (c) — that mirroring is the
 * point: (c) was adopted from this measurement and (d) was rejected by it, and any future change to
 * the shipped matcher has to be re-run here to keep the two honest. The join between a capture row
 * and its corpus text is *verified* by length, not assumed.
 *
 * Usage: node benchmark/phrase-match-rate.js [results.jsonl ...]
 */
const fs = require('fs');
const path = require('path');

const BENCH = __dirname;
const DEFAULT_FILES = [
  'results-nocap-clean-omkiro-sonnet-4-5.jsonl',
  'results-nocap-holdout-omkiro-sonnet-4-5.jsonl',
  'results-capclean-omkiro-sonnet-4-5.jsonl',
];
const CORPORA = ['corpus-clean.json', 'corpus-holdout-clean.json', 'corpus.json'];

/** Mirror of normalizeWithMap() in result-card.ts: collapse whitespace runs, keep the map. */
function normalizeWithMap(text) {
  let out = '';
  const map = [];
  const n = text.length;
  let i = 0;
  while (i < n && /\s/.test(text[i])) i++;
  while (i < n) {
    if (/\s/.test(text[i])) {
      const runStart = i;
      while (i < n && /\s/.test(text[i])) i++;
      if (i < n) { out += ' '; map.push(runStart); }
    } else { out += text[i]; map.push(i); i++; }
  }
  return { text: out, map };
}

/** (a) exact, (b) shipped whitespace-collapsed fallback. Returns the index a or the normalized
    string b, so the looser strategies can be layered on the same basis. */
function locateShipped(quote, text, norm) {
  const q = quote.trim();
  if (!q) return { exact: -1, normHit: -1, qn: '' };
  const exact = text.indexOf(q);
  const qn = q.replace(/\s+/g, ' ');
  return { exact, normHit: norm.text.indexOf(qn), qn };
}

const foldCase = (s) => s.toLowerCase();
// Strip the punctuation a model is known to re-shape when it quotes: smart/straight quote
// variants, then every other non-word non-space character, so only letters/digits/spaces remain.
const stripPunct = (s) => s
  .replace(/[“”‘’]/g, (c) => ({ '“': '"', '”': '"', '‘': "'", '’': "'" }[c]))
  .replace(/[^0-9A-Za-z '"-]+/g, ' ')
  .replace(/\s+/g, ' ')
  .trim();

function main() {
  // The same id (`ai-001`, `human-017`, …) exists in several corpora, and the variants differ —
  // dirty vs clean, clean vs the text actually sent. Keying one Map by id would let the last file
  // loaded win, so quotes would be measured against a text the model never saw. Keep every
  // candidate per id and accept only the one whose length equals the row's recorded
  // `text_length`: that is the join being verified, not assumed.
  const byId = new Map();
  const corpora = [];
  for (const f of CORPORA) {
    const p = path.join(BENCH, f);
    if (!fs.existsSync(p)) { corpora.push(`${f}: absent`); continue; }
    let added = 0;
    for (const item of JSON.parse(fs.readFileSync(p, 'utf8'))) {
      const t = item && (typeof item.text === 'string' ? item.text
              : typeof item.text_original === 'string' ? item.text_original : null);
      if (!item || !item.id || !t) continue;
      if (!byId.has(item.id)) byId.set(item.id, []);
      byId.get(item.id).push({ text: t, src: f });
      added++;
    }
    corpora.push(`${f}: ${added} texts`);
  }
  if (!byId.size) { console.error('no corpus text loaded'); process.exit(2); }

  const files = process.argv.slice(2).filter((a) => !a.startsWith('-'));
  const list = files.length ? files : DEFAULT_FILES;

  const tally = { quotes: 0, exact: 0, shipped: 0, caseFold: 0, punctFold: 0, lost: 0 };
  const rows = { matched: 0, noPhrases: 0, stripped: 0 };
  const join = { noText: 0, len: 0 };        // id absent from a loaded corpus / length disagrees
  const byFile = {};
  const newByCase = [];
  const newByPunct = [];
  const lostExamples = [];

  for (const f of list) {
    const p = path.isAbsolute(f) ? f : path.join(BENCH, f);
    if (!fs.existsSync(p)) { console.error(`missing capture: ${p}`); process.exit(2); }
    byFile[f] ||= { matched: 0, noText: 0, len: 0 };
    for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      let row;
      try { row = JSON.parse(line); } catch { continue; }
      const ph = Array.isArray(row.phrases) ? row.phrases : [];
      if (!ph.length) { rows.noPhrases++; continue; }
      if (row.salvaged || row.truncated) { rows.stripped++; continue; }
      const cands = byId.get(row.id) || [];
      // Length equality against the row's recorded `text_length` is the join proof. It is strong,
      // not ahash-free certainty — two variants of one id could share a length — but a wrong pick
      // would show up as quotes failing to match, and (a)/(b) below is exactly that measurement.
      const hit = cands.find((c) => c.text.length === row.text_length);
      if (!cands.length) { join.noText++; byFile[f].noText++; continue; }
      if (!hit) { join.len++; byFile[f].len++; continue; }
      const text = hit.text;
      rows.matched++;
      byFile[f].matched++;
      const norm = normalizeWithMap(text);
      const normCase = normalizeWithMap(foldCase(text));
      const normPunct = normalizeWithMap(stripPunct(foldCase(text)));

      for (const item of ph) {
        const quote = item && typeof item.quote === 'string' ? item.quote : '';
        if (!quote.trim()) continue;
        tally.quotes++;
        const { exact, normHit, qn } = locateShipped(quote, text, norm);
        if (exact >= 0) { tally.exact++; continue; }
        if (normHit >= 0) { tally.shipped++; continue; }
        // what the shipped matcher misses — now measure the candidate loosenings
        if (normCase.text.indexOf(foldCase(qn)) >= 0) {
          tally.caseFold++;
          if (newByCase.length < 6) newByCase.push({ id: row.id, quote: qn });
          continue;
        }
        if (normPunct.text.indexOf(stripPunct(qn)) >= 0) {
          tally.punctFold++;
          if (newByPunct.length < 6) newByPunct.push({ id: row.id, quote: qn });
          continue;
        }
        tally.lost++;
        if (lostExamples.length < 8) lostExamples.push({ id: row.id, quote: qn.slice(0, 120) });
      }
    }
  }

  const pct = (n) => ((100 * n) / (tally.quotes || 1)).toFixed(1) + '%';
  console.log('# Quote-locate coverage on real captures');
  console.log(corpora.map((c) => '  ' + c).join('\n'));
  console.log(`rows joined+verified: ${rows.matched}   id not in a loaded corpus: ${join.noText}` +
              `   length mismatch: ${join.len}   no phrases: ${rows.noPhrases}` +
              `   salvaged/truncated: ${rows.stripped}`);
  for (const [f, s] of Object.entries(byFile)) {
    console.log(`  ${f}: matched ${s.matched}, no text ${s.noText}, length ${s.len}`);
  }
  console.log(`quotes: ${tally.quotes}`);
  console.log(`  (a) exact substring            ${tally.exact}  ${pct(tally.exact)}`);
  console.log(`  (b) shipped whitespace fallback ${tally.shipped}  ${pct(tally.shipped)}   -> shipped total ${tally.exact + tally.shipped} ${pct(tally.exact + tally.shipped)}`);
  console.log(`  (c) + case-insensitive          ${tally.caseFold}  ${pct(tally.caseFold)}   -> shipped total (a+b+c) ` +
              `${tally.exact + tally.shipped + tally.caseFold} ${pct(tally.exact + tally.shipped + tally.caseFold)}`);
  console.log(`  (d) + punctuation-insensitive   ${tally.punctFold}  ${pct(tally.punctFold)}   (NOT shipped: buys nothing on this data` +
              `, and folding punctuation would paint spans the model did not quote verbatim)`);
  console.log(`  (e) still not found             ${tally.lost}  ${pct(tally.lost)}`);
  const show = (label, arr) => {
    if (!arr.length) return;
    console.log(`\n${label}:`);
    for (const e of arr) console.log(`  [${e.id}] ${JSON.stringify(e.quote)}`);
  };
  show('recovered only by case-folding', newByCase);
  show('recovered only by punctuation-folding', newByPunct);
  show('unfindable even after both', lostExamples);
}

main();
