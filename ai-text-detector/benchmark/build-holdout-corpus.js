// Build a DISJOINT out-of-sample corpus from HC3 (Hello-SimpleAI/HC3) via the HuggingFace
// datasets-server HTTP API -- the same citable source, ground-truth labels, as benchmark/corpus.json.
//
// Why this exists: every accuracy number in docs/free-model-research.md was fitted on the same 48
// texts, so none of them is a prediction. The Gutenberg book corpus cannot fill the gap either --
// all 46 of its texts are HUMAN and three different models have now scored specificity 1.000 on it,
// so it does not discriminate. This draws *new* HC3 rows, on both sides of the label, and proves
// they do not overlap the fitted corpus.
//
// It never touches corpus.json. Output is a separate file.
//
//   node build-holdout-corpus.js [--out corpus-holdout.json] [--total 48] [--seed 20261011]
//                                [--exclude corpus.json[,corpus-holdout.json]] [--start_offset 600]
//                                [--per_fetch 100] [--passes 8] [--min 300] [--max 1600]
//
// --min/--max move the length band. The shipped bands were probed against HC3 before being chosen
// (probe-length-bands.js, throwaway): 1601-3000 has both classes in finance/medicine/wiki_csai,
// 60-299 has plentiful HUMAN but AI is scarce (~23 in a 400-row-per-config scan), and 3001+ has
// ZERO ChatGPT answers -- the ultra-long band is not buildable from this source at all.
//
// Each row carries its own provenance (dataset, config, split, row_idx, question) so a reader can
// re-fetch the exact source text and check the label.

const fs = require('fs');
const path = require('path');

const argv = process.argv.slice(2);
const arg = (n, d) => {
  const i = argv.indexOf('--' + n);
  return i >= 0 && argv[i + 1] !== undefined && !argv[i + 1].startsWith('--') ? argv[i + 1] : d;
};

const OUT = arg('out', 'corpus-holdout.json');
const TOTAL = parseInt(arg('total', '48'), 10);
const SEED = parseInt(arg('seed', '20261011'), 10);
// Comma-separated list of corpus files whose texts must not reappear here.
const EXCLUDES = (arg('exclude', 'corpus.json') || '').split(',').map((s) => s.trim()).filter(Boolean);
const START_OFFSET = parseInt(arg('start_offset', '600'), 10);
const PER_FETCH = parseInt(arg('per_fetch', '100'), 10);

