#!/usr/bin/env node
/* End-to-end response-time benchmark through the REAL app path.

   For every model lane and every text in appbench-corpus.json it times two legs:
     detect   POST http://127.0.0.1:8787/detect            (what the browser waits for)
     webhook  POST http://localhost:5678/webhook/...       (same call minus the Flask backend)
   The gateway-only side is measured separately by run-benchmark.js on the same corpus
   (tags appgw-*), so  detect - webhook = Flask/key/tuning overhead  and
   webhook - gateway   = n8n workflow overhead.

   The corpus texts are latency probes taken from the verified HC3 corpora; nothing about
   accuracy is claimed here. Rows are sequential with a small gap so we never stack load
   on the shared pool on top of whatever capture is running.

   usage: node app-response-bench.js [--models a,b,c] [--gap_ms 2000] [--trials 1]
*/
const fs = require('fs');
const path = require('path');

const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? process.argv[i + 1] : d; };
const MODELS = arg('models', 'nvidia/nemotron-3-ultra-550b-a55b,z-ai/glm-5.3-flash,google/gemma-4-31b-it,deepseek-ai/deepseek-v4.1-flash').split(',');
const GAP_MS = parseInt(arg('gap_ms', '2000'), 10);
const TRIALS = parseInt(arg('trials', '1'), 10);
const BACKEND = 'http://127.0.0.1:8787';
const OUT = path.join(__dirname, 'results-appbench.jsonl');

// The texts that go through the normal single-lane measurement. The 'defer-ai' row is
// only sent with the secondary lane left at its default: if the primary defers, the
// tie-break second opinion runs and its wall time is the real gray-zone user experience.
const TEXTS = JSON.parse(fs.readFileSync(path.join(__dirname, 'appbench-corpus.json'), 'utf8'));

const TUNING = { request_timeout_ms: 260000 }; // slow-pool regime; workflow cap is 300000

async function post(url, body, headers) {
  const t0 = process.hrtime.bigint();
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify(body),
    });
    let json = null;
    try { json = await res.json(); } catch { /* non-JSON body */ }
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    return { ms, status: res.status, json };
  } catch (e) {
    return { ms: Number(process.hrtime.bigint() - t0) / 1e6, status: 0, json: null, transport_error: String(e && e.message || e) };
  }
}

function summarize(r) {
  if (!r.json) return {};
  return {
    classification: r.json.classification,
    method: r.json.method,
    ai_probability: r.json.ai_probability,
    confidence: r.json.confidence,
    error_code: r.json.error_code || null,
  };
}

// The stored detection carries the backend's own view of the round-trip (Flask->n8n->back),
// which separates client-side wall from server-side latency.
async function latestDetection(id) {
  try {
    const res = await fetch(`${BACKEND}/detections?limit=5`);
    const rows = await res.json();
    const hit = rows.find(x => x.id === id);
    return hit ? hit.latency_ms : null;
  } catch { return null; }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const out = fs.createWriteStream(OUT, { flags: 'a' });
  for (const model of MODELS) {
    for (const row of TEXTS) {
      for (let trial = 1; trial <= TRIALS; trial++) {
        const isDeferProbe = row.id === 'defer-ai';
        // detect leg: exactly what the Angular form sends (secondary OFF except on the
        // deferral probe, where the default tie-break lane stays on).
        const lane = isDeferProbe ? { primary: { model } } : { primary: { model }, secondary: { enabled: false } };
        const body = { text: row.text, document_id: `appbench_${model}_${row.id}_${trial}`, lane_config: lane, tuning: TUNING };
        const d = await post(`${BACKEND}/detect`, body, {});
        const backendMs = d.json && d.json.backend_detection_id ? await latestDetection(d.json.backend_detection_id) : null;
        const rec = {
          leg: 'detect', model, text: row.id, band: row.band, chars: row.text.length,
          trial, wall_ms: Math.round(d.ms), http: d.status,
          backend_ms: backendMs, ...summarize(d), transport_error: d.transport_error || null,
          ts: new Date().toISOString(),
        };
        out.write(JSON.stringify(rec) + '\n');
        console.log(`[detect ] ${model} ${row.id}${isDeferProbe ? '(tie-break armed)' : ''} -> ${rec.wall_ms}ms ${rec.classification || rec.error_code || ''}`);
        await sleep(GAP_MS);

        // webhook leg: same payload straight to n8n (the workflow's env key does the paying),
        // minus Flask's key lookup / tuning merge.
        const w = await post('http://localhost:5678/webhook/ai-detect-ai', body, {});
        const wrec = {
          leg: 'webhook', model, text: row.id, band: row.band, chars: row.text.length,
          trial, wall_ms: Math.round(w.ms), http: w.status, ...summarize(w), transport_error: w.transport_error || null,
          ts: new Date().toISOString(),
        };
        out.write(JSON.stringify(wrec) + '\n');
        console.log(`[webhook] ${model} ${row.id} -> ${wrec.wall_ms}ms ${wrec.classification || wrec.error_code || ''}`);
        await sleep(GAP_MS);
      }
    }
  }
  out.end();
  console.log('done ->', OUT);
})();
