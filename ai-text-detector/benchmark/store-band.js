/*
 * Fit, check and store a decision band for one benchmark capture.
 *
 * The scoring is not reimplemented here: it runs the shipped ensemble node through
 * n8n/test-ensemble.js, so a band that "wins" here wins under the logic the workflow actually
 * executes -- including the confidence gates and the salvaged-row deferral, which calibrate.js's
 * inline approximation does not model.
 *
 *   node benchmark/store-band.js --tag <captureTag> --ai 0.56 --human 0.45            # report only
 *   node benchmark/store-band.js --tag <captureTag> --ai 0.56 --human 0.45 --row 4     # and store
 *   node benchmark/store-band.js --tag <captureTag> --ai 0.62 --human 0.40 --model some/provider-x:free
 *
 * Two checks run before anything is written, because both have burned this project already:
 *  - MARGIN: a cut that sits 0.00 from a measured score is fitting the corpus, not the model.
 *    Live jitter on this pipeline is far larger than that (8 of 12 borderline texts flip category
 *    between identical runs), so a knife-edge band is refused unless --force says otherwise.
 *  - FOLD: the band is scored on stratified halves. A band that is better in one half and worse in
 *    the other has not been validated, it has been chosen.
 *
 * Notes written to the row always carry n, accuracy, separation, the corpus and the in-sample
 * caveat, so a future reader can tell a stored band from a proven one.
 */
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");

const B = __dirname;
const { config, evaluate, laneRecord, readJsonl } = require(path.join(B, "..", "n8n", "test-ensemble.js"));

const argv = process.argv.slice(2);
const arg = (n, d) => {
  const i = argv.indexOf("--" + n);
  return i >= 0 && argv[i + 1] !== undefined && !argv[i + 1].startsWith("--") ? argv[i + 1] : d;
};
const flag = (n) => argv.includes("--" + n);

const TAG = arg("tag", null);
const AI_T = parseFloat(arg("ai", "NaN"));
const HU_T = parseFloat(arg("human", "NaN"));
const AI_C = parseFloat(arg("aic", "0.6"));
const HU_C = parseFloat(arg("huc", "0.6"));
const SEC_T = arg("sec", null) === null ? null : parseFloat(arg("sec"));
const ROW = arg("row", null) === null ? null : parseInt(arg("row"), 10);
const MODEL_ID = arg("model", null);
const MIN_MARGIN = parseFloat(arg("min-margin", "0.03"));
if (!TAG || !Number.isFinite(AI_T) || !Number.isFinite(HU_T)) {
  console.error("usage: store-band.js --tag <captureTag> --ai N --human N [--aic N] [--huc N] [--row id | --model id] [--dry-run]");
  process.exit(2);
}
if (!(HU_T < AI_T)) {
  console.error(`the band is inverted: human cut ${HU_T} is not below ai cut ${AI_T}`);
  process.exit(2);
}

const FILE = path.join(B, "results-" + TAG + ".jsonl");
if (!fs.existsSync(FILE)) {
  console.error("no capture at " + FILE);
  process.exit(1);
}
const rows = readJsonl(FILE);
const scored = rows.filter((r) => r.ok && Number.isFinite(Number(r.ai_probability)));
if (scored.length < 8) {
  console.error(`only ${scored.length} scored rows in ${TAG} -- a band fitted on that is a story about noise`);
  process.exit(1);
}

const ai = scored.filter((r) => r.label === "AI");
const hu = scored.filter((r) => r.label === "HUMAN");
if (!ai.length || !hu.length) {
  console.error(`${TAG} has ${ai.length} AI and ${hu.length} HUMAN scored rows: a one-class capture cannot fit a band, only an anchor.`);
  process.exit(1);
}

const med = (v) => {
  const a = v.map((r) => Number(r.ai_probability)).sort((x, y) => x - y);
  return a.length % 2 ? a[(a.length - 1) / 2] : (a[a.length / 2 - 1] + a[a.length / 2]) / 2;
};

// The corpus tag for provenance: the manifest line carries the corpus file name.
const manifest = fs.readFileSync(path.join(B, "runs-manifest.log"), "utf8").trim().split(/\r?\n/);
const params = manifest
  .map((l) => {
    const m = l.match(/params: (\{.*\})/);
    try {
      return m ? JSON.parse(m[1]) : null;
    } catch (e) {
      return null;
    }
  })
  .filter((p) => p && p.tag === TAG)
  .pop();
