import type { Classification, DetectionResult } from './detection.types';

export type RingTone = 'red' | 'green' | 'amber' | 'neutral';

/** The verdict vocabulary, in one place. A run is read on three surfaces — as a row in the history
    list, inside the sheet opened over it, and as the score readout on the Detect rail — and the
    pill, the short label, the mark and the ring colour must be the same answer all three times. */

export function verdictLabel(c: string | null): string {
  switch (c as Classification | null) {
    case 'AI_GENERATED':
      return 'AI';
    case 'HUMAN_LIKELY':
      return 'Human';
    case 'REVIEW_REQUIRED':
      return 'Review';
    case 'DETECTION_UNAVAILABLE':
      return 'Unavailable';
    default:
      return '—';
  }
}

export function verdictPillClass(c: string | null): string {
  switch (c as Classification | null) {
    case 'AI_GENERATED':
      return 'verdict-pill ai';
    case 'HUMAN_LIKELY':
      return 'verdict-pill human';
    case 'REVIEW_REQUIRED':
      return 'verdict-pill review';
    default:
      return 'verdict-pill none';
  }
}

/** The dial's hue for a verdict. `neutral` is a row that was never scored: the ring there measures
    nothing, so it wears the instrument's own grey rather than a fourth verdict colour — and the app's
    interaction gold belongs to a response in flight, never to a standing state. */
export function verdictTone(c: string | null): RingTone {
  switch (c as Classification | null) {
    case 'AI_GENERATED':
      return 'red';
    case 'HUMAN_LIKELY':
      return 'green';
    case 'REVIEW_REQUIRED':
      return 'amber';
    default:
      return 'neutral';
  }
}

/** The mark drawn beside the verdict word: the four cases as shapes, so the status is readable
    without reading the label. */
export function verdictIcon(c: string | null): 'alert' | 'check' | 'question' | 'plug' {
  switch (c as Classification | null) {
    case 'AI_GENERATED':
      return 'alert';
    case 'HUMAN_LIKELY':
      return 'check';
    case 'REVIEW_REQUIRED':
      return 'question';
    default:
      return 'plug';
  }
}

/** Did this run walk the tie-break lane, or was it switched off? The workflow echoes the off switch
    in `config_used`, and half the card's rows — the gap between lanes, the "not called" secondary —
    only mean something once you know which of the two this was. */
export function secondaryOff(result: DetectionResult): boolean {
  return result.config_used?.secondary?.enabled === false;
}

/** Whose choice of model produced this verdict. Null on a stored row from before the using pool
    existed — an unknown authority is reported as unknown, not as "deployed". */
export function laneSourceLabel(result: DetectionResult): string | null {
  switch (result.lane_source) {
    case 'failover': return 'auto-switched within your pool';
    case 'user-pool': return 'your using pool';
    case 'request': return 'named in this request';
    case 'deployed-default': return 'the deployed default lane';
    default: return null;
  }
}

const REVIEW_REASONS: Record<string, string> = {
  SINGLE_MODEL_PRIMARY_UNCERTAIN:
    'The primary landed between its human and AI thresholds and the tie-break lane returned no usable verdict.',
};

/** Why the detector declined to decide, in the reader's terms rather than the method's. */
export function reviewReason(result: DetectionResult): string | null {
  if (
    result.method === 'SINGLE_MODEL_PRIMARY_UNCERTAIN' &&
    secondaryOff(result)
  ) {
    return 'The primary landed between its human and AI cuts and the second-opinion lane is switched off, so this text is left for review instead of a tie-break.';
  }
  return REVIEW_REASONS[result.method] ?? null;
}