const DATASET = 'Hello-SimpleAI/HC3';
const CONFIGS = ['open_qa', 'reddit_eli5', 'finance', 'medicine', 'wiki_csai'];
const MIN_CHARS = parseInt(arg('min', '300'), 10);
const MAX_CHARS = parseInt(arg('max', '1600'), 10);
const MAX_PASSES = parseInt(arg('passes', '8'), 10);
// datasets-server 429s in bursts after heavy fetch runs, then clears within minutes. Retry politely.
const FETCH_TRIES = parseInt(arg('tries', '8'), 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Same deterministic PRNG family as build-corpus.js, so the draw is reproducible from the seed.
let seed = SEED;
function rand() {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
}

function usable(t) {
  return typeof t === 'string' && t.length >= MIN_CHARS && t.length <= MAX_CHARS;
}

// Hash on normalised text, not on the raw string: whitespace is the only thing that tends to differ
// between two fetches of the same row, and a whitespace-only difference would fake "no overlap".
function norm(t) {
  return t.replace(/\s+/g, ' ').trim().toLowerCase();
}

async function fetchRows(config, offset, length) {
  const url = 'https://datasets-server.huggingface.co/rows'
    + '?dataset=' + encodeURIComponent(DATASET)
    + '&config=' + encodeURIComponent(config)
    + '&split=train&offset=' + offset + '&length=' + length;
  let lastStatus = '?';
  for (let t = 0; t < FETCH_TRIES; t++) {
    const res = await fetch(url);
    if (res.ok) return (await res.json()).rows || [];
    lastStatus = res.status;
    if (res.status === 429 || res.status >= 500) {
      const ra = parseInt(res.headers.get('retry-after') || '', 10);
      const back = Number.isFinite(ra) && ra > 0 ? ra * 1000 : Math.min(45000, 2000 * 2 ** t);
      await sleep(back + Math.floor(Math.random() * 500));
      continue;
    }
    throw new Error('HF ' + res.status + ' for ' + config + ': ' + (await res.text()).slice(0, 200));
  }
  throw new Error('HF ' + lastStatus + ' (rate-limited) for ' + config + ' offset ' + offset + ' after ' + FETCH_TRIES + ' tries');
}

(async () => {
  // ---- the texts that must NOT reappear ----
  const banned = new Set();
  for (const excl of EXCLUDES) {
    const p = path.join(__dirname, excl);
    if (!fs.existsSync(p)) { console.error('exclude corpus not found: ' + p); process.exit(1); }
    const before = banned.size;
    for (const r of JSON.parse(fs.readFileSync(p, 'utf8'))) banned.add(norm(r.text));
    console.log('excluding ' + excl + ': +' + (banned.size - before) + ' texts (normalised-hash match), now ' + banned.size + ' banned');
  }

  const human = [];
  const ai = [];
  const seen = new Set(banned);
  let skippedAsDuplicate = 0;

  for (const config of CONFIGS) {
    let offset = START_OFFSET;
    let guard = 0;
    while (guard++ < MAX_PASSES) {
      let rows;
      try {
        rows = await fetchRows(config, offset, PER_FETCH);
      } catch (e) {
        console.error('  ! ' + e.message);
        break;
      }
      if (!rows.length) break;

      for (const wrapper of rows) {
        const r = wrapper.row || {};
        const rowIdx = wrapper.row_idx;
        const h = Array.isArray(r.human_answers) ? r.human_answers : [];
        const c = Array.isArray(r.chatgpt_answers) ? r.chatgpt_answers : [];
        const prov = {
          dataset: DATASET, config, split: 'train', row_idx: rowIdx,
          question: r.question || null, source: 'huggingface datasets-server /rows',
          fetched_at: new Date().toISOString().slice(0, 10),
        };
        const ht = h.find(usable);
        const ct = c.find(usable);
        // One candidate per class per row, same rule as build-corpus.js: the FIRST usable answer.
        if (ht) {
          const k = norm(ht);
          if (seen.has(k)) skippedAsDuplicate++;
          else { seen.add(k); human.push({ text: ht, label: 'HUMAN', ...prov }); }
        }
        if (ct) {
          const k = norm(ct);
          if (seen.has(k)) skippedAsDuplicate++;
          else { seen.add(k); ai.push({ text: ct, label: 'AI', ...prov }); }
        }
      }
      offset += PER_FETCH;
    }
    console.log('  ' + config.padEnd(12) + ' pool human=' + human.length + ' ai=' + ai.length);
  }

  if (!human.length || !ai.length) { console.error('empty pool, nothing written'); process.exit(1); }

  // ---- balanced draw, spread across configs ----
  const perClass = Math.min(TOTAL / 2, human.length, ai.length);
  const byConfig = (pool) => {
    const m = {};
    for (const x of pool) (m[x.config] = m[x.config] || []).push(x);
    return m;
  };
  const sample = (pool, n) => {
    const m = byConfig(pool);
    const keys = Object.keys(m).sort();
    const out = [];
    let i = 0;
    while (out.length < n && keys.some((k) => m[k].length)) {
      const k = keys[i++ % keys.length];
      if (m[k].length) out.push(m[k].splice(Math.floor(rand() * m[k].length) % m[k].length, 1)[0]);
    }
    return out.slice(0, n);
  };

  const hSel = sample(human, perClass);
  const aSel = sample(ai, perClass);

  // Interleave so a partial run still sees both classes, and so ids stay tool-compatible
  // (verify-stored-band.js and friends look for ai-000 / hu-000).
  const corpus = [];
  for (let i = 0; i < Math.max(hSel.length, aSel.length); i++) {
    if (aSel[i]) corpus.push({ id: 'ai-' + String(i).padStart(3, '0'), ...aSel[i] });
    if (hSel[i]) corpus.push({ id: 'hu-' + String(i).padStart(3, '0'), ...hSel[i] });
  }

  const outPath = path.join(__dirname, OUT);
  fs.writeFileSync(outPath, JSON.stringify(corpus, null, 2));

  // ---- verification, printed rather than trusted ----
  const overlap = corpus.filter((r) => banned.has(norm(r.text))).length;
  const ids = new Set(corpus.map((r) => r.id));
  const dist = {};
  for (const x of corpus) dist[x.label + '/' + x.config] = (dist[x.label + '/' + x.config] || 0) + 1;
  const lens = corpus.map((x) => x.text.length).sort((a, b) => a - b);

  console.log('\nwrote ' + outPath);
  console.log('total: ' + corpus.length
    + ' | AI: ' + corpus.filter((x) => x.label === 'AI').length
    + ' | HUMAN: ' + corpus.filter((x) => x.label === 'HUMAN').length);
  console.log('seed ' + SEED + ', band ' + MIN_CHARS + '-' + MAX_CHARS + ' chars, start_offset ' + START_OFFSET + ', candidates rejected as already-seen: ' + skippedAsDuplicate);
  console.log('char length min/median/max: ' + lens[0] + '/' + lens[Math.floor(lens.length / 2)] + '/' + lens[lens.length - 1]);
  console.log('unique ids: ' + ids.size + ' of ' + corpus.length);
  console.log('OVERLAP with [' + (EXCLUDES.join(', ') || 'nothing') + ']: ' + overlap + (overlap === 0 ? '  OK - disjoint' : '  ** NOT DISJOINT, do not use **'));
  console.log('distribution: ' + JSON.stringify(dist));
  if (overlap !== 0) process.exit(2);
})();
