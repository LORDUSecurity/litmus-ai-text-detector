/*
 * Executes the SHIPPED n8n nodes over the parity fixtures and writes the golden trace.
 *
 *   node tools/engine_parity.js            -> writes n8n/parity-golden.json
 *   node tools/engine_parity.js --stdout   -> print the trace, write nothing
 *
 * This is the authority side of the Python-engine proof. backend/engine.py is a port of code nodes
 * whose text lives only as JS strings inside n8n/ai-only-workflow.json, and every band in the
 * benchmark was fitted through that JS. So the comparison is not a string diff of the two copies --
 * it is the same input run through the real node and through the port, per case.
 *
 * Nothing here talks to a provider or to n8n. The nodes are evaluated with `new Function` over the
 * artefact that gets imported into n8n (the recipe test-normalize.js / test-parse.js /
 * test-ensemble.js already use), and the gateway bodies come from the fixture file.
 */
const { createHash } = require('node:crypto');
const { readFileSync, writeFileSync } = require('node:fs');
const { join } = require('node:path');

const WF_PATH = join(__dirname, '..', 'n8n', 'ai-only-workflow.json');
const WF = JSON.parse(readFileSync(WF_PATH, 'utf8'));
const FIXTURES = JSON.parse(
  readFileSync(join(__dirname, '..', 'n8n', 'parity-fixtures.json'), 'utf8'));

const node = (name) => {
  const n = WF.nodes.find((x) => x.name === name);
  if (!n) throw new Error(`Node not found in the shipped workflow: ${name}`);
  return n;
};

const NORM = node('Normalize Input');
const ENS = node('Ensemble & Build Result');
const LANES = ['primary', 'secondary'];
const BUILD = {};
const PARSE = {};
for (const lane of LANES) {
  const title = lane[0].toUpperCase() + lane.slice(1);
  BUILD[lane] = node(`Build ${title} Request`);
  PARSE[lane] = node(`Parse ${title}`);
}

const fn = (n, args) => new Function(...args, n.parameters.jsCode);
const runNorm = fn(NORM, ['$input', '$env']);
const runBuild = {};
const runParse = {};
for (const lane of LANES) {
  runBuild[lane] = fn(BUILD[lane], ['$input', '$']);
  runParse[lane] = fn(PARSE[lane], ['$input', '$']);
}
const runEns = fn(ENS, ['$']);

const readNode = (store, name) => {
  const key = String(name);
  if (!store[key]) throw new Error(`node not executed in this run: ${key}`);
  return store[key][0];
};
const dollar = (store) => (name) => ({ first: () => readNode(store, name) });

function trace(c) {
  const out = { id: c.id };
  // n8n's webhook hands the first code node {headers, body, ...}; the node unwraps `body` itself.
  const raw = { body: c.payload, headers: c.headers || {} };
  const norm = runNorm({ first: () => ({ json: raw }) }, c.env || {})[0].json;
  out.normalize = norm;
  if (norm.valid === false) return out;

  const store = { 'Normalize Input': [{ json: norm }] };
  const $ = dollar(store);

  out.build = {};
  for (const lane of LANES) {
    out.build[lane] = runBuild[lane]({ first: () => ({ json: {} }) }, $)[0].json;
  }

  out.parse = {};
  const responses = c.responses || {};
  for (const lane of LANES) {
    if (!Object.prototype.hasOwnProperty.call(responses, lane)) break;
    if (lane === 'secondary') {
      const p = store['Parse Primary'];
      // The workflow's IF node runs the second branch only when the primary asked for it, so a
      // fixture that supplies a secondary body anyway must be ignored exactly as n8n would ignore it.
      if (!p || !p[0].json.need_secondary) { out.parse.secondary_not_called = true; break; }
    }
    const title = lane[0].toUpperCase() + lane.slice(1);
    const rec = runParse[lane]({ first: () => ({ json: responses[lane] }) }, $)[0].json;
    out.parse[lane] = rec;
    store[`Parse ${title}`] = [{ json: rec }];
  }
  out.ensemble = runEns($)[0].json;
  return out;
}

const traces = [];
for (const c of FIXTURES.cases) {
  try {
    traces.push(trace(c));
  } catch (e) {
    traces.push({ id: c.id, harness_error: String((e && e.message) || e) });
  }
}

const doc = {
  generated_by: 'tools/engine_parity.js',
  workflow_sha: createHash('sha256').update(readFileSync(WF_PATH)).digest('hex'),
  node_source: {
    normalize: NORM.parameters.jsCode,
    ensemble: ENS.parameters.jsCode,
    build_primary: BUILD.primary.parameters.jsCode,
    parse_primary: PARSE.primary.parameters.jsCode
  },
  cases: traces
};

const text = JSON.stringify(doc, null, 1);
if (process.argv.includes('--stdout')) {
  console.log(text);
} else {
  const path = join(__dirname, '..', 'n8n', 'parity-golden.json');
  writeFileSync(path, text + '\n');
  const bad = traces.filter((t) => t.harness_error);
  console.log(`wrote n8n/parity-golden.json: ${traces.length} cases, harness errors: ${bad.length}`);
  for (const b of bad) console.log(`  ${b.id}: ${b.harness_error}`);
}
