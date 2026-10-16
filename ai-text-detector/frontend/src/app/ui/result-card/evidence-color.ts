/** The phrase-evidence colour ramp, kept free of Angular so it can be measured and
    reasoned about on its own: `benchmark/phrase-strength-range.js` imports this exact
    file to print the ramp against the strength distribution the lanes actually emit.
    `result-card.ts` re-exports it, so the component and its specs keep one import path. */

const clamp01 = (n: number): number => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);
const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Green for human evidence, deepening with strength; yellow -> orange -> red for AI evidence.
    Scaled on the range the models actually emit, not on 0..1: measured over the phrase-bearing
    captures the minimum strength is 0.30 and the maximum 0.95, and AI evidence clusters high
    (p25 0.71, p50 0.80, p90 0.88). Run `node benchmark/phrase-strength-range.js` to reprint
    those numbers from the captures on disk. A 0..1 ramp spends its bottom third on values that
    never occur and then packs nearly all real AI evidence into one shade of red — exactly the
    evidence a reader has to tell apart. */
export const STRENGTH_FLOOR = 0.3;
export const STRENGTH_CEIL = 0.95;

/** Hue stops placed on the measured quantiles of the AI distribution, so the colour moves fastest
    where the evidence actually sits (t 0.63 = p25, 0.77 = p50, 0.89 = p90). */
const AI_STOPS: ReadonlyArray<readonly [number, readonly [number, number, number]]> = [
  [0.0, [236, 200, 106]], // pale yellow — the weakest quote any lane ever emits
  [0.46, [233, 166, 70]],
  [0.63, [231, 133, 55]], // orange
  [0.77, [235, 104, 47]], // deep orange
  [0.89, [243, 76, 52]],
  [1.0, [248, 52, 46]], // red — the strongest observed evidence
];

/** Strength -> 0..1 across the observed domain, then clamped: below the floor everything is the
    palest tint available, above the ceiling the full red. Monotonic, so ordering never inverts. */
export function normStrength(strength: number): number {
  return clamp01((clamp01(strength) - STRENGTH_FLOOR) / (STRENGTH_CEIL - STRENGTH_FLOOR));
}

function rampHue(t: number): readonly [number, number, number] {
  let i = 0;
  while (i < AI_STOPS.length - 2 && t > AI_STOPS[i + 1][0]) i++;
  const [t0, c0] = AI_STOPS[i];
  const [t1, c1] = AI_STOPS[i + 1];
  const f = t1 === t0 ? 0 : (t - t0) / (t1 - t0);
  return [lerp(c0[0], c1[0], f), lerp(c0[1], c1[1], f), lerp(c0[2], c1[2], f)];
}

/** Returned as rgba() so the tint sits over the panel and the text stays legible in both themes:
    alpha tops out at 0.60, and the strength signal is carried by hue as well as by depth. */
export function evidenceColor(direction: 'AI' | 'HUMAN', strength: number): string {
  const t = normStrength(strength);
  if (direction === 'HUMAN') {
    const a = 0.14 + 0.42 * t; // faint -> solid green
    return `rgba(71, 201, 138, ${a.toFixed(3)})`;
  }
  const [r, g, b] = rampHue(t);
  const a = 0.26 + 0.34 * t;
  return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, ${a.toFixed(3)})`;
}
