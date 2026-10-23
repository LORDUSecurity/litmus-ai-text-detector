import type { DetectionResult } from './detection.types';

/** Percent without decimals, for prose and for the cut labels on the scale: 0.9 -> "90%". A score
    readout keeps the decimal for the measurement itself; here the cut is only context, and a cut
    fitted on 48 texts does not have three significant figures of meaning. */
export function pc(n: number): string {
  return `${Math.round(n * 100)}%`;
}

/** A measurement, spoken to one decimal: 0.93 -> "93.0%". Null stays a dash rather than becoming a
    zero, because "0.0%" is a confident HUMAN answer and a run that never reached a model has no
    answer at all. */
export function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return (value * 100).toFixed(1) + '%';
}

/** The same number as the width of a bar, clamped to the track. */
export function barWidth(value: number | null | undefined): string {
  if (value === null || value === undefined) return '0%';
  return Math.max(0, Math.min(100, value * 100)).toFixed(2) + '%';
}

/** State which cut the headline probability was judged against.
 *
    The two lanes are different models on different scales and the workflow never blends them:
    the tie-break lane puts true-human text near 0.75 and only calls AI at >= 0.90, while the
    primary decides at 0.69 / 0.40. So the same number means opposite things depending on which
    lane produced it — a live run through the packaged app showed "85.0%" sitting next to
    HUMAN_LIKELY, which reads as a broken detector when it is a correct reading of the wrong
    scale. Because the deployed primary is currently failing, that single-lane-secondary path is
    what a fresh install shows by default, so the note belongs on the headline, not in a fold.
    Returns null when no scale was reported: the baked 0.90 is a workflow default the user never
    chose, and quoting it as if the response had said so would be inventing a number.

    Lives outside the result card because a probability has to name its ruler wherever it is
    printed — the card, the Detect rail and the history sheet all show the same ring and must all
    say which scale it was measured on. */
export function probScaleNote(result: DetectionResult): string | null {
  const lane = result.component_scores?.deciding_lane ?? null;
  if (lane === 'secondary') {
    const cut = result.component_scores?.secondary_decision_threshold;
    if (typeof cut === 'number' && Number.isFinite(cut)) {
      return `tie-break lane’s own scale — AI only at ≥ ${pc(cut)}`;
    }
    const eff = result.config_used?.thresholds?.secondary_decision;
    return typeof eff === 'number' && Number.isFinite(eff)
      ? `tie-break lane’s own scale — AI only at ≥ ${pc(eff)}`
      : null;
  }
  const t = result.config_used?.thresholds;
  if (lane === 'primary' && t && Number.isFinite(t.ai_prob) && Number.isFinite(t.human_prob)) {
    return `primary scale — AI ≥ ${pc(t.ai_prob)} · human ≤ ${pc(t.human_prob)}`;
  }
  return null;
}
