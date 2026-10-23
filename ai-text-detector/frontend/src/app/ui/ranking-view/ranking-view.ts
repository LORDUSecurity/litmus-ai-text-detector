import { Component, EventEmitter, OnInit, Output, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { DetectionService } from '../../core/detection.service';
import { CircularMetric } from '../circular-metric/circular-metric';
import { ProviderMark } from '../provider-mark/provider-mark';
import { ReportSheet } from '../report-sheet/report-sheet';
import type { ConfigProvider, LaneAuthority, PublicConfig, RankedModel, RankingResponse } from '../../core/backend.types';

/** The measured table: which model judges best, and the two moves a card offers from there.
    Controlling models and endpoints — BYOK keys, the guides, the using pool, discovery — lives in
    `ProvidersView`. This view reads `/config` and `/ranking` and spends nothing at all: no call,
    no key, no token. A card carries the button that points the next run at that model, the same
    pointing again under the shorter word the user actually reaches for, the route to that model's
    own row in the pool menu, and — on a card no key can pay for — the way to get one. Nothing here
    edits a pool or a key: the page that explains a measurement cannot also be the page that
    changes what is measured. */
@Component({
  selector: 'app-ranking-view',
  standalone: true,
  imports: [CircularMetric, ProviderMark, ReportSheet],
  styles: `
    /* The fresh-install state has to be explained, not just rendered empty: 48 ranking cards,
       zero pick buttons. It is a note about the deployment, so it carries no hue and no corner
       treatment either — the cut belongs to two boxes in the whole app, the primary action and the
       brand mark, and this is neither. (No backticks in this comment: it lives inside a template
       literal, and one would end the styles property and break the decorator.) */
    .lock-note {
      display: flex;
      gap: var(--s-3);
      align-items: flex-start;
      margin: 0 0 var(--s-4);
      padding: var(--s-4);
      border: 1px solid var(--line-strong);
      background-color: var(--panel);
    }
    .lock-note .ico { flex: 0 0 auto; margin-top: 2px; }
    .lock-note strong { display: block; margin-bottom: var(--s-1); }
    .lock-note p { margin: 0 0 var(--s-2); line-height: var(--lh-mono); }

    /* "Which model actually runs on my text?" — the question the ranking table never answered,
       and the one the brief makes the page for ("these ranking are the one used in auto order").
       Neutral by the same rule: it states which lane is authoritative, and the response gold is spent
       on the control that changes it (the pick button on a card, or the pool on the other page). */
    .lane-authority {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2) var(--s-3);
      margin: 0 0 var(--s-4);
      padding: var(--s-3) var(--s-4);
      border: 1px solid var(--line-strong);
      background-color: var(--panel);
      font-size: var(--fs-meta);
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
      gap: var(--s-2);
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
      line-height: var(--lh-mono);
    }

    /* The printable copy of the study, one click from the table that cites it. Outlined rather
       than filled so the archival URL (the primary link) stays the louder of the two, and in the
       accent because it is still an act — its old response-colour edge put a transient register on
       something just sitting there. */
    .pdf-link {
      display: inline-flex;
      align-items: center;
      gap: var(--s-2);
      padding: var(--s-2) var(--s-4);
      text-decoration: none;
      font-size: var(--fs-meta);
      letter-spacing: var(--tr-label);
      color: var(--accent);
      border: 1px solid var(--edge);
      background-color: transparent;
      transition: border-color var(--t-fast) var(--ease-out), transform var(--t-instant) var(--ease-out);
    }
    .pdf-link:hover {
      border-color: var(--touch);
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

    /* The two states the user has to read off a card without touching anything.
       In his pool: the card itself gets the accent frame, because "already yours" is a property of
       the whole row, not a label on it -- and no pool control appears here, since the order belongs
       to the other surface. The ring is INSET and the chamfer's diagonals turn accent-gold with it:
       this card is clipped, so an outer glow would arrive at the two cut corners and stop there, and
       a frame that lights up while its corner rule stays grey is a frame with a hole in the state.
       Cannot be paid for: the way out is drawn in the accent rather than as a ghost, because on a
       locked card it is not a secondary option, it is the only one. A row of these is deliberately a
       frame and a tint rather than 48 filled slabs. */
    .rank-card.in-pool {
      border-color: var(--accent);
      --cut-ink: var(--accent);
      box-shadow: inset 0 0 0 1px var(--accent);
    }
    .key-btn {
      border-color: var(--accent);
      color: var(--accent);
      background-color: var(--accent-a16);
      font-weight: 650;
    }

    /* The three moves on a card share one line because they are one question -- "what do I do with
       this model" -- and the two short ones must not look like a second panel. Neither writes from
       here: one points the next run, the other is a route to the page that owns the pool. */
    .rank-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2);
      /* The row's own separation from the stats above it. It lives here rather than as a margin on
         the first button, which used to push one control out of line with its two siblings. */
      margin-top: var(--s-3);
    }
    .test-btn,
    .pool-btn {
      flex: 0 0 auto;
      padding: var(--s-1) var(--s-3);
      font-size: var(--fs-meta);
    }
    /* The line under the heading belongs to the heading, not to the panel: one step of separation
       and no trailing margin, so the head reads as a single object. */
    .ranking-head p {
      margin: var(--s-1) 0 0;
    }
  `,
  template: `
    <section class="panel">
      <div class="ranking-head">
        <div>
          <h2>Model ranking</h2>
          <p class="muted">
            Scored on real captured corpora at each model's own calibrated band. Full captures
            first — fewest wrong, then widest coverage — then probes on a short sample.
            The ring's colour is the key: green is one in your own hand, grey runs on the
            deployment's shared key, amber has neither. Pick a card to score your text with it.
          </p>
        </div>
        <button type="button" class="primary paper-link" data-act="open-report" (click)="openReport()">
          <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 3h9l4 4v14H6z" /><path d="M14 3v5h5M9 13h7M9 17h7" />
          </svg>
          {{ paperTitle() }}
        </button>
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
             error. The way out is a route now: keys are entered on the other surface. -->
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
                Add one under keys — the guides link straight to each provider's key page — and the
                cards turn green and gain a pick button as soon as it verifies.
              } @else {
                Sign in first: keys belong to your account, which is also what saves your history.
              }
            </p>
            @if (signedIn()) {
              <!-- Says where it goes rather than pointing: this is a route, and the page it routes
                   to is the name of the button. The arrow is kept for outbound links, where the
                   destination is a domain and "leaves this site" is the fact worth marking. -->
              <button type="button" class="ghost" (click)="requestKey()">Open Models &amp; keys</button>
            }
          </div>
        </div>
      }

      @if (loading()) {
        <p class="empty">Loading the ranking…</p>
      } @else if (error(); as e) {
        <p class="auth-error" role="alert">{{ e }}</p>
      } @else if (ranked().length === 0) {
        <!-- The label is the deployment's, not the app's: a packaged single-owner install never
             shows the word "Admin" anywhere, because there is nothing to administer besides one's
             own parameters. -->
        <p class="empty">
          No measured models yet. The benchmark captures are imported from the
          {{ auth.local() ? 'Advanced parameters' : 'Admin' }} tab before the ranking can be shown.
        </p>
      } @else {
        <div class="rank-grid">
          @for (m of ranked(); track m.model + m.tag + m.rank) {
            <article class="rank-card" [class.in-pool]="m.in_pool">
              <div class="ring-metric">
                <!-- Rank over score, in one column: the two are the same reading of this model, and
                     the numeral used to float in a corner held off the card by a chamfer it no longer
                     has. -->
                <span class="rank-badge"><span class="rb-hash">#</span>{{ m.rank }}</span>
                <app-circular-metric
                  [value]="m.accuracy"
                  [tone]="m.verified_key ? 'green' : (m.admin_key ? 'neutral' : 'amber')"
                  [label]="m.accuracy === null ? 'no score' : 'accuracy'" />
              </div>
              <div class="rank-body">
                <h3>{{ m.model }}</h3>
                <div class="rank-meta">
                  <app-provider-mark
                    [provider]="m.provider"
                    [label]="endpointLabel(m)"
                    [lit]="!!m.verified_key" />
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
                  <!-- Availability, measured from real runs, not this page's to change. It used to
                       sit inside the pool action row, which is why it needed a pool to exist. -->
                  @if (m.health && m.health !== 'unknown') {
                    <span class="chip" [class.good]="m.health === 'ok'"
                          [class.warn]="m.health === 'degraded'"
                          [class.bad]="m.health === 'down'">
                      {{ m.health }}@if (m.consecutive_failures) { · {{ m.consecutive_failures }} failed }
                    </span>
                  }
                  <!-- Two different facts, and a card can carry one, both or neither: the admin's
                       promoted row, and the row that actually answers this caller's runs (which
                       is the pool's first healthy entry whenever the user has one). Ranking #1 is
                       neither of those things on its own. The second chip is read from the authority
                       rather than from the row, so it earns its place only where the row cannot say
                       it: a lane the deployment answers with that carries no lane role of its own.
                       When the chip above already names the lane, saying "deployed default" again
                       below it is the same claim in a second set of words. -->
                  @if (answersRuns(m)) { <span class="chip good">used for your runs</span> }
                  @if (isDeployedDefault(m) && !m.lane) { <span class="chip lane">deployed default</span> }
                </div>
                <div class="rank-stats">
                  <span><b>{{ m.correct }}</b> right</span>
                  <span><b>{{ m.wrong }}</b> wrong</span>
                  <span><b>{{ m.review }}</b> review</span>
                  <span>coverage <b>{{ pct(m.coverage) }}</b></span>
                  <span>on <b>{{ m.n_clean }}</b> texts</span>
                </div>
                <!-- The two moves a card offers: point the next run at this model, or get to its
                     row in the pool menu. Neither writes from this page — one is the pick the shell
                     owns, the other a route. A locked card has neither and gets the way out instead,
                     drawn loud because it is the only move available. -->
                <div class="rank-actions">
                  @if (m.verified_key || m.admin_key) {
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
                    <button type="button" class="ghost test-btn" data-act="test"
                            [title]="testTitle(m)"
                            (click)="pick(m)">Test</button>
                    <button type="button" class="ghost pool-btn" data-act="open-pool"
                            (click)="openPoolFor(m)">
                      {{ m.in_pool ? 'Find it in the using pool' : 'Add to using pool' }}
                    </button>
                  } @else {
                    <button class="key-btn" type="button" (click)="requestKey(m)">
                      Add a key for {{ endpointLabel(m) }}
                    </button>
                  }
                </div>
              </div>
            </article>
          }
        </div>
      }
    </section>

    @if (report()) {
      <app-report-sheet [paperUrl]="paperUrl()" [paperTitle]="paperTitle()"
                        (closed)="closeReport()" />
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
  /** Releasing a pin belongs to the shell for the same reason setting it does: `app.ts` also
      restores the tie-break lane it turned off for that pick, and a child that cleared the signal
      itself would leave the run silently one lane short. This view only reports the intent. */
  @Output() readonly clearModel = new EventEmitter<void>();
  /** Asking for a key is a route, not a form: `null` means "no endpoint in mind", a provider id
      means the keys surface should open preselected on that one — because this is the card the
      user was looking at. The id comes from `/config` rather than from the row, since `/ranking`
      only carries one for a model that has a `models` row. */
  @Output() readonly getKeys = new EventEmitter<number | null>();
  /** Ask the models-and-keys surface to land on THIS model's card in the add-models menu. The
      card emits itself rather than a pair string because the address the picker keys on is
      `provider|model` with the provider SLUG — the shell builds it, so the two pages cannot drift
      apart on the shape of the address. */
  @Output() readonly openPool = new EventEmitter<RankedModel>();

  readonly pickedModel = this.detection.pickedModel;

  pick(m: RankedModel): void {
    this.useModel.emit(m);
  }

  releasePick(): void {
    this.clearModel.emit();
  }

  /** The endpoints this deployment serves, keyed by the name `/ranking` puts on a row.
      A join gives a registered model its `provider_id`; a measured model with no `models` row gets
      only the bare slug and `provider_id: null` — most of the table on a fresh install. `/config`
      lists the same endpoints with id and label, so the card resolves its endpoint here instead of
      repeating a slug the keys page never shows. */
  readonly endpointsByName = computed(() => {
    const map = new Map<string, ConfigProvider>();
    for (const p of this.config()?.providers ?? []) map.set(p.name, p);
    return map;
  });

  private endpointOf(m?: RankedModel | null): ConfigProvider | undefined {
    if (!m) return undefined;
    return this.endpointsByName().get(m.provider_name || m.provider || '');
  }

  /** What the endpoint is called on the keys page, so the card and the page name it identically. */
  endpointLabel(m: RankedModel): string {
    return this.endpointOf(m)?.label || m.provider_name || m.provider || 'this endpoint';
  }

  requestKey(m?: RankedModel): void {
    this.getKeys.emit(this.endpointOf(m)?.id ?? m?.provider_id ?? null);
  }

  /** The Test button emits the same card the pick button does. It stays because that is the word
      the user reaches for; the answer it used to print ("answered in 1.8 s", from one 8-token
      `POST /my/probe` on his own key) could not be read as an action, whereas pressing this does
      send his next text to that model — which is the test that matters. */
  testTitle(m: RankedModel): string {
    return `Same as "Score with this model": your next run is judged by ${m.model} on ${this.endpointLabel(m)}.`;
  }

  // ---- the route to this model's row in the pool menu ------------------------------
  //
  // "Add to using pool" is a route rather than a write, and that is deliberate: this page explains
  // a measurement, the page that owns the pool knows which models a key can pay for, which rows he
  // already holds and what health did to them. Adding from here would mean re-deciding all of that
  // on the wrong side of the wire — which is how #142 came to promise a model that never answered.
  // The pair is `provider|model` with the SLUG, because that is what the picker addresses its cards
  // by; the row's `provider_name` is the endpoint's display label, and a route built from the label
  // lands on the right page with nothing highlighted.
  //
  // Nothing here scrolls or lights anything either: the card is on a page the user is leaving. The
  // shell switches the view and hands the pair over; the highlight belongs to the frame that is
  // found.

  openPoolFor(m: RankedModel): void {
    this.openPool.emit(m);
  }

  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly ranked = signal<RankedModel[]>([]);
  readonly config = signal<PublicConfig | null>(null);

  /** The study, open beside the table. A new tab used to take the reader off the page they were
      using to compare rows, and coming back meant losing the place they had read to. */
  readonly report = signal(false);

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

  openReport(): void {
    this.report.set(true);
  }

  closeReport(): void {
    this.report.set(false);
  }

  readonly signedIn = computed(() => this.auth.user() !== null);

  /** Cards a user can actually score with: their own verified key, or the shared key for that
      endpoint. Everything else on the page is informative. */
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

  ngOnInit(): void {
    this.backend.config().subscribe({
      next: (cfg) => {
        this.config.set(cfg);
        this.backend.ranking().subscribe({
          next: (r: RankingResponse) => {
            this.ranked.set(r.ranked);
            // Only a response that carries an answer may change the claim: an older backend omits
            // it, and "no data" must not overwrite "what /detect told us" with a guess.
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
