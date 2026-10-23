/** The phrase-evidence colour ramp, kept free of Angular so it can be measured and reasoned
    about on its own: `benchmark/phrase-strength-range.js` imports this exact file to print the
    ramp against the strength distribution the lanes actually emit, and
    `tools/check_palette_contrast.py` reads the same numbers. `result-card.ts` re-exports it, so
    the component and its specs keep one import path.

    What this ramp owes the reader: quoted evidence has to be legible AND tell its strength apart
    by eye. The superseded version interpolated sRGB triplets between hand-picked stops and put
    the strength signal in an alpha over a translucent panel — so there was no definite colour
    under the text, no definite contrast ratio, and equal steps in strength moved by wildly
    unequal amounts (a 0.05 change near the crowded top of the observed range was worth a third
    of the same change at the bottom). This version:

      1. defines a path in OKLCH per direction and theme (hue = direction, position = strength),
      2. puts every sample back inside sRGB by reducing CHROMA until it fits, never by clamping
         channels — clamping two out-of-gamut neighbours to the same in-gamut colour produces
         identical hexes, which is the shallow failure in its purest form,
      3. resamples the fitted path by equal perceptual arc length in OKLab, so an equal step in
         strength costs an equal step in appearance across the whole range,
      4. returns an OPAQUE background plus its own ink, so legibility is a property of the ramp
         that a spec can assert, not a property of whatever panel is behind it.

    Strength is scaled on the domain the lanes actually emit — min 0.30, max 0.95 over the 575
    real quotes in `benchmark/phrase-strength-range.js` — not on 0..1, and AI evidence clusters
    high (p25 0.71, p50 0.80, p90 0.88). A 0..1 ramp would spend its bottom third on values that
    never occur and then pack all real evidence into one shade. */

const clamp01 = (n: number): number => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);
const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

export const STRENGTH_FLOOR = 0.3;
export const STRENGTH_CEIL = 0.95;

export type EvidenceTheme = 'dark' | 'light';

/** A plate to paint text on, and the ink that is legible on it. Both are solid hexes: an alpha
    would make the colour (and therefore the contrast) depend on the panel behind it. */
export interface EvidencePaint {
  background: string;
  ink: string;
}

/** Strength -> 0..1 across the observed domain, then clamped. Below the floor and above the
    ceiling the ramp saturates instead of inventing a shade no lane ever reported. */
export function normStrength(strength: number): number {
  return clamp01((clamp01(strength) - STRENGTH_FLOOR) / (STRENGTH_CEIL - STRENGTH_FLOOR));
}

/* ---------------- OKLab / OKLCH <-> sRGB (Bjott Ottosson, public-domain reference) ---------- */

const compand = (c: number): number =>
  c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;

/** OKLCH -> linear sRGB. */
function oklchToLinear(L: number, C: number, Hdeg: number): [number, number, number] {
  const h = (Hdeg * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894843775 * a - 1.2914855480 * b;
  const l = l_ * l_ * l_;
  const m = m_ * m_ * m_;
  const s = s_ * s_ * s_;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309697492 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
}

/** Eight-bit round-trip: the ramp is consumed by a browser that stores 8 bits per channel, so a
    colour is only "in gamut" if it survives quantisation, not merely if it is mathematically
    inside [0,1]. Returns the channels clamped to the byte range for drawing. */
function toBytes(L: number, C: number, H: number): [number, number, number] {
  const lin = oklchToLinear(L, C, H);
  return [
    Math.round(255 * clamp01(compand(lin[0]))),
    Math.round(255 * clamp01(compand(lin[1]))),
    Math.round(255 * clamp01(compand(lin[2]))),
  ];
}

function inGamut(L: number, C: number, H: number): boolean {
  const lin = oklchToLinear(L, C, H);
  // Half a byte of tolerance: the value is about to be rounded, and a channel 0.4 units below
  // zero still paints as black. Refusing it would shrink the ramp for nothing.
  return lin.every((v) => v >= -0.0025 && v <= 1.0025);
}

/** Pull an out-of-gamut OKLCH colour back by desaturating it, not by clipping a channel. */
function fitToGamut(L: number, C: number, H: number): [number, number, number] {
  if (C <= 0 || inGamut(L, C, H)) return toBytes(L, C, H);
  let lo = 0;
  let hi = C;
  for (let i = 0; i < 24; i++) {
    const mid = (lo + hi) / 2;
    if (inGamut(L, mid, H)) lo = mid;
    else hi = mid;
  }
  return toBytes(L, lo, H);
}

const hex = ([r, g, b]: [number, number, number]): string =>
  '#' + [r, g, b].map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, '0')).join('');

