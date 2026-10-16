#!/usr/bin/env node
// resume-lanes.js — "remember where we stopped on each model, continue when quota refreshes".
//
// Reads runs-manifest.log, takes the LAST params line per tag (a resume appends a new line,
// so the newest is the truth), and re-runs every lane whose recorded usable < rows with
// run-benchmark.js --resume. The harness keeps every already-ok row, calls only the missing
// ones, and merges in corpus order — so yesterday's 22/48 becomes today's fill-26, not a new
// 48-call burn against the daily free cap.
//
//   node resume-lanes.js --dry              print the plan, spend nothing (default)
//   node resume-lanes.js --go               execute the plan sequentially
//   node resume-lanes.js --only tagA,tagB   restrict (comma-separated)
//
// Key/env rules mirror sweep-openrouter.sh / sweep-nvidia.sh: base_url is mapped back to the
// *_BASE_URL env that produced it; OpenRouter always rotates all three keys (task #37), the
// diffusiongemma model id uses the SECONDARY pair, everything else on integrate/omniroute uses
// PRIMARY/OMNIROUTE. Keys are never read or printed here — only their env NAMES are passed.
'use strict';
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const BENCH_DIR = __dirname;
const MANIFEST = path.join(BENCH_DIR, 'runs-manifest.log');
const ENV_FILE = path.join(BENCH_DIR, '..', '.env');
const PARSER_FIX_CUTOFF = Date.UTC(2026, 9, 7, 0, 0, 0); // 2026-10-07 salvage-delimiter rule

function readEnv() {
  const out = {};
  for (const line of fs.readFileSync(ENV_FILE, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

function latestParamsByTag() {
  const map = new Map();
  for (const line of fs.readFileSync(MANIFEST, 'utf8').split(/\r?\n/)) {
    const i = line.indexOf('params: ');
    if (i < 0) continue;
    let p;
    try { p = JSON.parse(line.slice(i + 8)); } catch (e) { continue; }
    map.set(p.tag, p); // last wins
  }
  return map;
}

function envNamesFor(p, env) {
  const url = p.base_url || '';
  let baseEnv = null;
  for (const k of Object.keys(env)) {
    if (/^[A-Z0-9_]*BASE_URL$/.test(k) && env[k] === url) { baseEnv = k; break; }
  }
  if (!baseEnv) return null; // mock endpoint, retired base, etc. — not resumable from .env
  const prefix = baseEnv.replace(/_BASE_URL$/, '');
  if (prefix === 'OPENROUTER') return { baseEnv, keyEnv: 'OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3,OPENROUTER_API_KEY_4,OPENROUTER_API_KEY_5,OPENROUTER_API_KEY_6' };
  if (prefix === 'PRIMARY' && /diffusiongemma/.test(p.model || '')) return { baseEnv: 'SECONDARY_BASE_URL', keyEnv: 'SECONDARY_API_KEY' };
  if (prefix === 'PRIMARY') return { baseEnv: 'PRIMARY_BASE_URL', keyEnv: 'PRIMARY_API_KEY' };
  if (prefix === 'SECONDARY') return { baseEnv: 'SECONDARY_BASE_URL', keyEnv: 'SECONDARY_API_KEY' };
  if (prefix === 'OMNIROUTE') return { baseEnv: 'OMNIROUTE_BASE_URL', keyEnv: 'OMNIROUTE_API_KEY' };
  if (prefix === 'GEMINI') return { baseEnv: 'GEMINI_BASE_URL', keyEnv: 'GEMINI_API_KEY' };
  return null;
}

function buildPlan() {
  const env = readEnv();
  const plan = [];
  const skip = [];
  for (const [tag, p] of latestParamsByTag()) {
    if (/-gate$/.test(tag)) continue;                           // gate runs are deleted, not lanes
    if (/^probe-/.test(tag) && !(only && only.has(tag))) continue; // probes skipped unless named via --only
    if (!(p.usable < p.rows)) continue;                         // complete
    const out = 'results-' + tag + '.jsonl';
    if (!fs.existsSync(path.join(BENCH_DIR, p.corpus || ''))) { skip.push([tag, 'corpus ' + p.corpus + ' gone']); continue; }
    const names = envNamesFor(p, env);
    if (!names) { skip.push([tag, 'base_url ' + (p.base_url || '?') + ' not in .env']); continue; }
    const warn = [];
    if (fs.existsSync(path.join(BENCH_DIR, out))) {
      const mt = fs.statSync(path.join(BENCH_DIR, out)).mtimeMs;
      if (mt < PARSER_FIX_CUTOFF) warn.push('file older than 2026-10-07 parser fix — ok rows kept may carry pre-fix salvage');
    } else {
      warn.push('no results file — resume runs the full corpus');
    }
    plan.push({ tag, p, out, ...names, warn });
  }
  plan.sort((a, b) => (a.p.usable / a.p.rows) - (b.p.usable / b.p.rows)); // worst-yield lanes first
  return { plan, skip };
}

function argvFlags(p) {
  const f = ['--tag', p.tag, '--model', p.model, '--base_env', '', '--key_env', '',
    '--corpus', p.corpus || 'corpus.json', '--limit', String(p.rows),
    '--concurrency', String(p.concurrency || 1), '--temperature', String(p.temperature),
    '--prompt', p.prompt || 'direct', '--max_tokens', String(p.max_tokens),
    '--tries', String(p.tries || 4), '--wait_ms', String(p.wait_ms || 3000),
    '--timeout', String(p.timeout_s || 120), '--resume'];
  return f;
}

const args = process.argv.slice(2);
const GO = args.includes('--go');
const only = args.includes('--only') ? new Set(args[args.indexOf('--only') + 1].split(',')) : null;
const { plan, skip } = buildPlan();
const chosen = only ? plan.filter((x) => only.has(x.tag)) : plan;

console.log('resume-lanes — ' + chosen.length + ' incomplete lane(s)' + (GO ? ' [GO]' : ' [dry]') + '\n');
for (const x of chosen) {
  console.log(x.tag + '  ' + x.p.model + '  usable=' + x.p.usable + '/' + x.p.rows + '  missing=' + (x.p.rows - x.p.usable));
  for (const w of x.warn) console.log('  WARN: ' + w);
}
if (skip.length) { console.log('\nskipped (not resumable):'); for (const [t, why] of skip) console.log('  ' + t + ' — ' + why); }

if (!GO) { console.log('\nnothing spent; rerun with --go to execute, --only tag[,tag] to narrow.'); process.exit(0); }

for (const x of chosen) {
  const flags = argvFlags(x.p);
  flags[flags.indexOf('--base_env') + 1] = x.baseEnv;
  flags[flags.indexOf('--key_env') + 1] = x.keyEnv;
  console.log('\n===== resume ' + x.tag + ' =====');
  const r = spawnSync(process.execPath, ['run-benchmark.js', ...flags], { cwd: BENCH_DIR, stdio: 'inherit' });
  console.log('===== ' + x.tag + ' exit=' + r.status + ' =====');
  if (r.status !== 0) console.log('lane failed; progress it did make is kept for the next --go');
}
