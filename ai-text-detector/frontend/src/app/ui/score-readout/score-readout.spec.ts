import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ScoreReadout } from './score-readout';
import type {
  ComponentScores,
  DetectionResult,
  EffectiveThresholds,
  LaneAttempt,
} from '../../core/detection.types';

/**
 * The score readout: the one place a run's number is printed on the Detect tab.
 *
 * It used to live inside the result card, which meant the rail had nowhere to put the verdict and
 * the card had to be read top-to-bottom to find the probability. Split out, it carries the claims
 * that used to be tested through the card — a verdict attributed to the wrong model is worse than no
 * verdict, so the substitution has to be visible without opening a fold — plus the ones the History
 * sheet already taught: a missing score must not render as 0%, and a probability must name the
 * scale it was measured on.
 */
const THRESHOLDS: EffectiveThresholds = {
  ai_prob: 0.69,
  human_prob: 0.4,
  ai_conf: 0.6,
  human_conf: 0.6,
  secondary_decision: 0.9,
  single_model_penalty: 0.15,
};

const base = (over: Partial<DetectionResult> = {}): DetectionResult =>
  ({
    classification: 'AI_GENERATED',
    ai_probability: 0.93,
    confidence: 0.88,
    method: 'PRIMARY_DECIDED_AI',
    ...over,
  }) as DetectionResult;

const scores = (over: Partial<ComponentScores> = {}): ComponentScores =>
  ({
    primary_probability: 0.62,
    primary_confidence: 0.7,
    secondary_called: true,
    secondary_probability: 0.83,
    secondary_confidence: 0.66,
    secondary_decision_threshold: 0.9,
    lane_gap: 0.21,
    deciding_lane: 'primary',
    ...over,
  }) as ComponentScores;

