#!/usr/bin/env node
/*
 * Independent out-of-band human/AI check for the benchmark corpora, using GPTZero
 * (https://api.gptzero.me) as a detector that is NOT any lane in results-*.jsonl.
 *
 * Why this exists: the detector scores in this project are all LLMs, so "the human
 * class reads as human to four models" is partly circular -- they could share a bias.
 * GPTZero is a different engine, so its read of HC3's HUMAN texts is a real, independent
 * second opinion on whether the labels hold.
 *
 * CONFIRMED LIVE (2026-10-11) request/response shape:
 *   POST https://api.gptzero.me/v2/predict/text
 *   header: x-api-key: <key>   (no key -> 401 "Require login or API key";
 *                               bogus key -> 403 "API key has no owner")
 *   body:   {"document": {"text": "..."}}
 *   resp:   document_classification in {HUMAN_ONLY, AI_ONLY, MIXED} (+ HUMAN_MAJORITY /
 *           AI_MAJORITY in some versions); class_probabilities object; confidence_category.
 *
 * Secrets: the key is read from .env and NEVER printed -- only its length. Run with
 * --mock to exercise parsing + the confusion matrix with no network and no key.
 *
 * Usage:
 *   node benchmark/gptzero-verify.js --corpus corpus.json --tag gptzero-hc348
 *   node benchmark/gptzero-verify.js --corpus corpus-holdout.json --tag gptzero-holdout
 *   node benchmark/gptzero-verify.js --mock            # offline self-test, spends nothing
 */
const fs = require('fs');
const path = require('path');

function loadEnv(file) {
  const env = {};
  const txt = fs.readFileSync(file, 'utf8');
  for (const line of txt.split(/\r?\n/)) {
    const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return env;
}

function getArgs(argv) {
  const a = { corpus: 'corpus.json', tag: 'gptzero', limit: 0, tries: 3, delay_ms: 400, mock: false, out: null, url: 'https://api.gptzero.me/v2/predict/text' };
  for (let i = 2; i < argv.length; i++) {
    const t = argv[i];
    if (t === '--mock') a.mock = true;
    else if (t === '--corpus') a.corpus = argv[++i];
    else if (t === '--tag') a.tag = argv[++i];
    else if (t === '--limit') a.limit = parseInt(argv[++i], 10);
    else if (t === '--tries') a.tries = parseInt(argv[++i], 10);
    else if (t === '--delay_ms') a.delay_ms = parseInt(argv[++i], 10);
    else if (t === '--out') a.out = argv[++i];
    else if (t === '--url') a.url = argv[++i];
  }
  a.out = a.out || path.join(__dirname, 'results-' + a.tag + '.jsonl');
  return a;
}

function readCorpus(file) {
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, file), 'utf8'));
  const rows = Array.isArray(raw) ? raw : (raw.texts || raw.rows || raw.items || []);
  return rows.map(r => ({ id: r.id, label: (r.label || '').toUpperCase(), text: r.text }));
}

// Map a GPTZero classification string to a coarse side, keeping MIXED explicit.
function sideOfClassification(cls) {
  const c = String(cls || '').toUpperCase();
  if (c.includes('HUMAN')) return 'HUMAN';        // HUMAN_ONLY / HUMAN_MAJORITY
  if (c.includes('AI')) return 'AI';              // AI_ONLY / AI_MAJORITY
  if (c.includes('MIXED')) return 'MIXED';
  return 'UNKNOWN';
}

function extractPrediction(json) {
  // Defensive: capture the whole raw object; pull the label from whichever field is present.
  const cls = json.document_classification || json.detection_class ||
    (json.results && json.results[0] && (json.results[0].document_classification || json.results[0].classification)) || null;
  const probs = json.class_probabilities || (json.results && json.results[0] && json.results[0].class_probabilities) || null;
  return { cls, probs, side: sideOfClassification(cls), raw: json };
}

function mockResponse(label, i) {
  // Canned, schema-faithful replies so parsing + the matrix are testable offline.
  // Make one HUMAN row come back AI (index 0) to prove contamination detection fires.
  if (label === 'HUMAN' && i === 0) {
    return { document_classification: 'AI_ONLY', class_probabilities: { prob_of_ai: 0.9, prob_of_human: 0.05, prob_of_mixed: 0.05 }, confidence_category: 'high' };
  }
  if (label === 'HUMAN') {
    return { document_classification: 'HUMAN_ONLY', class_probabilities: { prob_of_ai: 0.02, prob_of_human: 0.96, prob_of_mixed: 0.02 }, confidence_category: 'high' };
  }
  // AI texts: GPTZero should say AI, but leave one MIXED to exercise the third bucket.
  if (label === 'AI' && i === 0) {
    return { document_classification: 'MIXED', class_probabilities: { prob_of_ai: 0.5, prob_of_human: 0.1, prob_of_mixed: 0.4 }, confidence_category: 'medium' };
  }
  return { document_classification: 'AI_ONLY', class_probabilities: { prob_of_ai: 0.95, prob_of_human: 0.02, prob_of_mixed: 0.03 }, confidence_category: 'high' };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function callGPTZero(url, key, text, tries) {
  let lastErr = null;
  for (let attempt = 1; attempt <= tries; attempt++) {
    const t0 = Date.now();
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-api-key': key },
        body: JSON.stringify({ document: { text } }),
      });
      const bodyText = await res.text();
      let json = null;
      try { json = JSON.parse(bodyText); } catch (_) {}
      if (res.status === 200 && json) return { ok: true, json, attempt, latency_ms: Date.now() - t0 };
      // 429/5xx are retryable; 401/403 are not (auth won't fix itself by retry).
      if (res.status === 429 || res.status >= 500) { lastErr = { code: 'http_' + res.status, detail: bodyText.slice(0, 300) }; await sleep(1500 * attempt); continue; }
      return { ok: false, code: 'http_' + res.status, detail: bodyText.slice(0, 300), attempt, latency_ms: Date.now() - t0 };
    } catch (e) {
      lastErr = { code: 'exception', detail: String(e.message || e) };
      await sleep(1200 * attempt);
    }
  }
  return Object.assign({ ok: false }, lastErr, { attempt: tries });
}

