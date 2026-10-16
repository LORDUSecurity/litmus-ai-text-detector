/*
 * Long AI-generated stress corpus for the hard test (task 94, AI side).
 *
 * Generates ~24 long articles (target 3000-6000 chars) from NON-JUDGE models only, so
 * a model never scores text written by itself. Provenance follows the corpus-freshai
 * pattern: every row records the exact prompt, model, params, gateway and timestamp,
 * with dataset='self-generated'. Rows truncated by max_tokens (finish_reason='length')
 * are retried once, then flagged; rows under --min_chars are flagged short and are NOT
 * meant to enter the merged corpus.
 *
 *   node gen-stress-ai.js [--models a,b,c] [--prompts 8] [--out corpus-stress-ai.json]
 *                         [--base_env PRIMARY_BASE_URL] [--key_env PRIMARY_API_KEY]
 *                         [--min_chars 3000] [--max_tokens 4096] [--temperature 0.7]
 *                         [--top_p 0.9] [--timeout 300] [--tries 2] [--wait_ms 5000]
 *                         [--resume]
 *
 * Progress rows append to results-stressgen.jsonl as they complete; the JSON corpus is
 * (re)written at the end. --resume skips (model, prompt) pairs already in the jsonl.
 *
 * NOTE on pool etiquette: this shares the NVIDIA pool with any running capture. Queue
 * it behind the kimi long capture rather than stacking slow-pool traffic.
 */
const fs = require('fs');
const path = require('path');

