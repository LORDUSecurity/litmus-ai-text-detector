import { Component, ElementRef, EventEmitter, OnDestroy, OnInit, Output, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { DetectionService } from '../../core/detection.service';
import { CircularMetric } from '../circular-metric/circular-metric';
import type {
  ConfigProvider,
  DiscoveryJob,
  PoolEntry,
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
                  <!-- "Score with this" pins one model; the pool is the ordered list detection
                       falls back through when a model stops answering. Both are the user's call. -->
                  <div class="pool-actions">
                    @if (m.in_pool) {
                      <button class="ghost" type="button" (click)="removeFromPool(m.provider, m.model)">
                        In your pool at #{{ m.your_rank }} — remove
                      </button>
                    } @else {
                      <button class="ghost" type="button" (click)="addToPool(m)">
                        Add to using pool
                      </button>
                    }
                    @if (m.health && m.health !== 'unknown') {
                      <span class="chip" [class.good]="m.health === 'ok'"
                            [class.warn]="m.health === 'degraded'"
                            [class.bad]="m.health === 'down'">
                        {{ m.health }}@if (m.consecutive_failures) { · {{ m.consecutive_failures }} failed }
                      </span>
                    }
                  </div>
                }
              </div>
            </article>
          }
        </div>
      }
    </section>

    @if (signedIn()) {
      <section class="panel pool-panel">
        <div class="panel-head">
          <h2>Your using pool</h2>
          <span class="muted">
            The order detection follows. Drag to rank — a model that stops answering is served
            last until it answers again, and your rank comes back untouched.
          </span>
        </div>

        @if (poolBusy()) {
          <p class="empty">Saving the order…</p>
        }
        @if (poolMsg(); as pm) {
          <p class="doc-status" [class.bad]="pm.bad" aria-live="polite">{{ pm.text }}</p>
        }

        @if (pool().length === 0) {
          <p class="empty">
            Nothing in your pool yet. Detection runs on the deployed lane. Add models from the
            ranking above — or let discovery find the free ones your key can reach.
          </p>
        } @else {
          <ol class="pool-list">
            @for (p of pool(); track p.provider + '/' + p.model; let i = $index) {
              <li class="pool-row" [class.demoted]="p.demoted" [class.dragging]="dragIndex() === i"
                  [class.drop-before]="overIndex() === i"
                  draggable="true"
                  (dragstart)="onDragStart(i, $event)"
                  (dragover)="onDragOver(i, $event)"
                  (drop)="onDrop(i, $event)"
                  (dragend)="onDragEnd()">
                <span class="grip" aria-hidden="true">⋮⋮</span>
                <span class="pool-pos">{{ i + 1 }}</span>
                <span class="grow">
                  <strong>{{ p.model }}</strong>
                  <span class="hint"> · {{ p.provider }}</span>
                  @if (p.source === 'auto') { <span class="chip">discovered</span> }
                  @if (p.health !== 'ok' && p.health !== 'unknown') {
                    <span class="chip" [class.warn]="p.health === 'degraded'"
                          [class.bad]="p.health === 'down'"
                          [title]="p.last_error">
                      {{ p.health }} · {{ p.consecutive_failures }} in a row
                    </span>
                  }
                  @if (p.demoted) {
                    <span class="hint demote-note">
                      you ranked it #{{ p.your_rank }} — served last because it stopped answering
                    </span>
                  }
                </span>
                <!-- Keyboard path for the drag: the order is the whole feature, so it cannot
                     depend on a pointer being available. -->
                <button type="button" class="ghost icon-only" [disabled]="i === 0 || poolBusy()"
                        [attr.aria-label]="'Move ' + p.model + ' up'" (click)="move(i, i - 1)">↑</button>
                <button type="button" class="ghost icon-only"
                        [disabled]="i === pool().length - 1 || poolBusy()"
                        [attr.aria-label]="'Move ' + p.model + ' down'" (click)="move(i, i + 1)">↓</button>
                <button type="button" class="ghost danger" [disabled]="poolBusy()"
                        [attr.aria-label]="'Remove ' + p.model + ' from your using pool'"
                        (click)="removeFromPool(p.provider, p.model)">Remove</button>
              </li>
            }
          </ol>
        }

        <div class="discover">
          <div class="panel-head">
            <h3>Find free models on your key</h3>
            <span class="muted">
              Lists what the endpoint offers, then screens the untested ones on
              {{ screenTexts() }} real texts through the same engine path a detection uses.
            </span>
          </div>
          @if (discoverable().length === 0) {
            <p class="empty">
              Add and verify a key above first — discovery spends your own budget, so it never
              runs on the shared key.
            </p>
          } @else {
            <div class="discover-actions">
              @for (p of discoverable(); track p.id) {
                <button type="button" class="ghost" [disabled]="discovering() === p.id"
                        (click)="discover(p.id)">
                  {{ discovering() === p.id ? 'Screening…' : 'Discover ' + (p.label || p.name) }}
                </button>
              }
            </div>
          }
          @if (jobs().length) {
            <ul class="job-list">
              @for (j of jobs(); track j.id) {
                <li class="job-row" [class.bad]="j.status === 'failed'">
                  <span class="chip" [class.good]="j.status === 'done'"
                        [class.warn]="j.status === 'running' || j.status === 'queued'">
                    {{ j.status }}
                  </span>
                  <strong>{{ j.provider }}</strong>
                  <span class="hint">
                    {{ j.n_listed }} listed · {{ j.n_screened }} screened · {{ j.n_usable }} usable
                    @if (j.stage) { · {{ j.stage }} }
                    @if (j.error) { · {{ j.error }} }
                  </span>
                </li>
              }
            </ul>
            <p class="hint">{{ discoveryNote() }}</p>
          }
        </div>
      </section>
    }

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
export class RankingView implements OnInit, OnDestroy {
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
    this.loadPool();
    this.loadJobs();
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
          // A verified key also queues discovery on the server, so say so here rather than
          // letting models appear in the ranking later with no stated reason.
          const disc = k.discovery?.queued
            ? ' Discovery is now screening that endpoint\'s free models — see "Find free models" below.'
            : '';
          this.keyMsg.set({
            text: (k.verified
              ? 'Key saved and verified working — detection will use it automatically.'
              : 'Key saved. It was not verified (no model supplied or the check failed); detection can still use it.') + disc,
            bad: false,
          });
          this.loadKeys();
          this.loadJobs();
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

