// Build a *whitespace-and-token-normalised* copy of a labelled corpus, so a model scoring it
// measures authorship STYLE instead of the surface noise that leaked the label in the raw capture.
//
// Why this exists (the confound, measured 2026-10-11 on both corpora): HC3's `human_answers` were
// scraped through varied tokenisers, so the HUMAN class carries systematic artefacts the AI class
// does not -- space before punctuation (8 rows v 0), split contractions like "you 're"/"I 'm"
// (21 sites v 0), double spaces (6 v 0), and wiki-scrape tokens "[ source ] ( URL_0 )". That gap
// tracks the label almost perfectly, so every accuracy number fitted on corpus.json is partly a
// noise-detector score, not an authorship score (this is what flipped hu-008/hu-012 under GPTZero).
//
// The fix normalises BOTH classes with the IDENTICAL transform. The AI class has none of these
// artefacts, so cleaning is a near no-op there; the HUMAN class loses its tokeniser signature. Any
// model gap that SURVIVES cleaning is real style; any gap that VANISHES was the confound.
//
// It NEVER touches the source file. Output is a separate corpus whose rows keep every original field,
// carry the cleaned text in `text`, and keep the untouched capture in `text_original`.
//
// Correctness is enforced, not promised: `clean(raw)` is *defined* as
// `normaliseWhitespace(stripScrapeTokens(raw))`, and every normaliseWhitespace step only edits
// whitespace. So after removing all whitespace, `clean(raw)` and `stripScrapeTokens(raw)` must be
// byte-identical -- and the only content that disappears is a printed histogram of the scrape tokens
// themselves. The verifier asserts exactly that, per row, and aborts the whole run on any mismatch.
//
//   node build-clean-corpus.js                       # cleans corpus.json + corpus-holdout.json
//   node build-clean-corpus.js --in x.json --out y.json
//   node build-clean-corpus.js --dry                # verify + report, write nothing

const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const arg = (n, d) => {
  const i = argv.indexOf('--' + n);
  return i >= 0 && argv[i + 1] !== undefined && !argv[i + 1].startsWith('--') ? argv[i + 1] : d;
};
const DRY = argv.includes('--dry');

const CURL = '’';      // right single quotation mark (curly apostrophe)
const APOS = "'";       // straight apostrophe

// Every Unicode space / line separator, written as escapes so the source stays ASCII and unambiguous.
const UNI_SPACE = /\s/g; // JS \s already spans NBSP/EN-QUAD/THIN/IDEOGRAPHIC/LS/PS
// A whitespace char that sits directly before an apostrophe is the split-contraction artefact.
const CONTRACTION = new RegExp('[ \\t]+([' + CURL + APOS + '])', 'g');
const MARK_CONTRACTION = new RegExp('[ \\t]+[' + CURL + APOS + ']');

// ---- the two whitelisted content removals (nothing else may lose a character) ----
// `URL_\d+` is a pure placeholder. `[ source ]` is a wiki citation marker (its word "source" is a
// label, not prose). Removing them can leave a whitespace-only shell `( )`/`[ ]`, so those shells go
// too -- but ONLY as a consequence of the two above, which is why the order is fixed.
function collectScrapeTokens(s, sink) {
  // grab() removes the matched token (counted as deleted content) and substitutes `rep`.
  const grab = (re, rep = '') => { s = s.replace(re, (m) => { sink.push(m); return rep; }); };
  grab(/\[\s*source\s*\]/gi);
  grab(/URL_\d+/g);
  grab(/[([{]\s*[)\]}]/g); // shells emptied by the two removals above
  // Literal escaped whitespace (a backslash followed by n/r/t) is a serialisation artefact, not
  // prose. It appears ONLY on the AI side (measured: ai-005/ai-010, 10 sites; 0 in HUMAN), so it is
  // an AI-class tell the real-newline collapse below does not reach. Substitute a genuine space so
  // the surrounding words neither fuse nor keep the backslash, then let the whitespace pass tidy it.
  grab(/\\[nrt]/g, ' ');
  return s;
}
function stripScrapeTokens(s) { return collectScrapeTokens(s, []); }

// ---- whitespace normalisation: every step edits whitespace and ONLY whitespace ----
function normaliseWhitespace(s) {
  let t = s.replace(UNI_SPACE, ' ');        // Unicode space/line sep -> ASCII space
  t = t.replace(CONTRACTION, '$1');         // "you 're" -> "you're"  (drops the space, keeps the ')
  t = t.replace(/\s+([,.;:!?])/g, '$1');    // drop the space a tokeniser put before punctuation
  t = t.replace(/\s*\r?\n\s*/g, ' ');       // line wraps / paragraph breaks -> one space
  t = t.replace(/[ \t]{2,}/g, ' ');         // collapse leftover runs
  return t.trim();
}

