import { Component, ElementRef, EventEmitter, OnDestroy, OnInit, Output, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { DetectionService } from '../../core/detection.service';
import { CircularMetric } from '../circular-metric/circular-metric';
import type {
  ConfigProvider,
  DiscoveryJob,
  LaneAuthority,
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
       zero pick buttons. It is a note about the deployment, so it carries no hue — the corner
       cut comes from the shared --chamfer token in styles.css, which is why there is no radius
       here. (No backticks in this comment: it lives inside a template literal, and one would end
       the styles property and break the decorator.) */
    .lock-note {
      display: flex;
      gap: 12px;
      align-items: flex-start;
      margin: 0 0 18px;
      padding: 16px 18px;
      border: 1px solid var(--line-strong);
      background: var(--panel);
    }
    .lock-note .ico { flex: 0 0 auto; margin-top: 2px; }
    .lock-note strong { display: block; margin-bottom: 4px; }
    .lock-note p { margin: 0 0 8px; line-height: 1.45; }
    .rank-lock {
      margin: 8px 0 0;
      font-size: 12px;
      color: var(--muted, #9e9e9e);
    }

    /* "Which model actually runs on my text?" — the question the ranking table never answered,
       and the one the brief makes the page for ("these ranking are the one used in auto order").
       Neutral by the same rule: it states which lane is authoritative, and blue is reserved for
       the control that changes it (the using-pool rows below). */
    .lane-authority {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 10px;
      margin: 0 0 16px;
      padding: 14px 16px;
      border: 1px solid var(--line-strong);
      background: var(--panel);
      font-size: 12.5px;
      color: var(--ink-dim);
    }
    .lane-authority .ico {
      width: 15px;
      height: 15px;
      fill: none;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .lane-authority .la-lead {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--ink-dim);
    }
    .lane-authority .la-model {
      color: var(--ink);
      font-weight: 600;
      word-break: break-word;
    }
    .lane-authority .la-pin {
      border-color: color-mix(in srgb, var(--warn) 55%, transparent);
      color: var(--warn);
    }
    .lane-authority .la-note {
      flex-basis: 100%;
      margin: 2px 0 0;
      line-height: 1.45;
    }

    /* The printable copy of the study, one click from the table that cites it. Outlined rather
       than filled so the archival URL (the primary link) stays the louder of the two. */
    .pdf-link {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 8px 14px;
      border-radius: var(--r-pill);
      text-decoration: none;
      font-size: 12.5px;
      color: var(--accent-2);
      border: 1px solid color-mix(in srgb, var(--royal) 45%, transparent);
      background: linear-gradient(135deg, var(--accent-a08), transparent);
      transition: border-color 0.16s ease, transform 0.12s ease;
    }
    .pdf-link:hover {
      border-color: var(--royal);
      transform: translateY(-1px);
    }
    .pdf-link .ico {
      width: 14px;
      height: 14px;
      fill: none;
      stroke: currentColor;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }

    /* Inline slot editor: changing which model holds a rank is one atomic call, so the row owns
       its own control instead of making the user remove-and-re-add. No radius here — the shared
       --chamfer-sm token cuts this select's two right corners, so the padding has to clear them. */
    .pool-row select.pool-change-target {
      min-width: 150px;
      max-width: 240px;
      padding: 6px 10px;
      font-size: 12px;
      color: var(--ink);
      background: var(--field-bg);
      border: 1px solid var(--edge);
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
        <!-- The same study, printable. The report links it from inside; a user who wants the PDF
             before deciding to read the whole thing should not have to find that out first. -->
        <a class="pdf-link" href="/research/report.pdf" target="_blank" rel="noopener noreferrer">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 3v11M8 10.5l4 4 4-4" /><path d="M5 19h14" />
          </svg>
          PDF
        </a>
      </div>

      @if (authority(); as a) {
        <!-- Rendered only from what /ranking reports, and /ranking builds it from the very
             functions /detect walks. That is the point: #142 shipped a ranking page that named a
             deployed default no request ever reached, because the answer was being re-derived on
             the wrong side of the wire. No "lane_authority" in the response means an older
             backend — so this block says nothing, rather than inventing an answer from the pool it
             happens to have loaded. -->
        <div class="lane-authority" role="status">
          <span class="la-lead">
            <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 12h9" /><path d="M10 7.5l4.5 4.5L10 16.5" /><circle cx="18.5" cy="12" r="2.5" />
            </svg>
            Your next run is scored by
          </span>
          @if (a.next; as n) {
            <strong class="la-model">{{ n.model }}</strong>
            <span class="hint">
              · {{ n.provider }} ·
              {{ n.source === 'user-pool' ? 'first in your using pool' : 'the deployed default' }}
            </span>
            @if (a.failover.length) {
              <span class="hint">— if it stops answering, in your order: {{ failoverList() }}</span>
            } @else {
              <span class="hint">
                — nothing behind it: if it stops answering, this run has no rescue. Add another
                model to your pool and the failover has somewhere to go.
              </span>
            }
          } @else {
            <strong class="la-model">No primary model is enabled</strong>
            <span class="hint">
              · neither a deployed default nor an entry in your using pool, so an untouched Detect
              has no lane to walk. Add a verified key, then put a model in your pool.
            </span>
          }
          <span class="chip">
            Deployed default:
            {{ a.deployed_default ? a.deployed_default.model : 'none set' }}
          </span>
          <span class="chip" [class.good]="!!a.deployed_tie_break">
            tie-break lane {{ a.deployed_tie_break ? a.deployed_tie_break.model : 'off' }}
          </span>
          @if (pickedModel(); as pm) {
            <span class="chip la-pin">{{ pm }} — this run only</span>
            <button type="button" class="ghost release-pick" (click)="releasePick()">
              Score with my pool's order again
            </button>
          }
          @if (a.note) { <p class="hint la-note">{{ a.note }}</p> }
        </div>
      }

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
                  <!-- Two different facts, and a card can carry one, both or neither: the admin's
                       promoted row, and the row that actually answers this caller's runs (which
                       is the pool's first healthy entry whenever the user has one). Ranking #1 is
                       neither of those things on its own. -->
                  @if (answersRuns(m)) { <span class="chip good">used for your runs</span> }
                  @if (isDeployedDefault(m)) { <span class="chip lane">deployed default</span> }
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
                <!-- One atomic slot change: "remove, add, re-drag" costs the user his rank between
                     the calls and a half-finished sequence ends with two rows or none. The choices
                     are limited to models a key can actually pay for and not already in the pool —
                     the backend refuses the other two, so offering them would only be a delay. -->
                @if (editing() === p.provider + '/' + p.model) {
                  <select class="pool-change-target" [value]="changeTarget()"
                          (change)="onChangeTarget($event)"
                          [attr.aria-label]="'Model to put in the slot holding ' + p.model">
                    <option value="">choose a model…</option>
                    @for (c of poolChangeOptions(); track c.provider + '|' + c.model) {
                      <option [value]="c.provider + '|' + c.model">
                        {{ c.model }} · {{ c.provider_name || c.provider }}
                      </option>
                    }
                  </select>
                  <button type="button" class="ghost icon-only" data-act="save-change"
                          aria-label="Save the new model in this slot"
                          [disabled]="!changeTarget() || poolBusy()" (click)="saveChange(p)">✓</button>
                  <button type="button" class="ghost icon-only" data-act="cancel-change"
                          aria-label="Cancel changing this slot" (click)="cancelChange()">×</button>
                } @else {
                  <button type="button" class="ghost icon-only" data-act="change"
                          [attr.aria-label]="'Change the model in this slot'"
                          [disabled]="poolBusy() || poolChangeOptions().length === 0"
                          (click)="openChange(p)">✎</button>
                }
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
  /** Releasing a pin belongs to the shell for the same reason setting it does: `app.ts` also
      restores the tie-break lane it turned off for that pick, and a child that cleared the signal
      itself would leave the run silently one lane short. This view only reports the intent. */
  @Output() readonly clearModel = new EventEmitter<void>();
  readonly pickedModel = this.detection.pickedModel;

  pick(m: RankedModel): void {
    this.useModel.emit(m);
  }

  releasePick(): void {
    this.clearModel.emit();
  }

  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly ranked = signal<RankedModel[]>([]);
  readonly config = signal<PublicConfig | null>(null);
  readonly keys = signal<UserKeyView[]>([]);

  /** What /ranking says will answer the next run — see the `lane-authority` block. Held as a
      signal so the bar and the per-card chips invalidate together with the table they describe. */
  readonly authority = signal<LaneAuthority | null>(null);

  readonly failoverList = computed(() =>
    (this.authority()?.failover ?? []).map((f) => f.model).join('  →  '),
  );

  /** Read the authority, never the ranking: #1 on the table is a measurement, and the model that
      answers is whichever the pool puts first that is still answering. Both signal reads, so a
      re-ranked table or a changed pool re-checks the chip without a zone to do it for them. */
  answersRuns(m: RankedModel): boolean {
    const next = this.authority()?.next;
    return !!next && next.model === m.model;
  }

  isDeployedDefault(m: RankedModel): boolean {
    const d = this.authority()?.deployed_default;
    return !!d && d.model === m.model;
  }

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
            if (r.lane_authority !== undefined) this.authority.set(r.lane_authority);
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
          // Re-rank so "your key ready" — and the bar's "what answers your next run" — reflect
          // the new credential: a first verified key can move `next` from the deployed default
          // to this user's pool.
          this.refreshRanking();
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
        this.refreshRanking();
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

  // ---- changing which model holds one slot --------------------------------
  //
  // "Edit the model he put in the using pool" is a replace, not a remove-plus-add: the two-call
  // version leaves the slot empty while it is in flight, and if the second call fails the user has
  // lost an entry he never meant to lose. One call keeps the rank his too, because the backend
  // rebuilds the stored order with exactly one entry swapped in place of the old one.

  /** Which row shows its editor, by "provider/model". One at a time: two open selects would make
      `changeTarget` ambiguous — and in a zoneless app a plain field could not re-check itself. */
  readonly editing = signal<string | null>(null);
  /** The chosen replacement, as "provider|model" — the pair shape `POST /my/pool/replace` takes. */
  readonly changeTarget = signal('');

  private readonly poolKeys = computed(
    () => new Set(this.pool().map((p) => `${p.provider}/${p.model}`)),
  );

  /** What a slot may be filled with: a model a key can actually pay for that is not already in the
      pool. Those are exactly the two states the replace route refuses, so listing anything else
      would only turn one click into one error message. Deduplicated by pair, because the ranking
      can show the same model id under two corpora and `@for` cannot track a duplicate key. */
  readonly poolChangeOptions = computed<RankedModel[]>(() => {
    const held = this.poolKeys();
    const seen = new Set<string>();
    const out: RankedModel[] = [];
    for (const m of this.ranked()) {
      const k = `${m.provider}/${m.model}`;
      if (!(m.verified_key || m.admin_key) || held.has(k) || seen.has(k)) continue;
      seen.add(k);
      out.push(m);
    }
    return out;
  });

  openChange(p: PoolEntry): void {
    this.editing.set(`${p.provider}/${p.model}`);
    this.changeTarget.set('');
    this.poolMsg.set(null);
  }

  cancelChange(): void {
    this.editing.set(null);
    this.changeTarget.set('');
  }

  onChangeTarget(e: Event): void {
    this.changeTarget.set((e.target as HTMLSelectElement).value);
  }

  saveChange(p: PoolEntry): void {
    const target = this.changeTarget();
    const bar = target.indexOf('|');
    if (bar <= 0 || bar === target.length - 1) return;
    const newProvider = target.slice(0, bar);
    const newModel = target.slice(bar + 1);
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend.replaceInPool(p.provider, p.model, newProvider, newModel).subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        this.cancelChange();
        // The cards' "In your pool at #n" chip and the health badge are both about this row now.
        this.refreshRanking();
      },
      error: (e) => {
        this.poolBusy.set(false);
        // Nothing was written, so nothing moved: the row stays exactly as the user left it, and
        // the editor stays open so he can pick a different model instead of starting over.
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  /** Write a new order. The list is moved locally first so the drag feels immediate, then
      confirmed from the server's answer — the response is the authority, not the guess.
      The payload is expressed in the USER's rank space: the rows are drawn in served order,
      which pushes a model that stopped answering to the back, and splicing that list would
      rewrite a demoted model's stored position — silently making the user's #1 their #3 while
      the panel promises their rank comes back untouched. With no demotion the two spaces are the
      same list, so this changes nothing a healthy pool could notice. */
  move(from: number, to: number): void {
    const rows = this.pool();
    if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return;
    const order = this.userOrderFor(from, to);
    if (!order) return;
    const next = [...rows];
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    this.pool.set(next);
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend
      .setPoolOrder(order.map((p) => ({ provider: p.provider, model: p.model })))
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

  /** The whole pool ordered by `your_rank`, with the served-space move applied to it. */
  private userOrderFor(from: number, to: number): PoolEntry[] | null {
    const rows = this.pool();
    const key = (r: PoolEntry) => `${r.provider}/${r.model}`;
    const user = [...rows].sort((a, b) => a.your_rank - b.your_rank || a.position - b.position);
    const mi = user.findIndex((r) => key(r) === key(rows[from]));
    const ti = user.findIndex((r) => key(r) === key(rows[to]));
    if (mi < 0 || ti < 0 || mi === ti) return null;
    const [moved] = user.splice(mi, 1);
    user.splice(ti, 0, moved);
    return user;
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
        // Only a response that carries an answer may change the claim: an older backend omits
        // it, and "no data" must not overwrite "what /detect told us" with a guess.
        if (r.lane_authority !== undefined) this.authority.set(r.lane_authority);
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
