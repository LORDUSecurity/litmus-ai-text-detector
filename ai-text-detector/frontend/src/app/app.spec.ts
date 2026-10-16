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
    expect(compiled.textContent).toContain('91.0%');
  });

  it('names the stored parameters that actually decided the verdict', async () => {
    const tuned: DetectionResult = {
      ...RESULT,
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
    expect(compiled.textContent).toContain('AI ≥ 0.69');
    expect(compiled.textContent).toContain('HUMAN ≤ 0.3');
    expect(compiled.textContent).toContain('temperature 0.1 · top_p 0.9 · max_tokens 700');
    expect(compiled.textContent).toContain('human_prob_threshold=0.3');
    expect(compiled.textContent).toContain('sent with the request');
    expect(compiled.textContent).not.toContain('from this page');
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
    expect(compiled.textContent).toContain('nvapi-...(41 chars)');
    expect(compiled.textContent).toContain('nvapi-...(38 chars)');
    expect(compiled.textContent).toContain('never');
    expect(compiled.textContent).not.toContain('20:04:11.512');
    expect(compiled.textContent).toContain('Purge gateway active');
    expect(compiled.textContent).toContain('rows deleted since start: 4');
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
    a.setView('admin'); // guarded: a non-admin must not reach it
    expect(a.view()).toBe('detect');
    http.verify();
  });

  it('ranks measured models at their own band and links the paper on the Ranking tab', () => {
    app.setView('ranking');
    fixture.detectChanges();

    // config() and myKeys() are both subscribed up front; ranking() only after config resolves.
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

    http.expectOne((req) => req.url === '/api/my/keys').flush([]);

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
    expect(compiled.querySelector('a.paper-link')?.getAttribute('href')).toBe(
      'https://zenodo.org/record/1234567',
    );
    expect(compiled.querySelector('app-circular-metric')).toBeTruthy();
    expect(text).toContain('Get an NVIDIA key');
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
    http.expectOne((req) => req.url === '/api/my/keys').flush([]);
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
});
