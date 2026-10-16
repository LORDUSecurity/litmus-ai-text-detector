#!/usr/bin/env node
/*
 * Prove that a tuned models row actually SHIPS -- through the real backend, the real n8n webhook,
 * and the real gateway. Exit 0 only if all three claims hold.
 *
 *   node tools/verify-stored-band.js --model inclusionai/ling-3.1-flash
 *   node tools/verify-stored-band.js --model <id> --corpus benchmark/corpus.json --n 3
 *
 * Why this exists separately from `tools/verify-free-lane.js`: that tool puts a model in the primary
 * seat with a BYOK header supplied by the caller, so it proves the *gateway* path works. This one
 * sends no key at all -- it names a model that has a row in the database and asks whether the row's
 * own numbers and the row's own endpoint reach the request. On 2026-10-09 they did not: the key and
 * the tuning followed the stored model but `base_url` did not, so an OpenRouter model was POSTed at
 * the NVIDIA default, the gateway returned the plain-text "404 page not found", and the tie-break
 * quietly produced a plausible verdict from the other lane. A classification alone cannot see that,
 * which is why every assertion below reads `config_used` and `evidence.models`, never the verdict.
 *
 * Four checks per run:
 *   1. the workflow echoes this row's cuts and sampling, not the baked defaults;
 *   2. the lane that runs is the named model's own provider endpoint;
 *   3. the credential is the stored one (key_source "header"), never the browser's;
 *   4. the named lane answers ok:true -- a verdict decided by the fallback lane is a failure here,
 *      however correct it looks.
 *
 * Costs one real completion per text, on the model's own account. It prints no key material: only
 * the key_source label the workflow reports.
 */
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const BACKEND = process.env.BACKEND_URL || "http://127.0.0.1:8787";
const arg = (name, dflt) => {
  const i = process.argv.indexOf("--" + name);
  return i === -1 ? dflt : process.argv[i + 1];
};

const MODEL = arg("model");
if (!MODEL || MODEL.startsWith("--")) {
  console.error("usage: verify-stored-band.js --model <models.model_id> [--corpus f] [--n 2]");
  process.exit(2);
}
const CORPUS = arg("corpus", path.join("benchmark", "corpus-screen12.json"));
const N = parseInt(arg("n", "2"), 10);

async function json(res) {
  if (!res.ok) throw new Error(`HTTP ${res.status} ${await res.text().catch(() => "")}`.slice(0, 300));
  return res.json();
}

(async () => {
  const corpus = JSON.parse(fs.readFileSync(path.join(ROOT, CORPUS), "utf8"));
  if (!Array.isArray(corpus) || !corpus.length) throw new Error(`${CORPUS} is not a JSON array`);

  const rows = await json(await fetch(`${BACKEND}/models`));
  const row = (rows.models || rows).find((m) => m.model_id === MODEL && m.enabled);
  if (!row) {
    console.error(`FAIL  no enabled models row for ${MODEL} -- store one before verifying it`);
    process.exit(1);
  }
  console.log(`row ${row.id}  ${MODEL}  lane=${row.lane}  provider=${row.provider_name}` +
    `  cuts ai>=${row.ai_prob_threshold} hu<=${row.human_prob_threshold}` +
    ` conf ${row.ai_conf_threshold}/${row.human_conf_threshold}` +
    `  sec=${row.secondary_decision_threshold}  mt=${row.max_tokens} t=${row.temperature}`);

  const picks = [];
  for (const want of ["AI", "HUMAN"]) {
    corpus.filter((r) => String(r.label || "").toUpperCase().startsWith(want))
      .slice(0, Math.max(1, Math.floor(N / 2)))
      .forEach((r) => picks.push(r));
  }
  if (!picks.length) picks.push(corpus[0]);

  const problems = [];
  const detEchoes = [];
  for (const text of picks) {
    let det;
    try {
      const res = await json(await fetch(`${BACKEND}/detect`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: text.text, lane_config: { primary: { model: MODEL } } }),
      }));
      if (res.error) throw new Error(`${res.error} / ${res.detail}`.slice(0, 200));
      det = await json(await fetch(`${BACKEND}/detections/${res.backend_detection_id}`));
    } catch (e) {
      problems.push(`${text.id}: request failed -- ${e.message}`);
      console.log(`  ${text.id} (${text.label})  REQUEST FAILED  ${e.message}`);
      continue;
    }
    const cu = (det.payload || {}).config_used || {};
    const ev = (((det.payload || {}).evidence || {}).models || [])
      .filter((m) => m.model === MODEL);
    const lane = ev[0];
    const th = cu.thresholds || {};
    const p = cu.primary || {};

    const checks = [
      [`echoes this row's ai cut`, th.ai_prob === row.ai_prob_threshold,
        `got ${th.ai_prob}, row ${row.ai_prob_threshold}`],
      [`echoes this row's human cut`, th.human_prob === row.human_prob_threshold,
        `got ${th.human_prob}, row ${row.human_prob_threshold}`],
      [`echoes this row's max_tokens`, cu.sampling && cu.sampling.max_tokens === row.max_tokens,
        `got ${cu.sampling && cu.sampling.max_tokens}, row ${row.max_tokens}`],
      [`named the stored model`, p.model === MODEL, `got ${p.model}`],
      [`reached the row's provider endpoint`, p.base_url === row.provider_base_url,
        `got ${p.base_url} (${p.base_url_source}), row provider is ${row.provider_base_url}`],
      [`used the stored key`, p.key_source === "header",
        `key_source ${p.key_source} -- "env" means n8n's own credential answered`],
      [`the named lane answered`, !!lane && lane.ok === true,
        lane ? `${lane.error_code}: ${String(lane.error_detail || "").slice(0, 90)}`
          : "no lane record for this model"],
      [`the named lane decided`, det.deciding_lane === "primary",
        `deciding_lane ${det.deciding_lane}, method ${det.method}`],
    ];
    const bad = checks.filter((c) => !c[1]);
    bad.forEach((c) => problems.push(`${text.id}: ${c[0]} -- ${c[2]}`));
    console.log(`  ${text.id} (${text.label})  p=${det.ai_probability} -> ${det.classification}` +
      `  [${det.deciding_lane}]  ${bad.length ? bad.map((c) => c[0]).join(", ") : "all checks pass"}`);
    if (bad.length) bad.forEach((c) => console.log(`      FAIL ${c[0]}: ${c[2]}`));
    detEchoes.push(JSON.stringify(det.payload && det.payload.config_used));
  }

  // A leak check, not a style rule: config_used is stored in the database and the panel renders it.
  for (const needle of ["api_key", "authorization", "bearer ", "sk-or-"]) {
    if (detEchoes.some((s) => s.toLowerCase().includes(needle))) {
      problems.push(`the stored config_used echo contains a key-ish string: ${needle}`);
    }
  }

  if (problems.length) {
    console.log(`\nNOT OK (${problems.length}):`);
    problems.forEach((p) => console.log("  - " + p));
    process.exit(1);
  }
  console.log(`\nOK  ${picks.length} text(s): the row's cuts, endpoint, key and lane all shipped.`);
})();
