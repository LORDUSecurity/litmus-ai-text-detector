#!/usr/bin/env node
/**
 * phrase-strength-range.js — measure the phrase-strength domain the lanes actually emit, then
 * print the shipped colour ramp against it.
 *
 * The UI paints each quoted phrase from `evidencePaint(direction, strength, theme)` in
 * `frontend/src/app/ui/evidence/evidence-color.ts`. That ramp is scaled on the *observed*
 * strength domain, not on 0..1, so this script is the source of truth for the two constants
 * STRENGTH_FLOOR / STRENGTH_CEIL. Run it after any new capture to check the ramp still spans the
 * data; if a lane ever emits 0.05 or a third decimal place, the endpoints move.
 *
 *   node benchmark/phrase-strength-range.js                        # default phrase-era corpus
 *   node benchmark/phrase-strength-range.js results-x.jsonl ...     # an explicit file set
 *
 * Only rows that carry a non-empty `phrases` array count: a salvaged or truncated answer has
 * its evidence stripped by the parser, so it contributes quotes to nothing (the script asserts
 * that rather than assuming it).
 *
 * The colour half of the output is a SECOND, independent implementation of sRGB -> OKLab and of
 * WCAG relative luminance. That is deliberate: if the shipped module's conversion were wrong, a
 * tool that imported its own maths could never say so. What is shared with production is the ramp
 * itself, loaded from the real file through esbuild.
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

/* ---------------- colour maths, written out again on purpose ---------------- */

const hexToBytes = (h) => {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const toLinear = (v) => {
  const c = v / 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
};
const relLum = ([r, g, b]) => 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
const wcag = (x, y) => {
  const a = relLum(x);
  const b = relLum(y);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
};
const toOklab = (bytes) => {
  const [r, g, b] = bytes.map(toLinear);
  const l_ = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m_ = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1078259299 * b);
  const s_ = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l_ + 0.793617785 * m_ - 0.0040753852 * s_,
    1.9779984951 * l_ - 2.4285922602 * m_ + 0.4505937099 * s_,
    0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757553 * s_,
  ];
};
/** OKLab distance x100, the unit this file reports every perceptual step in. */
const dE = (hexA, hexB) => {
  const a = toOklab(hexToBytes(hexA));
  const b = toOklab(hexToBytes(hexB));
  return 100 * Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
};
const hueOf = (h) => {
  const [, a, b] = toOklab(hexToBytes(h));
  const deg = (Math.atan2(b, a) * 180) / Math.PI;
  return deg < 0 ? deg + 360 : deg;
};

/** Composite an rgba() tint over the theme's ground, so the superseded alpha ramp can be measured
    on the same ruler as the opaque one. The ground it is composited on is today's absolute black /
    absolute white — the old translucent glass panel had no definite colour at all, which is the
    point of the row this feeds. */
const rgbaOver = (css, groundHex) => {
  const m = css.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
  if (!m) throw new Error('not an rgba colour: ' + css);
  const a = m[4] === undefined ? 1 : +m[4];
  const g = hexToBytes(groundHex);
  const out = [1, 2, 3].map((i) => Math.round(+m[i] * a + g[i - 1] * (1 - a)));
  return '#' + out.map((v) => v.toString(16).padStart(2, '0')).join('');
};

/** Load the *shipped* ramp rather than reimplementing it: esbuild transpiles the TS module in
    memory, then it is evaluated as CommonJS. The module has no imports, so no bundling is needed
    and the numbers printed below are the ones the browser runs. */
