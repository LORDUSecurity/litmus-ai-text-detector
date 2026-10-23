import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { DetectionResult, FailoverAttempt } from '../../core/detection.types';
import { probScaleNote } from '../../core/prob-scale';
import {
  laneSourceLabel,
  reviewReason,
  secondaryOff,
  verdictIcon,
  verdictTone,
} from '../../core/verdict-words';
import { CircularMetric } from '../circular-metric/circular-metric';
import { LaneList } from '../lane-list/lane-list';

/** The number, on its own, above the analysis. This is what the Detect rail answers a press with:
    the verdict, who produced it, and the magnitudes that made it.

    It used to be the top of the result card, which meant the card had to be read top-to-bottom to
    find the score and the rail had nowhere to put the verdict. Split out, the card keeps the
    evidence and the provenance and this keeps the one claim a run is made of.

    The figures are rings now. They were a needle on a ruler, and the ruler was the loud thing about
    a number whose only interesting property is which side of a cut it fell on — a fact the ruler
    could state only by putting the reader's eye to work measuring pixels, and which the response
    also says outright in the scale note. A ring states the magnitude and nothing else, which is what
    a rail this narrow can afford: three of them fit across it without the page growing a screenful.

    Two rules carried over from the surfaces that already had to learn them: a missing value is a dash
    and an empty ring, never a zero — 0% is a confident HUMAN answer — and a probability has to name
    the ruler that measured it, because the two lanes are different models on different scales and the
    same number means opposite things depending on which answered. */
@Component({
  selector: 'app-score-readout',
  standalone: true,
  imports: [CircularMetric, LaneList],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './score-readout.html',
})
export class ScoreReadout {
  readonly result = input.required<DetectionResult>();
  readonly elapsedSeconds = input(0);

  readonly icon = computed(() => verdictIcon(this.result().classification));
  readonly method = computed(() => this.result().method);
  readonly laneSource = computed(() => laneSourceLabel(this.result()));
  readonly secondaryDisabled = computed(() => secondaryOff(this.result()));
  readonly scaleNote = computed(() => probScaleNote(this.result()));
  readonly switched = computed(() => this.result().auto_switch ?? null);
  readonly tone = computed(() => verdictTone(this.result().classification));

  /** A run that never reached a model has no probability to put on a ring. The rail says so in
      prose; here the ring is the loudest thing in the rail, so the absence is what renders. */
  readonly scored = computed(() => this.result().ai_probability !== null);

  readonly probability = computed(() => this.result().ai_probability);
  readonly confidence = computed(() => this.result().confidence);
  readonly laneGap = computed(() => this.result().component_scores?.lane_gap ?? null);

  readonly scaleLabel = computed(() => {
    switch (this.result().component_scores?.deciding_lane) {
      case 'secondary':
        return 'AI prob · tie-break scale';
      case 'primary':
        return 'AI prob · primary scale';
      default:
        return 'AI prob.';
    }
  });

  /** The digits of the figure, without the unit: at this size a % competes with the measurement, so
      it is drawn one rung down and beside it instead of inside it. */
  readonly figure = computed(() => {
    const p = this.result().ai_probability;
    return p === null ? '—' : (p * 100).toFixed(1);
  });

  /** Which models answered, and which did not with what they said back, is the lane list's job — the
      sheet opened over a History row prints the same two lists from the same payload. */

  readonly reviewNote = computed(() => {
    const r = this.result();
    if (r.classification === 'DETECTION_UNAVAILABLE') {
      return `Every lane failed.${r.error ? ` ${r.error}` : ''}`;
    }
    return reviewReason(r);
  });

  /** A candidate that was never called is a different fact from one that was called and failed, so
      the two must not read the same. */
  attemptLabel(a: FailoverAttempt): string {
    if (a.skipped) return `${a.to} — not called: ${a.skipped}`;
    return a.ok ? `${a.to} — answered` : `${a.to} — ${a.error_code || 'no verdict'}`;
  }
}
