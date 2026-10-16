/*
 * Captures human-written prose from public-domain books on Project Gutenberg.
 * Nothing here is written by a model: every excerpt is a contiguous slice of the
 * downloaded file (whitespace normalised, Gutenberg page markers dropped), and each
 * one carries the source URL, block range and a sha256 prefix so anyone can re-fetch
 * the book and prove the words came from it. verify-book-corpus.js does exactly that.
 *
 *   node benchmark/build-book-corpus.js                 # 2 passages per book
 *   node benchmark/build-book-corpus.js --per 3 --min 700
 *
 * Output: benchmark/corpus-books.json (item shape of corpus.json, plus source).
 * Downloads are cached in benchmark/book-sources/, so a re-run is reproducible offline.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const argv = process.argv.slice(2);
function arg(name, dflt) {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : dflt;
}
const PER_BOOK = parseInt(arg('per', '2'), 10);
const MIN_LEN = parseInt(arg('min', '500'), 10);
const MAX_LEN = parseInt(arg('max', '1500'), 10);
const OUT = path.join(__dirname, arg('out', 'corpus-books.json'));
const IDP = arg('idp', 'book');
const CACHE = path.join(__dirname, 'book-sources');

// --disjoint f1.json[,f2.json] : refuse passages whose block range overlaps any range
// already captured in those corpora (plus a 1-block margin). Without it, a long-excerpt
// rerun of the same books would silently re-test on text the judges already saw.
const RESERVED = new Map();
let reservedSkips = 0;
for (const f of String(arg('disjoint', '')).split(',').filter(Boolean)) {
  for (const it of JSON.parse(fs.readFileSync(path.join(__dirname, f), 'utf8'))) {
    const g = it.source && it.source.gutenberg;
    const r = it.source && it.source.block_range;
    if (g == null || !r) continue;
    const [a, b] = String(r).split('-').map(Number);
    (RESERVED.get(g) || RESERVED.set(g, []).get(g)).push([a, b]);
  }
}

// Candidate Project Gutenberg ebook ids. Titles/authors are NOT assumed here — they are
// read out of each downloaded file's own header, because these ids are easy to misremember
// (pg5200 is Kafka's Metamorphosis, pg880 is a garden diary, pg557 is Kipling).
const BOOKS = [
  { g: 1342 }, { g: 2701 }, { g: 84 }, { g: 174 }, { g: 74 },
  { g: 5200 }, { g: 880 }, { g: 1400 }, { g: 25345 }, { g: 15962 },
  { g: 1228 }, { g: 3300 }, { g: 2680 }, { g: 557 }, { g: 45107 }, { g: 205 },
  { g: 35 }, { g: 36 }, { g: 1497 }, { g: 1080 }, { g: 3021 }, { g: 1727 },
  { g: 646 }, { g: 2814 }, { g: 1260 }, { g: 41445 }, { g: 7370 }, { g: 2542 },
];

// pg41445 is the same Frankenstein text as pg84 in a second edition — one work, one entry.
const EXCLUDE = new Set([41445]);

const URLS = (g) => [
  `https://www.gutenberg.org/cache/epub/${g}/pg${g}.txt`,
  `https://www.gutenberg.org/files/${g}/${g}-0.txt`,
  `https://www.gutenberg.org/files/${g}/${g}.txt`,
];

async function fetchBook(g) {
  fs.mkdirSync(CACHE, { recursive: true });
  const canonical = URLS(g)[0];
  const cached = path.join(CACHE, `pg${g}.txt`);
  if (fs.existsSync(cached)) return { text: fs.readFileSync(cached, 'utf8'), url: canonical, from: 'cache' };
  let lastErr = 'no attempt';
  for (const url of URLS(g)) {
    try {
      const res = await fetch(url, { headers: { 'User-Agent': 'calibration-harness/1.0 (offline research)' } });
      if (!res.ok) { lastErr = `HTTP ${res.status} ${url}`; continue; }
      const buf = Buffer.from(await res.arrayBuffer());
      const text = decode(buf);
      if (text.length < 20000) { lastErr = `only ${text.length} chars ${url}`; continue; }
      fs.writeFileSync(cached, text, 'utf8');
      return { text, url, from: url };
    } catch (e) {
      lastErr = `${e.name} ${e.message} ${url}`;
    }
  }
  throw new Error(lastErr);
}

function decode(buf) {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buf);
  } catch (e) {
    return new TextDecoder('windows-1252').decode(buf);
  }
}

/** Drop the Gutenberg license boilerplate and the title page ahead of the text. */
function body(raw) {
  let t = raw.replace(/\r\n/g, '\n');
  const start = t.search(/\*\*\*\s*START OF THE PROJECT GUTENBERG EBOOK.*\*\*\*/i);
  if (start >= 0) t = t.slice(t.indexOf('\n', start) + 1);
  const end = t.search(/\*\*\*\s*END OF THE PROJECT GUTENBERG EBOOK/i);
  if (end >= 0) t = t.slice(0, end);
  return t;
}