function loadEnv(p) {
  const env = {};
  for (const line of fs.readFileSync(p, 'utf8').split(/\r?\n/)) {
    if (line.trim().startsWith('#')) continue;
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return env;
}
const ENV = loadEnv(path.join(__dirname, '..', '.env'));

const argv = process.argv.slice(2);
const has = (n) => argv.includes('--' + n);
function arg(name, dflt) {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const KNOWN = ['models', 'prompts', 'out', 'jsonl', 'base_env', 'key_env', 'base_url', 'min_chars',
  'max_tokens', 'temperature', 'top_p', 'timeout', 'tries', 'wait_ms'];
for (const f of argv.filter((a) => a.startsWith('--')).map((a) => a.slice(2))) {
  if (f !== 'resume' && !KNOWN.includes(f)) {
    console.error('unknown flag --' + f + ' (known: ' + KNOWN.join(', ') + ', resume)');
    process.exit(2);
  }
}

const MODELS = arg('models', 'google/gemma-4-31b-it,deepseek-ai/deepseek-v4.1-flash,nvidia/nemotron-3.5-lightning-30b-a3b').split(',');
const N_PROMPTS = parseInt(arg('prompts', '8'), 10);
const OUT = path.join(__dirname, arg('out', 'corpus-stress-ai.json'));
const JSONL = path.join(__dirname, arg('jsonl', 'results-stressgen.jsonl'));
const BASE_URL = arg('base_url', ENV[arg('base_env', 'PRIMARY_BASE_URL')]);
const KEYS = arg('key_env', 'PRIMARY_API_KEY').split(',').map((n) => ENV[n.trim()]).filter(Boolean);
if (!BASE_URL) { console.error('base url missing from .env'); process.exit(1); }
if (!KEYS.length) { console.error('no API key in .env'); process.exit(1); }
const MIN_CHARS = parseInt(arg('min_chars', '3000'), 10);
const MAX_TOKENS = parseInt(arg('max_tokens', '4096'), 10);
const TEMP = parseFloat(arg('temperature', '0.7'));
const TOP_P = parseFloat(arg('top_p', '0.9'));
const TIMEOUT_S = parseInt(arg('timeout', '300'), 10);
const TRIES = parseInt(arg('tries', '2'), 10);
const WAIT_MS = parseInt(arg('wait_ms', '5000'), 10);

// Varied long-form assignments. The same 8 go to every model (paired prompts => a
// like-for-like comparison across generators); length is demanded in words AND chars
// because models track word counts better and the band is defined in chars.
const ALL_PROMPTS = [
  'Write a 700-word (about 4,500-character) article for an educated general audience explaining how DNS resolution actually works, from typing a name in a browser to receiving the first byte. Cover resolvers, root servers, TLD servers, caching, TTLs, and what happens during an outage. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) historical essay about the global spread of coffee from Ethiopia through the Ottoman coffeehouses to European Enlightenment cafes, focusing on how the drink itself changed public discussion and commerce. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) essay about why cities flood more every decade, covering impermeable surfaces, storm sewer design standards, subsidence, sea level, and the politics of rebuilding versus relocating. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) popular-science article on how the human balance system works — the inner ear, vision, proprioception — and why some people get motion sickness while pilots adapt. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) article examining how standardized tests changed school systems in several countries, including teaching to the test, curriculum narrowing, and at least one country that deliberately reduced testing. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) essay for a trade blog about maintaining century-old cast-iron water mains, covering tuberculation, lining methods, lead service lines, why municipalities deferred this work, and how crews replace a main under a live street. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) article about the economics and ecology of the global tuna trade, from purse seiners and Fish Aggregating Devices to the sushi counter, covering quotas, bycatch, and why bluefin became so expensive. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) essay explaining how vaccines train the immune system, covering innate versus adaptive responses, memory B and T cells, adjuvants, why some vaccines need boosters, and how cold chain logistics shape what the world can vaccinate against. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) article on the history of the shipping container and how one steel box rewrote global trade routes, port cities, and labor unions. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) essay about why deep-sea cables, not satellites, still carry almost all intercontinental internet traffic, covering laying ships, repeaters, landing stations, repair ships, and the geopolitics of routes. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) popular-science article about sleep cycles: what REM and deep sleep each do, how adenosine and circadian clocks interact, and why shift work is hard on the body. Plain flowing prose, no bullet lists.',
  'Write a 700-word (about 4,500-character) article about how credit scores came to dominate everyday life, covering the data industry, score inflation, what the models actually measure, and countries that use different approaches. Plain flowing prose, no bullet lists.',
];
const PROMPTS = ALL_PROMPTS.slice(0, N_PROMPTS);

function callGateway(key, model, prompt) {
  const body = {
    model,
    messages: [{ role: 'user', content: prompt }],
    max_tokens: MAX_TOKENS,
    temperature: TEMP,
    top_p: TOP_P,
    stream: false,
  };
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT_S * 1000);
  return fetch(BASE_URL + '/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + key },
    body: JSON.stringify(body),
    signal: ctrl.signal,
  }).then(async (res) => {
    clearTimeout(t);
    const text = await res.text();
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + text.slice(0, 200));
    const j = JSON.parse(text);
    const choice = (j.choices && j.choices[0]) || null;
    if (!choice || !choice.message) throw new Error('empty choices');
    const content = choice.message.content;
    if (content == null) {
      // reasoning models (nemotron-3.5-lightning) leave content null until their
      // thinking budget is spent; a reasoning trace is not a representative article.
      const rc = choice.message.reasoning_content;
      throw new Error('no_content (reasoning' + (rc ? '-only, ' + rc.length + ' chars' : ' absent') +
        ', finish=' + (choice.finish_reason || '?') + ') — raise max_tokens or drop the model');
    }
    return { content: content.trim(), finish: choice.finish_reason || null };
  }).finally(() => clearTimeout(t));
}

