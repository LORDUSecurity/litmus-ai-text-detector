// Print the markdown model-comparison table straight from the backend, so a number in
// docs/free-model-research.md can be re-derived instead of hand-copied. The summary
// statistics (including the median convention) come from backend/app.py summarize_run --
// reimplementing them here is exactly how a doc and a panel end up disagreeing.
//
//   node doc-table.js [corpus]        (default hc3-48; base URL from $DOC_TABLE_API)

const BASE = process.env.DOC_TABLE_API || 'http://127.0.0.1:8787';
const CORPUS = process.argv[2] || 'hc3-48';

fetch(`${BASE}/benchmark/models`)
  .then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status} from ${BASE}/benchmark/models`);
    return r.json();
  })
  .then((rows) => {
    const sel = rows.filter((x) => x.corpus === CORPUS);
    // A run that scored no texts ranks nothing, and its model column may be a tag standing in
    // for an unattributable one (`or-gemma26b-smoke4`: no manifest line, no run log, 4x http_429).
    // Printing it as a model row is how a filename fragment ends up in a comparison table.
    const scored = sel.filter((x) => (x.n_scored ?? 0) > 0);
    const skipped = sel.length - scored.length;
    if (!scored.length) {
      console.log(`no scored runs recorded under corpus=${CORPUS}`);
      return;
    }
    const f = (v, d = 3) => (v === null || v === undefined ? '—' : Number(v).toFixed(d));
    scored.sort((a, b) => (b.separation ?? -9) - (a.separation ?? -9));
    console.log('| tag | model | n | acc | acc / corpus | wrong | deferred | med AI | med human | separation |');
    console.log('|---|---|---|---|---|---|---|---|---|---|');
    for (const r of scored) {
      console.log(`| ${r.tag} | ${r.model} | ${r.n_scored} | ${f(r.accuracy_over_scored)} | `
        + `${f(r.accuracy_over_corpus)} | ${r.wrong} | ${r.deferred} | `
        + `${f(r.median_ai_text)} | ${f(r.median_human_text)} | ${f(r.separation)} |`);
    }
    if (skipped) {
      console.log(`\n_${skipped} run(s) under this corpus are omitted: they scored zero texts, `
        + 'so they are capture history rather than a measurement._');
    }
  })
  .catch((e) => { console.error(e.message); process.exit(1); });
