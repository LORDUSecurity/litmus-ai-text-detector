#!/usr/bin/env node
/*
 * Print one /benchmark/models row by tag, on one line, the way the round scripts want to log it.
 *
 *   node benchmark/panel-row.js <tag> [<tag2> ...]
 *
 * This exists because the round-7 script used to inline the same logic in `node -e '...'`, and that
 * inline copy had an unbalanced paren -- a syntax error bash cannot see, since `bash -n` does not
 * parse the quoted Node script. Anything a shell script needs to run belongs in its own file.
 *
 * Reads the panel from the backend, so it needs `python -m backend.app` up. Exit 1 if a tag is
 * missing, because a round that did not register is not a round that ran.
 */
const BACKEND = process.env.BACKEND_URL || "http://127.0.0.1:8787";
const tags = process.argv.slice(2);

(async () => {
  if (!tags.length) {
    console.error("usage: panel-row.js <captureTag> [more tags]");
    process.exit(2);
  }
  const res = await fetch(BACKEND + "/benchmark/models");
  if (!res.ok) {
    console.error(`GET /benchmark/models -> HTTP ${res.status}; is the backend running?`);
    process.exit(1);
  }
  const rows = await res.json();
  const list = Array.isArray(rows) ? rows : (rows.models || []);
  let missing = 0;
  for (const tag of tags) {
    const r = list.find((x) => x.tag === tag);
    if (!r) {
      console.log(`${tag}: NOT IN /benchmark/models -- the import did not attribute this capture`);
      missing++;
      continue;
    }
    console.log(
      `${tag} ${r.model} [${r.corpus} mt=${r.params && r.params.max_tokens} ` +
      `t=${r.params && r.params.temperature}] ` +
      `n=${r.n_scored}/${r.n_rows} no_answer=${r.n_no_answer} bad_answer=${r.n_parse_failed} ` +
      `acc/scored=${r.accuracy_over_scored} acc/corpus=${r.accuracy_over_corpus} ` +
      `medAI=${r.median_ai_text} medHU=${r.median_human_text} sep=${r.separation} ` +
      `wrong=${r.wrong} deferred=${r.deferred}`
    );
  }
  process.exit(missing ? 1 : 0);
})();