async function main() {
  const done = new Set();
  const rows = [];
  if (fs.existsSync(JSONL)) {
    for (const line of fs.readFileSync(JSONL, 'utf8').split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line);
        // only SUCCESSFUL rows count as done — error rows must be retried on resume.
        if (!r.error) done.add(r.source_model + '||' + r.prompt_index);
        rows.push(r);
      } catch (e) { /* skip torn line */ }
    }
    console.log('resume: ' + rows.length + ' existing rows in ' + path.basename(JSONL));
  }

  console.log('gen-stress-ai: models=' + MODELS.join(', ') + ' prompts=' + PROMPTS.length +
    ' max_tokens=' + MAX_TOKENS + ' temp=' + TEMP + ' top_p=' + TOP_P +
    ' timeout_s=' + TIMEOUT_S + ' tries=' + TRIES + ' base_url=' + BASE_URL);

  let key_i = 0;
  for (let pi = 0; pi < PROMPTS.length; pi++) {
    for (const model of MODELS) {
      if (done.has(model + '||' + pi)) { console.log(`skip ${model} #${pi} (already captured)`); continue; }
      let row = null;
      for (let attempt = 1; attempt <= TRIES && !row; attempt++) {
        const key = KEYS[(key_i++) % KEYS.length];
        const started = Date.now();
        try {
          const r = await callGateway(key, model, PROMPTS[pi]);
          const ms = Date.now() - started;
          const short = r.content.length < MIN_CHARS;
          row = {
            source_model: model, prompt_index: pi,
            text: r.content, chars: r.content.length,
            finish_reason: r.finish, truncated: r.finish === 'length', short,
            latency_ms: ms,
            error: r.finish === 'length' ? 'truncated_by_max_tokens' : (short ? 'too_short' : null),
          };
          console.log(`${model} #${pi}: ${r.content.length} chars in ${(ms / 1000).toFixed(1)}s` +
            (r.finish === 'length' ? ' TRUNCATED' : '') + (short ? ' SHORT' : ''));
        } catch (e) {
          const ms = Date.now() - started;
          console.log(`${model} #${pi} attempt ${attempt} FAILED in ${(ms / 1000).toFixed(1)}s: ${e.message.slice(0, 160)}`);
          if (attempt === TRIES) {
            row = {
              source_model: model, prompt_index: pi,
              text: '', chars: 0, finish_reason: null, truncated: false, short: true,
              latency_ms: ms, error: 'fetch_failed: ' + e.message.slice(0, 200),
            };
          } else {
            await new Promise((res) => setTimeout(res, WAIT_MS));
          }
        }
      }
      fs.appendFileSync(JSONL, JSON.stringify(row) + '\n');
      rows.push(row); // the final assembly reads `rows` — without this push a fresh run writes 0 rows
      await new Promise((res) => setTimeout(res, WAIT_MS));
    }
  }

  // Assemble the corpus file in the freshai row shape, ids ordered prompt-first.
  const good = rows.filter((r) => r.text && !r.truncated && !r.short);
  const bad = rows.length - good.length;
  const corpus = good
    .sort((a, b) => a.prompt_index - b.prompt_index || a.source_model.localeCompare(b.source_model))
    .map((r, i) => ({
      id: 'stress-ai-' + String(i).padStart(3, '0'),
      text: r.text,
      label: 'AI',
      config: { model: r.source_model, base_url: BASE_URL, temperature: TEMP, top_p: TOP_P, max_tokens: MAX_TOKENS },
      question: PROMPTS[r.prompt_index],
      dataset: 'self-generated',
      source: 'nvidia ' + r.source_model,
      fetched_at: new Date().toISOString(),
      chars: r.chars,
      latency_ms: r.latency_ms,
    }));
  fs.writeFileSync(OUT, JSON.stringify(corpus, null, 2));
  const lens = corpus.map((c) => c.chars);
  console.log(`\nwrote ${corpus.length} rows to ${path.basename(OUT)}` +
    (lens.length ? ` (min=${Math.min(...lens)} max=${Math.max(...lens)})` : '') +
    `; ${bad} row(s) dropped for truncation/shortness/errors out of ${rows.length}.`);
  if (corpus.length < 20) console.log('WARNING: fewer than 20 usable rows — consider a resume pass or swapping generators.');
}

main().catch((e) => { console.error('FATAL: ' + e.message); process.exit(1); });
