import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { App } from './app';
import type { DetectionResult } from './core/detection.types';
import type { AuthResponse, SessionUser } from './core/backend.types';

const RESULT: DetectionResult = {
  document_id: 'doc_test',
  classification: 'AI_GENERATED',
  ai_probability: 0.91,
  confidence: 0.78,
  method: 'SINGLE_MODEL_PRIMARY_AI',
  review_required: false,
  evidence: {
    provenance: { checked: false, reason: 'unavailable in ai-only mode' },
    watermark: { checked: false, reason: 'unavailable in ai-only mode' },
    statistics: { checked: false, reason: 'unavailable in ai-only mode' },
    models: [
      {
        lane: 'primary',
        model: 'nvidia/nemotron-3-ultra-550b-a55b',
        ok: true,
        skipped: false,
        error_code: null,
        error_detail: null,
        salvaged: false,
        truncated: false,
        result: {
          ai_probability: 0.95,
          confidence: 0.8,
          signals: [{ name: 'parallel structure', direction: 'AI', strength: 0.7 }],
        },
      },
    ],
  },
  warning: 'Human-likely means no strong AI-generation signal was detected.',
};

const ADMIN: SessionUser = {
  id: 1,
  username: 'root',
  email: null,
  display_name: 'Root',
  role: 'admin',
  theme: 'dark',
  created_at: '2026-10-06T20:00:00Z',
  last_login_at: null,
};