describe('ScoreReadout', () => {
  async function render(result: DetectionResult, elapsedSeconds = 0) {
    // Some cases are read twice in one test — the gap row only means something against the
    // single-lane run beside it — so the module has to be released before it is configured again.
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({ imports: [ScoreReadout] }).compileComponents();
    const f = TestBed.createComponent(ScoreReadout);
    f.componentRef.setInput('result', result);
    if (elapsedSeconds) f.componentRef.setInput('elapsedSeconds', elapsedSeconds);
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    return f;
  }

  const text = (f: ComponentFixture<ScoreReadout>, sel: string): string =>
    (f.nativeElement.querySelector(sel) as HTMLElement | null)?.textContent?.replace(/\s+/g, ' ').trim() ?? '';

  /** The dials, in the order the rail draws them. `data-tone` is the arc's hue, which is the only
      place the verdict's colour reaches the measurement itself. */
  const rings = (f: ComponentFixture<ScoreReadout>): Element[] =>
    Array.from(f.nativeElement.querySelectorAll('.ring'));

  const figure = (f: ComponentFixture<ScoreReadout>): string =>
    f.nativeElement.querySelector('.ring-lead .ring-num')?.textContent?.trim() ?? '';

  /** The arc's own numbers: the dash offset is the unfilled remainder, so offset/dasharray reads the
      fraction back out of what was actually handed to the SVG. */
  const arc = (f: ComponentFixture<ScoreReadout>, i = 0): number => {
    const c = f.nativeElement.querySelectorAll('.ring-value')[i] as Element | undefined;
    if (!c) return NaN;
    const array = Number(c.getAttribute('stroke-dasharray'));
    const offset = Number(c.getAttribute('stroke-dashoffset'));
    return 1 - offset / array;
  };

  /** `fixture.nativeElement` is untyped, so a typed `querySelectorAll` has to go through an
      HTMLElement. */
  const root = (f: ComponentFixture<ScoreReadout>): HTMLElement => f.nativeElement as HTMLElement;

  const lanes = (f: ComponentFixture<ScoreReadout>, sel: string): string[] =>
    Array.from(root(f).querySelectorAll<HTMLElement>(sel)).map((e) =>
      e.textContent.replace(/\s+/g, ' ').trim(),
    );

  const ringLabels = (f: ComponentFixture<ScoreReadout>): string[] =>
    Array.from(root(f).querySelectorAll<HTMLElement>('.ring-label')).map((e) => e.textContent.trim());

  /** The three non-model evidence legs are irrelevant to the readout, so only the lanes are written
      out here — and only the fields a lane row is asked about. */
  const evidence = (models: Partial<LaneAttempt>[]) => ({
    provenance: { checked: false, reason: 'not run' },
    watermark: { checked: false, reason: 'not run' },
    statistics: { checked: false, reason: 'not run' },
    models: models.map(
      (m) =>
        ({
          lane: 'primary',
          model: null,
          ok: true,
          skipped: false,
          error_code: null,
          error_detail: null,
          salvaged: false,
          truncated: false,
          result: null,
          ...m,
        }) as LaneAttempt,
    ),
  });

  it('prints the probability on a dial and names the scale that judged it', async () => {
    const f = await render(
      base({ component_scores: scores(), config_used: { thresholds: THRESHOLDS } as never }),
    );
    // The figure keeps the decimal the run was measured to; the ruler that used to carry it drew the
    // same number as a needle's pixel position, which could not hold a decimal at all.
    expect(figure(f)).toBe('93.0');
    expect(text(f, '.ring-lead .ring-unit')).toBe('%');
    // AI_GENERATED is red, and the arc is filled to the measurement — the dial states the magnitude,
    // which is the one thing a needle-on-a-ruler could state only by making the reader measure it.
    expect(f.nativeElement.querySelector('.ring-lead .ring')?.getAttribute('data-tone')).toBe('red');
    expect(arc(f, 0)).toBeCloseTo(0.93, 5);
    expect(text(f, '.ring-lead .ring-label')).toContain('primary scale');
    expect(text(f, '.scale-note')).toContain('primary scale');
    expect(text(f, '.scale-note')).toContain('AI ≥ 69%');
    // Confidence and the gap are the two qualifiers, each on its own dial beside the answer.
    expect(rings(f).length).toBe(3);
    expect(f.nativeElement.querySelectorAll('.ring-num')[1].textContent).toBe('88');
    expect(f.nativeElement.querySelectorAll('.ring-num')[2].textContent).toBe('21');
  });

  it('names the tie-break cut when the second lane decided', async () => {
    const f = await render(
      base({
        classification: 'HUMAN_LIKELY',
        ai_probability: 0.85,
        component_scores: scores({ deciding_lane: 'secondary' }),
        config_used: { thresholds: THRESHOLDS } as never,
      }),
    );
    expect(text(f, '.verdict')).toContain('HUMAN_LIKELY');
    // 85 % beside a Human verdict is correct on THAT ruler — the tie-break lane puts true human text
    // near 0.75 and only calls AI at 0.90. So the ruler is named twice over: on the dial's own label
    // and in the note under the row. The primary's 69/40 is not quoted, because it never judged this
    // number, and an 85 drawn next to "AI ≥ 69%" would read as a broken detector.
    expect(text(f, '.ring-lead .ring-label')).toContain('tie-break scale');
    expect(text(f, '.scale-note')).toContain('tie-break lane’s own scale');
    expect(text(f, '.scale-note')).toContain('≥ 90%');
    expect(text(f, '.scale-note')).not.toContain('69');
    // The hue follows the verdict the cut produced, not the height of the number.
    expect(f.nativeElement.querySelector('.ring-lead .ring')?.getAttribute('data-tone')).toBe('green');
    expect(figure(f)).toBe('85.0');
  });

  it('quotes no cut when the response named none, rather than reciting a default', async () => {
    // The baked 0.90 is the workflow's own number, not one the run reported and not one the user
    // chose; printing it as if the response had said so would dress an assumption up as the
    // calibration. The measurement still stands — it just stops claiming a ruler.
    const f = await render(base({ component_scores: scores({ deciding_lane: null }) }));
    expect(f.nativeElement.querySelector('.scale-note')).toBe(null);
    expect(text(f, '.ring-lead .ring-label')).toBe('AI prob.');
    expect(figure(f)).toBe('93.0');
    expect(rings(f).length).toBe(3);
  });

  it('draws an empty slot instead of a dial at zero when the run reached no model', async () => {
    const f = await render(
      base({ classification: 'DETECTION_UNAVAILABLE', ai_probability: null, confidence: null }),
    );
    // A ring filled to 0% is a confident HUMAN answer, and this run never reached a model to be
    // confident about anything. No dial is drawn at all, so no fraction can be misread.
    expect(f.nativeElement.querySelector('app-circular-metric')).toBe(null);
    expect(f.nativeElement.querySelector('.no-score')).toBeTruthy();
    expect(f.nativeElement.textContent).not.toMatch(/(^|[^.\d])0(\.0)?%/);
  });

  it('shows the gap between the lanes only when a second lane actually answered', async () => {
    const withTieBreak = await render(base({ component_scores: scores() }));
    expect(rings(withTieBreak).length).toBe(3);
    expect(ringLabels(withTieBreak)).toContain('Gap between lanes');

    const oneLane = await render(
      base({
        component_scores: scores(),
        config_used: { secondary: { enabled: false } } as never,
      }),
    );
    // The number exists in the response either way, but a gap between one lane and itself measures
    // nothing, so the third dial is not drawn and the row says why in the method line.
    expect(rings(oneLane).length).toBe(2);
    expect(ringLabels(oneLane)).not.toContain('Gap between lanes');
    expect(text(oneLane, '.method')).toContain('single lane');
  });

  it('names the models that answered, and which of them the verdict belongs to', async () => {
    const f = await render(
      base({
        component_scores: scores({ deciding_lane: 'secondary' }),
        evidence: evidence([
          { lane: 'primary', model: 'p/one' },
          { lane: 'secondary', model: 'b/y' },
          { lane: 'tertiary', model: 'c/z', ok: false, skipped: true, error_detail: 'no time left' },
        ]),
      }),
    );
    // Two lanes can disagree, and a reader comparing the dial above to the model beside it has to
    // know which of the two the claim belongs to. A lane that never answered is not a model that was
    // used, and crediting it would be a false statement about a model that made no verdict.
    const rows = lanes(f, '.readout-models li');
    expect(rows.length).toBe(2);
    // The parts of a row are separated by the row's flex gap, not by a space in the text, so the
    // concatenation reads `primarya` in the DOM. Assert the pieces, not the join.
    expect(rows[0]).toContain('primary');
    expect(rows[0]).toContain('p/one');
    expect(rows[0]).not.toContain('decided');
    expect(rows[1]).toContain('secondary');
    expect(rows[1]).toContain('b/y');
    expect(rows[1]).toContain('decided');
    const decided = Array.from(
      root(f).querySelectorAll<HTMLElement>('.readout-models li'),
    ).map((li) => li.classList.contains('decided'));
    expect(decided).toEqual([false, true]);
  });

  it('prints the reason each lane that did not answer gave back', async () => {
    const f = await render(
      base({
        classification: 'DETECTION_UNAVAILABLE',
        ai_probability: null,
        confidence: null,
        method: 'ALL_MODELS_FAILED',
        error: 'All configured models failed to return a usable verdict.',
        evidence: evidence([
          {
            lane: 'primary',
            model: 'nvidia/nemotron-3-ultra-550b-a55b',
            ok: false,
            error_code: '401',
            error_detail: 'Invalid API key provided',
          },
          { lane: 'secondary', model: 'b/y', ok: false, skipped: true, error_code: null, error_detail: null },
        ]),
      }),
    );
    // Nothing else in the response is the output of this run. "Invalid API key provided" is the one
    // sentence that tells the reader to go and replace a key, so it is on the face of the rail and
    // not in a fold — and the lane it belongs to is what names which key to replace.
    const rows = lanes(f, '.readout-failures li');
    expect(rows.length).toBe(2);
    expect(rows[0]).toContain('primary');
    expect(rows[0]).toContain('401');
    expect(rows[0]).toContain('Invalid API key provided');
    // A lane that was never called is a different fact from one that was called and refused.
    expect(rows[1]).toContain('not called');
    expect(rows[1]).not.toContain('401');
    expect(text(f, '.readout-note')).toContain('Every lane failed');
  });

  it('names the model that actually answered, above the score', async () => {
    const f = await render(
      base({
        lane_source: 'failover',
        auto_switch: {
          from: 'nvidia/dead-model',
          to: 'z-ai/glm-5.3-flash',
          reason: 'the first model returned no usable verdict',
          attempts: [
            { from: 'nvidia/dead-model', to: 'z-ai/glm-5.3-flash', provider: 'nvidia', ok: true },
          ],
        },
      }),
    );
    const note = f.nativeElement.querySelector('.switch-note');
    expect(note, 'the substitution must be visible without opening a fold').toBeTruthy();
    expect(note.textContent).toContain('nvidia/dead-model');
    expect(note.textContent).toContain('z-ai/glm-5.3-flash');
    expect(note.textContent).toContain('answered');
    expect(text(f, '.method')).toContain('auto-switched within your pool');
  });

  it('tells a candidate that was never called from one that was called and failed', async () => {
    const f = await render(
      base({
        lane_source: 'failover',
        auto_switch: {
          from: 'a/x',
          to: 'c/z',
          reason: 'the first model returned no usable verdict',
          attempts: [
            { from: 'a/x', to: 'b/y', provider: 'p', skipped: 'no time left in this request budget' },
            { from: 'a/x', to: 'c/z', provider: 'p', ok: true },
          ],
        },
      }),
    );
    const attempts = Array.from(
      f.nativeElement.querySelectorAll('.switch-attempt'),
    ).map((e: any) => e.textContent.trim());
    expect(attempts.length).toBe(2);
    expect(attempts[0]).toContain('not called');
    expect(attempts[1]).toContain('answered');
  });

  it('reports a pool-sourced verdict as the pool, not as the deployed default', async () => {
    const f = await render(base({ lane_source: 'user-pool' }));
    expect(f.nativeElement.querySelector('.switch-note')).toBe(null);
    expect(text(f, '.method')).toContain('your using pool');
  });

  it('claims no authority for a stored row that predates the pool', async () => {
    // An unknown authority reported as "the deployed default lane" would be a false claim
    // about every history row saved before this feature existed.
    const f = await render(base({}));
    expect(text(f, '.method')).not.toContain('pool');
    expect(text(f, '.method')).not.toContain('deployed');
  });

  it('explains a review deferral that the switched-off tie-break caused', async () => {
    const f = await render(
      base({
        classification: 'REVIEW_REQUIRED',
        method: 'SINGLE_MODEL_PRIMARY_UNCERTAIN',
        config_used: { secondary: { enabled: false } } as never,
      }),
    );
    expect(text(f, '.readout-note')).toContain('second-opinion lane is switched off');
  });

  it('says every lane failed when the run reached no verdict at all', async () => {
    const f = await render(
      base({ classification: 'DETECTION_UNAVAILABLE', ai_probability: null, error: 'n8n unreachable' }),
    );
    expect(text(f, '.readout-note')).toContain('Every lane failed');
    expect(text(f, '.readout-note')).toContain('n8n unreachable');
  });

  it('records how long the run took, beside the verdict', async () => {
    const f = await render(base(), 82);
    expect(text(f, '.elapsed')).toBe('82s');
  });
});