  // ---- the using pool: the user's order, and what health did to it ---------
  //
  // Two different orders are on screen and the panel has to keep them apart. `your_rank` is
  // where the user put a model; the list itself is rendered in *served* order, which pushes a
  // model that stopped answering to the back. The backend derives that at read time and never
  // rewrites `position`, so a recovery restores the user's ranking with nothing to undo.

  readonly pool = signal<PoolEntry[]>([]);
  readonly poolBusy = signal(false);
  readonly poolMsg = signal<{ text: string; bad: boolean } | null>(null);
  readonly dragIndex = signal<number | null>(null);
  readonly overIndex = signal<number | null>(null);

  private loadPool(): void {
    if (!this.signedIn()) return;
    this.backend.myPool().subscribe({
      next: (r) => this.pool.set(r.pool ?? []),
      error: () => this.pool.set([]),
    });
  }

  addToPool(m: RankedModel): void {
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend.addToPool(m.provider, m.model).subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        this.poolMsg.set({
          text: `${m.model} added at the end of your pool. Drag it up to make it the first tried.`,
          bad: false,
        });
        this.refreshRanking();
      },
      error: (e) => {
        this.poolBusy.set(false);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  removeFromPool(provider: string, model: string): void {
    this.poolBusy.set(true);
    this.backend.removeFromPool(provider, model).subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        this.poolMsg.set(null);
        this.refreshRanking();
      },
      error: (e) => {
        this.poolBusy.set(false);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  /** Write a new order. The list is moved locally first so the drag feels immediate, then
      confirmed from the server's answer — the response is the authority, not the guess. */
  move(from: number, to: number): void {
    const rows = this.pool();
    if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return;
    const next = [...rows];
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    this.pool.set(next);
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend
      .setPoolOrder(next.map((p) => ({ provider: p.provider, model: p.model })))
      .subscribe({
        next: (r) => {
          this.pool.set(r.pool ?? []);
          this.poolBusy.set(false);
        },
        error: (e) => {
          this.poolBusy.set(false);
          this.poolMsg.set({ text: this.describe(e), bad: true });
          this.loadPool();   // the server kept the old order; show that, not the local guess
        },
      });
  }

  onDragStart(i: number, e: DragEvent): void {
    this.dragIndex.set(i);
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(i));
    }
  }