const CORPUS = params ? params.corpus : "unknown-corpus";
const MAX_TOKENS = params ? params.max_tokens : "?";

function score(cfg, subset, cuts) {
  const aiT = cuts.aiT, huT = cuts.huT;
  let correct = 0, wrong = 0, review = 0, tp = 0, fn = 0, tn = 0, fp = 0;
  const margins = [];
  for (const r of subset) {
    const out = evaluate(cfg, laneRecord(r, "primary", cfg.primary_model), null);
    const c = out.classification;
    const want = r.label === "AI" ? "AI_GENERATED" : "HUMAN_LIKELY";
    if (c === "REVIEW_REQUIRED" || c === "DETECTION_UNAVAILABLE") review++;
    else if (c === want) {
      correct++;
      if (r.label === "AI") tp++; else tn++;
    } else {
      wrong++;
      if (r.label === "AI") fn++; else fp++;
    }
    const p = Number(r.ai_probability);
    if (c === "AI_GENERATED") margins.push(p - aiT);
    else if (c === "HUMAN_LIKELY") margins.push(huT - p);
  }
  const n = subset.length;
  return {
    correct, wrong, review, tp, fn, tn, fp,
    accuracy: n ? (correct + 0) / n : 0,          // review counted as not-correct, same as calibrate.js
    decided_prec: correct + wrong ? correct / (correct + wrong) : 0,
    ai_recall: tp + fn ? tp / (tp + fn) : 0,
    hu_spec: tn + fp ? tn / (tn + fp) : 0,
    min_margin: margins.length ? Math.min(...margins.map(Math.abs)) : null,
    tight: margins.filter((m) => Math.abs(m) < MIN_MARGIN).length,
  };
}

const fittedCfg = config({
  ai_prob_threshold: AI_T,
  human_prob_threshold: HU_T,
  ai_conf_threshold: AI_C,
  human_conf_threshold: HU_C,
});
if (SEC_T !== null) fittedCfg.secondary_decision_threshold = SEC_T;
const shippedCfg = config();

const f = score(fittedCfg, scored, { aiT: AI_T, huT: HU_T });
const s = score(shippedCfg, scored, { aiT: shippedCfg.ai_prob_threshold, huT: shippedCfg.human_prob_threshold });
console.log("=".repeat(78));
console.log(`BAND FIT  tag=${TAG}  model=${params ? params.model : "?"}  corpus=${CORPUS}  max_tokens=${MAX_TOKENS}`);
console.log(`capture: ${rows.length} rows, ${scored.length} scored (AI ${ai.length} / HUMAN ${hu.length}), ` +
  `medAI=${med(ai).toFixed(3)} medHU=${med(hu).toFixed(3)} sep=${(med(ai) - med(hu)).toFixed(3)}`);
console.log("=".repeat(78));
const line = (name, g, cfg) =>
  `  ${name.padEnd(26)} ai>=${cfg.ai_prob_threshold} hu<=${cfg.human_prob_threshold} conf>=${cfg.ai_conf_threshold}/${cfg.human_conf_threshold}` +
  `  => acc=${g.accuracy.toFixed(3)} prec=${g.decided_prec.toFixed(3)} review=${g.review}` +
  ` AIrecall=${g.ai_recall.toFixed(2)} HUMANspec=${g.hu_spec.toFixed(2)} [tp${g.tp} tn${g.tn} fp${g.fp} fn${g.fn}]`;
console.log(line("SHIPPED default", s, shippedCfg));
console.log(line("FITTED band", f, fittedCfg));

// ---- margin check ----
console.log(`\nMARGIN  min |score - cut| over deciding rows = ${f.min_margin === null ? "n/a" : f.min_margin.toFixed(3)}` +
  `, rows within ${MIN_MARGIN} of a cut = ${f.tight}`);
if (f.tight > 0 || (f.min_margin !== null && f.min_margin < MIN_MARGIN)) {
  console.log("  NOT OK: this band reads a score that sits on top of the cut. On this pipeline that");
  console.log("  difference is inside the run-to-run noise, so the band would only be reproducible on");
  console.log("  this exact capture." + (flag("force") ? " --force given, proceeding to the fold check." : " Refusing to store."));
  if (!flag("force") && !flag("dry-run")) process.exit(3);
} else {
  console.log("  OK: no decided score sits on its cut.");
}

