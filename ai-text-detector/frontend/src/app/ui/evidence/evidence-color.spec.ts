import {
  evidencePaint,
  normStrength,
  STRENGTH_CEIL,
  STRENGTH_FLOOR,
  type EvidenceTheme,
} from './evidence-color';

/** The ramp's own contract, measured on the hexes the component is handed.
 *
    These helpers are a SECOND implementation of the colour maths, on purpose: the module under
    test converts OKLCH to sRGB, and a spec that reused that conversion would still pass if the
    conversion were wrong. Everything below is arithmetic on the painted value, which is also what
    the WCAG ratio and the perceptual distance are defined over.

    Why the ramp moved at all: the superseded version interpolated sRGB triplets between hand-picked
    stops and carried strength in an alpha. An alpha over an unknown panel is not a measurable colour
    (there was no opaque backdrop to score, which is why the old spec could only bound the alpha
    between 0.1 and 0.62), and equal steps in strength produced wildly unequal steps in appearance:
    measured in OKLab, a 0.05 change near the crowded top of the observed range was worth a third of
    the same change at the bottom, and the whole AI ramp moved 248-52-46 minus 236-200-106, which is
    one hue family read five ways. What replaces it is a path through OKLCH, gamut-fitted by reducing
    chroma instead of clamping channels, resampled by equal perceptual arc length, and composited to
    an OPAQUE colour so legibility is a property of the ramp rather than of the panel behind it. */
