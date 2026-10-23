import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { TimeoutError } from 'rxjs';
import { ConfigPanel } from './ui/config-panel/config-panel';
import { TextPanel } from './ui/text-panel/text-panel';
import { DetectRail } from './ui/detect-rail/detect-rail';
import { AdminPanel } from './ui/admin-panel/admin-panel';
import { LoginView } from './ui/login-view/login-view';
import { RankingView } from './ui/ranking-view/ranking-view';
import { ProvidersView } from './ui/providers-view/providers-view';
import { HistoryView } from './ui/history-view/history-view';
import { DetectionService, MAX_TEXT_LENGTH, MIN_TEXT_LENGTH } from './core/detection.service';
import { HTTP_PATIENCE_MS } from './core/http-budget';
import { DetectionFormState } from './core/form-state';
import { AuthService } from './core/auth.service';
import { MODEL_PATTERN, composeBaseUrl } from './core/providers';
import type { DetectionResult } from './core/detection.types';
import type { RankedModel } from './core/backend.types';

const KEY_PATTERN = /^[A-Za-z0-9._:+\/-]+$/;

export type ViewId = 'detect' | 'ranking' | 'history' | 'providers' | 'admin';

export type Notice =
  | { kind: 'preflight'; lines: string[] }
  | { kind: 'rejected'; status: number; error: string; errorCode: string; hint: string | null }
  | { kind: 'engine'; status: number; detail: string }
  | { kind: 'unreachable'; detail: string }
  | { kind: 'timeout'; detail: string };

@Component({
  selector: 'app-root',
  imports: [
    ConfigPanel,
    TextPanel,
    DetectRail,
    AdminPanel,
    LoginView,
    RankingView,
    ProvidersView,
    HistoryView,
  ],
  templateUrl: './app.html',
})
export class App implements OnInit, OnDestroy {
  private readonly service = inject(DetectionService);
  readonly auth = inject(AuthService);

  readonly form = new DetectionFormState();

  /** Derived from the one constant that actually aborts the request. The card used to print a
      literal "150 seconds" — the value the client gave up at before #140 raised it to the 360 s
      that clears nginx's 330 s — so a user who waited the real 6 minutes was told a number the
      code had already stopped using. Same rule as the timeout chain in the docs: never restate a
      limit at a tier that cannot read the tier it belongs to. */
  readonly patienceSeconds = Math.round(HTTP_PATIENCE_MS / 1000);

  /** The one place a tab's name is written, because two of these names change with the deployment
      and a template string elsewhere would be a second authority. `admin` is the same surface in
      both cases — thresholds, bands, shared keys, capture imports — and only the WORD differs: a
      packaged single-owner install has nobody to administer but the owner, so calling it "Admin"
      told the one person using it that there was somebody else in charge. */
  private readonly VIEW_LABELS: Record<ViewId, string> = {
    detect: 'Detect',
    ranking: 'Ranking',
    history: 'History',
    providers: 'Models & keys',
    admin: 'Advanced parameters',
  };

  label(id: ViewId): string {
    return id === 'admin' && !this.auth.local() ? 'Admin' : this.VIEW_LABELS[id];
  }

  /** Role-aware nav: everyone gets Detect / Ranking / History; the surface that controls models and
      endpoints needs a session to own a key or a pool, so it appears once there is one; and only an
      admin sees the deployed parameters. */
  readonly tabs = computed(() => {
    const ids: ViewId[] = ['detect', 'ranking', 'history'];
    if (this.auth.user() !== null) ids.push('providers');
    if (this.auth.isAdmin()) ids.push('admin');
    return ids.map((id) => ({ id, label: this.label(id) }));
  });

  readonly view = signal<ViewId>('detect');

  /** Which endpoint the keys surface should open on, set by a locked ranking card. */
  readonly pendingProvider = signal<number | null>(null);

  /** Which MODEL's card the add-models menu should light up and scroll to, as the picker's own
      `provider|model` address. Built here rather than on the ranking card so the shape has one
      owner: `/ranking` labels an endpoint (`NVIDIA NIM`) and `/my/pool/options` keys its cards by
      the slug (`nvidia`), and a route built from the label arrives with nothing highlighted. */
  readonly pendingPoolPair = signal<string | null>(null);

  /** A rank card's "Add to using pool" is a route, not a write — this view keeps its hands off the
      pool, which is the other surface's to own and to explain. Cleared when the user leaves, so a
      later visit to that page is not still centring a card he asked for three views ago. */
  openPoolFor(m: RankedModel): void {
    this.pendingPoolPair.set(`${m.provider}|${m.model}`);
    this.setView('providers');
  }

  /** A ranking card the user cannot pay for still has exactly one action, and this is where it
      goes: the surface that holds keys, preselected on the endpoint that card belongs to. */
  openKeyForm(providerId: number | null): void {
    this.pendingProvider.set(providerId);
    this.setView('providers');
  }