function loadShippedRamp() {
  const src = path.join(ROOT, 'frontend', 'src', 'app', 'ui', 'evidence', 'evidence-color.ts');
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

/** The strength grid the component can index: STRENGTH_FLOOR..STRENGTH_CEIL at two decimals, which
    is exactly the resolution the lanes emit. 66 points, every 5th one a 0.05 step. */
const GRID = Array.from({ length: 66 }, (_, i) => Number((0.3 + (i / 65) * 0.65).toFixed(2)));

/** Summarise one (theme, direction) ramp on the four properties the panel is held to. */
function rampMetrics(hexes, inkHex) {
  const cum = [0];
  for (let i = 1; i < hexes.length; i++) cum.push(cum[i - 1] + dE(hexes[i - 1], hexes[i]));
  const total = cum[cum.length - 1];
  const coarse = [];
  for (let k = 1; k * 5 < hexes.length; k++) coarse.push(dE(hexes[(k - 1) * 5], hexes[k * 5]));
  const meanStep = coarse.reduce((a, b) => a + b, 0) / coarse.length;
  let cumDev = 0;
  cum.forEach((c, i) => {
    cumDev = Math.max(cumDev, Math.abs(c - (i / (hexes.length - 1)) * total));
  });
  return {
    arc: total,
    uniformity: Math.min(...coarse) / meanStep,
    cumDev,
    minContrast: Math.min(...hexes.map((h) => wcag(hexToBytes(inkHex), hexToBytes(h)))),
    distinct: new Set(hexes).size,
    hue0: hueOf(hexes[0]),
    hue1: hueOf(hexes[hexes.length - 1]),
    first: hexes[0],
    mid: hexes[Math.floor(hexes.length / 2)],
    last: hexes[hexes.length - 1],
  };
}

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

  // Where each observed strength value lands, so an endpoint placement can be argued from counts.
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

  /* ---------------- what each ramp actually paints, per theme ---------------- */

  const GROUND = { dark: '#000000', light: '#ffffff' };
  const metrics = {};
  for (const theme of ['dark', 'light']) {
    metrics[theme] = {};
    for (const dir of ['AI', 'HUMAN']) {
      const paints = GRID.map((s) => shipped.evidencePaint(dir, s, theme));
      const inks = new Set(paints.map((p) => p.ink));
      if (inks.size !== 1) throw new Error(`${theme}/${dir}: the ramp changes ink mid-panel`);
      metrics[theme][dir] = { ...rampMetrics(paints.map((p) => p.background), [...inks][0]), inks };
    }
    const sep = [];
    for (let k = 0; k < GRID.length; k += 5) {
      const s = GRID[k];
      sep.push(
        dE(shipped.evidencePaint('HUMAN', s, theme).background, shipped.evidencePaint('AI', s, theme).background)
      );
    }
    metrics[theme].sep = Math.min(...sep);
  }

  console.log('');
  console.log('| theme | dir   | min contrast vs its ink | arc dE | 0.05-step uniformity | max arc-length deviation | own p25->p90 dE | distinct/66 | hue start->end | weakest / middle / strongest |');
  console.log('| ----- | ----- | ----------------------- | ------ | -------------------- | ------------------------ | --------------- | ----------- | -------------- | ---------------------------- |');
  const ownQuartiles = { AI: [q(ai, 0.25), q(ai, 0.9)], HUMAN: [q(hum, 0.25), q(hum, 0.9)] };
  const row = (cells) => '| ' + cells.map((c) => String(c).padEnd(2)).join(' | ') + ' |';
  for (const theme of ['dark', 'light']) {
    for (const dir of ['AI', 'HUMAN']) {
      const m = metrics[theme][dir];
      const [lo, hi] = ownQuartiles[dir];
      const p25to90 = dE(
        shipped.evidencePaint(dir, lo, theme).background,
        shipped.evidencePaint(dir, hi, theme).background
      );
      console.log(
        row([
          theme,
          dir,
          fmt(m.minContrast, 2),
          fmt(m.arc, 1),
          fmt(m.uniformity, 3),
          fmt(m.cumDev, 2),
          `${fmt(lo)}->${fmt(hi)} ${fmt(p25to90, 1)}`,
          m.distinct,
          `${fmt(m.hue0, 0)}->${fmt(m.hue1, 0)}`,
          `${m.first} / ${m.mid} / ${m.last}`,
        ])
      );
    }
  }
  console.log('');
  console.log(`human-vs-AI separation at matched strength, weakest over the grid: dark ${fmt(metrics.dark.sep, 1)} dE, light ${fmt(metrics.light.sep, 1)} dE`);

  /* ---------------- the same measurement on the superseded ramp ---------------- */

  const legacyRows = [];
  for (const theme of ['dark', 'light']) {
    for (const dir of ['AI', 'HUMAN']) {
      const hexes = GRID.map((s) => rgbaOver(LEGACY.color(dir, s), GROUND[theme]));
      // The old version had no ink of its own: it inherited whatever the panel's text colour was,
      // so contrast is measured against that theme's current ink and the answer is the range it spans.
      const ink = theme === 'dark' ? '#f2f2f2' : '#0a0a0a';
      const m = rampMetrics(hexes, ink);
      m.contrastRange = [
        Math.min(...hexes.map((h) => wcag(hexToBytes(ink), hexToBytes(h)))),
        Math.max(...hexes.map((h) => wcag(hexToBytes(ink), hexToBytes(h)))),
      ];
      legacyRows.push({ theme, dir, ...m });
    }
  }
  console.log('');
  console.log('Superseded alpha ramp, composited over today\'s ground (' + GROUND.dark + ' / ' + GROUND.light + ') for the same arithmetic:');
  console.log('| theme | dir   | contrast range vs panel ink | arc dE | 0.05-step uniformity | distinct/66 | hue start->end |');
  console.log('| ----- | ----- | --------------------------- | ------ | -------------------- | ----------- | -------------- |');
  for (const r of legacyRows) {
    console.log(
      row([
        r.theme,
        r.dir,
        `${fmt(r.contrastRange[0], 2)}-${fmt(r.contrastRange[1], 2)}`,
        fmt(r.arc, 1),
        fmt(r.uniformity, 3),
        r.distinct,
        `${fmt(r.hue0, 0)}->${fmt(r.hue1, 0)}`,
      ])
    );
  }

  console.log('');
  console.log('Note: the shipped ramp returns an OPAQUE plate and the ink that is legible on it, so every');
  console.log('contrast ratio above is a property of the ramp. The superseded version carried its strength');
  console.log('signal in an alpha over a translucent glass panel: no definite colour under the text, so no');
  console.log('definite ratio either — its range is the arithmetic of compositing it over a ground it never');
  console.log('actually specified.');
}

main();
