/*
 * Runs the shipped "Ensemble & Build Result" code node.
 *
 * Two modes:
 *   node n8n/test-ensemble.js                 decision-table unit cases
 *   node n8n/test-ensemble.js replay <prim> <sec> [--secondary-threshold 0.9]
 *                                             replay the 48-text benchmark score
 *                                             files through the shipped code
 *
 * The JS is read out of ai-only-workflow.json and the config is produced by running
 * the shipped Normalize Input node, so this exercises the artefact that gets imported
 * into n8n — not a reimplementation that can drift from it.
 */
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

const WF = JSON.parse(readFileSync(join(__dirname, 'ai-only-workflow.json'), 'utf8'));
const NORM = WF.nodes.find((n) => n.name === 'Normalize Input');
const ENS = WF.nodes.find((n) => n.name === 'Ensemble & Build Result');
if (!NORM || !ENS) throw new Error('Expected Normalize Input and Ensemble & Build Result nodes');

const runNormalize = new Function('$input', '$env', NORM.parameters.jsCode);
const runEnsemble = new Function('$', ENS.parameters.jsCode);

// The real config the workflow would use, read from the project .env so the numbers
// under test are the deployed numbers.
function loadEnv(p) {
  const env = {};
  for (const line of readFileSync(p, 'utf8').split(/\r?\n/)) {
    if (line.trim().startsWith('#')) continue;
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return env;
}
const ENV = loadEnv(join(__dirname, '..', '.env'));

function config(overrides) {
  const out = runNormalize({ first: () => ({ json: { text: 'a long enough sentence for detection' } }) }, ENV);
  const cfg = out[0].json.config;
  return Object.assign(cfg, overrides || {});
}

// Mirror of the need_secondary line in the parse node: the secondary lane runs exactly
// when the primary would defer. Reimplemented here because the parse node is templated
// per lane and its output is a single record, not a reusable function.
function needSecondary(rec, cfg) {
  if (!rec.ok) return true;
  const p = rec.result.ai_probability, c = rec.result.confidence;
  const decisive = (p >= cfg.ai_prob_threshold && c >= cfg.ai_conf_threshold)
                || (p <= cfg.human_prob_threshold && c >= cfg.human_conf_threshold);
  return !decisive || !!rec.salvaged;
}

function laneRecord(rec, lane, model) {
  return {
    ok: !!rec.ok,
    lane: lane,
    model: model,
    at: new Date().toISOString(),
    salvaged: !!rec.salvaged,
    truncated: !!rec.truncated,
    error_code: rec.error_code || null,
    error_detail: rec.error_detail || null,
    result: rec.ok ? { ai_probability: rec.ai_probability, confidence: rec.confidence, signals: rec.signals || [] } : undefined
  };
}

// Execute the shipped ensemble node with the given lane records. secondary === null
// means that branch was never taken, exactly as $('Parse Secondary') throwing.
function evaluate(cfg, primary, secondary) {
  const normOut = {
    document_id: 'test',
    config: cfg,
    lane_warnings: [],
    lane_meta: {},
    text_fingerprint: 'fp',
    text_length: 42,
    timestamp: new Date().toISOString()
  };
  const store = {
    'Normalize Input': [{ json: normOut }],
    'Parse Primary': [{ json: primary }]
  };
  if (secondary !== null) store['Parse Secondary'] = [{ json: secondary }];
  const $ = (name) => ({
    first: () => {
      if (!store[name]) throw new Error('node not executed in this run: ' + name);
      return store[name][0];
    }
  });
  return runEnsemble($)[0].json;
}

// ----------------------------------------------------------------- unit cases
function unitCases() {
  const cfg = config();
  let failures = 0;

  const P = (p, c) => ({ ok: true, ai_probability: p, confidence: c });
  const S = (p, c) => ({ ok: true, ai_probability: p, confidence: c });
  const FAIL = (code) => ({ ok: false, error_code: code || 'http_503' });

  // [name, primary, secondary (null = branch not taken), expect classification, expect method]
  const cases = [
    ['primary sure AI, secondary not called', P(0.95, 0.9), null, 'AI_GENERATED', 'SINGLE_MODEL_PRIMARY_AI'],
    ['primary sure HUMAN, secondary not called', P(0.05, 0.9), null, 'HUMAN_LIKELY', 'SINGLE_MODEL_PRIMARY_HUMAN'],
    ['primary decisive even though the secondary ran', P(0.95, 0.9), S(0.10, 0.9), 'AI_GENERATED', 'PRIMARY_DECIDED_AI'],
    ['primary defers, secondary above its threshold', P(0.60, 0.9), S(0.95, 0.4), 'AI_GENERATED', 'TIE_BREAK_AI'],
    ['primary defers, secondary below its threshold', P(0.60, 0.9), S(0.88, 0.4), 'HUMAN_LIKELY', 'TIE_BREAK_HUMAN'],
    ['deferral is decided by the secondary scale, not the primary one', P(0.50, 0.2), S(0.75, 0.3), 'HUMAN_LIKELY', 'TIE_BREAK_HUMAN'],
    ['primary defers and the secondary failed', P(0.60, 0.9), FAIL('http_503'), 'REVIEW_REQUIRED', 'SINGLE_MODEL_PRIMARY_UNCERTAIN'],
    ['primary failed, secondary carries the call', FAIL('http_500'), S(0.97, 0.8), 'AI_GENERATED', 'SINGLE_MODEL_SECONDARY_AI'],
    ['primary failed, secondary below its threshold', FAIL('http_500'), S(0.30, 0.8), 'HUMAN_LIKELY', 'SINGLE_MODEL_SECONDARY_HUMAN'],
    ['both lanes failed', FAIL('http_500'), FAIL('http_503'), 'DETECTION_UNAVAILABLE', 'ALL_MODELS_FAILED'],
  ];

  for (const [name, p, s, wantClass, wantMethod] of cases) {
    const primary = laneRecord(p, 'primary', cfg.primary_model);
    const secondary = s === null ? null : laneRecord(s, 'secondary', cfg.secondary_model);
    const out = evaluate(cfg, primary, secondary);
    const ok = out.classification === wantClass && out.method === wantMethod;
    if (!ok) failures++;
    console.log((ok ? 'PASS  ' : 'FAIL  ') + name);
    if (!ok) console.log('        got ' + out.classification + '/' + out.method + ' want ' + wantClass + '/' + wantMethod);
  }

  // The old failure mode this rewrite removes: two differently-scaled lanes "disagreeing"
  // by 0.5 on every text, which used to route everything to REVIEW_REQUIRED.
  const gapCfg = config({ secondary_decision_threshold: 0.9 });
  const out = evaluate(gapCfg, laneRecord(P(0.60, 0.9), 'primary', gapCfg.primary_model),
    laneRecord(S(0.10, 0.9), 'secondary', gapCfg.secondary_model));
  const gapOk = out.classification === 'HUMAN_LIKELY' && out.component_scores.lane_gap === 0.5;
  if (!gapOk) failures++;
  console.log((gapOk ? 'PASS  ' : 'FAIL  ') + 'lane_gap reported, never used as a gate');

  // Weighted-blending leftovers must be gone from the contract.
  const leftovers = JSON.stringify(out).match(/weights_used|model_disagreement|ENSEMBLE_|HIGH_MODEL_DISAGREEMENT/);
  if (leftovers) { failures++; console.log('FAIL  retired fields still present: ' + leftovers[0]); }
  else console.log('PASS  no weights_used / model_disagreement / ENSEMBLE_* fields left');

  // The optional-lane contract end to end: with secondary_enabled false the tie-break
  // branch never runs (secondary === null mirrors $('Parse Secondary') throwing), so a
  // gray-zone primary answers REVIEW_REQUIRED with the disabling warning and the echo
  // config_used.secondary.enabled === false; a failed primary with no second opinion is
  // DETECTION_UNAVAILABLE, never a silent pass. And a normal run must echo enabled:true.
  const offCfg = config({ secondary_enabled: false });
  const offOut = evaluate(offCfg, laneRecord(P(0.60, 0.9), 'primary', offCfg.primary_model), null);
  const offOk = offOut.classification === 'REVIEW_REQUIRED'
    && offOut.method === 'SINGLE_MODEL_PRIMARY_UNCERTAIN'
    && (offOut.warnings || []).some((w) => /Second-opinion lane is disabled/.test(w))
    && ((offOut.config_used || {}).secondary || {}).enabled === false;
  if (!offOk) failures++;
  console.log((offOk ? 'PASS  ' : 'FAIL  ') + 'disabled lane: gray zone -> REVIEW_REQUIRED + warning + enabled:false echo'
    + (offOk ? '' : '   got ' + offOut.classification + '/' + offOut.method + ' ' + JSON.stringify(offOut.warnings)));

  const offFailOut = evaluate(offCfg, laneRecord(FAIL('http_500'), 'primary', offCfg.primary_model), null);
  const offFailOk = offFailOut.classification === 'DETECTION_UNAVAILABLE'
    && offFailOut.method === 'ALL_MODELS_FAILED';
  if (!offFailOk) failures++;
  console.log((offFailOk ? 'PASS  ' : 'FAIL  ') + 'disabled lane: failed primary -> DETECTION_UNAVAILABLE'
    + (offFailOk ? '' : '   got ' + offFailOut.classification + '/' + offFailOut.method));

  const onEcho = evaluate(cfg, laneRecord(P(0.95, 0.9), 'primary', cfg.primary_model), null);
  const onEchoOk = ((onEcho.config_used || {}).secondary || {}).enabled === true;
  if (!onEchoOk) failures++;
  console.log((onEchoOk ? 'PASS  ' : 'FAIL  ') + 'enabled:true echoed for a normal (on) run');

  console.log(failures ? '\n' + failures + ' case(s) failed' : '\nall unit cases passed');
  return failures;
}

// ---------------------------------------------------------------- corpus replay
function readJsonl(p) {
  return readFileSync(p, 'utf8').trim().split(/\r?\n/).map((l) => JSON.parse(l));
}

// cfgOverrides lets an offline audit re-band a replay without editing .env. Kept last and
// merged last so an existing positional call (prim, sec, threshold, quiet) is unaffected.
function replay(primFile, secFile, secThreshold, quiet, cfgOverrides) {
  const log = quiet ? function () {} : console.log;
  const cfg = config(Object.assign(
    {},
    secThreshold === null ? null : { secondary_decision_threshold: secThreshold },
    cfgOverrides || {}
  ));
  const prim = readJsonl(primFile);
  const secById = {};
  for (const r of readJsonl(secFile)) secById[r.id] = r;

  const tally = { correct: 0, wrong: 0, review: 0 };
  const byMethod = {};
  const rows = [];

  for (const p of prim) {
    const pRec = laneRecord(p, 'primary', cfg.primary_model);
    const s = secById[p.id];
    const call = pRec.ok ? needSecondary(pRec, cfg) : true;
    const sRec = (s && call) ? laneRecord(s, 'secondary', cfg.secondary_model) : null;
    const out = evaluate(cfg, pRec, sRec);

    const truth = p.label === 'AI' ? 'AI_GENERATED' : 'HUMAN_LIKELY';
    let outcome;
    if (out.classification === 'REVIEW_REQUIRED' || out.classification === 'DETECTION_UNAVAILABLE') outcome = 'review';
    else outcome = out.classification === truth ? 'correct' : 'wrong';
    tally[outcome]++;
    byMethod[out.method] = (byMethod[out.method] || 0) + 1;
    rows.push({ id: p.id, label: p.label, outcome, method: out.method, p: out.ai_probability, lane: out.component_scores.deciding_lane });
  }

  const n = prim.length;
  log('replay  primary=' + primFile.replace(/^.*[\\/]/, '') + '  secondary=' + secFile.replace(/^.*[\\/]/, '')
    + '  secondary_decision_threshold=' + cfg.secondary_decision_threshold);
  // Name the ruler every time. A replay that does not say which band it judged scores against
  // can be read as if it used the model's own band when it used someone else's.
  log('  lanes: ' + cfg.primary_model + ' / ' + cfg.secondary_model
    + '   primary band ' + cfg.human_prob_threshold + '..' + cfg.ai_prob_threshold
    + ' conf>=' + cfg.ai_conf_threshold + '/' + cfg.human_conf_threshold);
  log('  n=' + n + '  correct=' + tally.correct + '  wrong=' + tally.wrong + '  review=' + tally.review);
  log('  accuracy over answered=' + (tally.correct / Math.max(1, tally.correct + tally.wrong)).toFixed(3)
    + '   accuracy over all=' + (tally.correct / n).toFixed(3));
  log('  methods: ' + JSON.stringify(byMethod));
  const wrong = rows.filter((r) => r.outcome === 'wrong');
  if (wrong.length) {
    log('  misjudged:');
    for (const w of wrong) log('    ' + w.id + ' is ' + w.label + ' -> ' + w.method + ' p=' + w.p);
  }
  return { tally: tally, byMethod: byMethod, rows: rows, n: n };
}

module.exports = { replay: replay, config: config, evaluate: evaluate, laneRecord: laneRecord, needSecondary: needSecondary, readJsonl: readJsonl };

if (require.main === module) {
  const argv = process.argv.slice(2);
  if (argv[0] === 'replay') {
    if (!argv[1] || !argv[2]) { console.error('usage: node test-ensemble.js replay <primary.jsonl> <secondary.jsonl> [--secondary-threshold N]'); process.exit(2); }
    const ti = argv.indexOf('--secondary-threshold');
    replay(argv[1], argv[2], ti >= 0 ? parseFloat(argv[ti + 1]) : null);
  } else {
    process.exit(unitCases() ? 1 : 0);
  }
}
