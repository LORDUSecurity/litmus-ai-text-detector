import { answeredLanes, failedLanes } from './lane-answers';
import type { DetectionResult, LaneAttempt } from './detection.types';

/** `evidence.models` is one list carrying three different facts: the models that gave an answer,
    the ones that were called and gave none, and the ones the run never called at all. The Detect
    rail and the sheet opened over a History row both print the first group beside the number it
    produced and the other two under it, so a lane can be credited with a verdict it never made on
    one surface and not the other. These tests pin the split itself, not either template. */
describe('lane-answers', () => {
  const lane = (over: Partial<LaneAttempt> = {}): LaneAttempt => ({
    lane: 'primary',
    model: 'z-ai/glm-5.3-flash',
    ok: true,
    skipped: false,
    error_code: null,
    error_detail: null,
    salvaged: false,
    truncated: false,
    result: null,
    ...over,
  });

  const run = (models: LaneAttempt[], deciding: string | null = 'primary'): DetectionResult =>
    ({
      evidence: {
        provenance: { checked: false, reason: 'not run' },
        watermark: { checked: false, reason: 'not run' },
        statistics: { checked: false, reason: 'not run' },
        models,
      },
      component_scores: { deciding_lane: deciding },
    }) as DetectionResult;

  const BOTH = [
    lane({ model: 'p/one' }),
    lane({ lane: 'secondary', model: 'b/two' }),
  ];

  describe('answeredLanes', () => {
    it('names the models that answered, in the order the run considered them', () => {
      expect(answeredLanes(run(BOTH))).toEqual([
        { model: 'p/one', lane: 'primary', decided: true },
        { model: 'b/two', lane: 'secondary', decided: false },
      ]);
    });

    it('leaves a lane that was called and gave nothing out of the models used', () => {
      const r = run([
        lane({ model: 'p/one' }),
        lane({ lane: 'secondary', model: 'b/two', ok: false, error_code: '401' }),
      ]);
      const named = answeredLanes(r).map((a) => a.model);
      expect(named).toEqual(['p/one']);
      expect(named).not.toContain('b/two');
    });

    it('leaves a lane that was never called out too', () => {
      const r = run([
        lane({ model: 'p/one' }),
        lane({ lane: 'secondary', model: 'b/two', ok: false, skipped: true }),
      ]);
      expect(answeredLanes(r).map((a) => a.model)).toEqual(['p/one']);
    });

    /** A probe row that answers without ever naming a model cannot be credited with the verdict:
        printing `null` beside the ring would be a blank where a fact belongs. */
    it('drops a row that has no model to name', () => {
      expect(answeredLanes(run([lane({ model: null })]))).toEqual([]);
    });

    it('marks the lane whose number became the verdict, and only that one', () => {
      const secondary = answeredLanes(run(BOTH, 'secondary'));
      expect(secondary.map((a) => a.decided)).toEqual([false, true]);
    });

    it('marks no lane when the response names none, rather than guessing the primary', () => {
      expect(answeredLanes(run(BOTH, null)).map((a) => a.decided)).toEqual([false, false]);
    });

    it('says nothing for a run with no evidence, no result, or no rows', () => {
      expect(answeredLanes(null)).toEqual([]);
      expect(answeredLanes(undefined)).toEqual([]);
      expect(answeredLanes({} as DetectionResult)).toEqual([]);
      expect(answeredLanes(run([]))).toEqual([]);
    });
  });

  describe('failedLanes', () => {
    it('keeps every lane that did not answer, with the reason it gave back', () => {
      const r = run([
        lane({ model: 'p/one' }),
        lane({ lane: 'secondary', model: 'b/two', ok: false, error_code: '401', error_detail: 'Invalid API key provided' }),
      ]);
      const failed = failedLanes(r);
      expect(failed.length).toBe(1);
      expect(failed[0].model).toBe('b/two');
      expect(failed[0].error_code).toBe('401');
      expect(failed[0].error_detail).toBe('Invalid API key provided');
    });

    /** When no lane answers, this list is the entire output of the run — so a run in which every
        row failed must lose none of them to the filter. */
    it('keeps all of them when nothing answered', () => {
      const r = run([
        lane({ model: 'p/one', ok: false, error_code: '504' }),
        lane({ lane: 'secondary', model: 'b/two', ok: false, skipped: true }),
      ]);
      expect(failedLanes(r).length).toBe(2);
      expect(answeredLanes(r)).toEqual([]);
    });

    /** "Switched off before the call" is a different fact from "called and rejected", and the two
        must stay separable: the reason a reader got one answer instead of two is not an error. */
    it('keeps a not-called lane distinguishable from a failed one', () => {
      const [skipped] = failedLanes(
        run([lane({ lane: 'secondary', model: 'b/two', ok: false, skipped: true, error_code: null })]),
      );
      expect(skipped.skipped).toBe(true);
      expect(skipped.error_code).toBeNull();
    });

    it('says nothing for a run with no evidence, no result, or no rows', () => {
      expect(failedLanes(null)).toEqual([]);
      expect(failedLanes(undefined)).toEqual([]);
      expect(failedLanes({} as DetectionResult)).toEqual([]);
      expect(failedLanes(run([]))).toEqual([]);
    });
  });

  /** The two lists are read as a complete account of the run: "these answered, these did not, and
      here is why". A lane row that fell through both would make the rail silently under-report. */
  it('accounts for every named lane exactly once', () => {
    const rows = [
      lane({ model: 'p/one' }),
      lane({ lane: 'secondary', model: 'b/two', ok: false, error_code: '429' }),
      lane({ lane: 'secondary', model: 'b/three', ok: false, skipped: true }),
    ];
    const r = run(rows);
    const named = answeredLanes(r).map((a) => a.model);
    const failed = failedLanes(r).map((m) => m.model);
    expect([...named, ...failed].sort()).toEqual(['b/three', 'b/two', 'p/one']);
    expect(named.filter((m) => failed.includes(m)).length).toBe(0);
  });
});
