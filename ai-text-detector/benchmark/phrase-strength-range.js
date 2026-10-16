#!/usr/bin/env node
/**
 * phrase-strength-range.js — measure the phrase-strength domain the lanes actually emit, then
 * print the shipped colour ramp against it.
 *
 * The UI paints each quoted phrase with `evidenceColor(direction, strength)` from
 * `frontend/src/app/ui/result-card/evidence-color.ts`. That ramp is scaled on the *observed*
 * strength domain, not on 0..1, so this script is the source of truth for the two constants
 * STRENGTH_FLOOR / STRENGTH_CEIL and for the quantiles the hue stops sit on. Run it after any
 * new capture to check the ramp still spans the data; if a lane ever emits 0.05 or a second
 * decimal place, the stops move.
 *
 *   node benchmark/phrase-strength-range.js                        # default phrase-era corpus
 *   node benchmark/phrase-strength-range.js results-x.jsonl ...     # an explicit file set
 *
 * Only rows that carry a non-empty `phrases` array count: a salvaged or truncated answer has
 * its evidence stripped by the parser, so it contributes quotes to nothing (the script asserts
 * that rather than assuming it).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const Module = require('module');

const BENCH = __dirname;
const ROOT = path.resolve(BENCH, '..');

/** The corpus the ramp was fitted on: the uncapped fit capture, the uncapped disjoint holdout,
    and the capped control, all on `kiro/claude-sonnet-4.5` — the deployed primary. */
const DEFAULT_FILES = [
  'results-nocap-clean-omkiro-sonnet-4-5.jsonl',
  'results-nocap-holdout-omkiro-sonnet-4-5.jsonl',
  'results-capclean-omkiro-sonnet-4-5.jsonl',
];

const rgba = (css) => {
  const m = css.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
  if (!m) throw new Error('not an rgba colour: ' + css);
  return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
};

/** Load the *shipped* ramp rather than reimplementing it: esbuild transpiles the TS module in
    memory, then it is evaluated as CommonJS. The module has no imports, so no bundling is needed
    and the numbers printed below are the ones the browser runs. */
function loadShippedRamp() {
  const src = path.join(ROOT, 'frontend', 'src', 'app', 'ui', 'result-card', 'evidence-color.ts');
  if (!fs.existsSync(src)) throw new Error('missing ' + src);
  const { transformSync } = require(path.join(ROOT, 'frontend', 'node_modules', 'esbuild'));
  const js = transformSync(fs.readFileSync(src, 'utf8'), {
    loader: 'ts',
    format: 'cjs',
    platform: 'node',
    target: 'node20',
  }).code;
  const m = new Module(src, null);
  m.filename = src;
  m.paths = Module._nodeModulePaths(path.dirname(src));
  m._compile(js, src);
  return m.exports;
}

/** The ramp this replaces, reproduced so the comparison is arithmetic and not adjectives:
    strength was scaled on 0..1, hue ran yellow -> orange -> red over two segments, and the whole
    signal was carried by alpha. Superseded by round 17 of docs/free-model-research.md. */
const LEGACY = {
  stops: [
    [0.0, [232, 184, 78]],
    [0.5, [232, 122, 48]],
    [1.0, [255, 72, 62]],
  ],
  color(direction, s) {
    const x = Math.min(1, Math.max(0, Number.isFinite(s) ? s : 0));
    if (direction === 'HUMAN') return `rgba(71, 201, 138, ${(0.16 + 0.46 * x).toFixed(3)})`;
    let i = x <= this.stops[1][0] ? 0 : 1;
    const [t0, c0] = this.stops[i];
    const [t1, c1] = this.stops[i + 1];
    const f = (x - t0) / (t1 - t0);
    const ch = (a, b) => Math.round(a + (b - a) * f);
    return `rgba(${ch(c0[0], c1[0])}, ${ch(c0[1], c1[1])}, ${ch(c0[2], c1[2])}, ${(0.3 + 0.42 * x).toFixed(3)})`;
  },
};

const q = (sorted, p) => {
  if (sorted.length === 0) return NaN;
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  return lo === hi ? sorted[lo] : sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
};

const fmt = (n, d = 2) => (Number.isFinite(n) ? n.toFixed(d) : 'n/a');

