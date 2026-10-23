import { barWidth, pct, probScaleNote } from './prob-scale';
import type { ComponentScores, DetectionResult, EffectiveThresholds } from './detection.types';

/** The probability headline has to be readable against the cut that produced the verdict.
    A live run through the packaged app showed "85.0%" sitting next to HUMAN_LIKELY: the
    tie-break lane scores true-human text around 0.75 and only calls AI at ≥ 0.90, so 0.85
    is a *human* score on that scale and an alarming score on the primary's 0.69/0.40 one.
    Because the deployed primary is currently failing, that single-lane path is what a fresh
    install shows by default. The fix is not a new number — it is the same number, stated
    on its own scale.

    Three surfaces print that number — the Detect rail, the history sheet and the card's lane
    table — which is why the note is a function of the result rather than a line of a template. */
describe('probScaleNote', () => {
  const cuts = (over: Partial<EffectiveThresholds> = {}): EffectiveThresholds => ({
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

  const configUsed = (over: Partial<EffectiveThresholds> = {}) =>
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

describe('pct', () => {
  it('keeps the decimal the measurement was reported at', () => {
    expect(pct(0.93)).toBe('93.0%');
    expect(pct(0.8)).toBe('80.0%');
  });

  /** A missing score rendered as "0.0%" is a confident HUMAN answer, and History rows have shown
      one for a run that never reached a model. */
  it('prints a dash for a missing value, never a zero', () => {
    expect(pct(null)).toBe('—');
    expect(pct(undefined)).toBe('—');
  });
});

describe('barWidth', () => {
  it('fills the track in proportion and never past it', () => {
    expect(barWidth(0.21)).toBe('21.00%');
    expect(barWidth(1.7)).toBe('100.00%');
    expect(barWidth(-0.5)).toBe('0.00%');
    expect(barWidth(null)).toBe('0%');
  });
});