  /** Read-only for the template: which ranked model, if any, the next run scores with. */
  readonly pickedModel = this.service.pickedModel;
  /** True when the chosen model IS the deployed tie-break lane, so the banner can say that the
      second opinion was dropped rather than let it silently confirm the first one. */
  readonly pickedIsTieBreak = signal(false);
  private autoDisabledSecondary = false;

  /** "pick them auto if key verified working is there", made actionable: clicking a ranking
      card chooses that model for the next detection and brings the user back to Detect.
      Clicking the chosen one again releases it and falls back to the admin's deployed lane.
      Nothing else about the run changes — the model's endpoint, its calibrated band and the
      key are all resolved server-side from the admin's stored row. */
  useRankedModel(m: RankedModel): void {
    if (this.pickedModel() === m.model) {
      this.clearPickedModel();
      return;
    }
    this.service.usePickedModel(m.model);
    this.pickedIsTieBreak.set(m.lane === 'secondary');
    if (m.lane === 'secondary' && this.form.secondaryEnabled()) {
      this.form.secondaryEnabled.set(false);
      this.autoDisabledSecondary = true;
    }
    this.view.set('detect');
  }

  clearPickedModel(): void {
    this.service.clearPickedModel();
    this.pickedIsTieBreak.set(false);
    if (this.autoDisabledSecondary) {
      // Only put the tie-break back if this pick is what took it away — a user who had
      // switched it off themselves in Configuration stays off.
      this.form.secondaryEnabled.set(true);
      this.autoDisabledSecondary = false;
    }
  }

  readonly result = signal<DetectionResult | null>(null);
  readonly notice = signal<Notice | null>(null);
  readonly pending = signal(false);
  readonly elapsed = signal(0);

  /** The exact text that was scored, frozen at run time. The result card paints the
      model's quoted evidence back onto this string; binding it to the live textarea
      instead would misalign the highlights the moment the user edits after a run. */
  readonly detectedText = signal('');

  /** Honest about what is being waited on: the tie-break only ever runs on a
      gray-zone answer, so a switched-off lane means exactly one model call. */
  readonly inflightHint = computed(() =>
    this.form.secondaryEnabled()
      ? 'primary scoring · tie-break only for the gray zone · free pools queue, 15–120 s is normal'
      : 'single lane · free pools queue, 15–120 s is normal');