function main() {
  const files = process.argv.slice(2).filter((a) => !a.startsWith('-'));
  const list = files.length ? files : DEFAULT_FILES;
  const shipped = loadShippedRamp();

  const byDir = { AI: [], HUMAN: [] };
  const perFile = [];
  let rowsWithPhrases = 0;
  let strippedWithPhrases = 0;

  for (const f of list) {
    const p = path.isAbsolute(f) ? f : path.join(BENCH, f);
    if (!fs.existsSync(p)) {
      console.error(`missing capture: ${p}`);
      process.exit(2);
    }
    let quotes = 0;
    let texts = 0;
    for (const line of fs.readFileSync(p, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      let row;
      try {
        row = JSON.parse(line);
      } catch {
        continue;
      }
      const ph = Array.isArray(row.phrases) ? row.phrases : [];
      if (!ph.length) continue;
      if (row.salvaged || row.truncated) {
        strippedWithPhrases++;
        continue; // evidence is stripped for these; never let one set the range
      }
      rowsWithPhrases++;
      texts++;
      for (const item of ph) {
        const s = Number(item && item.strength);
        const d = item && item.direction;
        if (!Number.isFinite(s) || !byDir[d]) continue;
        byDir[d].push(s);
        quotes++;
      }
    }
    perFile.push({ file: path.basename(p), texts, quotes });
  }

  const all = [...byDir.AI, ...byDir.HUMAN].sort((a, b) => a - b);
  const ai = byDir.AI.slice().sort((a, b) => a - b);
  const hum = byDir.HUMAN.slice().sort((a, b) => a - b);

  console.log('# Phrase-strength evidence range');
  console.log(`corpus: ${list.length} capture file(s), ${rowsWithPhrases} answered texts carrying quotes`);
  for (const f of perFile) console.log(`  ${f.file}: ${f.texts} texts, ${f.quotes} quotes`);
  console.log(`stripped (salvaged/truncated) rows skipped: ${strippedWithPhrases}`);
  console.log(`quotes: ${all.length} total = ${ai.length} AI + ${hum.length} HUMAN`);
  console.log('');
  console.log('| set   |  min |   p10 |   p25 |   p50 |   p75 |   p90 |   p95 |  max | distinct |');
  console.log('| ----- | ---- | ----- | ----- | ----- | ----- | ----- | ----- | ---- | -------- |');
  for (const [name, arr] of [['ALL', all], ['AI', ai], ['HUMAN', hum]]) {
    const distinct = new Set(arr.map((x) => x.toFixed(2))).size;
    console.log(
      `| ${name.padEnd(5)} | ${fmt(arr[0])} | ${fmt(q(arr, 0.10))} | ${fmt(q(arr, 0.25))} | ` +
        `${fmt(q(arr, 0.5))} | ${fmt(q(arr, 0.75))} | ${fmt(q(arr, 0.9))} | ${fmt(q(arr, 0.95))} | ` +
        `${fmt(arr[arr.length - 1])} | ${String(distinct).padEnd(8)} |`
    );
  }

  // Where each observed strength value lands, so a stop placement can be argued from counts.
  const hist = new Map();
  for (const s of ai) hist.set(s.toFixed(2), (hist.get(s.toFixed(2)) || 0) + 1);
  const hhist = new Map();
  for (const s of hum) hhist.set(s.toFixed(2), (hhist.get(s.toFixed(2)) || 0) + 1);
  console.log('');
  console.log('AI strength histogram:  ' + [...hist.entries()].sort().map(([v, n]) => `${v}x${n}`).join(' '));
  console.log('HUMAN strength histogram: ' + [...hhist.entries()].sort().map(([v, n]) => `${v}x${n}`).join(' '));

  // Every distinct strength value some lane actually emitted — the set a reader has to tell apart.
  const observed = [...new Set(ai.map((x) => Number(x.toFixed(2))))].sort((a, b) => a - b);
  console.log(`distinct observed AI strengths (${observed.length}): ${observed.join(' ')}`);

  console.log('');
  console.log(`shipped ramp: floor ${shipped.STRENGTH_FLOOR}, ceil ${shipped.STRENGTH_CEIL}`);
  console.log('');
  console.log('| strength | t shipped | shipped AI rgba        | a     | legacy AI rgba       | a     |');
  console.log('| -------- | --------- | ---------------------- | ----- | ------------------ | ----- |');
  const probes = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.71, 0.75, 0.8, 0.85, 0.88, 0.9, 0.95];
  for (const s of probes) {
    const t = shipped.normStrength(s);
    const a = rgba(shipped.evidenceColor('AI', s));
    const l = rgba(LEGACY.color('AI', s));
    console.log(
      `| ${fmt(s)} | ${fmt(t, 3)} | rgba(${a.r}, ${a.g}, ${a.b})       | ${fmt(a.a, 3)} | rgba(${l.r}, ${l.g}, ${l.b})       | ${fmt(l.a, 3)} |`
    );
  }

  // The claim the ramp exists to satisfy: neighbouring evidence must be distinguishable.
  const greenOf = (fn) => observed.map((s) => rgba(fn(s)).g);
  const shippedG = greenOf((s) => shipped.evidenceColor('AI', s));
  const legacyG = greenOf((s) => LEGACY.color('AI', s));
  const spreadOf = (g) => Math.max(...g) - Math.min(...g);
  console.log('');
  console.log('| measure (over the observed AI strengths)          | shipped | legacy |');
  console.log('| ------------------------------------------------- | ------- | ------ |');
  console.log(`| number of observed values                          | ${observed.length} | ${observed.length} |`);
  console.log(`| green-channel spread across the range (units)      | ${Math.round(spreadOf(shippedG))} | ${Math.round(spreadOf(legacyG))} |`);
  const shippedAlphas = observed.map((s) => rgba(shipped.evidenceColor('AI', s)).a);
  const legacyAlphas = observed.map((s) => rgba(LEGACY.color('AI', s)).a);
  console.log(`| alpha spread across the range (0..1)               | ${(Math.max(...shippedAlphas) - Math.min(...shippedAlphas)).toFixed(3)} | ${(Math.max(...legacyAlphas) - Math.min(...legacyAlphas)).toFixed(3)} |`);
  console.log(`| alpha ceiling                                      | ${Math.max(...shippedAlphas).toFixed(3)} | ${Math.max(...legacyAlphas).toFixed(3)} |`);
  const distinctShipped = new Set(observed.map((s) => shipped.evidenceColor('AI', s))).size;
  const distinctLegacy = new Set(observed.map((s) => LEGACY.color('AI', s))).size;
  console.log(`| distinct colours produced for ${String(observed.length).padEnd(2)} observed values | ${distinctShipped} | ${distinctLegacy} |`);
  const p25 = q(ai, 0.25);
  const p90 = q(ai, 0.9);
  const dgShipped = Math.round(rgba(shipped.evidenceColor('AI', p25)).g - rgba(shipped.evidenceColor('AI', p90)).g);
  const dgLegacy = Math.round(rgba(LEGACY.color('AI', p25)).g - rgba(LEGACY.color('AI', p90)).g);
  const daShipped = (rgba(shipped.evidenceColor('AI', p90)).a - rgba(shipped.evidenceColor('AI', p25)).a).toFixed(3);
  const daLegacy = (rgba(LEGACY.color('AI', p90)).a - rgba(LEGACY.color('AI', p25)).a).toFixed(3);
  console.log(`| green units lost from AI p25 (${fmt(p25)}) to p90 (${fmt(p90)})  | ${dgShipped} | ${dgLegacy} |`);
  console.log(`| alpha gained over that same interval             | ${daShipped} | ${daLegacy} |`);
  const haLo = rgba(shipped.evidenceColor('HUMAN', Math.min(...hum))).a;
  const haHi = rgba(shipped.evidenceColor('HUMAN', Math.max(...hum))).a;
  const hAlphaLegacy = (0.16 + 0.46 * Math.min(...hum)).toFixed(3);
  console.log(`| HUMAN alpha, weakest quote (${fmt(Math.min(...hum))}) -> strongest (${fmt(Math.max(...hum))}) | ${haLo.toFixed(3)} -> ${haHi.toFixed(3)} (+${(haHi - haLo).toFixed(3)}) | ${hAlphaLegacy} -> 0.598 (+${(0.598 - Number(hAlphaLegacy)).toFixed(3)}) |`);
  console.log('');
  console.log('Note: no WCAG contrast ratio is reported. The tint is painted over a translucent glass');
  console.log('panel, so there is no opaque backdrop to composite against and any ratio would be invented.');
}

main();