/* ---------------- the four paths, one per direction and theme ------------------------------ */

interface Path {
  L: readonly [number, number];
  C: readonly [number, number];
  H: readonly [number, number];
}

/** Endpoints measured against the panel each theme draws evidence on: the lightness never
    crosses the theme's own ground, and chroma rises with strength so the weak end is a pale
    plate and the strong end is saturated. Hue stays inside ONE family for the whole run —
    yellow for AI evidence, green for human — so direction is read from hue and strength from
    distance away from the ground, never from both at once.

    Why the AI branch no longer runs into red. Red was doing two jobs at once: it marked the top
    of the AI scale AND it is the palette's error colour (`--bad`), so the strongest evidence and
    "this run failed" were the same hue, and a reader could not tell a confident quote from a
    broken one. The scale is also not a heat bar: it is one axis with the reader's own page at the
    middle. A weak AI claim now washes out toward the ground — "the model barely claimed anything"
    looks like barely anything — and strengthens into a saturated gold; the human side leaves the
    same neutral and deepens into green. Yellow → pale → green is one continuous reading, and
    there is no red in it at any strength.

    Both branches share that surface as their start because a highlight which stays loud at strength
    0.30 shouts about the quotes the lanes are least sure of.

    Each set is the one that maximises perceptual travel subject to the obligations above, found
    by substituting candidates into this file and scoring the ramp the component actually indexes
    (`node benchmark/phrase-strength-range.js`: contrast >= 4.5 at every one of the 66 strengths
    the grid tests, per-0.05-step interval ratio >= 0.85, cumulative arc-length deviation <= 1.2
    dE, no repeated hex, distance from the floor strictly increasing, p25->p90 >= 7 dE). Two
    numbers here are load-bearing and were not obvious: the dark AI ceiling stops at L 0.50 rather
    than a bright lemon, because light ink on a lemon-yellow plate is 1.4:1 — in dark mode the
    strongest yellow the reader's text can sit on is a deep gold, and the punch has to come from
    chroma. The light AI ceiling stops at L 0.70 for the mirror reason: black ink needs the plate
    bright, which caps how dark a gold the light theme may paint. And 0.70 is where the arc-length
    budget runs out, not where contrast does — at the same chroma the deviation measures 1.00 dE at
    ceiling 0.70, 1.19 at 0.66, 1.39 at 0.64. A darker ceiling would buy real range by making equal
    strength steps look unequal, which is the exact defect this module was written to remove.

    How faint the weak end is allowed to get is also measured, not asserted. The dark floor already
    lands 5.9 dE off the panel it paints on — a tint, not a highlight — and it cannot go much closer to
    absolute black: dropping it from L 0.20 to 0.12 takes the human ramp's 0.05-step uniformity to
    0.810 and to 0.04 the ramp collapses to 0.605 with repeated hexes, because below L ~0.15 the 8-bit
    grid is coarser than one strength step. Near black, "fainter" and "tells 0.05 from 0.10" are
    mutually exclusive; light mode has the headroom (L 0.965 measures 5.2 dE off white) and spends it. */
