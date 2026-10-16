#!/usr/bin/env node
/* Deployed-lane reachability probe (added 2026-10-16 after round 22).

   The deployed primary, nvidia/nemotron-3-ultra-550b-a55b, answers HTTP 404 "Function id … is not
   found" on every chat completion while STILL being listed in GET /v1/models — and OpenRouter's
   :free mirror relays the identical function id. The decision recorded in docs/free-model-research.md
   (round 22) is to leave the lane config alone and find out whether NVIDIA restores it. This is the
   "finding out", one tiny request per target, so nobody has to re-run a 48-row benchmark to learn
   the model is still dead.

   It is a reachability check, NOT a benchmark: it never scores a text, never counts toward a capture,
   and never writes to the database. Keys come from .env and are never printed.

   usage: node tools/reachability-probe.js [--quiet] [--mirror]
     exit 0  every probed target answered 2xx
     exit 1  at least one target failed (codes are in the log line)
   log: tools/reachability-log.jsonl, one JSON object per probe.
*/
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const LOG = path.join(__dirname, 'reachability-log.jsonl');
const QUIET = process.argv.includes('--quiet');
// A "stalled" line means *no answer within the client timeout*, not "dead": several NVIDIA ids serve
// but slowly (measured 2026-10-16: glm-5.3 269 s, nemotron-3.5-lightning 134 s, deepseek-v4.1-flash
// 301 s for a 16-token request, all HTTP 200). Raise this above the pool's own ~300 s ceiling to tell
// slow-but-alive apart from hanging.
const argNum = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? parseInt(process.argv[i + 1], 10) || d : d; };
const TIMEOUT_MS = argNum('timeout_ms', 60000);

function loadEnv() {
  const out = {};
  try {
    for (const line of fs.readFileSync(path.join(ROOT, '.env'), 'utf8').split(/\r?\n/)) {
      const i = line.indexOf('=');
      if (i > 0 && /^[A-Z0-9_]/.test(line)) out[line.slice(0, i)] = line.slice(i + 1).trim();
    }
  } catch (e) { /* no .env: nothing to probe, say so plainly */ }
  return out;
}

const env = loadEnv();
const PROMPT = [{ role: 'user', content: 'Reply with exactly: OK' }];

async function probeTarget({ name, base, key, model, timeoutMs }) {
  const rec = { ts: new Date().toISOString(), target: name, base, model };
  if (!base || !key || !model) {
    rec.status = 'skipped';
    rec.error_code = 'not_configured';
    return rec;
  }
  const t0 = Date.now();
  // A client timeout is the whole point: NVIDIA's gateway holds an unanswered request to ~300 s, and
  // a probe without one reports nothing at all, which looks identical to "still thinking".
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(base.replace(/\/$/, '') + '/chat/completions', {
      method: 'POST',
      headers: { authorization: 'Bearer ' + key, 'content-type': 'application/json' },
      signal: ac.signal,
      body: JSON.stringify({ model, messages: PROMPT, temperature: 0.1, max_tokens: 16 }),
    });
    const txt = await res.text();
    rec.http_status = res.status;
    if (res.ok) {
      rec.status = 'ok';
    } else {
      rec.status = 'http_' + res.status;
      // Keep the part that identifies the failure class, drop anything that looks like a credential.
      rec.detail = txt.slice(0, 220).replace(/nvapi-[A-Za-z0-9_-]+/g, 'nvapi-<masked>');
    }
  } catch (e) {
    const aborted = e && (e.name === 'AbortError' || String(e.message).includes('abort'));
    rec.status = aborted ? 'stalled' : 'exception';
    rec.error_code = aborted ? 'client_timeout_' + timeoutMs + 'ms' : String(e.message || e).slice(0, 120);
  } finally {
    clearTimeout(timer);
    rec.latency_ms = Date.now() - t0;
  }
  return rec;
}

function previousStatus(name) {
  try {
    const lines = fs.readFileSync(LOG, 'utf8').trim().split('\n').filter(Boolean);
    for (let i = lines.length - 1; i >= 0; i--) {
      let r; try { r = JSON.parse(lines[i]); } catch { continue; }
      if (r && r.target === name) return r.status;
    }
  } catch (e) { /* first run */ }
  return null;
}

(async () => {
  if (!Object.keys(env).length) {
    console.log('no .env next to the repo — nothing to probe (this script reads PRIMARY_*/SECONDARY_* only)');
    process.exit(2);
  }
  const targets = [
    { name: 'deployed_primary', base: env.PRIMARY_BASE_URL, key: env.PRIMARY_API_KEY, model: env.PRIMARY_MODEL, timeoutMs: TIMEOUT_MS },
    { name: 'deployed_tiebreak', base: env.SECONDARY_BASE_URL, key: env.SECONDARY_API_KEY, model: env.SECONDARY_MODEL, timeoutMs: TIMEOUT_MS },
  ];
  if (process.argv.includes('--mirror')) {
    targets.push({
      name: 'primary_or_mirror',
      base: env.OPENROUTER_BASE_URL, key: env.OPENROUTER_API_KEY,
      model: (env.PRIMARY_MODEL || '') + ':free', timeoutMs: TIMEOUT_MS,
    });
  }

  const results = [];
  for (const t of targets) {
    const rec = await probeTarget(t);
    const was = previousStatus(t.name);
    rec.changed = was !== null && was !== rec.status;
    if (rec.changed) rec.transition = was + ' -> ' + rec.status;
    results.push(rec);
    fs.appendFileSync(LOG, JSON.stringify(rec) + '\n');
    if (rec.status === 'ok' && was && was !== 'ok') {
      console.log('RESTORED  ' + t.name + '  ' + t.model + '  ' + was + ' -> ok (' + rec.latency_ms + 'ms)');
    } else if (!QUIET) {
      console.log(String(rec.status).padEnd(10) + String(rec.latency_ms + 'ms').padEnd(10) +
        t.name.padEnd(18) + t.model + (rec.detail ? '  ' + rec.detail.replace(/\s+/g, ' ').slice(0, 90) : ''));
    }
  }

  if (!QUIET) console.log('\nlogged -> ' + LOG);
  process.exit(results.some((r) => r.status !== 'ok' && r.status !== 'skipped') ? 1 : 0);
})();