// ---- fold check: stratified by label so both halves keep the class mix ----
const folds = [[], []];
const perLabel = {};
for (const r of scored) {
  (perLabel[r.label] = perLabel[r.label] || []).push(r);
}
for (const label of Object.keys(perLabel)) {
  perLabel[label].forEach((r, i) => folds[i % 2].push(r));
}
console.log("\nFOLD CHECK (in-sample halves -- a band must move the aggregate, not one fold)");
for (let k = 0; k < 2; k++) {
  const ff = score(fittedCfg, folds[k], { aiT: AI_T, huT: HU_T });
  const ss = score(shippedCfg, folds[k], { aiT: shippedCfg.ai_prob_threshold, huT: shippedCfg.human_prob_threshold });
  const verdict = ff.correct > ss.correct ? "fitted better" : ff.correct < ss.correct ? "fitted WORSE" : "tie";
  console.log(`  fold ${k + 1} (n=${folds[k].length})  fitted ${ff.correct}/${folds[k].length} vs shipped ${ss.correct}/${folds[k].length}  -> ${verdict}`);
}

if (flag("dry-run") || (!ROW && !MODEL_ID)) {
  console.log("\nNo --row/--model given (or --dry-run): nothing written.");
  return;
}

const notes =
  `Band fitted on the ${TAG} capture (${CORPUS}, n=${scored.length} answered of ${rows.length}, ` +
  `max_tokens ${MAX_TOKENS}, ${new Date().toISOString().slice(0, 10)}): ` +
  `acc ${f.accuracy.toFixed(3)} (${f.correct}/${scored.length}, ${f.review} deferrals), ` +
  `AI recall ${f.ai_recall.toFixed(2)}, HUMAN specificity ${f.hu_spec.toFixed(2)}, ` +
  `medAI ${med(ai).toFixed(3)} vs medHU ${med(hu).toFixed(3)}. ` +
  `Tightest score-to-cut margin ${f.min_margin === null ? "n/a" : f.min_margin.toFixed(2)} across the deciding rows. ` +
  (SEC_T !== null ? `Secondary tie-break cut ${SEC_T}. ` : "") +
  `IN-SAMPLE: this band was chosen on these texts, so the accuracy is an upper bound, not a ` +
  `prediction about live traffic.`;

const body = {
  ai_prob_threshold: AI_T,
  human_prob_threshold: HU_T,
  ai_conf_threshold: AI_C,
  human_conf_threshold: HU_C,
  measured: true,
  notes,
};
if (SEC_T !== null) body.secondary_decision_threshold = SEC_T;

function request(method, url, payload) {
  return new Promise((resolve, reject) => {
    const data = payload ? JSON.stringify(payload) : null;
    const u = new URL(url);
    const req = http.request(
      { hostname: u.hostname, port: u.port, path: u.pathname, method, headers: data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {} },
      (res) => {
        let buf = "";
        res.on("data", (d) => (buf += d));
        res.on("end", () => resolve({ status: res.statusCode, body: buf }));
      },
    );
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

(async () => {
  const BASE = "http://127.0.0.1:8787";
  let target = ROW;
  if (!target) {
    const models = JSON.parse((await request("GET", BASE + "/models")).body);
    const hit = models.find((m) => m.model_id === MODEL_ID);
    if (!hit) {
      const providers = JSON.parse((await request("GET", BASE + "/providers")).body);
      const p = (providers.items || providers).find((x) => params && x.base_url === params.base_url);
      if (!p) {
        console.error(`no model row for ${MODEL_ID} and no provider with base_url ${params && params.base_url} to create one under`);
        process.exit(1);
      }
      const created = await request("POST", BASE + "/models", { model_id: MODEL_ID, provider_id: p.id, role: "either", ...body });
      console.log(`\nPOST /models -> ${created.status}\n${created.body}`);
      process.exitCode = created.status === 201 ? 0 : 1;
      return;
    }
    target = hit.id;
  }
  const r = await request("PATCH", `${BASE}/models/${target}`, body);
  console.log(`\nPATCH /models/${target} -> ${r.status}`);
  if (r.status !== 200) {
    console.log(r.body);
    process.exitCode = 1;
    return;
  }
  const m = JSON.parse(r.body);
  console.log(`stored on ${m.model_id} (row ${m.id}, lane=${m.lane}): ai=${m.ai_prob_threshold} human=${m.human_prob_threshold} sec=${m.secondary_decision_threshold} measured=${m.measured}`);
  console.log("notes: " + m.notes);
  console.log("\nReminder: a stored band is not inert -- tuning_block() ships the row of whichever model a request names.");
})();
