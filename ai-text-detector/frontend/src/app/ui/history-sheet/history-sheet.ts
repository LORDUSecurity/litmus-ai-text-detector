import {
  ChangeDetectionStrategy,
  Component,
  computed,
  ElementRef,
  afterNextRender,
  input,
  output,
  viewChild,
} from '@angular/core';
import type { DetectionResult } from '../../core/detection.types';
import type { DetectionRow } from '../../core/backend.types';
import { probScaleNote } from '../../core/prob-scale';
import { verdictLabel, verdictPillClass, verdictTone } from '../../core/verdict-words';
import { CircularMetric } from '../circular-metric/circular-metric';
import { EvidenceText } from '../evidence/evidence-text';
import { LaneList } from '../lane-list/lane-list';
import { paintEvidence } from '../evidence/evidence-segments';

/** One saved run, read in place: the score the run reached, the models that reached it, and the
    writing it was reached on, with the model's own quotes painted back onto it. It opens over the
    history list rather than switching to the Detect tab, because re-reading a past run is a lookup,
    and the lookup should not cost the user the position they were reading from.

    What the run is made of lives here — the verdict, its magnitudes on their dials, which models
    answered and which said no — and nothing else. The sampling parameters, the endpoints the request
    was sent to and the raw payload are not part of an answer and are not repeated here.

    "Open on Detect" carries the stored writing with the verdict, so the reader can re-run it, edit
    it, or read the paint at full width. That is the one onward move the sheet offers.

    The text can legitimately be missing: runs saved before the backend kept it (schema v7) carry
    only a fingerprint and a length. Saying so is the honest render — an empty panel would read as
    a corrupt save, and repainting nothing while quoting the model's strengths would be worse. */
@Component({
  selector: 'app-history-sheet',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CircularMetric, EvidenceText, LaneList],
  host: { '(document:keydown.escape)': 'closed.emit()' },
  template: `
    <div class="sheet-scrim" (click)="closed.emit()"></div>
    <section class="sheet" role="dialog" aria-modal="true" aria-label="Saved run"
             tabindex="-1" #panel>
      <header class="sheet-head">
        <span class="verdict-pill" [class]="pill()">{{ label() }}</span>
        <button type="button" class="ghost sheet-close" (click)="closed.emit()">Close</button>
      </header>

      <div class="sheet-score">
        @if (row().ai_probability === null) {
          <!-- No score is not zero: a ring at 0% is a confident HUMAN answer, and this run never
               reached a model at all. -->
          <div class="no-score">
            <span class="ns-ring" aria-hidden="true"></span>
            <span class="ns-text">not scored</span>
          </div>
        } @else {
          <div class="readout-rings">
            <app-circular-metric class="ring-metric ring-lead" unit="%" [value]="probability()"
                                 [displayText]="figure()" [tone]="tone()" [label]="scaleLabel()" />
            @if (confidence() !== null && confidence() !== undefined) {
              <app-circular-metric class="ring-metric" [compact]="true" unit="%"
                                   [value]="confidence()" tone="neutral" label="Confidence" />
            }
          </div>
          @if (scaleNote()) { <p class="scale-note">{{ scaleNote() }}</p> }
        }
      </div>

      @if (result()) { <app-lane-list [result]="result()" /> }

      @if (text()) {
        <div class="ev-text sheet-text"><app-evidence-text [segments]="segments()" /></div>
        @if (unmatched()) {
          <p class="ev-unmatched">
            {{ unmatched() }} of {{ phraseCount() }} quoted phrases could not be found verbatim in
            the text, so they are not painted above.
          </p>
        }
      } @else if (result()) {
        <p class="sheet-empty">
          This run was saved before the analyzed text was stored, so its highlights cannot be
          painted here. The score above is the run's own.
        </p>
      } @else {
        <p class="sheet-empty">
          This run never reached a verdict, so there is no evidence to paint — only the summary the
          list already shows.
        </p>
      }

      @if (result()) {
        <footer class="sheet-foot">
          <button type="button" class="ghost sheet-reopen" (click)="sendToDetect()">
            Open on Detect
          </button>
        </footer>
      }
    </section>
  `,
})
export class HistorySheet {
  readonly row = input.required<DetectionRow>();
  /** The stored verdict, or null when the row's payload is an error body rather than a result. */
  readonly result = input<DetectionResult | null>(null);
  /** What the run scored, verbatim — null for a row saved before schema v7. */
  readonly text = input<string | null>(null);

  readonly closed = output<void>();
  /** Hands the stored verdict and the writing it was reached on to the shell, so the Detect tab
      reopens the run the way it was scored rather than as a bare number. */
  readonly reopen = output<{ result: DetectionResult; text: string | null }>();

  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  constructor() {
    // The dialog is only reachable by keyboard if focus lands inside it: the list behind the scrim
    // is still in the tab order, and leaving the reader's caret there means Escape and Tab both act
    // on what they cannot see.
    afterNextRender(() => this.panel().nativeElement.focus());
  }

  readonly label = computed(() => verdictLabel(this.row().classification));
  readonly pill = computed(() => verdictPillClass(this.row().classification));
  readonly tone = computed(() => verdictTone(this.row().classification));
  readonly scaleNote = computed(() => {
    const r = this.result();
    return r ? probScaleNote(r) : null;
  });

  /** The row's own number is what was stored as the answer, so the dial is driven from it and not
      from a recomputation of the payload — the two can differ on a row saved by an older engine. */
  readonly probability = computed(() => this.row().ai_probability);
  readonly confidence = computed(() => this.result()?.confidence ?? null);
  readonly figure = computed(() => {
    const p = this.probability();
    return p === null ? '—' : (p * 100).toFixed(1);
  });
  readonly scaleLabel = computed(() => {
    switch (this.result()?.component_scores?.deciding_lane) {
      case 'secondary':
        return 'AI prob · tie-break scale';
      case 'primary':
        return 'AI prob · primary scale';
      default:
        return 'AI prob.';
    }
  });

  /** The same painter the Detect card runs, so a quote painted green in the sheet is green on the
      card, at the strength the model reported and not a re-derived one. */
  private readonly painted = computed(() =>
    paintEvidence(this.result() ?? ({} as DetectionResult), this.text() ?? ''),
  );
  readonly segments = computed(() => this.painted().segments);
  readonly unmatched = computed(() => this.painted().unmatched);
  readonly phraseCount = computed(() => this.painted().phrases.length);

  sendToDetect(): void {
    const r = this.result();
    if (r) this.reopen.emit({ result: r, text: this.text() });
  }
}