describe('evidencePaint', () => {
  const THEMES: EvidenceTheme[] = ['dark', 'light'];
  const HEX = /^#[0-9a-f]{6}$/;

  const parse = (hex: string): [number, number, number] => {
    if (!HEX.test(hex)) throw new Error('not a solid hex colour: ' + hex);
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  /** Both helpers below take 0..255 channels and normalise them once, at the top: WCAG's
      relative luminance and Ottosson's OKLab are both defined on 0..1 component values, and
      feeding them bytes inflates every ratio by orders of magnitude. */
  const lin = (v: number): number => {
    const c = v / 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  const relLum = ([r, g, b]: [number, number, number]): number =>
    0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const wcag = (x: [number, number, number], y: [number, number, number]): number => {
    const a = relLum(x);
    const b = relLum(y);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const oklab = ([r, g, b]: [number, number, number]): number[] => {
    const l = Math.cbrt(0.4122214708 * lin(r) + 0.5363325363 * lin(g) + 0.0514459929 * lin(b));
    const m = Math.cbrt(0.2119034982 * lin(r) + 0.6806995451 * lin(g) + 0.1078259299 * lin(b));
    const s = Math.cbrt(0.0883024619 * lin(r) + 0.2817188376 * lin(g) + 0.6299787005 * lin(b));
    return [
      0.2104542553 * l + 0.793617785 * m - 0.0040753852 * s,
      1.9779984951 * l - 2.4285922602 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.8086757553 * s,
    ];
  };
  /** OKLab distance scaled by 100. One unit is roughly the just-noticeable difference, so a
      separation of 2.3 is the conventional floor for "a person can tell these two apart". */
  const dE = (x: string, y: string): number => {
    const A = oklab(parse(x));
    const B = oklab(parse(y));
    return 100 * Math.hypot(A[0] - B[0], A[1] - B[1], A[2] - B[2]);
  };
  const hueOf = (hex: string): number => {
    const [, a, b] = oklab(parse(hex));
    const h = (Math.atan2(b, a) * 180) / Math.PI;
    return h < 0 ? h + 360 : h;
  };
  /** Every strength a lane can report, at the resolution models actually emit (two decimals). */
  const CENT = (i: number): number =>
    Number((STRENGTH_FLOOR + (i / 65) * (STRENGTH_CEIL - STRENGTH_FLOOR)).toFixed(2));
  /** `n+1` colours spread evenly across the WHOLE observed domain, snapped to the two decimals a
      lane can emit: n=65 is every value, n=13 is one every 0.05. */
  const sweep = (d: 'AI' | 'HUMAN', theme: EvidenceTheme, n = 65): string[] =>
    Array.from({ length: n + 1 }, (_, i) =>
      evidencePaint(d, CENT(Math.round((i * 65) / n)), theme).background);
  const steps = (colors: string[]): number[] =>
    colors.slice(1).map((c, i) => dE(colors[i], c));

  it('hands the component an opaque background and an opaque ink', () => {
    for (const theme of THEMES) {
      for (const d of ['AI', 'HUMAN'] as const) {
        const p = evidencePaint(d, 0.8, theme);
        expect(p.background).toMatch(HEX);
        expect(p.ink).toMatch(HEX);
      }
    }
  });

  /** The claim the alpha ramp could not make: there is now a definite colour under the quoted
      text, so the text on it has a definite contrast ratio — and it clears AA at every strength
      the lanes emit, in both themes. 0 and 1.7 are the clamped inputs. */
  it('keeps the quoted text at 4.5:1 or better everywhere in the domain', () => {
    for (const theme of THEMES) {
      for (const d of ['AI', 'HUMAN'] as const) {
        for (let i = 0; i <= 65; i++) {
          const p = evidencePaint(d, CENT(i), theme);
          expect(wcag(parse(p.ink), parse(p.background))).toBeGreaterThanOrEqual(4.5);
        }
        const clamped = evidencePaint(d, 1.7, theme);
        expect(wcag(parse(clamped.ink), parse(clamped.background))).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  /** "Not shallow", first half: equal steps in strength must be equal steps in appearance. The
      path is resampled by arc length in OKLab, so no third of the ramp is allowed to move at less
      than 85% of the average rate — a hand-picked stop list cannot promise that. */
  it('spends the same perceptual work on every part of the range', () => {
    for (const theme of THEMES) {
      for (const d of ['AI', 'HUMAN'] as const) {
        const s = steps(sweep(d, theme, 13)); // 0.05 in strength
        const avg = s.reduce((a, b) => a + b, 0) / s.length;
        expect(Math.min(...s) / avg).toBeGreaterThanOrEqual(0.85);
      }
    }
  });

  /** "Not shallow", second half: the ramp has to travel far enough to be read as a scale at all.
      The middle half of the observed AI evidence (p25 0.71 to p90 0.88) is the part a reader is
      actually shown twice on one run, and it must span at least three just-noticeable differences. */
  it('puts three just-noticeable differences between typical and strong AI evidence', () => {
    for (const theme of THEMES) {
      expect(dE(evidencePaint('AI', 0.71, theme).background, evidencePaint('AI', 0.88, theme).background))
        .toBeGreaterThanOrEqual(7);
    }
  });

  it('never reverses or stalls as strength rises', () => {
    for (const theme of THEMES) {
      for (const d of ['AI', 'HUMAN'] as const) {
        const colors = sweep(d, theme, 13);
        const base = colors[0];
        let last = 0;
        for (const c of colors.slice(1)) {
          const away = dE(base, c);
          expect(away).toBeGreaterThan(last);
          last = away;
        }
        // 66 distinct strengths must not collapse into one shade at the 0.01 resolution either.
        expect(new Set(sweep(d, theme)).size).toBeGreaterThanOrEqual(60);
      }
    }
  });

  /** Direction is hue, and hue never leaves its family: no strength of AI evidence is painted red
      or orange. Red does double duty in this palette — it is also `--bad`, the colour of a run that
      failed — so the superseded ramp made the most confident quote and a broken detection the same
      colour. That version ended the AI run at hue 20 (dark) / 24 (light); this one is measured at
      87-97 across all 66 strengths and both themes. ~12-40 is red, ~50-70 orange. */
  it('never paints AI evidence with the error colour at any strength', () => {
    for (const theme of THEMES) {
      for (let i = 0; i <= 65; i++) {
        const s = CENT(i);
        const ai = hueOf(evidencePaint('AI', s, theme).background);
        const human = hueOf(evidencePaint('HUMAN', s, theme).background);
        expect(ai).toBeGreaterThanOrEqual(80);
        expect(ai).toBeLessThan(110);
        expect(human).toBeGreaterThan(110);
        expect(human).toBeLessThan(210);
      }
    }
  });

  /** Strength is distance from the page, and the page is the middle of the scale, not an end of it.
      Because a highlight is an overlay, "less certain" is the same thing as "more transparent": a
      0.30 claim lands 2-5 dE off the surface it is painted on (--field-bg) and a 0.95 claim 27-34
      dE off it, in both themes. A reader shown a loud plate for the quotes the lanes are least sure
      of is being shown confidence the score does not contain. */
  it('washes a weak claim back into the page and drives a strong one away from it', () => {
    const SURFACE: Record<EvidenceTheme, string> = { dark: '#101010', light: '#ffffff' };
    for (const theme of THEMES) {
      for (const d of ['AI', 'HUMAN'] as const) {
        const off = (s: number): number => dE(SURFACE[theme], evidencePaint(d, s, theme).background);
        expect(off(STRENGTH_FLOOR)).toBeLessThanOrEqual(8);
        expect(off(STRENGTH_CEIL)).toBeGreaterThanOrEqual(25);
      }
    }
  });

  /** The two halves of one axis. At the weak end the directions converge on the page and are NOT
      reliably distinguishable — measured 2.5 dE apart (dark) and 3.6 (light) at strength 0.30 — and
      the spec says so rather than pretending otherwise. From 0.50 up, where a claim is worth
      reading, a matched-strength pair is at least 4 dE apart, and the gap widens to 12.8 (dark) /
      18.5 (light) at the ceiling. Checked at the 0.05 resolution: each direction is resampled on
      its own arc length, so at 0.01 the matched pair can wiggle by ~0.07 dE, which is a fifth of
      the smallest step a reader is being asked to see. */
  it('converges where the lanes are unsure and separates where they are not', () => {
    for (const theme of THEMES) {
      const sepAt = (s: number): number =>
        dE(evidencePaint('HUMAN', s, theme).background, evidencePaint('AI', s, theme).background);
      expect(sepAt(STRENGTH_FLOOR)).toBeLessThanOrEqual(6);
      let last = 0;
      for (let i = 0; i <= 13; i++) {
        const s = CENT(Math.round((i * 65) / 13));
        const sep = sepAt(s);
        expect(sep).toBeGreaterThan(last);
        last = sep;
        if (s >= 0.5) expect(sep).toBeGreaterThanOrEqual(4);
      }
    }
  });

  /** Scaled on the measured domain (min 0.30, max 0.95 over 575 quotes), not on 0..1: below the
      floor and above the ceiling the ramp saturates instead of inventing a sixth of green. */
  it('saturates outside the strength range the lanes actually emit', () => {
    expect(normStrength(STRENGTH_FLOOR)).toBe(0);
    expect(normStrength(STRENGTH_CEIL)).toBe(1);
    for (const theme of THEMES) {
      expect(evidencePaint('AI', STRENGTH_FLOOR, theme)).toEqual(evidencePaint('AI', 0, theme));
      expect(evidencePaint('AI', STRENGTH_CEIL, theme)).toEqual(evidencePaint('AI', 1, theme));
    }
  });

  it('is a pure function of (direction, strength, theme)', () => {
    for (const theme of THEMES) {
      expect(evidencePaint('AI', 0.83, theme)).toEqual(evidencePaint('AI', 0.83, theme));
      expect(evidencePaint('AI', 0.83, 'dark').background)
        .not.toBe(evidencePaint('AI', 0.83, 'light').background);
    }
  });
});
