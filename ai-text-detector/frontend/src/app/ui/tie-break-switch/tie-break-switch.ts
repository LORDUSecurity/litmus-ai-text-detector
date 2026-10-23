import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import type { ModelView, N8nStatus, ProviderView } from '../../core/backend.types';

/**
 * The one switch that turns the second opinion ON.
 *
 * It sits on `Models & keys`, not on Advanced parameters, because it is a fact about the credentials
 * and models this account spends: which row is dialled when the first one defers. What stays on
 * Advanced is the chooser — the per-model bands and the lane each row is assigned to.
 *
 * The switch writes `models.enabled` on the row that owns the secondary lane, and nothing else can:
 * a request body is only allowed to switch a lane OFF (see `detect()` in backend/app.py), so a
 * toggle that only wrote localStorage would look saved while every detection kept running the
 * shipped single-lane default. That was the defect this replaced.
 */
@Component({
  selector: 'app-tie-break-switch',
  template: `
    <article class="card lane-card">
      <header class="card-head">
        <h3>Second lane (tie-break)</h3>
        <span class="pill" [class.pill-off]="!laneOn()">{{ laneOn() ? 'on' : 'off' }}</span>
      </header>

      @if (secondaryRow(); as row) {
        <label class="lane-switch">
          <input type="checkbox" name="secondary_lane" [checked]="laneOn()"
                 (change)="setSecondaryLane($any($event.target).checked)" />
          Ask a second model when the first one defers
        </label>
        <p class="hint">
          This writes <code>models.enabled</code> on {{ row.model_id }} (row {{ row.id }}), the column
          the backend reads before it dials a second model at all. It is one switch for every user of
          this install, not a setting for this browser.
        </p>
        <p class="hint lane-note">
          While the run starts from your pool, the lane follows that ranking: it starts at the pool
          entry right below the model that answered — never the model that just scored the text,
          which cannot corroborate itself.
          @if (laneWalks()) {
            On this install the lane then keeps going down the pool while a leg gives no answer, the
            same way the first lane does, until this request's time budget runs out.
          } @else {
            This deploy scores through the n8n workflow, so a leg that gives no answer does not get
            replaced by the next one: walking the pool needs the in-process engine.
          }
          This row's own model answers only where the ranking has nothing to say: the primary is your
          last pool entry, or you pinned it with "Score with this model", which skips the pool
          entirely and dials {{ row.model_id }} alone. Switch the lane off and both of those runs
          come back as REVIEW instead of asking.
        </p>
        @if (message(); as m) {
          <p class="doc-status" [class.bad]="m.tone === 'bad'" aria-live="polite">{{ m.text }}</p>
        }
      } @else {
        <p class="hint lane-note">
          There is nothing for this switch to write: no model row is tagged secondary. Assign one in
          the lane column of a model table on the {{ tabName() }} surface, then come back.
        </p>
      }
    </article>
  `,
})
export class TieBreakSwitch implements OnInit {
  private readonly backend = inject(BackendService);
  private readonly auth = inject(AuthService);

  readonly models = signal<ModelView[]>([]);
  readonly engine = signal<N8nStatus | null>(null);
  readonly laneOn = signal(false);
  readonly message = signal<{ tone: 'ok' | 'bad'; text: string } | null>(null);

  /** The `models` row that owns the tie-break lane. */
  readonly secondaryRow = computed(
    () => this.models().find((m) => m.lane === 'secondary') ?? null,
  );

  /** Whether this install answers `/detect` inside its own process — the one fact that decides
      whether a leg that does not answer is replaced by the next pooled model. `undefined` (no
      status read yet) reads as "still on the workflow", which is the hosted case and the one that
      cannot be edited away by accident. */
  readonly nativeEngine = computed(() => this.engine()?.on_request_path === false);

  readonly laneWalks = computed(() => this.nativeEngine());

  /** The empty state points at the surface that can assign a lane, and that surface is called two
      different things: "Advanced parameters" on a single-owner install, "Admin" on the hosted one. */
  readonly tabName = computed(() => (this.auth.local() ? 'Advanced parameters' : 'Admin'));

  ngOnInit(): void {
    this.reload();
    this.backend.n8nStatus().subscribe({
      next: (s) => this.engine.set(s),
      error: () => this.engine.set(null),
    });
  }

  reload(): void {
    this.backend.providers().subscribe({
      next: (rows: ProviderView[]) => {
        this.models.set(rows.flatMap((p) => p.models));
        // The row is the authority for the switch, so every read re-derives it. A write the server
        // refused puts the box back through this same path.
        this.laneOn.set(this.secondaryRow()?.enabled ?? false);
      },
      error: () => this.message.set({ tone: 'bad', text: 'Could not read the model rows.' }),
    });
  }

  setSecondaryLane(on: boolean): void {
    const row = this.secondaryRow();
    if (!row) return;
    this.laneOn.set(on);
    this.backend.updateModel(row.id, { enabled: on }).subscribe({
      next: () => {
        // The written value is kept on the row rather than re-read: a refused write later rolls the
        // box back to `row.enabled`, and that has to be the state the server agreed to, not the one
        // this card loaded when it opened.
        this.models.update((rows) =>
          rows.map((m) => (m.id === row.id ? { ...m, enabled: on } : m)));
        this.message.set({
          tone: 'ok',
          text: `The tie-break lane is ${on ? 'on' : 'off'} for every user of this install.`
            + (on ? '' : ' Gray-zone answers come back as REVIEW instead of a second call.'),
        });
      },
      error: (err) => {
        // Nothing was written, so the switch goes back to the state the server is running.
        this.laneOn.set(row.enabled);
        this.message.set({ tone: 'bad', text: `The second lane was not changed. ${this.detail(err)}` });
      },
    });
  }

  private detail(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      return (err.error as { error?: string })?.error ?? err.message;
    }
    return String(err);
  }
}