function clean(p) {
  return p.replace(/\n+/g, '\n').replace(/[ \t]+/g, ' ').trim();
}

/**
 * Every paragraph block, in file order, each flagged usable. The filtered-out ones stay
 * in the array: joining across a gap would produce text that is not contiguous in the
 * book, and the verifier would (correctly) reject it.
 */
function blocks(text) {
  return text
    .split(/\n\s*\n+/)
    .map((p) => p.replace(/\s*\n\s*/g, '\n').trim())
    .map((p) => ({ text: p, ok: usable(p) }));
}

function usable(p) {
  if (p.length < 120 || p.length > MAX_LEN * 3) return false;
  if (/[\[\]]/.test(p)) return false;                              // editorial brackets
  if (/^[ \t]*[#*_]/m.test(p) || /[*_]/.test(p)) return false;     // heading / italic markup
  if (/^[A-Z0-9 ,.:;'&-]+$/.test(p)) return false;                  // ALL CAPS heading block
  if (/[\u2022\u00b7]/.test(p)) return false;                        // bulleted front matter
  const lines = p.split('\n');
  if (lines.length > 3) {
    const med = lines.map((l) => l.length).sort((a, b) => a - b)[Math.floor(lines.length / 2)];
    if (med < 60) return false;                                     // verse / dramatic text
  }
  return true;
}

const ENDS = /[.!?]["')\]]?\s*$/;
const STARTS = /^[A-Z"'[\u201c\u2018]/;

/**
 * Join file-adjacent blocks from `start` into a passage of MIN_LEN..MAX_LEN that begins a
 * paragraph and ends on a sentence. Only blocks next to each other in the file are joined,
 * so the result is one contiguous run of the book.
 */
function passage(bs, start) {
  if (!bs[start].ok) return null;
  let acc = bs[start].text;
  if (!STARTS.test(acc) || !ENDS.test(acc)) return null;
  if (acc.length > MAX_LEN) {
    // Melville-sized paragraph: keep a whole-sentence prefix rather than drop the passage.
    const cut = acc.slice(0, MAX_LEN);
    const last = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('? '), cut.lastIndexOf('! '));
    if (last < MIN_LEN) return null;
    return { text: acc.slice(0, last + 1), from: start, to: start };
  }
  let i = start;
  while (acc.length < MIN_LEN && i + 1 < bs.length && bs[i + 1].ok &&
    acc.length + 1 + bs[i + 1].text.length <= MAX_LEN) {
    if (!ENDS.test(bs[i + 1].text)) break;
    acc += '\n' + bs[++i].text;
  }
  if (acc.length < MIN_LEN || acc.length > MAX_LEN || !ENDS.test(acc)) return null;
  return { text: clean(acc), from: start, to: i };
}

function sha(s) {
  return crypto.createHash('sha256').update(s, 'utf8').digest('hex').slice(0, 16);
}

/**
 * Identity comes from the file itself, not from the id list: line 1 is
 * "The Project Gutenberg eBook of <Title>" and the header block carries Author lines.
 */
function headerInfo(raw) {
  const head = raw.slice(0, 4000);
  let title = (head.match(/The Project Gutenberg eBook of\s+(.+)/i) || [])[1] || null;
  let author = (head.match(/^Author:\s*(.+)$/mi) || [])[1] || null;
  const byLine = body(raw).match(/^By\s+(.+)$/mi);
  const dateLine = head.match(/^(?:Release date|Posting date):\s*(.+)$/mi);
  let from = 'file header';
  if (!title) {
    // Old-style files start straight at the title page.
    const t = body(raw).trim().split('\n').slice(0, 4).map((s) => s.trim()).filter(Boolean).join(' ');
    title = t.slice(0, 80) || null;
    from = 'title page';
  }
  return {
    title: title ? title.trim() : null,
    author: author ? author.trim() : (byLine ? byLine[1].trim() : null),
    author_from: author ? 'header' : (byLine ? 'title page' : null),
    header_date: dateLine ? dateLine[1].trim() : null,
    title_from: from,
  };
}

(async () => {
  const items = [];
  const failures = [];
  for (const bk of BOOKS) {
    if (EXCLUDE.has(bk.g)) continue;
    let got;
    try {
      got = await fetchBook(bk.g);
    } catch (e) {
      failures.push(`pg${bk.g}: ${e.message}`);
      console.log(`  FAIL  pg${bk.g} — ${e.message}`);
      continue;
    }
    const info = headerInfo(got.text);
    const bs = blocks(body(got.text));
    const usableCount = bs.filter((b) => b.ok).length;
    if (!usableCount) {
      failures.push(`pg${bk.g} ${info.title}: 0 usable blocks from ${got.url}`);
      console.log(`  FAIL  pg${bk.g} ${info.title} — 0 usable blocks`);
      continue;
    }
    // Evenly spaced through the book, with a short forward scan when the landing point
    // isn't a usable paragraph. Same input file => same passages on every run.
    const used = [];
    let takenCount = 0;
    for (let k = 0; k < PER_BOOK; k++) {
      const idx = Math.floor(((k + 1) * bs.length) / (PER_BOOK + 1));
      let found = null;
      for (let j = idx; j < Math.min(idx + 40, bs.length) && !found; j++) {
        const cand = passage(bs, j);
        if (!cand) continue;
        const clash = (r) => cand.from <= r.to + 1 && cand.to >= r.from - 1;
        if (used.some(clash)) continue;
        if ((RESERVED.get(bk.g) || []).some(([a, b]) => cand.from <= b + 1 && cand.to >= a - 1)) { reservedSkips++; continue; }
        found = cand;
      }
      if (!found) continue;
      used.push(found);
      takenCount++;
      items.push({
        id: 'x',
        text: found.text,
        label: 'HUMAN',
        config: 'published_book',
        question: null,
        source: {
          gutenberg: bk.g,
          title: info.title,
          author: info.author,
          header_date: info.header_date,
          url: got.url,
          fetched_from: got.from,
          block_range: found.from + '-' + found.to,
          sha256_16: sha(found.text),
          capture: 'contiguous slice of the downloaded file; only whitespace was normalised',
        },
      });
    }
    console.log(`  ok    pg${String(bk.g).padEnd(5)} ${(info.title || '?').slice(0, 46).padEnd(48)} ${info.author || '?'} — ${usableCount}/${bs.length} usable blocks, ${takenCount}/${PER_BOOK} passage(s)`);
  }

  const seen = new Set();
  const uniq = items.filter((it) => !seen.has(it.source.sha256_16) && seen.add(it.source.sha256_16));
  uniq.sort((a, b) => a.source.gutenberg - b.source.gutenberg || a.source.block_range.localeCompare(b.source.block_range));
  uniq.forEach((it, i) => { it.id = IDP + '-' + String(i).padStart(3, '0'); });
  fs.writeFileSync(OUT, JSON.stringify(uniq, null, 2), 'utf8');
  if (reservedSkips) console.log(`(skipped ${reservedSkips} candidate passage(s) overlapping the --disjoint corpora)`);

  console.log(`\nwrote ${uniq.length} excerpts to ${path.relative(process.cwd(), OUT)}`);
  if (uniq.length) {
    const lens = uniq.map((x) => x.text.length).sort((a, b) => a - b);
    console.log(`lengths min ${lens[0]} / median ${lens[Math.floor(lens.length / 2)]} / max ${lens[lens.length - 1]} chars`);
    console.log(`labels ${JSON.stringify(uniq.reduce((a, x) => ((a[x.label] = (a[x.label] || 0) + 1), a), {}))}`);
    console.log(`corpus sha256 ${crypto.createHash('sha256').update(JSON.stringify(uniq.map((x) => x.text))).digest('hex').slice(0, 16)}`);
  }
  if (failures.length) console.log(`\n${failures.length} book(s) failed:\n  ` + failures.join('\n  '));
  const byBook = {};
  for (const it of uniq) {
    const k = it.source.gutenberg;
    byBook[k] = byBook[k] || { title: it.source.title, author: it.source.author, n: 0 };
    byBook[k].n++;
  }
  console.log('\nbooks captured (identity parsed from each file header):');
  for (const k of Object.keys(byBook)) {
    console.log(`  pg${String(k).padEnd(5)} x${byBook[k].n}  ${byBook[k].title || '?'} — ${byBook[k].author || '?'}`);
  }
  console.log('\nfirst 110 chars of each passage — check these against the source URLs above:');
  for (const it of uniq) {
    console.log(`  ${it.id} pg${String(it.source.gutenberg).padEnd(5)} ${String(it.text.length).padStart(4)}ch blk${String(it.source.block_range).padEnd(9)} ${it.source.sha256_16}  ${it.text.slice(0, 110).replace(/\n/g, ' / ')}…`);
  }
})();
