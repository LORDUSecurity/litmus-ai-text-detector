/*
 * Independent provenance check for corpus-holdout.json.
 *
 * Does NOT reuse build-holdout-corpus.js. For every row it goes back to the citable source
 * (HuggingFace datasets-server, Hello-SimpleAI/HC3, config + split + row_idx recorded on the row
 * itself), re-fetches that single row, and asserts:
 *
 *   1. the stored text appears in the answer list its label claims -- HUMAN in `human_answers`,
 *      AI in `chatgpt_answers` -- as a whitespace-collapsed exact match;
 *   2. it does NOT appear in the other list (a text that is in both proves nothing about a label);
 *   3. the recorded question matches the source row's question.
 *
 * A single fabricated, edited or truncated word anywhere in a text fails that row. This is the
 * check that separates "real captures from a citable corpus" from "text someone wrote".
 *
 *   node benchmark/verify-holdout-corpus.js [--file corpus-holdout.json] [--concurrency 3]
 */
const fs = require('fs');
const path = require('path');

const B = __dirname;
const argv = process.argv.slice(2);
const arg = (n, d) => {
  const i = argv.indexOf('--' + n);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d;
};
const FILE = arg('file', 'corpus-holdout.json');
const CONCURRENCY = parseInt(arg('concurrency', '3'), 10);

const corpus = JSON.parse(fs.readFileSync(path.join(B, FILE), 'utf8'));
const squash = (s) => String(s).replace(/\s+/g, ' ').trim();

// datasets-server 429s in bursts after a large build run, then recovers within a couple of minutes.
// Honour Retry-After when present, otherwise exponential backoff with jitter; give up after MAX_TRIES.
const MAX_TRIES = parseInt(arg('tries', '8'), 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitter = () => Math.floor(Math.random() * 400);

async function fetchRow(config, rowIdx) {
  const url = 'https://datasets-server.huggingface.co/rows'
    + '?dataset=Hello-SimpleAI%2FHC3'
    + '&config=' + encodeURIComponent(config)
    + '&split=train&offset=' + rowIdx + '&length=1';
  let lastStatus = '?';
  for (let t = 0; t < MAX_TRIES; t++) {
    const res = await fetch(url);
    if (res.ok) {
      const j = await res.json();
      const rows = j.rows || [];
      if (!rows.length) throw new Error('no row at offset ' + rowIdx);
      return rows[0].row || {};
    }
    lastStatus = res.status;
    if (res.status === 429 || res.status >= 500) {
      const ra = parseInt(res.headers.get('retry-after') || '', 10);
      const back = Number.isFinite(ra) && ra > 0 ? ra * 1000 : Math.min(30000, 1500 * 2 ** t);
      await sleep(back + jitter());
      continue;
    }
    throw new Error('HF ' + res.status + ' for ' + config + ' offset ' + rowIdx);
  }
  throw new Error('HF ' + lastStatus + ' (rate-limited) for ' + config + ' offset ' + rowIdx + ' after ' + MAX_TRIES + ' tries');
}

function inAny(list, needle) {
  return (Array.isArray(list) ? list : []).some((t) => squash(t) === needle);
}

async function check(r) {
  try {
    const row = await fetchRow(r.config, r.row_idx);
    const text = squash(r.text);
    const claimed = r.label === 'HUMAN' ? row.human_answers : row.chatgpt_answers;
    const other = r.label === 'HUMAN' ? row.chatgpt_answers : row.human_answers;
    const fails = [];
    if (!inAny(claimed, text)) fails.push('text not found in ' + (r.label === 'HUMAN' ? 'human_answers' : 'chatgpt_answers'));
    if (inAny(other, text)) fails.push('text ALSO appears in the opposite list - label is not evidence');
    if (squash(row.question || '') !== squash(r.question || '')) fails.push('question mismatch');
    return { id: r.id, label: r.label, config: r.config, row_idx: r.row_idx, ok: !fails.length, fails };
  } catch (e) {
    return { id: r.id, label: r.label, config: r.config, row_idx: r.row_idx, ok: false, fails: ['fetch: ' + e.message] };
  }
}

(async () => {
  console.log('verifying ' + corpus.length + ' rows of ' + FILE + ' against Hello-SimpleAI/HC3 (datasets-server)');
  const results = [];
  let i = 0;
  async function worker() {
    while (i < corpus.length) {
      const k = i++;
      results[k] = await check(corpus[k]);
      const r = results[k];
      console.log('  ' + (r.ok ? 'OK  ' : 'FAIL') + ' ' + r.id.padEnd(8) + r.label.padEnd(6)
        + r.config.padEnd(12) + 'row_idx=' + String(r.row_idx).padEnd(6)
        + (r.ok ? '' : ' <- ' + r.fails.join('; ')));
    }
  }
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));

  const ok = results.filter((r) => r.ok).length;
  console.log('\n' + ok + '/' + results.length + ' rows verified against the source.');
  const byLabel = {};
  for (const r of results) byLabel[r.label] = (byLabel[r.label] || 0) + (r.ok ? 1 : 0);
  console.log('  by label: ' + JSON.stringify(byLabel));
  if (ok !== results.length) {
    console.log('** provenance NOT established - do not benchmark on this file until every row passes **');
    process.exit(2);
  }
  console.log('provenance established: every text is a verbatim HC3 answer carrying its own label.');
})();