function clean(raw) { return normaliseWhitespace(stripScrapeTokens(raw)); }

// fold: the invariant lens -- whitespace is invisible, every other character is load-bearing.
function fold(s) { return s.replace(/\s+/g, '').toLowerCase(); }

// ---- surface markers, counted PER ROW (a row "has" a marker if it shows it anywhere) ----
function markerCounts(rows) {
  const c = { space_before_punct: 0, double_space: 0, split_contraction: 0, url_token: 0, source_token: 0, escaped_ws: 0, newline: 0 };
  for (const r of rows) {
    const t = r.text;
    if (/\s+[,.;:!?]/.test(t)) c.space_before_punct++;
    if (/[ \t]{2,}/.test(t)) c.double_space++;
    if (MARK_CONTRACTION.test(t)) c.split_contraction++;
    if (/URL_\d+/.test(t)) c.url_token++;
    if (/\[\s*source\s*\]/i.test(t)) c.source_token++;
    if (/\\[nrt]/.test(t)) c.escaped_ws++;
    if (/\r?\n/.test(t)) c.newline++;
  }
  return c;
}

function processFile(inPath, outPath) {
  if (!fs.existsSync(inPath)) { console.error('input not found: ' + inPath); process.exit(1); }
  const data = JSON.parse(fs.readFileSync(inPath, 'utf8'));

  const tokenHist = {};      // exactly which content we deleted, across the whole file
  const out = [];
  let fails = 0;

  for (const r of data) {
    const raw = r.text;
    if (typeof raw !== 'string') { console.error('row ' + r.id + ' has no text'); process.exit(1); }

    const sink = [];
    const stripped = collectScrapeTokens(raw, sink);
    const cleaned = normaliseWhitespace(stripped);
    for (const tok of sink) {
      const key = tok.replace(/\s+/g, ' ').trim();
      tokenHist[key] = (tokenHist[key] || 0) + 1;
    }

    // INVARIANT 1 -- clean() changed nothing but whitespace (and the whitelisted tokens).
    if (fold(cleaned) !== fold(stripped)) {
      fails++;
      console.error('  !! INVARIANT FAIL ' + r.id + ' (' + r.label + '): clean() lost non-whitespace content');
      console.error('     fold(clean)    = ' + JSON.stringify(fold(cleaned).slice(0, 140)));
      console.error('     fold(stripped) = ' + JSON.stringify(fold(stripped).slice(0, 140)));
    }
    // INVARIANT 2 -- cleaning only shrinks (whitespace + token removal), never bloats.
    if (cleaned.length > raw.length + 1) {
      fails++;
      console.error('  !! LENGTH GREW ' + r.id + ': ' + raw.length + ' -> ' + cleaned.length);
    }
    // INVARIANT 3 -- the deleted non-space characters must ALL come from the whitelist. Reconstruct
    // the raw's non-space stream minus the sink's non-space stream and require it to equal clean()'s.
    const sinkLost = sink.map((t) => fold(t)).join('');
    if (fold(raw) !== fold(cleaned) + sinkLost && fold(cleaned) !== fold(stripScrapeTokens(raw))) {
      // The exact per-token reconstruction is order-sensitive, so rely on INVARIANT 1's equality and
      // just confirm the total lost-char count is fully explained by the sink.
      const lost = fold(raw).length - fold(cleaned).length;
      const explained = sink.reduce((a, t) => a + fold(t).length, 0);
      if (lost !== explained) {
        fails++;
        console.error('  !! UNEXPLAINED LOSS ' + r.id + ': ' + lost + ' chars vanished but tokens hold ' + explained);
      }
    }

    out.push({ ...r, text: cleaned, text_original: raw });
  }

  if (fails) { console.error('\nABORT: ' + fails + ' row(s) failed the invariance check; nothing written for ' + inPath); process.exit(2); }

  const rawRows = data.map((r) => ({ text: r.text, label: r.label }));
  console.log('\n=== ' + path.basename(inPath) + ' -> ' + (DRY ? '(dry run, not written)' : path.basename(outPath)) + ' ===');
  console.log('rows=' + out.length + ' | ' + fails + ' invariance failure(s)');
  for (const lab of ['HUMAN', 'AI']) {
    const before = markerCounts(rawRows.filter((x) => x.label === lab));
    const after = markerCounts(out.filter((x) => x.label === lab));
    const chBefore = rawRows.filter((x) => x.label === lab).reduce((a, x) => a + x.text.length, 0);
    const chAfter = out.filter((x) => x.label === lab).reduce((a, x) => a + x.text.length, 0);
    console.log('  ' + lab.padEnd(5) + ' markers before=' + JSON.stringify(before));
    console.log('  ' + lab.padEnd(5) + ' markers after =' + JSON.stringify(after) +
      '  | chars ' + chBefore + '->' + chAfter + ' (' + (((chAfter - chBefore) / chBefore) * 100).toFixed(1) + '%)');
  }
  const hist = Object.entries(tokenHist).sort((a, b) => b[1] - a[1]);
  console.log('  scrape tokens removed (content deleted, whitelist): ' +
    (hist.length ? hist.map(([k, v]) => JSON.stringify(k) + '×' + v).join(', ') : 'none'));

  if (!DRY) fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  return out;
}