  private timer: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    // Pick up an existing session cookie. Until `ready` flips the shell shows a splash
    // rather than flashing the login card at someone who is already signed in.
    this.auth.boot().subscribe();
  }

  ngOnDestroy(): void {
    this.stopTimer();
  }

  setView(id: ViewId): void {
    // Never let a stale admin view survive a logout, and never open the model-control surface for
    // a visitor who has no keys or pool to show there.
    if (id === 'admin' && !this.auth.isAdmin()) return;
    if (id === 'providers' && this.auth.user() === null) return;
    // Leaving the picker retires the address it was asked to centre on. Held any longer, the next
    // visit to that page would scroll back to a card from a previous route.
    if (id !== 'providers') this.pendingPoolPair.set(null);
    this.view.set(id);
  }

  logout(): void {
    this.auth.logout().subscribe({
      next: () => this.view.set('detect'),
      error: () => this.view.set('detect'),
    });
  }

  /** A History row's "Open on Detect": put the stored verdict and the writing it was reached on back
      on the Detect tab. The text goes into the field as well as behind the highlights, so the reader
      can edit it and score it again — and because `detectedText` is set to the same characters, the
      evidence paint lights up on arrival instead of showing a bare number over a quote that is not on
      screen. Rows saved before the backend kept the text reopen with the verdict alone. */
  reopenFromHistory(reopened: { result: DetectionResult; text: string | null }): void {
    this.result.set(reopened.result);
    this.notice.set(null);
    const text = reopened.text ?? '';
    this.form.text.set(text);
    this.detectedText.set(text);
    this.view.set('detect');
  }

  /** Checked here, on the client, before anything leaves the page — a key with a
      space or a CRLF in it would be rejected by the engine anyway, but on a hosted deploy the
      inbound request is stored whole before the workflow even runs, and that record would carry
      it. Refusing to send it is what keeps it off somebody else's disk. */
  private preflight(): string[] {
    const lines: string[] = [];
    const text = this.form.text();
    const n = text.trim().length;
    if (n === 0) {
      lines.push('Text is empty.');
    } else if (n < MIN_TEXT_LENGTH) {
      lines.push(`Text is ${n} characters; the detector needs at least ${MIN_TEXT_LENGTH}.`);
    } else if (text.length > MAX_TEXT_LENGTH) {
      lines.push(`Text is ${text.length} characters; the limit is ${MAX_TEXT_LENGTH}.`);
    }

    ([
      ['primary', this.form.primary],
      ['secondary', this.form.secondary],
    ] as const).forEach(([lane, state]) => {
      // A switched-off tie-break lane is never sent, so validating it would
      // block a run over a field the user cannot even see.
      if (lane === 'secondary' && !this.form.secondaryEnabled()) return;
      const model = state.model().trim();
      if (model) {
        if (model.length > 200) {
          lines.push(`${lane} model is ${model.length} characters; the limit is 200.`);
        } else if (!MODEL_PATTERN.test(model)) {
          lines.push(
            `${lane} model "${model}" has illegal characters — letters, digits and . _ : / @ + - only.`,
          );
        }
      }
      if (state.host().trim()) {
        if (state.host().includes('@')) {
          lines.push(`${lane} host may not embed credentials ("${state.host()}").`);
        }
        const url = composeBaseUrl(state.fields());
        if (!/^https?:\/\//i.test(url)) {
          lines.push(`${lane} base_url "${url}" is not an absolute http(s) URL.`);
        }
        if (state.path().includes('#') || state.path().includes('?')) {
          lines.push(`${lane} path may not contain a query string or a fragment.`);
        }
        const port = state.port().trim();
        if (port && !/^\d{1,5}$/.test(port)) {
          lines.push(`${lane} port "${port}" is not a number between 1 and 65535.`);
        } else if (port) {
          const p = Number(port);
          if (p < 1 || p > 65535) {
            lines.push(`${lane} port ${p} is outside 1-65535.`);
          }
        }
      }
    });

    ([
      ['primary', this.form.primaryApiKey],
      ['secondary', this.form.secondaryApiKey],
    ] as const).forEach(([lane, key]) => {
      if (lane === 'secondary' && !this.form.secondaryEnabled()) return;
      const value = key();
      if (!value) return;
      if (value.length > 512) {
        lines.push(`${lane} key is ${value.length} characters; the limit is 512.`);
      } else if (!KEY_PATTERN.test(value)) {
        lines.push(
          `${lane} key contains a character that is not a letter, digit or . _ : + / - ` +
            `(spaces and line breaks included). Nothing was sent, so the key never left this page.`,
        );
      }
    });

    return lines;
  }

  run(): void {
    this.result.set(null);
    this.notice.set(null);

    const problems = this.preflight();
    if (problems.length) {
      this.notice.set({ kind: 'preflight', lines: problems });
      return;
    }

    this.pending.set(true);
    this.elapsed.set(0);
    this.startTimer();

    const text = this.form.text();
    this.detectedText.set(text);

    this.service.detect(text, this.form.laneConfig(), this.form.keys()).subscribe({
      next: (res) => {
        this.stopTimer();
        this.pending.set(false);
        this.result.set(res);
      },
      error: (err: unknown) => {
        this.stopTimer();
        this.pending.set(false);
        this.notice.set(this.classifyError(err));
      },
    });
  }

  private classifyError(err: unknown): Notice {
    if (err instanceof TimeoutError) {
      return {
        kind: 'timeout',
        detail:
          `No answer within ${this.patienceSeconds} seconds. Free-tier pools queue: a slow lane ` +
          'is not a dead lane, but nothing came back in time — try again, or put another model ' +
          `first in the pool on the ${this.label('providers')} tab.`,
      };
    }
    if (!(err instanceof HttpErrorResponse)) {
      return { kind: 'engine', status: 0, detail: String(err) };
    }

    const body = err.error as
      | { error?: string; message?: string; error_code?: string; hint?: string }
      | null;
    const bodyText =
      body && typeof body === 'object'
        ? (body.error ?? body.message ?? '')
        : typeof body === 'string'
          ? body
          : '';

    if (err.status === 0) {
      return {
        kind: 'unreachable',
        detail:
          'Nothing answered on /api/detect. Is the Flask backend running ' +
          '(python -m backend.app, port 8787) and is this dev server still using proxy.conf.json?',
      };
    }
    if (err.status === 502) {
      // Two different 502s. The Flask backend answers 502 with a JSON body when the engine it
      // forwards to is down; the Angular dev-server proxy answers 502 with no body when nothing
      // is listening on the backend port. Naming a server program in the second case would send
      // the user to the wrong process.
      return {
        kind: 'unreachable',
        detail:
          bodyText ||
          'HTTP 502 with no body from the dev-server proxy, which means nothing is listening ' +
            'on port 8787 at all. Start python -m backend.app — the purge status line on the ' +
            'Configuration tab should turn green when it is up.',
      };
    }
    if (err.status === 400 || err.status === 403) {
      return {
        kind: 'rejected',
        status: err.status,
        error: bodyText || `HTTP ${err.status}`,
        errorCode: body?.error_code ?? 'INPUT_REJECTED',
        hint: body?.hint ?? null,
      };
    }
    // Anything else the server answered with is not a verdict, whatever it is: a gateway's own
    // error page, a workflow that failed before its Respond node, an exception body. The sentence
    // has to name the fact the reader can check -- an HTTP status and the body that came with it --
    // and not a program this deployment may not even run. A packaged install answers /detect in
    // this process, and telling its owner to go and look at n8n would send them to software that is
    // not installed.
    return {
      kind: 'engine',
      status: err.status,
      detail: bodyText || err.statusText || 'The server answered, but not with a verdict.',
    };
  }

  private startTimer(): void {
    const started = Date.now();
    this.timer = setInterval(() => this.elapsed.set(Math.round((Date.now() - started) / 1000)), 500);
  }

  private stopTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
