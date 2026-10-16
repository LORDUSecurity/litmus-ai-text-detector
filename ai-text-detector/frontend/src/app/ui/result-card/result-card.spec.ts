import {
  buildSegments,
  evidenceColor,
  probScaleNote,
  STRENGTH_CEIL,
  STRENGTH_FLOOR,
  type EvidenceSegment,
} from './result-card';
import type { ComponentScores, DetectionResult, ModelPhrase } from '../../core/detection.types';

/** Flatten segments back to plain text — the paint must never lose or reorder a
    single character of the original. */
const rejoin = (segs: EvidenceSegment[]): string => segs.map((s) => s.text).join('');

const p = (quote: string, direction: 'AI' | 'HUMAN', strength: number): ModelPhrase => ({
  quote,
  direction,
  strength,
});

describe('buildSegments', () => {
  it('returns nothing for empty text', () => {
    const { segments, unmatched } = buildSegments('', [p('x', 'AI', 0.9)]);
    expect(segments).toEqual([]);
    expect(unmatched).toBe(0);
  });

  it('leaves text untouched when there are no phrases', () => {
    const text = 'a plain paragraph with nothing to show';
    const { segments, unmatched } = buildSegments(text, []);
    expect(segments.length).toBe(1);
    expect(segments[0].direction).toBeNull();
    expect(rejoin(segments)).toBe(text);
    expect(unmatched).toBe(0);
  });

  it('paints an exact quote and keeps the surrounding text plain', () => {
    const text = 'The model leaned on ubiquitous leverage across the ecosystem.';
    const { segments, unmatched } = buildSegments(text, [p('ubiquitous leverage', 'AI', 0.8)]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    const hit = segments.find((s) => s.direction === 'AI');
    expect(hit?.text).toBe('ubiquitous leverage');
    expect(hit?.strength).toBe(0.8);
    // plain before, hit, plain after
    expect(segments.filter((s) => s.direction === null).length).toBe(2);
  });

  it('falls back to a whitespace-insensitive match across line breaks', () => {
    const text = 'Some prose that\nwraps   awkwardly across spaces.';
    const quote = 'that wraps awkwardly across';
    const { segments, unmatched } = buildSegments(text, [p(quote, 'HUMAN', 0.6)]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    const hit = segments.find((s) => s.direction === 'HUMAN');
    // the painted span covers the real characters, newlines and extra spaces included
    expect(hit?.text).toBe('that\nwraps   awkwardly across');
  });

  it('paints a quote the model re-cased, in the text\'s own capitalisation', () => {
    const text = 'Those types of funds are particularly risky.';
    const quote = 'those types of funds are particularly'; // mid-sentence cut, lower-cased
    const { segments, unmatched } = buildSegments(text, [p(quote, 'HUMAN', 0.55)]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    // the span is sliced from the original, so the highlight shows "Those", not the model's "those"
    expect(segments.find((s) => s.direction === 'HUMAN')?.text).toBe('Those types of funds are particularly');
  });

  it('still falls back to case-folding after a whitespace collapse', () => {
    const text = 'Those types of\nfunds   are particularly risky.';
    const { segments, unmatched } = buildSegments(text, [p('those types of funds are', 'HUMAN', 0.5)]);
    expect(unmatched).toBe(0);
    expect(segments.find((s) => s.direction === 'HUMAN')?.text).toBe('Those types of\nfunds   are');
  });

  it('does not stretch the case-folded step into a paraphrase', () => {
    // same words, different *words* — a quote the text never contained must stay unpainted
    const text = 'Those types of funds are particularly risky.';
    const { segments, unmatched } = buildSegments(text, [p('those kinds of funds are particularly', 'AI', 0.9)]);
    expect(unmatched).toBe(1);
    expect(segments.every((s) => s.direction === null)).toBe(true);
    expect(rejoin(segments)).toBe(text);
  });

  it('lets the strongest phrase own an overlap', () => {
    const text = 'aaa bbb ccc';
    // weak covers "aaa bbb", strong covers "bbb ccc"; the shared "bbb" must go strong.
    const { segments, unmatched } = buildSegments(text, [
      p('aaa bbb', 'HUMAN', 0.3),
      p('bbb ccc', 'AI', 0.9),
    ]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    const aiSeg = segments.find((s) => s.direction === 'AI');
    const humanSeg = segments.find((s) => s.direction === 'HUMAN');
    // the overlap "bbb" is owned by the stronger AI phrase, not the weaker human one
    expect(aiSeg?.text).toContain('bbb');
    expect(aiSeg?.strength).toBe(0.9);
    expect(humanSeg?.text).not.toContain('bbb');
    expect(humanSeg?.text).toBe('aaa ');
  });

  it('counts quotes it cannot locate and still paints the rest', () => {
    const text = 'Only one of these is really present.';
    const { segments, unmatched } = buildSegments(text, [
      p('really present', 'AI', 0.7),
      p('a quote that is not in the text at all', 'AI', 0.95),
    ]);
    expect(unmatched).toBe(1);
    expect(rejoin(segments)).toBe(text);
    expect(segments.some((s) => s.direction === 'AI' && s.text === 'really present')).toBe(true);
  });

  it('clamps an out-of-range strength instead of producing a wild colour weight', () => {
    const text = 'over the top strength';
    const { segments } = buildSegments(text, [p('over the top', 'AI', 1.7)]);
    const hit = segments.find((s) => s.direction === 'AI');
    expect(hit?.strength).toBe(1);
  });
});

describe('evidenceColor', () => {
  const rgb = (css: string): number[] => {
    const m = css.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    if (!m) throw new Error('not an rgb colour: ' + css);
    return [Number(m[1]), Number(m[2]), Number(m[3])];
  };
  const alpha = (css: string): number => Number(css.match(/,\s*([\d.]+)\)$/)?.[1]);

  it('paints human evidence green', () => {
    const [r, g, b] = rgb(evidenceColor('HUMAN', 0.8));
    expect(g).toBeGreaterThan(r);
    expect(g).toBeGreaterThan(b);
  });

  it('deepens the human tint as strength rises', () => {
    const weakAlpha = Number(evidenceColor('HUMAN', 0.1).match(/,\s*([\d.]+)\)$/)?.[1]);
    const strongAlpha = Number(evidenceColor('HUMAN', 0.95).match(/,\s*([\d.]+)\)$/)?.[1]);
    expect(strongAlpha).toBeGreaterThan(weakAlpha);
  });

  it('degrades AI evidence from yellow toward red as strength rises', () => {
    const weak = rgb(evidenceColor('AI', 0.15)); // yellow-ish: green channel high
    const strong = rgb(evidenceColor('AI', 0.95)); // red-ish: green channel drops, red peaks
    expect(weak[1]).toBeGreaterThan(strong[1]); // green falls
    expect(strong[0]).toBeGreaterThanOrEqual(weak[0]); // red stays high / rises
    expect(strong[2]).toBeLessThan(weak[1]); // blue never dominates the red end
  });

  it('never returns a transparent colour for real evidence', () => {
    for (const d of ['AI', 'HUMAN'] as const) {
      for (const s of [0, 0.5, 1]) {
        const alpha = Number(evidenceColor(d, s).match(/,\s*([\d.]+)\)$/)?.[1]);
        expect(alpha).toBeGreaterThan(0);
      }
    }
  });

  /** 575 quotes over three sonnet captures: min 0.30, max 0.95, AI p50 0.80 / p90 0.88. A ramp
      scaled on 0..1 spends its bottom 30% on values no model emits and crams the whole observed
      AI range into the top of the red, so the evidence that exists is nearly one colour. */
  it('spans the strength range the models actually emit', () => {
    expect(evidenceColor('AI', STRENGTH_FLOOR)).toBe(evidenceColor('AI', 0)); // floored
    expect(evidenceColor('AI', STRENGTH_CEIL)).toBe(evidenceColor('AI', 1)); // topped
    const floor = rgb(evidenceColor('AI', STRENGTH_FLOOR));
    const ceil = rgb(evidenceColor('AI', STRENGTH_CEIL));
    expect(floor[1] - ceil[1]).toBeGreaterThan(110); // full yellow -> full red
  });

  it('separates the crowded top of the observed AI range', () => {
    const p25 = rgb(evidenceColor('AI', 0.71)); // measured AI p25
    const p90 = rgb(evidenceColor('AI', 0.88)); // measured AI p90
    const a25 = alpha(evidenceColor('AI', 0.71));
    const a90 = alpha(evidenceColor('AI', 0.88));
    expect(p25[1] - p90[1]).toBeGreaterThanOrEqual(35); // visibly different hue
    expect(a90 - a25).toBeGreaterThanOrEqual(0.06); // and a different depth
  });

  it('keeps the whole ramp legible on a panel', () => {
    for (const d of ['AI', 'HUMAN'] as const) {
      for (let s = 0; s <= 1.0001; s += 0.05) {
        const a = alpha(evidenceColor(d, s));
        expect(a).toBeGreaterThanOrEqual(0.1);
        expect(a).toBeLessThanOrEqual(0.62); // above this the text under the tint goes unreadable
      }
    }
  });
});

