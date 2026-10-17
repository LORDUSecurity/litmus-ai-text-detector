import { Component, ElementRef, EventEmitter, OnInit, Output, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { DetectionService } from '../../core/detection.service';
import { CircularMetric } from '../circular-metric/circular-metric';
import type {
  ConfigProvider,
  PublicConfig,
  RankedModel,
  RankingResponse,
  UserKeyView,
} from '../../core/backend.types';

/** Replaces the old "Free-model research" tab. It ranks every measured model at its OWN
    calibrated band, tells each signed-in user whether a working key is already in hand
    (theirs, verified → auto-picked by /detect; or the admin's shared key), lets them add a
    key for an endpoint the admin allows, points at small guides for getting one, and links
    straight to the paper. Users never touch thresholds or sampling — those stay admin-set. */
@Component({
  selector: 'app-ranking-view',
  standalone: true,
  imports: [CircularMetric],
  styles: `
    /* The fresh-install state has to be explained, not just rendered empty: 48 ranking cards,
       zero pick buttons. Blue glass like the rest of the system, and it reads in both themes
       because it uses the shared tokens. */
    .lock-note {
      display: flex;
      gap: 12px;
      align-items: flex-start;
      margin: 0 0 18px;
      padding: 14px 16px;
      border: 1px solid var(--line-strong);
      border-radius: 14px;
      background: var(--glass-sheen), color-mix(in srgb, var(--accent) 10%, transparent);
      backdrop-filter: blur(var(--glass-blur));
    }
    .lock-note .ico { flex: 0 0 auto; margin-top: 2px; }
    .lock-note strong { display: block; margin-bottom: 4px; }
    .lock-note p { margin: 0 0 8px; line-height: 1.45; }
    .rank-lock {
      margin: 8px 0 0;
      font-size: 12px;
      color: var(--muted, rgba(150, 170, 210, 0.85));
    }
  `,
  template: `
    <section class="panel">
      <div class="ranking-head">
        <div>
          <h2>Model ranking</h2>
          <p class="muted" style="margin:4px 0 0">
            Scored on real captured corpora at each model's own calibrated band. Full captures
            first — fewest wrong, then widest coverage — then probes on a short sample.
            Green ring = a working key is already in hand. Pick one to score your text with it.
          </p>
        </div>
        <a class="paper-link" [href]="paperUrl()" target="_blank" rel="noopener noreferrer">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 3h9l4 4v14H6z" /><path d="M14 3v5h5M9 13h7M9 17h7" />
          </svg>
          {{ paperTitle() }}
        </a>
      </div>

      @if (noScoringKey()) {
        <!-- Measured on a packaged first boot, 2026-10-16: 48 cards, zero pick buttons, and the page
             said nothing. The state is legitimate (detection still runs on the deployed lane, because
             the workflow's own credential pays for it), so it needs the reason and the way out, not an
             error. -->
        <div class="lock-note" role="status">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" />
          </svg>
          <div>
            <strong>Nothing can be picked yet — no verified key for these endpoints.</strong>
            <p class="muted">
              Detection still works: the deployed lane scores your text without any setting of yours.
              What a key unlocks is choosing <em>which</em> model scores it.
              @if (signedIn()) {
                Paste one into "Your API keys" below — the guides link straight to each provider's key
                page — and the cards turn green and gain a pick button as soon as it verifies.
              } @else {
                Sign in first: keys belong to your account, which is also what saves your history.
              }
            </p>
            @if (signedIn()) {
              <button type="button" class="ghost" (click)="focusKeyForm()">Add a key ↓</button>
            }
          </div>
        </div>
      }

      @if (loading()) {
        <p class="empty">Loading the ranking…</p>
      } @else if (error(); as e) {
        <p class="auth-error" role="alert">{{ e }}</p>
      } @else if (ranked().length === 0) {
        <p class="empty">
          No measured models yet. An admin imports benchmark captures from the Admin tab before
          the ranking can be shown.
        </p>
      } @else {
        <div class="rank-grid">
          @for (m of ranked(); track m.model + m.tag + m.rank) {
            <article class="rank-card" [class.deployed]="m.lane !== null">
              <span class="rank-badge">#{{ m.rank }}</span>
              <div class="ring-metric">
                <app-circular-metric
                  [value]="m.accuracy"
                  [tone]="m.verified_key ? 'green' : (m.admin_key ? 'blue' : 'amber')"
                  [label]="m.accuracy === null ? 'no score' : 'accuracy'" />
              </div>
              <div class="rank-body">
                <h3>{{ m.model }}</h3>
                <div class="rank-meta">
                  <span class="chip">{{ m.provider_name || m.provider }}</span>
                  @if (m.free_tier) { <span class="chip free">free tier</span> }
                  @if (m.lane) { <span class="chip lane">deployed · {{ m.lane }}</span> }
                  @if (m.provisional) {
                    <span class="chip warn" [title]="'Only ' + m.n_clean + ' texts measured - below the sample floor'">
                      probe
                    </span>
                  }
                  @if (m.verified_key) {
                    <span class="chip good">your key ready</span>
                  } @else if (m.admin_key) {
                    <span class="chip good">shared key</span>
                  } @else {
                    <span class="chip">add a key</span>
                  }
                </div>
                <div class="rank-stats">
                  <span><b>{{ m.correct }}</b> right</span>
                  <span><b>{{ m.wrong }}</b> wrong</span>
                  <span><b>{{ m.review }}</b> review</span>
                  <span>coverage <b>{{ pct(m.coverage) }}</b></span>
                  <span>on <b>{{ m.n_clean }}</b> texts</span>
                </div>
                @if (!(m.verified_key || m.admin_key)) {
                  <p class="rank-lock">
                    No key for {{ m.provider_name || m.provider }} — this one stays un-pickable.
                  </p>
                }
                @if (m.verified_key || m.admin_key) {
                  <!-- The ranking's payoff: a model with a key behind it can be run. Only the
                       id travels; the backend resolves the endpoint, the model's own calibrated
                       band and the key, so this is not an advanced-parameter control. -->
                  <button class="pick-btn" type="button"
                          [class.active]="pickedModel() === m.model"
                          (click)="pick(m)">
                    @if (pickedModel() === m.model) {
                      <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
                      Scoring with this model — switch back
                    } @else {
                      Score with this model
                    }
                  </button>
                }
              </div>
            </article>
          }
        </div>
      }
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Your API keys</h2>
        <span class="muted">Bring your own key for an allowed endpoint — it is auto-picked when verified.</span>
      </div>

      @if (keys().length) {
        <div class="byok" style="margin-bottom:16px">
          @for (k of keys(); track k.id) {
            <div class="key-row">
              <div class="grow">
                <strong>{{ k.provider_name || ('provider #' + k.provider_id) }}</strong>
                <span class="hint"> · {{ k.key_hint }} · {{ k.label }}</span>
              </div>
              @if (k.verified) {
                <span class="chip good">verified</span>
              } @else {
                <span class="chip">not verified</span>
              }
              <button type="button" class="ghost danger" (click)="removeKey(k.id)">Remove</button>
            </div>
          }
        </div>
      }

      <form class="inline-form" (submit)="$event.preventDefault(); saveKey()">
        <label>
          <span>Endpoint</span>
          <select name="provider" [value]="providerId()" (change)="onProviderChange($event)"
                  [disabled]="saving()">
            @for (p of providers(); track p.id) {
              <option [value]="p.id">{{ p.label || p.name }}</option>
            }
          </select>
        </label>
        <label>
          <span>API key</span>
          <input type="password" name="key" [value]="newKey()" (input)="onKeyInput($event)"
                 placeholder="paste your key" autocomplete="off" [disabled]="saving()" />
        </label>
        <label>
          <span>Model to verify with (optional)</span>
          <input name="model" [value]="verifyModel()" (input)="onModelInput($event)"
                 placeholder="e.g. the model id" [disabled]="saving()" />
        </label>
        <button class="primary" type="submit" [disabled]="saving() || !canSave()">
          {{ saving() ? 'Verifying…' : 'Save key' }}
        </button>
      </form>
      @if (keyMsg(); as m) {
        <p class="doc-status" [class.bad]="m.bad" aria-live="polite">{{ m.text }}</p>
      }
      <p class="muted" style="margin:10px 0 0">
        The plaintext is encrypted at rest and never returned to the page — only the hint above.
        When a verified key exists for an endpoint, detection uses it automatically.
      </p>
    </section>

    @if (guides().length) {
      <section class="panel">
        <div class="panel-head">
          <h2>How to get an API key</h2>
          <span class="muted">Short guides and direct links.</span>
        </div>
        <div class="guides">
          @for (g of guides(); track g.provider + g.url) {
            <div class="guide">
              <h4>{{ g.title || g.provider }}</h4>
              @if (g.steps) { <p>{{ g.steps }}</p> }
              <a [href]="g.url" target="_blank" rel="noopener noreferrer">
                Open {{ g.provider }} →
              </a>
            </div>
          }
        </div>
      </section>
    }
  `,
})
export class RankingView implements OnInit {
  private readonly backend = inject(BackendService);
  readonly auth = inject(AuthService);
  private readonly detection = inject(DetectionService);

  /** The shell owns the pick: it records it on DetectionService AND moves to the Detect tab,
      which a ranking card cannot do from inside itself. This view only reports the intent and
      reads the current value back for the "active" styling, so there is one writer. */
  @Output() readonly useModel = new EventEmitter<RankedModel>();
  readonly pickedModel = this.detection.pickedModel;

  pick(m: RankedModel): void {
    this.useModel.emit(m);
  }

  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly ranked = signal<RankedModel[]>([]);
  readonly config = signal<PublicConfig | null>(null);
  readonly keys = signal<UserKeyView[]>([]);

  /** The technical report ships inside this bundle (public/research/), so `/research/report.html` is
      always a valid same-origin target — and the settings validator only accepts an absolute http(s)
      URL for `paper_url`, so an in-site path cannot be stored there. Hence: admin URL = the archival
      copy (a DOI), blank = the copy this deployment serves. Neither state leaves the page with no
      citation. */
  readonly researchPath = '/research/report.html';

  readonly paperUrl = computed(() => this.config()?.paper_url || this.researchPath);

  readonly paperTitle = computed(() => {
    const c = this.config();
    if (!c?.paper_url) return 'Technical report';
    return c.paper_title || 'Read the paper';
  });
  readonly guides = computed(() => this.config()?.api_key_guides ?? []);
  readonly providers = computed(() => this.config()?.providers ?? []);

  private readonly host = inject(ElementRef);

  readonly signedIn = computed(() => this.auth.user() !== null);

  /** Cards a user can actually score with: their own verified key, or the admin's shared key for
      that endpoint. Everything else on the page is informative. */
  readonly pinnable = computed(
    () => this.ranked().filter((m) => !!(m.verified_key || m.admin_key)).length,
  );

  /** True exactly in the fresh-install state: a ranking to show, no key behind any of it. Once one
      card becomes pickable the note is noise, so it is tied to the count, not to the key list. */
  readonly noScoringKey = computed(
    () =>
      !this.loading() &&
      this.error() === null &&
      this.ranked().length > 0 &&
      this.pinnable() === 0,
  );

  /** The note's way out: jump to the form rather than describing it. */
  focusKeyForm(): void {
    const el = this.host.nativeElement?.querySelector?.('input[type=password]') as
      | HTMLInputElement
      | null
      | undefined;
    el?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    el?.focus();
  }

  // add-key form. These are signals, not plain fields, on purpose: the app runs zoneless
  // (no zone.js dependency), so a `computed` over plain fields is cached after its first
  // evaluation and nothing schedules a re-check when the user types. Live symptom on 2026-10-16:
  // typing a real key left the DOM input non-empty while "Save key" stayed disabled forever, so
  // no user could add a key at all. Signal producers make `canSave` invalidate correctly, and the
  // template-bound (input)/(change) listeners schedule change detection.
  readonly providerId = signal<number | null>(null);
  readonly newKey = signal('');
  readonly verifyModel = signal('');
  readonly saving = signal(false);
  readonly keyMsg = signal<{ text: string; bad: boolean } | null>(null);

  readonly canSave = computed(() => this.providerId() !== null && this.newKey().trim().length > 0);

  onProviderChange(e: Event): void {
    const raw = (e.target as HTMLSelectElement).value;
    const id = Number(raw);
    this.providerId.set(raw === '' || !Number.isFinite(id) ? null : id);
  }

  onKeyInput(e: Event): void {
    this.newKey.set((e.target as HTMLInputElement).value);
  }

  onModelInput(e: Event): void {
    this.verifyModel.set((e.target as HTMLInputElement).value);
  }

  ngOnInit(): void {
    this.reload();
  }

  private reload(): void {
    this.loading.set(true);
    this.backend.config().subscribe({
      next: (cfg) => {
        this.config.set(cfg);
        if (this.providerId() === null && cfg.providers.length) {
          this.providerId.set(cfg.providers[0].id);
        }
        this.backend.ranking().subscribe({
          next: (r: RankingResponse) => {
            this.ranked.set(r.ranked);
            this.loading.set(false);
          },
          error: (e) => {
            this.error.set(this.describe(e));
            this.loading.set(false);
          },
        });
      },
      error: (e) => {
        this.error.set(this.describe(e));
        this.loading.set(false);
      },
    });
    this.loadKeys();
  }

  private loadKeys(): void {
    this.backend.myKeys().subscribe({
      next: (k) => this.keys.set(k),
      error: () => this.keys.set([]),
    });
  }

  saveKey(): void {
    if (!this.canSave()) return;
    this.saving.set(true);
    this.keyMsg.set(null);
    const model = this.verifyModel().trim();
    this.backend
      .addMyKey({
        provider_id: this.providerId() ?? undefined,
        api_key: this.newKey().trim(),
        model_id: model || undefined,
      })
      .subscribe({
        next: (k) => {
          this.saving.set(false);
          this.newKey.set('');
          this.verifyModel.set('');
          this.keyMsg.set({
            text: k.verified
              ? 'Key saved and verified working — detection will use it automatically.'
              : 'Key saved. It was not verified (no model supplied or the check failed); detection can still use it.',
            bad: false,
          });
          this.loadKeys();
          // Re-rank so "your key ready" reflects the new credential.
          this.backend.ranking().subscribe({ next: (r) => this.ranked.set(r.ranked) });
        },
        error: (e) => {
          this.saving.set(false);
          this.keyMsg.set({ text: this.describe(e), bad: true });
        },
      });
  }

  removeKey(id: number): void {
    this.backend.deleteMyKey(id).subscribe({
      next: () => {
        this.loadKeys();
        this.backend.ranking().subscribe({ next: (r) => this.ranked.set(r.ranked) });
      },
      error: (e) => this.keyMsg.set({ text: this.describe(e), bad: true }),
    });
  }

  pct(v: number | null): string {
    return v === null ? '—' : Math.round(v * 100) + '%';
  }

  private describe(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      const body = err.error as { error?: string; message?: string } | null;
      const text = body && typeof body === 'object' ? (body.error ?? body.message) : '';
      if (err.status === 0) return 'Cannot reach the backend on /api.';
      return text || `HTTP ${err.status}`;
    }
    return String(err);
  }
}
