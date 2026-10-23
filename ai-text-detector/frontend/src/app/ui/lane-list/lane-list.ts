import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { DetectionResult } from '../../core/detection.types';
import { answeredLanes, failedLanes } from '../../core/lane-answers';

/** Which models made this run, and which ones did not.
 *
    Drawn on the Detect rail, where a press has to say who answered, and on the sheet opened over a
    stored History row, where the same question is asked a week later about the same payload. One
    copy, because a run credited to a model that returned nothing is a false statement about a
    model, and two lists that must agree are the sort of thing that stops agreeing.
 *
    The rows are deliberately quiet: mono, one line per lane, `decided` marking the lane whose number
    became the verdict. A failure is not a verdict, so it gets a rule in the failure colour rather
    than a hue of its own. */
@Component({
  selector: 'app-lane-list',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (answered().length) {
      <ul class="readout-models">
        @for (m of answered(); track m.lane + m.model) {
          <li [class.decided]="m.decided">
            <span class="rm-lane">{{ m.lane }}</span>
            <code class="rm-id">{{ m.model }}</code>
            @if (m.decided) { <span class="rm-tag">decided</span> }
          </li>
        }
      </ul>
    }

    @if (failures().length) {
      <!-- "401 Invalid API key provided" is the one sentence that tells the reader to go and replace
           a key, and the lane it belongs to names which one. It sits on the face of the answer, not
           in a fold, because on a run that reached no verdict it IS the answer. -->
      <ul class="readout-failures">
        @for (m of failures(); track m.lane + $index) {
          <li [class.skipped]="m.skipped">
            <span class="rf-lane">{{ m.lane }}</span>
            <code class="rf-id">{{ m.model || 'unknown model' }}</code>
            <span class="rf-tag">{{ m.skipped ? 'not called' : (m.error_code || 'failed') }}</span>
            @if (m.error_detail) { <span class="rf-detail">{{ m.error_detail }}</span> }
          </li>
        }
      </ul>
    }
  `,
})
export class LaneList {
  readonly result = input<DetectionResult | null>(null);

  readonly answered = computed(() => answeredLanes(this.result()));
  readonly failures = computed(() => failedLanes(this.result()));
}
