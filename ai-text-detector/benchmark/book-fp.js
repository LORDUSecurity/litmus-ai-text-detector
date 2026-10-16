/*
 * False-positive report for the Project Gutenberg corpus (all 46 passages are real
 * published human prose). Answers one question: how often does a lane call a book
 * AI-generated, and which books does it get wrong?
 *
 *   node benchmark/book-fp.js bookultra550
 *   node benchmark/book-fp.js bookultra550 bookdiffgemma      # ensemble behaviour too
 *
 * Thresholds come from .env, the same values the deployed workflow reads.
 */
const fs = require('fs');
const { join } = require('node:path');
const B = __dirname;
const { config } = require(join(B, '..', 'n8n', 'test-ensemble.js'));

const cfg = config();
const corpus = {};
for (const it of JSON.parse(fs.readFileSync(join(B, 'corpus-books.json'), 'utf8'))) corpus[it.id] = it;

function load(tag) {
  const rows = [];
  for (const line of fs.readFileSync(join(B, 'results-' + tag + '.jsonl'), 'utf8').split(/\r?\n/)) {
    if (line.trim()) rows.push(JSON.parse(line));
  }
  return rows;
}

const pct = (n, d) => d ? (100 * n / d).toFixed(1) + '%' : 'n/a';
function stats(v) {
  if (!v.length) return null;
  v = v.slice().sort((a, b) => a - b);
  return { n: v.length, min: v[0], med: v[Math.floor(v.length / 2)], p90: v[Math.floor(v.length * 0.9)], max: v[v.length - 1] };
}

const tags = process.argv.slice(2);
if (!tags.length) {
  console.error('usage: node benchmark/book-fp.js <tag> [tag...]');
  process.exit(2);
}

const all = {};
for (const tag of tags) {
  const rows = load(tag);
  all[tag] = rows;
  const ok = rows.filter((r) => r.ok);
  const errs = {};
  for (const r of rows.filter((x) => !x.ok)) errs[r.error_code || 'unknown'] = (errs[r.error_code] || 0) + 1;
  const s = stats(ok.map((r) => r.ai_probability));
  const lat = stats(ok.map((r) => r.latency_ms));

  // What the shipped decision rule would do with this lane alone.
  const asPrimaryAI = ok.filter((r) => r.ai_probability >= cfg.ai_prob_threshold && r.confidence >= cfg.ai_conf_threshold);
  const asPrimaryHuman = ok.filter((r) => r.ai_probability <= cfg.human_prob_threshold && r.confidence >= cfg.human_conf_threshold);
  const defers = ok.filter((r) => !(r.ai_probability >= cfg.ai_prob_threshold && r.confidence >= cfg.ai_conf_threshold) &&
    !(r.ai_probability <= cfg.human_prob_threshold && r.confidence >= cfg.human_conf_threshold));
  const asSecondaryAI = ok.filter((r) => r.ai_probability >= cfg.secondary_decision_threshold);

  console.log(`\n=== ${tag}`);
  console.log(`usable ${ok.length}/${rows.length}  errors ${JSON.stringify(errs)}`);
  if (s) console.log(`ai_probability min/med/max = ${s.min.toFixed(2)} / ${s.med.toFixed(2)} / ${s.max.toFixed(2)}`);
  if (lat) console.log(`latency ms med/p90/max  = ${lat.med} / ${lat.p90} / ${lat.max}`);
  console.log(`as PRIMARY at ${cfg.ai_prob_threshold}/${cfg.human_prob_threshold} + conf ${cfg.ai_conf_threshold}: ` +
    `would-declare-AI ${asPrimaryAI.length} (${pct(asPrimaryAI.length, ok.length)} FP), ` +
    `decisive-human ${asPrimaryHuman.length}, defer ${defers.length}`);
  console.log(`as SECONDARY at ${cfg.secondary_decision_threshold}: would-declare-AI ${asSecondaryAI.length} (${pct(asSecondaryAI.length, ok.length)} FP)`);

  const worst = ok.slice().sort((a, b) => b.ai_probability - a.ai_probability).slice(0, 8);
  console.log(`highest scores (each is genuine published prose):`);
  for (const r of worst) {
    const src = corpus[r.id] && corpus[r.id].source;
    console.log(`  ${r.id} p=${r.ai_probability.toFixed(2)} c=${r.confidence.toFixed(2)}  ` +
      `${src ? src.title + ' — ' + src.author : '?'}`);
  }

  const byBook = {};
  for (const r of ok) {
    const src = corpus[r.id] && corpus[r.id].source;
    if (!src) continue;
    (byBook[src.gutenberg] = byBook[src.gutenberg] || { title: src.title, author: src.author, ps: [] }).ps.push(r.ai_probability);
  }
  console.log(`per book (median ai_probability over its passages):`);
  for (const g of Object.keys(byBook).sort((a, b) => {
    const A = stats(byBook[a].ps), Bb = stats(byBook[b].ps);
    return Bb.med - A.med;
  })) {
    const st = stats(byBook[g].ps);
    console.log(`  ${st.med.toFixed(2)} (n=${st.n}, max ${st.max.toFixed(2)})  ${byBook[g].title.slice(0, 44)} — ${byBook[g].author}`);
  }
}

// Two lanes present: replay the real rule — primary first, secondary only on deferral.
if (tags.length === 2) {
  const [p, s] = tags;
  const prow = {}; for (const r of all[p]) prow[r.id] = r;
  const srow = {}; for (const r of all[s]) srow[r.id] = r;
  let ai = 0, human = 0, review = 0, unavailable = 0, called = 0;
  const hits = [];
  for (const id of Object.keys(corpus)) {
    const pr = prow[id];
    if (!pr || !pr.ok) { unavailable++; continue; }
    const decisiveAI = pr.ai_probability >= cfg.ai_prob_threshold && pr.confidence >= cfg.ai_conf_threshold;
    const decisiveHuman = pr.ai_probability <= cfg.human_prob_threshold && pr.confidence >= cfg.human_conf_threshold;
    if (decisiveAI) { ai++; hits.push([id, 'primary', pr.ai_probability]); continue; }
    if (decisiveHuman) { human++; continue; }
    const sr = srow[id];
    if (!sr || !sr.ok) { review++; continue; }
    called++;
    if (sr.ai_probability >= cfg.secondary_decision_threshold) { ai++; hits.push([id, 'secondary', sr.ai_probability]); }
    else human++;
  }
  console.log(`\n=== ensemble ${p} + ${s} over ${Object.keys(corpus).length} real book passages`);
  console.log(`HUMAN ${human}  wrongly-AI ${ai}  review ${review}  lane-failed ${unavailable}  tie-break fired ${called}`);
  for (const [id, lane, p_] of hits) {
    const src = corpus[id].source;
    console.log(`  FP ${id} via ${lane} p=${p_.toFixed(2)}  ${src.title} — ${src.author}`);
  }
}