describe('App', () => {
  let fixture: ComponentFixture<App>;
  let app: App;
  let http: HttpTestingController;

  /** Boot the shell as a signed-in admin: ngOnInit fires GET /api/auth/me, and the
      login gate keeps everything else unrendered until that resolves. Flushing it here
      means each test starts on the Detect tab with the session already adopted. */
  function signIn(user: SessionUser | null = ADMIN): void {
    fixture.detectChanges(); // triggers ngOnInit -> auth.boot()
    const req = http.expectOne((r) => r.url === '/api/auth/me');
    req.flush({ user } as AuthResponse);
    fixture.detectChanges(); // ready flips, shell renders
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(App);
    app = fixture.componentInstance;
    http = TestBed.inject(HttpTestingController);

    signIn(ADMIN);

    // The default view is Detect, which fires no further request until the user runs something.
    http.expectNone((req) => req.url.startsWith('/api/'));
  });

  /** Two lane rows, both enabled: what the Configuration card reads on open, and the state the
      rest of this suite was written against. Only the fields the card looks at are filled in. */
  const LANE_ROWS = [1, 2].map((id) => ({
    id,
    provider_id: 1,
    model_id: id === 1 ? 'z-ai/glm-5.3-flash' : 'google/diffusiongemma-26b-a4b-it',
    display_name: id === 1 ? 'glm' : 'diffusiongemma',
    role: id === 1 ? 'primary' : 'secondary',
    lane: id === 1 ? 'primary' : 'secondary',
    temperature: 0.1,
    top_p: 0.95,
    max_tokens: 0,
    timeout_ms: id === 1 ? 180000 : 90000,
    ai_prob_threshold: id === 1 ? 0.8 : 0.77,
    ai_conf_threshold: 0.6,
    human_prob_threshold: id === 1 ? 0.55 : 0.4,
    human_conf_threshold: 0.6,
    secondary_decision_threshold: id === 1 ? null : 0.9,
    notes: '',
    measured: true,
    enabled: true,
  }));

  afterEach(() => {
    // The Configuration card reads the shared lane state when it opens (GET /api/models), because
    // the tie-break switch now writes the admin's model row instead of only this browser's
    // preference. Tests that never assert on it must still leave nothing in flight for verify().
    // Flushing an *enabled* secondary row keeps the card in the state every test below was
    // written against; a disabled row would collapse it and change what they see for a reason
    // that has nothing to do with what they check.
    http
      .match((r) => r.url === '/api/models' || r.url.startsWith('/api/models/'))
      .forEach((req) =>
        req.flush(
          req.request.method === 'GET'
            ? (LANE_ROWS as never)
            : ({ ...LANE_ROWS[1], enabled: true } as never),
        ),
      );

    // Opening the Ranking tab now also reads the caller's using pool and their discovery jobs
    // (GET /api/my/pool, GET /api/my/discover). Those tests assert on the ranked cards, not on
    // the pool, so they never flush them; drain them here with the empty answers that mean
    // "no pool, no jobs" -- an empty job list is also what stops the 4 s poll from being
    // scheduled, which would otherwise leave a timer running past the test.
    http
      .match((r) => r.url === '/api/my/pool' || r.url === '/api/my/discover')
      .forEach((req) =>
        req.flush(
          (req.request.url === '/api/my/pool'
            ? { pool: [] }
            : { jobs: [], screen_texts: 'probe2', note: '' }) as never,
        ),
      );

    // The tie-break card on Models & keys reads the model rows and asks which engine answers, the
    // moment an admin opens that tab. Tests about the keys, the pool or the nav are not about the
    // lane, so drain both with the answers that say "no rows yet, hosted deploy".
    http
      .match((r) => r.url === '/api/providers' && r.method === 'GET')
      .forEach((req) => req.flush([] as never));
    http
      .match((r) => r.url === '/api/n8n/status')
      .forEach((req) =>
        req.flush({
          n8n_url: 'http://localhost:5678',
          workflow_id: 'ai-detection-ai-only',
          reachable: true,
          db_readable: true,
          on_request_path: true,
        } as never),
      );
    http.verify();
  });

  function setValidText(): void {
    app.form.text.set('this is clearly long enough to be judged by the workflow');
  }

  it('creates the app', () => {
    expect(app).toBeTruthy();
  });

  it('shows the login gate and no app shell when there is no session', () => {
    // Fresh component, signed OUT.
    const f = TestBed.createComponent(App);
    f.detectChanges();
    http.expectOne((r) => r.url === '/api/auth/me').flush({ user: null } as AuthResponse);
    f.detectChanges();

    const compiled = f.nativeElement as HTMLElement;
    expect(compiled.querySelector('app-login-view')).toBeTruthy();
    expect(compiled.querySelector('.appbar')).toBeFalsy();
    http.verify();
  });

  it('opens straight into the app when the backend says this is a single-owner install', () => {
    // The packaged exe. One built bundle serves both deployments, so the difference has to come
    // from the server, not from a build flag -- and the shell must not ask this user to sign in
    // to their own machine, or offer to sign them out of it.
    const f = TestBed.createComponent(App);
    f.detectChanges();
    http
      .expectOne((r) => r.url === '/api/auth/me')
      .flush({ user: ADMIN, auth_mode: 'local' } as AuthResponse);
    f.detectChanges();

    const compiled = f.nativeElement as HTMLElement;
    expect(compiled.querySelector('app-login-view')).toBeFalsy();
    expect(compiled.querySelector('.appbar')).toBeTruthy();
    expect(compiled.querySelector('.user-chip')?.textContent).toContain('Root');
    expect(compiled.querySelector('.user-chip [aria-label="Sign out"]')).toBeFalsy();
    http.verify();
  });

  it('does not fall back to the sign-in page when a local install reports no session', () => {
    // Defensive: the guard answers as the owner, so `user: null` should not be reachable in this
    // mode. If it ever is -- a database with no admin row -- a login card would be a dead end,
    // because the backend refuses sign-up in local mode. Opening the app is the honest answer.
    const f = TestBed.createComponent(App);
    f.detectChanges();
    http
      .expectOne((r) => r.url === '/api/auth/me')
      .flush({ user: null, auth_mode: 'local' } as AuthResponse);
    f.detectChanges();

    const compiled = f.nativeElement as HTMLElement;
    expect(compiled.querySelector('app-login-view')).toBeFalsy();
    expect(compiled.querySelector('.appbar')).toBeTruthy();
    http.verify();
  });

  it('refuses to send an empty text and reports why', () => {
    app.form.text.set('');
    app.run();
    fixture.detectChanges();

    expect(app.notice()?.kind).toBe('preflight');
    expect(app.pending()).toBe(false);
    http.expectNone('/api/detect');

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.textContent).toContain('Not sent');
  });

  it('refuses to send an illegal BYOK key, so it never leaves the page', () => {
    setValidText();
    app.form.primaryApiKey.set('has spaces and (parens)');
    app.run();

    expect(app.notice()?.kind).toBe('preflight');
    http.expectNone('/api/detect');
  });

  it('refuses an illegal model id without sending', () => {
    setValidText();
    app.form.primary.model.set('x"} , {"role":"system');
    app.run();

    expect(app.notice()?.kind).toBe('preflight');
    http.expectNone('/api/detect');
  });

  it('sends a valid request and renders the verdict', async () => {
    setValidText();
    app.run();

    const req = http.expectOne('/api/detect');
    expect(req.request.method).toBe('POST');
    req.flush(RESULT);
    await fixture.whenStable();
    fixture.detectChanges();

    expect(app.notice()).toBeNull();
    expect(app.result()?.classification).toBe('AI_GENERATED');

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.verdict')?.textContent).toContain('AI_GENERATED');
    // The number the rail answers with is the run's own measurement, printed on the dial rather
    // than drawn as a length. The unit is the dial's own, so it sits beside the digits, not in them.
    expect(compiled.querySelector('.ring-lead .ring-num')?.textContent).toBe('91.0');
    expect(compiled.querySelector('.ring-lead .ring-unit')?.textContent).toBe('%');
  });

  it('keeps the engine internals out of the verdict', async () => {
    // A run reports what it sampled and where it sent the request. None of that is the output the
    // reader asked for, and it used to be a fold in the middle of the answer. What stays is the
    // verdict, the magnitudes, the models that answered and — if one failed — its reason.
    const tuned: DetectionResult = {
      ...RESULT,
      component_scores: {
        primary_probability: 0.91,
        primary_confidence: 0.78,
        secondary_probability: null,
        secondary_confidence: null,
        lane_gap: null,
        secondary_called: false,
        decision_mode: 'SINGLE_MODEL_PRIMARY_AI',
        deciding_lane: 'primary',
        secondary_decision_threshold: null,
      },
      config_used: {
        byok_enabled: true,
        primary: {
          base_url: 'https://integrate.api.nvidia.com/v1',
          model: 'nvidia/nemotron-3-ultra-550b-a55b',
          base_url_source: 'env',
          model_source: 'env',
          key_source: 'header',
        },
        secondary: {
          base_url: 'https://integrate.api.nvidia.com/v1',
          model: 'google/diffusiongemma-26b-a4b-it',
          base_url_source: 'env',
          model_source: 'env',
          key_source: 'header',
        },
        thresholds: {
          ai_prob: 0.69, ai_conf: 0.6, human_prob: 0.3,
          human_conf: 0.6, secondary_decision: 0.9, single_model_penalty: 1,
        },
        sampling: { temperature: 0.1, top_p: 0.9, max_tokens: 700 },
        tuning_applied: { human_prob_threshold: 0.3, temperature: 0.1 },
      },
    };
    setValidText();
    app.run();
    http.expectOne('/api/detect').flush(tuned);
    await fixture.whenStable();
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const text = compiled.textContent ?? '';
    expect(text).toContain('AI ≥ 69%');
    expect(text).not.toContain('temperature 0.1');
    expect(text).not.toContain('max_tokens');
    expect(text).not.toContain('human_prob_threshold');
    expect(text).not.toContain('integrate.api.nvidia.com');
    expect(text).not.toContain('sent with the request');
    // The model that answered is still named — that is output, not internals.
    expect(compiled.querySelector('.readout-models')?.textContent).toContain(
      'nvidia/nemotron-3-ultra-550b-a55b',
    );
  });

  it('shows the server rejection verbatim on HTTP 400', () => {
    setValidText();
    app.run();

    const req = http.expectOne('/api/detect');
    req.flush(
      {
        error: 'base_url host "evil.com" is not in ALLOWED_LLM_HOSTS (localhost, 127.0.0.1)',
        error_code: 'LANE_CONFIG_INVALID',
        hint: 'lane_config accepts base_url and model only',
      },
      { status: 400, statusText: 'Bad Request' },
    );
    fixture.detectChanges();

    const notice = app.notice();
    expect(notice?.kind).toBe('rejected');
    if (notice?.kind !== 'rejected') throw new Error('expected a rejection notice');
    expect(notice.errorCode).toBe('LANE_CONFIG_INVALID');

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.textContent).toContain('not in ALLOWED_LLM_HOSTS');
    expect(compiled.textContent).toContain('lane_config accepts base_url and model only');
  });

  it('reports an unreachable gateway when nothing answers', () => {
    setValidText();
    app.run();

    const req = http.expectOne('/api/detect');
    req.error(new ProgressEvent('error'));
    fixture.detectChanges();

    expect(app.notice()?.kind).toBe('unreachable');
    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.textContent).toContain('Cannot reach the detection path');
  });

  it('renders the per-lane failure detail when every model fails', () => {
    setValidText();
    app.run();

    const req = http.expectOne('/api/detect');
    req.flush(
      {
        ...RESULT,
        classification: 'DETECTION_UNAVAILABLE',
        method: 'ALL_MODELS_FAILED',
        review_required: true,
        ai_probability: null,
        confidence: null,
        error: 'All configured models failed to return a usable verdict.',
        evidence: {
          ...RESULT.evidence!,
          models: [
            {
              lane: 'primary',
              model: 'nvidia/nemotron-3-ultra-550b-a55b',
              ok: false,
              skipped: false,
              error_code: '401',
              error_detail: 'Invalid API key provided',
              salvaged: false,
              truncated: false,
              result: null,
            },
          ],
        },
      } as DetectionResult,
      { status: 200, statusText: 'OK' },
    );
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.verdict')?.textContent).toContain('DETECTION_UNAVAILABLE');
    expect(compiled.textContent).toContain('Invalid API key provided');
  });

  it('loads providers, stored keys and tuned parameters from SQLite on the Admin tab', () => {
    app.setView('admin');
    fixture.detectChanges();

    http
      .expectOne((req) => req.url === '/api/providers')
      .flush([
        {
          id: 1,
          name: 'nvidia',
          label: 'NVIDIA NIM',
          base_url: 'https://integrate.api.nvidia.com/v1',
          auth_header: 'Authorization',
          auth_prefix: 'Bearer ',
          free_tier: true,
          enabled: true,
          notes: 'shared worker pool saturates',
          key_count: 1,
          model_count: 1,
          keys: [
            {
              id: 7,
              provider_id: 1,
              label: 'primary',
              key_hint: 'nvapi-...(41 chars)',
              is_active: true,
              last_used_at: '2026-10-06 20:04:11.512',
              created_at: '2026-10-06T20:00:00Z',
              updated_at: '2026-10-06T20:00:00Z',
            },
            {
              id: 8,
              provider_id: 1,
              label: 'secondary lane',
              key_hint: 'nvapi-...(38 chars)',
              is_active: true,
              last_used_at: null,
              created_at: '2026-10-06T20:00:00Z',
              updated_at: '2026-10-06T20:00:00Z',
            },
          ],
          models: [
            {
              id: 3,
              provider_id: 1,
              model_id: 'nvidia/nemotron-3-ultra-550b-a55b',
              display_name: 'Nemotron 3 Ultra 550B',
              role: 'primary',
              lane: 'primary',
              temperature: 0.05,
              top_p: 0.9,
              max_tokens: 700,
              timeout_ms: 90000,
              ai_prob_threshold: 0.69,
              ai_conf_threshold: 0.6,
              human_prob_threshold: 0.4,
              human_conf_threshold: 0.6,
              secondary_decision_threshold: 0.9,
              notes: 'best measured separation',
              enabled: true,
              measured: true,
            },
          ],
        },
      ]);

    // The Admin tab now also carries the engine card: it reads the admin settings and the
    // engine's reachability on load, so both have an answer here or they sit outstanding and
    // `http.verify()` in afterEach fails the run.
    http
      .expectOne((req) => req.url === '/api/settings')
      .flush({
        paper_url: '', paper_title: '', api_key_guides: [], allowed_llm_hosts: '',
        n8n_url: '', webhook_path: '', ai_prob_threshold: '', human_prob_threshold: '',
        secondary_decision_threshold: '',
      });
    http
      .expectOne((req) => req.url === '/api/n8n/status')
      .flush({
        n8n_url: 'http://localhost:5678',
        workflow_id: 'ai-detection-ai-only',
        reachable: true,
        healthz: 200,
        db_readable: true,
        published: true,
        webhook_registered: true,
      });

    http
      .expectOne((req) => req.url === '/api/gateway-health')
      .flush({
        ok: true,
        purge_enabled: true,
        rows_purged: 4,
        startup_max_id: 10,
        newest_id: 10,
        quiet_sweeps: 3,
        workflow_id: 'ai-detection-ai-only',
        db_path: 'backend/data/detector.sqlite3',
        db_counts: { providers: 1, api_keys: 1, models: 1 },
      });

    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.textContent).toContain('nvidia/nemotron-3-ultra-550b-a55b');
    // The stored-key HINTS are not drawn here any more. That table moved to Models & keys, beside the
    // endpoint mark a stored key lights up (`app-admin-keys`); this surface keeps the count, because
    // it still decides which model row is dialled with those keys.
    expect(compiled.textContent).toContain('1 key(s) · 1 model(s)');
    expect(compiled.textContent).not.toContain('nvapi-...(41 chars)');
    expect(compiled.textContent).toContain('Purge gateway active');
    expect(compiled.textContent).toContain('rows deleted since start: 4');
    // Hosted keeps the old vocabulary: here the settings really are one role's controls over
    // other people's defaults, which is the distinction the local rename removes.
    expect(compiled.textContent).toContain('Admin controls');
    expect(compiled.querySelector('.nav')?.textContent).toContain('Admin');
    expect(compiled.querySelector('.user-chip .role')?.textContent).toContain('admin');
  });

  it('hides the Admin tab from a non-admin and refuses to open it', () => {
    const f = TestBed.createComponent(App);
    const a = f.componentInstance;
    f.detectChanges();
    http
      .expectOne((r) => r.url === '/api/auth/me')
      .flush({ user: { ...ADMIN, role: 'user', username: 'alice' } } as AuthResponse);
    f.detectChanges();

    expect(a.tabs().some((t) => t.id === 'admin')).toBe(false);
    // The other half of the split: a plain account gets the surface that holds its own keys and
    // pool -- that is the tab a locked ranking card routes to -- and still not the engine's.
    const nav = (f.nativeElement as HTMLElement).querySelector('.nav')?.textContent ?? '';
    expect(nav).toContain('Models & keys');
    expect(nav).not.toContain('Admin');
    a.setView('admin'); // guarded: a non-admin must not reach it
    expect(a.view()).toBe('detect');
    http.verify();
  });

  it('ranks measured models at their own band and links the paper on the Ranking tab', () => {
    app.setView('ranking');
    fixture.detectChanges();

    // The split's contract on this page: `/config` then `/ranking`, and nothing else -- no key
    // list, no pool, no jobs. Those are the other tab's reads now.
    http
      .expectOne((req) => req.url === '/api/config')
      .flush({
        paper_url: 'https://zenodo.org/record/1234567',
        paper_title: 'Free-model lanes for AI-text detection',
        api_key_guides: [
          { provider: 'NVIDIA', title: 'Get an NVIDIA key', url: 'https://build.nvidia.com', steps: 'Sign up, copy the nvapi key.' },
        ],
        providers: [
          {
            id: 1,
            name: 'nvidia',
            label: 'NVIDIA NIM',
            base_url: 'https://integrate.api.nvidia.com/v1',
            free_tier: true,
            host: 'integrate.api.nvidia.com',
            has_admin_key: true,
            models: [
              { model_id: 'nvidia/nemotron-3-ultra-550b-a55b', display_name: 'Nemotron', lane: 'primary' },
            ],
          },
        ],
        user: ADMIN,
      });

    http
      .expectOne((req) => req.url === '/api/ranking')
      .flush({
        ranked: [
          {
            rank: 1,
            model: 'nvidia/nemotron-3-ultra-550b-a55b',
            tag: 'ultra550',
            corpus: 'hc3-48',
            provider: 'nvidia',
            provider_id: 1,
            provider_name: 'NVIDIA NIM',
            free_tier: true,
            n_rows: 48,
            n_clean: 48,
            correct: 45,
            wrong: 0,
            review: 3,
            decided: 45,
            accuracy: 0.9375,
            coverage: 0.9375,
            band: { ai: 0.69, human: 0.4, gate: 0 },
            lane: 'primary',
            verified_key: false,
            admin_key: true,
          },
        ],
        paper_url: 'https://zenodo.org/record/1234567',
        note: '',
      });

    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const text = compiled.textContent ?? '';
    expect(text).toContain('nvidia/nemotron-3-ultra-550b-a55b');
    expect(text).toContain('#1');
    expect(text).toContain('deployed · primary');
    expect(text).toContain('shared key');
    expect(text).toContain('Free-model lanes for AI-text detection');
    // The study is read in a sheet over the table, so the head carries an act and not a link — and
    // the sheet is not mounted until that act is taken.
    expect(compiled.querySelector('a.paper-link')).toBeNull();
    expect(compiled.querySelector('button.paper-link')).toBeTruthy();
    expect(compiled.querySelector('app-report-sheet')).toBeFalsy();
    expect(compiled.querySelector('app-circular-metric')).toBeTruthy();
    // The negative half of the split, asserted on the page that used to carry all of it: no key
    // form, no key guides, no pool. Those live on `Models & keys` now, and a card that cannot be
    // pointed at a scorer sends the user there instead of hosting the form beside the table.
    expect(text).not.toContain('Get an NVIDIA key');
    expect(compiled.querySelector('input[type=password]')).toBeFalsy();
    expect(compiled.querySelector('.pool-panel')).toBeFalsy();
  });

  it('scores with the model picked on the Ranking tab, from the card button to the request body', () => {
    app.setView('ranking');
    fixture.detectChanges();

    http.expectOne((req) => req.url === '/api/config').flush({
      paper_url: '', paper_title: '', api_key_guides: [],
      providers: [{ id: 1, name: 'nvidia', label: 'NVIDIA NIM',
                    base_url: 'https://integrate.api.nvidia.com/v1', free_tier: true,
                    host: 'integrate.api.nvidia.com', has_admin_key: true, models: [] }],
      user: ADMIN,
    });
    const card = (over: Record<string, unknown>) => ({
      rank: 1, model: 'm', tag: 't', corpus: 'hc3-48', provider: 'nvidia', provider_id: 1,
      provider_name: 'NVIDIA NIM', free_tier: true, n_rows: 48, n_clean: 48, correct: 45,
      wrong: 0, review: 3, decided: 45, accuracy: 0.9, coverage: 0.9,
      band: { ai: 0.69, human: 0.4, gate: 0 }, lane: null, verified_key: false, admin_key: true,
      ...over,
    });
    http.expectOne((req) => req.url === '/api/ranking').flush({
      ranked: [
        card({ model: 'nvidia/nemotron-3-ultra-550b-a55b', tag: 'ultra', lane: 'primary' }),
        card({ model: 'google/diffusiongemma-26b-a4b-it', tag: 'diff', rank: 2, lane: 'secondary' }),
      ],
      paper_url: '', note: '',
    });
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const btns = Array.from(compiled.querySelectorAll<HTMLButtonElement>('.pick-btn'));
    expect(btns.length).toBe(2);

    // Clicking the DEPLOYED TIE-BREAK model's card: it becomes the scorer, and because that
    // model would otherwise be asked to confirm itself, the second lane is switched off.
    btns[1].click();
    fixture.detectChanges();

    expect(app.view()).toBe('detect');
    expect(app.pickedModel()).toBe('google/diffusiongemma-26b-a4b-it');
    expect(app.pickedIsTieBreak()).toBe(true);
    expect(app.form.secondaryEnabled()).toBe(false);
    expect(compiled.querySelector('.pick-banner')?.textContent)
      .toContain('google/diffusiongemma-26b-a4b-it');
    expect(compiled.querySelector('.pick-note')?.textContent).toContain('cannot corroborate itself');

    setValidText();
    app.run();
    const req = http.expectOne('/api/detect');
    expect(req.request.body).toEqual({
      text: app.form.text(),
      lane_config: { secondary: { enabled: false }, primary: { model: 'google/diffusiongemma-26b-a4b-it' } },
    });
    req.flush(RESULT);

    // Releasing the pick falls back to the deployed lane AND puts the tie-break back, because
    // this pick is the only reason it was off.
    app.clearPickedModel();
    fixture.detectChanges();
    expect(app.pickedModel()).toBeNull();
    expect(app.form.secondaryEnabled()).toBe(true);
    expect(compiled.querySelector('.pick-banner')).toBeFalsy();
  });

  /** Two endpoints, so a card's `provider_id` has something to select: one with a shared key
      behind it (pickable) and one without (not). Only the fields the two surfaces read are set. */
  const TWO_PROVIDERS = {
    paper_url: '',
    paper_title: '',
    api_key_guides: [
      { provider: 'openrouter', title: 'Get an OpenRouter key', url: 'https://openrouter.ai/keys', steps: 'Sign in, create a key.' },
    ],
    providers: [
      { id: 1, name: 'nvidia', label: 'NVIDIA NIM', base_url: 'https://integrate.api.nvidia.com/v1',
        free_tier: true, host: 'integrate.api.nvidia.com', has_admin_key: true, models: [] },
      { id: 2, name: 'openrouter', label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1',
        free_tier: true, host: 'openrouter.ai', has_admin_key: false, models: [] },
    ],
    user: ADMIN,
  };

  const RANKED_ROW = (over: Record<string, unknown>) => ({
    rank: 1, model: 'x/y', tag: 'xy', corpus: 'hc3-48', provider: 'nvidia', provider_id: 1,
    provider_name: 'NVIDIA NIM', free_tier: true, n_rows: 48, n_clean: 48, correct: 45, wrong: 0,
    review: 3, decided: 45, accuracy: 0.9, coverage: 0.9,
    band: { ai: 0.8, human: 0.55, gate: 0 }, lane: null,
    verified_key: false, admin_key: true, health: 'unknown', in_pool: false,
    ...over,
  });

  const POOL_ROW = (over: Record<string, unknown>) => ({
    provider: 'nvidia', model: 'x/y', source: 'user', your_rank: 1, position: 1, health: 'ok',
    consecutive_failures: 0, last_error: '', last_ok_at: null, demoted: false, ...over,
  });

  /** Answer the five reads the Models & keys surface makes on open. It is the only place that
      touches keys or the pool now, so every test that lands on it owes all five -- otherwise
      `verify()` fails on an outstanding request for a reason unrelated to what the test checks.
      The pickable list is `/api/my/pool/options`, NOT `/api/ranking`: a model that has never been
      measured is still pickable, and a ranking read cannot express that. */
  function flushKeySurface(options: unknown[] = []): void {
    http.expectOne((req) => req.url === '/api/config').flush(TWO_PROVIDERS);
    http
      .expectOne((req) => req.url === '/api/my/pool/options')
      .flush({ options, window_s: 600, fast_ms: 45000, note: '' });
    http.expectOne((req) => req.url === '/api/my/keys').flush([]);
    http.expectOne((req) => req.url === '/api/my/pool').flush({ pool: [POOL_ROW({})] });
    http
      .expectOne((req) => req.url === '/api/my/discover')
      .flush({ jobs: [], screen_texts: 'probe2', note: '' });
  }

  it('holds the key form, the guides and the using pool on the Models & keys tab', () => {
    app.setView('providers');
    fixture.detectChanges();
    flushKeySurface();
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    // The endpoints are stickers and the key form is shut until asked for: this test is about which
    // surface holds keys at all, so it checks the sticker row and then opens the form.
    expect(compiled.querySelectorAll('button[data-act="pick-endpoint"]').length).toBe(2);
    expect(compiled.querySelector('button[data-act="add-key"]')).toBeTruthy();
    (compiled.querySelector('button[data-act="add-key"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(compiled.querySelector('input[type=password]')).toBeTruthy();
    expect(compiled.querySelector('.pool-panel')).toBeTruthy();
    expect(compiled.querySelectorAll('.pool-row').length).toBe(1);

    const text = compiled.textContent ?? '';
    expect(text).toContain('Get an OpenRouter key');
    expect(text).toContain('Your using pool');
    expect(text).toContain('x/y');
    // With nothing routing the user here, the picker opens on the first endpoint.
    expect(compiled.querySelector('button[data-act="pick-endpoint"][aria-pressed="true"]')
      ?.textContent).toContain('NVIDIA NIM');
  });

  it('routes a locked ranking card to the keys surface, preselected on that endpoint', () => {
    app.setView('ranking');
    fixture.detectChanges();
    http.expectOne((req) => req.url === '/api/config').flush(TWO_PROVIDERS);
    http
      .expectOne((req) => req.url === '/api/ranking')
      .flush({
        ranked: [
          RANKED_ROW({ model: 'openrouter/deepseek', provider: 'openrouter', provider_id: 2,
                       provider_name: 'OpenRouter', verified_key: false, admin_key: false }),
        ],
        note: '',
      });
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const btn = compiled.querySelector('button.key-btn') as HTMLButtonElement;
    expect(btn.textContent).toContain('OpenRouter');
    // The card names the route rather than hosting the form: no key field is rendered on the
    // page that only measures.
    expect(compiled.querySelector('input[type=password]')).toBeFalsy();

    btn.click();
    fixture.detectChanges();
    expect(app.view()).toBe('providers');
    expect(app.pendingProvider()).toBe(2);

    flushKeySurface();
    fixture.detectChanges();
    const pressed = compiled.querySelector(
      'button[data-act="pick-endpoint"][aria-pressed="true"]',
    ) as HTMLElement;
    expect(pressed.textContent).toContain('OpenRouter');
    // Not just preselected: the card sent him here to add a key, so the field is already open.
    expect(compiled.querySelector('input[type=password]')).toBeTruthy();
  });

  it('routes a rank card to that model\'s own picker card, and lights the frame it was sent for', () => {
    // jsdom has no layout, so no element has a `scrollIntoView` at all and the render effect that
    // centres the found card would throw on every run. `providers-view.spec.ts` shims the same
    // method to assert which element got scrolled; here the scroll is not the subject — that the
    // frame lights on the page the route landed on is — so a no-op keeps the output clean.
    (HTMLElement.prototype as any).scrollIntoView ??= () => {};

    app.setView('ranking');
    fixture.detectChanges();
    http.expectOne((req) => req.url === '/api/config').flush(TWO_PROVIDERS);
    http
      .expectOne((req) => req.url === '/api/ranking')
      .flush({
        ranked: [RANKED_ROW({ model: 'z-ai/glm-5.3-flash', verified_key: true, admin_key: false })],
        note: '',
      });
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const btn = compiled.querySelector('button[data-act="open-pool"]') as HTMLButtonElement;
    expect(btn, 'a pickable card has to be able to send him to the pool').toBeTruthy();
    expect(btn.textContent).toContain('Add to using pool');
    btn.click();
    fixture.detectChanges();

    expect(app.view()).toBe('providers');
    // The address is the SLUG pair, because that is what the picker builds `data-pair` from. A
    // route built from the row's `provider_name` ("NVIDIA NIM") would land with nothing lit.
    expect(app.pendingPoolPair()).toBe('nvidia|z-ai/glm-5.3-flash');

    flushKeySurface([
      { provider: 'nvidia', provider_id: 1, provider_name: 'nvidia',
        base_url: 'https://integrate.api.nvidia.com/v1', free_tier: true,
        host: 'integrate.api.nvidia.com', model: 'z-ai/glm-5.3-flash',
        measured: true, timeout_ms: 120_000, notes: '', probe_status: 'usable', probe_note: '',
        answer_ms: 86_000, verified_key: true, admin_key: false, needs_key: false,
        in_pool: false, your_rank: null, health: 'unknown', offered: false,
        offer_seconds_left: null },
    ]);
    fixture.detectChanges();

    const card = compiled.querySelector(
      '.pick-card[data-pair="nvidia|z-ai/glm-5.3-flash"]',
    ) as HTMLElement;
    expect(card, 'the shell handed the pair over, so the menu has a frame to light').toBeTruthy();
    expect(card.classList.contains('neon')).toBe(true);
  });

  it('never says Admin on a single-owner install, in the nav or on the tab itself', () => {
    const f = TestBed.createComponent(App);
    const a = f.componentInstance;
    f.detectChanges();
    http
      .expectOne((r) => r.url === '/api/auth/me')
      .flush({ user: ADMIN, auth_mode: 'local' } as AuthResponse);
    f.detectChanges();

    const compiled = f.nativeElement as HTMLElement;
    expect(compiled.querySelector('.nav')?.textContent).toContain('Advanced parameters');
    expect(compiled.querySelector('.nav')?.textContent).not.toContain('Admin');
    // The role chip is where the word used to escape second: on an install with one account,
    // "admin" is not a role next to someone else's, it is a tautology.
    expect(compiled.querySelector('.user-chip .role')).toBeFalsy();

    a.setView('admin');
    f.detectChanges();
    // The engine card on that tab reads the admin settings and the engine's reachability on open.
    http.expectOne((req) => req.url === '/api/providers').flush([]);
    http
      .expectOne((req) => req.url === '/api/settings')
      .flush({
        paper_url: '', paper_title: '', api_key_guides: [], allowed_llm_hosts: '',
        n8n_url: '', webhook_path: '', ai_prob_threshold: '', human_prob_threshold: '',
        secondary_decision_threshold: '',
      });
    http
      .expectOne((req) => req.url === '/api/n8n/status')
      .flush({ n8n_url: 'http://localhost:5678', workflow_id: 'ai-detection-ai-only',
               reachable: true, healthz: 200, db_readable: true, published: true,
               webhook_registered: true });
    http
      .expectOne((req) => req.url === '/api/gateway-health')
      .flush({ ok: true, purge_enabled: true, rows_purged: 0, startup_max_id: 10, newest_id: 10,
               quiet_sweeps: 3, workflow_id: 'ai-detection-ai-only',
               db_path: 'backend/data/detector.sqlite3',
               db_counts: { providers: 0, api_keys: 0, models: 0 } });
    f.detectChanges();

    const text = compiled.textContent ?? '';
    expect(text).toContain('Advanced parameters. The models');
    expect(text).not.toContain('Admin controls');
    // No verify() here: the Configuration card on that tab has a GET /api/models in flight, which
    // afterEach answers for exactly that reason.
  });
});
