// Build a balanced, labeled benchmark corpus from the HC3 corpus
// (Human ChatGPT Comparison Corpus) via the HuggingFace datasets-server HTTP API.
//
// HC3 pairs a real human answer with a real ChatGPT answer to the same question,
// so labels are ground truth rather than guessed.
//
// Usage: node build-corpus.js [rowsPerConfig] [targetTotal]

const fs = require('fs');
const path = require('path');

const CONFIGS = ['open_qa', 'reddit_eli5', 'finance', 'medicine', 'wiki_csai'];
const MIN_CHARS = 300;
const MAX_CHARS = 1600;

const rowsPerConfig = parseInt(process.argv[2] || '100', 10);
const targetTotal = parseInt(process.argv[3] || '48', 10);

async function fetchRows(config, offset, length) {
  const url = 'https://datasets-server.huggingface.co/rows'
    + '?dataset=Hello-SimpleAI%2FHC3'
    + '&config=' + encodeURIComponent(config)
    + '&split=train&offset=' + offset + '&length=' + length;
  const res = await fetch(url);
  if (!res.ok) throw new Error('HF ' + res.status + ' for ' + config + ': ' + (await res.text()).slice(0, 200));
  return (await res.json()).rows || [];
}

// Deterministic PRNG so the corpus is reproducible across runs.
let seed = 20260929;
function rand() {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
}
function pick(arr) { return arr[Math.floor(rand() * arr.length) % arr.length]; }

function usable(t) {
  return typeof t === 'string' && t.length >= MIN_CHARS && t.length <= MAX_CHARS;
}

(async () => {
  const human = [];
  const ai = [];
  const seen = new Set();

  for (const config of CONFIGS) {
    let offset = 0;
    let guard = 0;
    while (guard++ < 6) {
      let rows;
      try {
        rows = await fetchRows(config, offset, rowsPerConfig);
      } catch (e) {
        console.error('  ! ' + e.message);
        break;
      }
      if (!rows.length) break;

      for (const wrapper of rows) {
        const r = wrapper.row || {};
        const h = Array.isArray(r.human_answers) ? r.human_answers : [];
        const c = Array.isArray(r.chatgpt_answers) ? r.chatgpt_answers : [];
        const ht = h.find(usable);
        const ct = c.find(usable);
        if (ht && !seen.has('h:' + ht.slice(0, 120))) {
          seen.add('h:' + ht.slice(0, 120));
          human.push({ text: ht, label: 'HUMAN', config, question: r.question || null });
        }
        if (ct && !seen.has('a:' + ct.slice(0, 120))) {
          seen.add('a:' + ct.slice(0, 120));
          ai.push({ text: ct, label: 'AI', config, question: r.question || null });
        }
      }
      offset += rowsPerConfig;
    }
    console.log(config.padEnd(12), 'human=' + human.length, 'ai=' + ai.length);
  }

  // Balance: equal counts per class, spread across configs.
  const perClass = Math.min(targetTotal / 2, human.length, ai.length);
  const byConfig = (pool) => {
    const m = {};
    for (const x of pool) (m[x.config] = m[x.config] || []).push(x);
    return m;
  };
  const sample = (pool, n) => {
    const m = byConfig(pool);
    const keys = Object.keys(m);
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

  const corpus = [];
  for (let i = 0; i < Math.max(hSel.length, aSel.length); i++) {
    if (aSel[i]) corpus.push({ id: 'ai-' + String(i).padStart(3, '0'), ...aSel[i] });
    if (hSel[i]) corpus.push({ id: 'hu-' + String(i).padStart(3, '0'), ...hSel[i] });
  }

  const outPath = path.join(__dirname, 'corpus.json');
  fs.writeFileSync(outPath, JSON.stringify(corpus, null, 2));

  const dist = {};
  for (const x of corpus) dist[x.label + '/' + x.config] = (dist[x.label + '/' + x.config] || 0) + 1;
  const lens = corpus.map((x) => x.text.length).sort((a, b) => a - b);

  console.log('\nwrote', outPath);
  console.log('total:', corpus.length,
    '| AI:', corpus.filter((x) => x.label === 'AI').length,
    '| HUMAN:', corpus.filter((x) => x.label === 'HUMAN').length);
  console.log('char length min/median/max:', lens[0], lens[Math.floor(lens.length / 2)], lens[lens.length - 1]);
  console.log('distribution:', JSON.stringify(dist, null, 2));
})();