const PATHS: Record<EvidenceTheme, Record<'AI' | 'HUMAN', Path>> = {
  dark: {
    AI: { L: [0.2, 0.5], C: [0.03, 0.118], H: [96, 88] },
    HUMAN: { L: [0.2, 0.49], C: [0.014, 0.1], H: [152, 166] },
  },
  light: {
    AI: { L: [0.965, 0.7], C: [0.038, 0.19], H: [96, 88] },
    HUMAN: { L: [0.978, 0.765], C: [0.011, 0.138], H: [152, 164] },
  },
};

/** The ink is the theme's own text colour, held constant across the whole ramp: a highlight that
    flips the reader's text colour mid-panel is more disorienting than the extra range is worth. */
const INK: Record<EvidenceTheme, string> = { dark: '#f2f2f2', light: '#0a0a0a' };

const SAMPLES = 512;

/** Turn one OKLCH path into the ramp the component indexes: SAMPLES+1 gamut-fitted colours,
    then read out at equal perceptual arc length. Computed once per (direction, theme) and cached.
    The readout takes the NEAREST fitted sample rather than a colour synthesised between two,
    because sRGB is not convex in OKLab: the chord between two in-gamut points can leave the
    gamut, and re-clamping it paints two neighbours as one identical hex — the shallow failure
    this whole module exists to avoid. At 512 samples the granularity is 1/512 of the arc, far
    under the 8-bit step the browser can even show. */
function buildRamp(p: Path): string[] {
  const fitted: [number, number, number][] = [];
  for (let i = 0; i <= SAMPLES; i++) {
    const t = i / SAMPLES;
    fitted.push(
      fitToGamut(
        lerp(p.L[0], p.L[1], t),
        lerp(p.C[0], p.C[1], t),
        lerp(p.H[0], p.H[1], t),
      ),
    );
  }
  const lab = fitted.map((f) => oklabBytes(...f));
  const cum: number[] = [0];
  for (let i = 1; i <= SAMPLES; i++) {
    const a = lab[i - 1];
    const b = lab[i];
    cum.push(cum[i - 1] + Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]));
  }
  const total = cum[SAMPLES];
  const out: string[] = [];
  let k = 0;
  for (let i = 0; i <= SAMPLES; i++) {
    const target = (i / SAMPLES) * total;
    while (k + 1 < cum.length && Math.abs(cum[k + 1] - target) <= Math.abs(cum[k] - target)) k++;
    out.push(hex(fitted[k]));
  }
  return out;
}

/* Linear sRGB -> OKLab: the space the arc length is measured in. */
const delinearize = (c: number): number => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));

function oklabBytes(r: number, g: number, b: number): number[] {
  const R = delinearize(r / 255);
  const G = delinearize(g / 255);
  const B = delinearize(b / 255);
  const l_ = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B);
  const m_ = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1078259299 * B);
  const s_ = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B);
  return [
    0.2104542553 * l_ + 0.793617785 * m_ - 0.0040753852 * s_,
    1.9779984951 * l_ - 2.4285922602 * m_ + 0.4505937099 * s_,
    0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757553 * s_,
  ];
}

const RAMPS: Record<EvidenceTheme, Record<'AI' | 'HUMAN', string[]>> = (() => {
  const make = (theme: EvidenceTheme) => ({
    AI: buildRamp(PATHS[theme].AI),
    HUMAN: buildRamp(PATHS[theme].HUMAN),
  });
  return { dark: make('dark'), light: make('light') };
})();

/** The plate a quoted phrase is drawn on, and the ink that stays legible on it.
    `strength` is the value the model reported for that phrase; anything outside the observed
    domain saturates rather than extrapolating. */
export function evidencePaint(
  direction: 'AI' | 'HUMAN',
  strength: number,
  theme: EvidenceTheme,
): EvidencePaint {
  const ramp = RAMPS[theme][direction];
  const t = normStrength(strength);
  const at = Math.round(t * (ramp.length - 1));
  return { background: ramp[at], ink: INK[theme] };
}