  onDragOver(i: number, e: DragEvent): void {
    e.preventDefault();               // without this the browser refuses the drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    this.overIndex.set(i);
  }

  onDrop(i: number, e: DragEvent): void {
    e.preventDefault();
    const from = this.dragIndex();
    this.onDragEnd();
    if (from !== null) this.move(from, i);
  }

  onDragEnd(): void {
    this.dragIndex.set(null);
    this.overIndex.set(null);
  }

  private refreshRanking(): void {
    this.backend.ranking().subscribe({
      next: (r) => {
        this.ranked.set(r.ranked);
        if (r.pool) this.pool.set(r.pool);
      },
      error: () => undefined,
    });
  }

  // ---- discovery: bring the free models this key can reach ----------------

  readonly jobs = signal<DiscoveryJob[]>([]);
  readonly discoveryNote = signal('');
  readonly screenTexts = signal('2');
  readonly discovering = signal<number | null>(null);

  private pollHandle: ReturnType<typeof setTimeout> | null = null;
  private readonly finishedSeen = new Set<number>();

  /** Endpoints the caller has a key for. Discovery is offered only on those: it runs on the
      caller's credential, so a provider without one can only answer 409. */
  readonly discoverable = computed(() => {
    const owned = new Set(this.keys().map((k) => k.provider_id));
    return this.providers().filter((p) => owned.has(p.id));
  });

  discover(providerId: number): void {
    this.discovering.set(providerId);
    this.poolMsg.set(null);
    this.backend.discover(providerId).subscribe({
      next: (r) => {
        this.discovering.set(null);
        // `queued: false` is not a failure to report as one, and not a success to hide: the
        // backend refused to write a job no worker would pick up, and says why.
        this.poolMsg.set({
          text: r.queued
            ? 'Screening started. Each model is run on 2 real texts through the detection engine, so this takes a few minutes.'
            : `Not queued: ${r.reason || r.error || r.status}`,
          bad: !r.queued,
        });
        this.loadJobs();
      },
      error: (e) => {
        this.discovering.set(null);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  private loadJobs(): void {
    if (!this.signedIn()) return;
    this.backend.discoveryStatus().subscribe({
      next: (s) => {
        this.jobs.set(s.jobs ?? []);
        this.discoveryNote.set(s.note || '');
        if (s.screen_texts) this.screenTexts.set(s.screen_texts);
        // A finished job may have written ranking rows: re-read once per job, not on every poll.
        let newlyDone = false;
        for (const j of s.jobs ?? []) {
          if ((j.status === 'done' || j.status === 'failed') && !this.finishedSeen.has(j.id)) {
            this.finishedSeen.add(j.id);
            if (j.status === 'done') newlyDone = true;
          }
        }
        if (newlyDone) this.refreshRanking();
        this.schedulePoll(s.jobs ?? []);
      },
      error: () => this.schedulePoll([]),
    });
  }

  /** Poll only while something can still change. An idle page that keeps asking every four
      seconds is a request budget spent on nothing. */
  private schedulePoll(jobs: DiscoveryJob[]): void {
    if (this.pollHandle !== null) {
      clearTimeout(this.pollHandle);
      this.pollHandle = null;
    }
    const pending = jobs.some((j) => j.status === 'queued' || j.status === 'running');
    if (!pending) return;
    this.pollHandle = setTimeout(() => this.loadJobs(), 4_000);
  }

  ngOnDestroy(): void {
    if (this.pollHandle !== null) {
      clearTimeout(this.pollHandle);
      this.pollHandle = null;
    }
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