/** The probability headline has to be readable against the cut that produced the verdict.
    A live run through the packaged app showed "85.0%" sitting next to HUMAN_LIKELY: the
    tie-break lane scores true-human text around 0.75 and only calls AI at ≥ 0.90, so 0.85
    is a *human* score on that scale and an alarming score on the primary's 0.69/0.40 one.
    Because the deployed primary is currently failing, that single-lane path is what a fresh
    install shows by default. The fix is not a new number — it is the same number, stated
    on its own scale. */
describe('probScaleNote', () => {
  const cuts = (over: Record<string, number> = {}) => ({
    ai_prob: 0.69,
    ai_conf: 0.6,
    human_prob: 0.4,
    human_conf: 0.6,
    secondary_decision: 0.9,
    single_model_penalty: 1,
    ...over,
  });

  const scores = (over: Partial<ComponentScores> = {}): ComponentScores => ({
    primary_probability: null,
    primary_confidence: null,
    secondary_probability: 0.85,
    secondary_confidence: 0.92,
    lane_gap: null,
    secondary_called: true,
    decision_mode: 'tie_break',
    deciding_lane: 'secondary',
    secondary_decision_threshold: 0.9,
    ...over,
  });

  const res = (over: Partial<DetectionResult> = {}): DetectionResult =>
    ({
      classification: 'HUMAN_LIKELY',
      ai_probability: 0.85,
      confidence: 0.92,
      method: 'SINGLE_MODEL_SECONDARY_HUMAN',
      ...over,
    }) as DetectionResult;

  const configUsed = (over: Record<string, number> = {}) =>
    ({ thresholds: cuts(over) }) as DetectionResult['config_used'];

  it('names the tie-break cut a secondary verdict was measured against', () => {
    const note = probScaleNote(res({ component_scores: scores() }));
    expect(note).toContain('tie-break');
    expect(note).toContain('90%');
    // the primary's cuts say nothing about this number, so they must not appear
    expect(note).not.toContain('69%');
  });

  it('names the primary band when the primary decided', () => {
    const note = probScaleNote(
      res({
        classification: 'AI_GENERATED',
        ai_probability: 0.8,
        method: 'PRIMARY_DECIDED_AI',
        component_scores: scores({ deciding_lane: 'primary', secondary_called: false }),
        config_used: configUsed(),
      }),
    );
    expect(note).toContain('AI ≥ 69%');
    expect(note).toContain('human ≤ 40%');
  });

  it('reads the cut off the decision when both the decision and the config report one', () => {
    const note = probScaleNote(
      res({
        component_scores: scores({ secondary_decision_threshold: 0.95 }),
        config_used: configUsed({ secondary_decision: 0.9 }),
      }),
    );
    expect(note).toContain('95%');
    expect(note).not.toContain('90%');
  });

  it('says nothing when no scale is reported, rather than inventing the baked 0.90', () => {
    expect(probScaleNote(res())).toBeNull();
    expect(probScaleNote(res({ component_scores: scores({ secondary_decision_threshold: null }) }))).toBeNull();
    expect(
      probScaleNote(
        res({
          component_scores: scores({ deciding_lane: 'primary', secondary_decision_threshold: null }),
        }),
      ),
    ).toBeNull();
  });
});
