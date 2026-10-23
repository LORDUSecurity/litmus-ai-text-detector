import { Component, OnInit, Output, EventEmitter, inject, signal } from '@angular/core';
import { BackendService } from '../../core/backend.service';
import { CircularMetric } from '../circular-metric/circular-metric';
import { HistorySheet } from '../history-sheet/history-sheet';
import type { DetectionRow } from '../../core/backend.types';
import type { DetectionResult } from '../../core/detection.types';
import { verdictLabel, verdictPillClass, verdictTone } from '../../core/verdict-words';

/** The signed-in user's own saved runs (the backend scopes the list to the session, and
    reads someone else's id as 404). Clicking a row opens that run in a sheet over the list — the
    ring, its scale, and the text it scored with the model's quotes painted on it — so reading a
    past verdict does not move you off the page you were reading. The sheet carries one way onward:
    "Open on Detect" hands the stored verdict to the shell, which is what the `reopen` output below
    is for. Runs saved before the backend kept the analyzed text still open; the sheet says the
    writing predates storage instead of showing an empty panel. */
@Component({
  selector: 'app-history-view',
  standalone: true,
  imports: [CircularMetric, HistorySheet],
  template: `
    <section class="panel">
      <div class="panel-head">
        <h2>Your history</h2>
        <div class="panel-actions">
          <button type="button" class="ghost" (click)="reload()" [disabled]="loading()">
            {{ loading() ? 'Loading…' : 'Refresh' }}
          </button>
        </div>
      </div>

      @if (loading() && rows().length === 0) {
        <p class="empty">Loading your saved runs…</p>
      } @else if (rows().length === 0) {
        <p class="empty">
          Nothing saved yet. Run a detection on the Detect tab and it will appear here, private to
          your account.
        </p>
      } @else {
        <div class="history-list">
          @for (r of rows(); track r.id) {
            <div class="history-row" (click)="open(r)" role="button" tabindex="0"
                 (keydown.enter)="open(r)"
                 (keydown.space)="open(r); $event.preventDefault()">
              <span class="verdict-pill" [class]="pillClass(r.classification)">
                {{ label(r.classification) }}
              </span>
              <!-- A run that never reached a model has no score at all. Rendering the ring with
                   null coerced to 0 showed "0% AI prob." next to a Human-looking pill, which is
                   the one reading that is definitely wrong: 0% is a confident HUMAN answer, not a
                   missing one. Un-scored rows get a grey "not scored" block instead. -->
              @if (r.ai_probability === null) {
                <div class="ring-metric no-score">
                  <span class="ns-ring" aria-hidden="true"></span>
                  <span class="ns-text">not scored</span>
                </div>
              } @else {
                <div class="ring-metric">
                  <app-circular-metric [compact]="true" unit="%" [value]="r.ai_probability"
                    [tone]="tone(r.classification)" label="AI prob." />
                </div>
              }
              <div class="h-main">
                <div class="h-model">{{ r.primary_model || 'model' }}{{ r.secondary_model ? ' + ' + r.secondary_model : '' }}</div>
                <div class="h-when">
                  {{ when(r.created_at) }} · {{ chars(r.text_length) }}
                  @if (r.latency_ms) { · {{ dur(r.latency_ms) }} }
                  <!-- The ring shows the deciding lane's own score, and the tie-break lane sits on
                       a different scale — it calls AI only at its own cut, well above the primary's.
                       A row decided there has to say so, or 85 % next to a Human pill reads as a
                       corrupt save rather than a correct answer on the other scale. -->
                  @if (r.ai_probability !== null && r.deciding_lane === 'secondary') {
                    · tie-break scale
                  }
                  @if (r.ai_probability === null && r.http_status) { · HTTP {{ r.http_status }} }
                </div>
              </div>
            </div>
          }
        </div>
      }
    </section>

    @if (sheet(); as s) {
      <app-history-sheet [row]="s.row" [result]="s.result" [text]="s.text"
                         (closed)="closeSheet()" (reopen)="reopenFromSheet($event)" />
    }
  `,
})
export class HistoryView implements OnInit {
  private readonly backend = inject(BackendService);

  readonly rows = signal<DetectionRow[]>([]);
  readonly loading = signal(false);

  /** The run whose sheet is open. Null means the list is on screen alone. */
  readonly sheet = signal<{
    row: DetectionRow;
    result: DetectionResult | null;
    text: string | null;
  } | null>(null);

  /** Emits the stored verdict with the writing it was reached on, so the shell can reopen the run on
      the Detect tab rather than only its number. */
  @Output() reopen = new EventEmitter<{ result: DetectionResult; text: string | null }>();

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.loading.set(true);
    this.backend.detections(50).subscribe({
      next: (rows) => {
        this.rows.set(rows);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  open(row: DetectionRow): void {
    this.backend.detection(row.id).subscribe({
      next: (d) => {
        // Only a stored verdict can be reopened. A failed run stores the error body as its payload
        // ({"error", "detail", "config_used"}), and handing that to a verdict panel — or to the
        // Detect tab — would build a score out of a non-result. The sheet still opens, because the
        // reader clicked this row and an unresponsive click is its own bug; it just shows no verdict.
        const p = d.payload as unknown as DetectionResult | null;
        const verdict =
          p && typeof p === 'object' && typeof p.classification === 'string' ? p : null;
        this.sheet.set({ row, result: verdict, text: d.text ?? null });
      },
      error: () => this.sheet.set({ row, result: null, text: null }),
    });
  }

  closeSheet(): void {
    this.sheet.set(null);
  }

  /** Leaving the list for the Detect tab has to dismiss the sheet, or it is still mounted over a
      page the reader cannot see and greets them when they come back. */
  reopenFromSheet(reopened: { result: DetectionResult; text: string | null }): void {
    this.sheet.set(null);
    this.reopen.emit(reopened);
  }

  label(c: string | null): string {
    return verdictLabel(c);
  }

  pillClass(c: string | null): string {
    return verdictPillClass(c);
  }

  tone(c: string | null): 'red' | 'green' | 'amber' | 'neutral' {
    return verdictTone(c);
  }

  when(iso: string): string {
    const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
    return isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  /** 120023 ms reads as nothing; 2 min is what actually happened. Under a second keep ms. */
  dur(ms: number | null): string {
    if (!ms || ms < 1) return '';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
    return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  }

  /** The length is only known when the workflow ran; a transport failure never got there. */
  chars(n: number | null): string {
    return n === null || n === undefined ? 'length not recorded' : `${n.toLocaleString()} chars`;
  }
}