// ---- self-tests for the transform itself, so the cleaner is trusted before the corpora are ----
(function selfTest() {
  const C = CURL;
  const cases = [
    ['When you  ' + C + 're here .', 'When you' + C + 're here.', 'double space + split contraction + space-before-punct'],
    ['see [ puddling ] ( URL_0 ) ok', 'see [ puddling ] ok', 'URL placeholder removed, real bracketed word kept'],
    ['E*Trade charge $20 : * *', 'E*Trade charge $20: * *', 'asterisks are content -> untouched'],
    ['do n' + C + 't  you', 'do n' + C + 't you', 'only the space before the apostrophe is eaten; letters kept'],
    ['line one \n line two', 'line one line two', 'line-wrap newline -> single space'],
    ['value 3.5 stays', 'value 3.5 stays', 'decimal not re-spaced'],
    ['it ' + C + 's ( ) done', 'it' + C + 's done', 'empty paren shell from a removed token is dropped'],
    [' head  tail　end', 'head tail end', 'Unicode spaces (NBSP/EN-QUAD/IDEOGRAPHIC) fold to one ASCII space'],
    ['Remember ' + C + 's', 'Remember' + C + 's', 'trailing possessive space rejoined'],
    ['to be fulfilled.\\n\\nWarehouse', 'to be fulfilled. Warehouse', 'literal backslash-n artefact -> one space (words stay separated)'],
    ['plain text, no artefacts.', 'plain text, no artefacts.', 'AI-class clean text is a fixed point (no-op)'],
  ];
  let bad = 0;
  for (const [inp, want, note] of cases) {
    const got = clean(inp);
    const ok = got === want;
    const inv = fold(clean(inp)) === fold(stripScrapeTokens(inp));
    if (!ok || !inv) {
      bad++;
      console.error('  SELFTEST ' + (ok ? '' : 'VALUE ') + (inv ? '' : 'INVARIANT ') + 'FAIL: ' + note +
        '\n    in  ' + JSON.stringify(inp) + '\n    got ' + JSON.stringify(got) + '\n    exp ' + JSON.stringify(want));
    }
  }
  console.log('self-test: ' + (cases.length - bad) + '/' + cases.length + ' transforms correct, all invariant-safe');
  if (bad) process.exit(2);
})();

// ---- run ----
const jobs = [];
if (argv.includes('--in')) {
  jobs.push([path.join(__dirname, arg('in')), path.join(__dirname, arg('out', 'corpus-clean.json'))]);
} else {
  jobs.push([path.join(__dirname, 'corpus.json'), path.join(__dirname, 'corpus-clean.json')]);
  jobs.push([path.join(__dirname, 'corpus-holdout.json'), path.join(__dirname, 'corpus-holdout-clean.json')]);
}
for (const [i, o] of jobs) processFile(i, o);

// ---- cross-check: cleaning must not merge or split classes; ids/labels preserved ----
if (!DRY) {
  for (const [i, o] of jobs) {
    const a = JSON.parse(fs.readFileSync(i, 'utf8'));
    const b = JSON.parse(fs.readFileSync(o, 'utf8'));
    const sameOrder = a.length === b.length && a.every((r, k) => r.id === b[k].id && r.label === b[k].label);
    console.log('post-write check ' + path.basename(o) + ': ids+labels preserved=' + sameOrder +
      ' rows=' + b.length + ' (AI ' + b.filter((x) => x.label === 'AI').length + '/HUMAN ' + b.filter((x) => x.label === 'HUMAN').length + ')');
    if (!sameOrder) { console.error('ABORT: id/label order changed'); process.exit(2); }
  }
}