function buildMatrix(rows) {
  // rows: [{label, side}] where side is GPTZero's coarse verdict.
  const m = { HUMAN: { HUMAN: 0, AI: 0, MIXED: 0, UNKNOWN: 0, n: 0 }, AI: { HUMAN: 0, AI: 0, MIXED: 0, UNKNOWN: 0, n: 0 } };
  for (const r of rows) { if (m[r.label] && m[r.label][r.side] !== undefined) { m[r.label][r.side]++; m[r.label].n++; } }
  return m;
}

(async () => {
  const args = getArgs(process.argv);
  let key = null;
  if (!args.mock) {
    let env = {};
    try { env = loadEnv(path.join(__dirname, '..', '.env')); } catch (_) {}
    key = env.GPTZERO_API_KEY || process.env.GPTZERO_API_KEY || null;
    if (!key) {
      console.log('NO GPTZero key found (looked for GPTZERO_API_KEY in ../.env and in the environment).');
      console.log('Add one safely WITHOUT printing it, e.g. from your shell:');
      console.log("  printf '%s\\n' 'GPTZERO_API_KEY=<paste-key-here>' >> .env");
      console.log('Or run this file with --mock to test its parsing and matrix offline (spends nothing).');
      process.exit(1);
    }
    console.log('GPTZero key present (length ' + key.length + ', value not shown).');
  }

  const corpus = readCorpus(args.corpus);
  const work = args.limit ? corpus.slice(0, args.limit) : corpus;
  console.log((args.mock ? '[MOCK] ' : '') + 'corpus=' + args.corpus + ' texts=' + work.length + '  endpoint=' + args.url);

  const out = [];
  for (let i = 0; i < work.length; i++) {
    const r = work[i];
    const rec = { id: r.id, label: r.label, ok: false, class: null, side: null, probs: null, latency_ms: null, attempt: 0 };
    if (args.mock) {
      const pred = extractPrediction(mockResponse(r.label, out.filter(x => x.label === r.label).length));
      rec.ok = true; rec.class = pred.cls; rec.side = pred.side; rec.probs = pred.probs;
    } else {
      const res = await callGPTZero(args.url, key, r.text, args.tries);
      rec.attempt = res.attempt; rec.latency_ms = res.latency_ms;
      if (res.ok) { const pred = extractPrediction(res.json); rec.ok = true; rec.class = pred.cls; rec.side = pred.side; rec.probs = pred.probs; }
      else { rec.ok = false; rec.error_code = res.code; rec.error_detail = res.detail; }
      await sleep(args.delay_ms);
    }
    out.push(rec);
    process.stdout.write('  ' + r.id.padEnd(9) + ' label=' + r.label.padEnd(5) + ' gptzero=' + (rec.side || 'FAIL') + (rec.ok ? '' : ' (' + (rec.error_code || '?') + ')') + '\n');
  }

  if (!args.mock) {
    fs.writeFileSync(args.out, out.map(o => JSON.stringify(o)).join('\n') + '\n');
    console.log('wrote ' + args.out);
  }

  const scored = out.filter(o => o.ok);
  const m = buildMatrix(scored);
  console.log('\n================ GPTZero vs HC3 labels (' + scored.length + ' of ' + work.length + ' scored) ================');
  function line(name, cell) {
    console.log('  ' + name.padEnd(6) + ' n=' + String(cell.n).padEnd(3) +
      ' -> HUMAN ' + String(cell.HUMAN).padEnd(3) +
      ' AI ' + String(cell.AI).padEnd(3) +
      ' MIXED ' + String(cell.MIXED).padEnd(3) +
      ' UNKNOWN ' + cell.UNKNOWN);
  }
  line('HC3 HUMAN', m.HUMAN);
  line('HC3 AI', m.AI);
  console.log('');
  if (m.HUMAN.n) {
    console.log('  Contamination signal: HC3-HUMAN texts GPTZero flags AI     = ' + m.HUMAN.AI + '  (' + (100 * m.HUMAN.AI / m.HUMAN.n).toFixed(1) + '%)');
    console.log('  Contamination (incl MIXED, ambiguous):                    = ' + (m.HUMAN.AI + m.HUMAN.MIXED) + '  (' + (100 * (m.HUMAN.AI + m.HUMAN.MIXED) / m.HUMAN.n).toFixed(1) + '%)');
  }
  if (m.AI.n) {
    console.log('  GPTZero own error: HC3-AI texts it calls HUMAN            = ' + m.AI.HUMAN + '  (' + (100 * m.AI.HUMAN / m.AI.n).toFixed(1) + '%)');
    const strict = (m.HUMAN.HUMAN + m.AI.AI) / (m.HUMAN.n + m.AI.n || 1);
    console.log('  GPTZero strict 2-way accuracy (MIXED counted as miss)     = ' + strict.toFixed(3));
  }
  console.log('\nNote: this does not prove any individual label. It gives a second detector\'s read.');
  console.log('Human texts GPTZero flags AI are candidates to eyeball, not confirmed mislabels.');
  process.exitCode = 0;
})();
