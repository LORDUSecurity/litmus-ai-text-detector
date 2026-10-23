import type { DetectionResult, LaneAttempt } from './detection.types';

/** One of the models that produced this run's answer. */
export interface AnsweredLane {
  lane: string;
  model: string;
  /** True for the lane whose number became the verdict. Two lanes can disagree, and a reader
      comparing the figure above to the model beside it has to know which of them the claim is
      the one being made. */
  decided: boolean;
}

/** A row is not a lane: `evidence.models` carries every candidate the run considered, including
    the ones it never called. So "the models used" and "the models that failed" are two different
    filters over the same list, and both surfaces that print them — the Detect rail and the sheet
    opened over a History row — have to agree on which is which. Living here, one wrong list cannot
    be written on one of the two. */

/** The models that actually answered, in the order the run considered them. A lane that was
    switched off, or was called and produced nothing usable, is not one of the models used, and
    naming it would credit a verdict to a model that never made one. */
export function answeredLanes(result: DetectionResult | null | undefined): AnsweredLane[] {
  if (!result) return [];
  const deciding = result.component_scores?.deciding_lane ?? null;
  return (result.evidence?.models ?? [])
    .filter((m) => m.ok && m.model)
    .map((m) => ({ model: m.model as string, lane: m.lane, decided: m.lane === deciding }));
}

/** Every lane that did not answer, with the reason it gave back. When nothing answered this is not
    detail about a bad run — it is the whole output of the run, and the only sentence a reader can
    act on: which lane, and whether the key was rejected, the model gone, or the reply empty. A lane
    that was never called is kept in this list and labelled apart, because "not called" is why the
    reader got one answer instead of two. */
export function failedLanes(result: DetectionResult | null | undefined): LaneAttempt[] {
  if (!result) return [];
  return (result.evidence?.models ?? []).filter((m) => !m.ok);
}
