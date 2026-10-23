import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AdminPanel } from './admin-panel';
import { AuthService } from '../../core/auth.service';
import type {
  AdminSettings, ModelView, N8nStatus, ProviderView,
} from '../../core/backend.types';

/**
 * The engine card is the only place a packaged local app can be pointed at a detection engine it
 * can actually reach, and the only place an admin sets the paper link + key guides the Ranking page
 * shows every user. Both are behind `/settings`, which the backend gates to admins on GET and POST.
 * These tests drive the DOM, because the interesting failures (a Save button that never enables, a
 * 403 that renders as five empty fields) are invisible when poking component properties.
 */
const SETTINGS: AdminSettings = {
  paper_url: 'https://zenodo.org/doi/10.5281/zenodo.000',
  paper_title: 'Read the paper',
  api_key_guides: [{ provider: 'nvidia', url: 'https://build.nvidia.com/playground', steps: 'sign up' }],
  allowed_llm_hosts: 'integrate.api.nvidia.com, openrouter.ai',
  n8n_url: 'http://127.0.0.1:5678',
  webhook_path: '/webhook/ai-detect-ai',
  ai_prob_threshold: '',
  human_prob_threshold: '',
  secondary_decision_threshold: '',
};

const REACHABLE: N8nStatus = {
  n8n_url: 'http://127.0.0.1:5678',
  workflow_id: 'ai-detection-ai-only',
  reachable: true,
  healthz: 200,
  db_readable: true,
  workflow: null,
  published: true,
  webhook_registered: true,
  on_request_path: true,
};

/** The rows `GET /providers` nests under its provider. The secondary one starts disabled because
    that is the shipped operating point, and the column below is the authority: `models.enabled` is
    what the backend reads before it will run a tie-break at all, because a request body can only
    ever switch the lane OFF, never on. */
const PRIMARY_ROW: ModelView = {
  id: 41, provider_id: 1, model_id: 'z-ai/glm-5.3-flash', display_name: '', role: 'either',
  lane: 'primary', temperature: 0, top_p: 1, max_tokens: 0, timeout_ms: 120000,
  ai_prob_threshold: 0.8, ai_conf_threshold: null, human_prob_threshold: 0.55,
  human_conf_threshold: null, secondary_decision_threshold: null, notes: '',
  enabled: true, measured: true,
};

const SECONDARY_ROW: ModelView = {
  id: 44, provider_id: 1, model_id: 'google/diffusiongemma-26b-a4b-it', display_name: '',
  role: 'either', lane: 'secondary', temperature: 0, top_p: 1, max_tokens: 0, timeout_ms: 90000,
  ai_prob_threshold: null, ai_conf_threshold: null, human_prob_threshold: null,
  human_conf_threshold: null, secondary_decision_threshold: 0.9, notes: '',
  enabled: false, measured: true,
};

const PROVIDER: ProviderView = {
  id: 1, name: 'nvidia', label: 'NVIDIA NIM', base_url: 'https://integrate.api.nvidia.com/v1',
  auth_header: 'Authorization', auth_prefix: 'Bearer ', free_tier: true, enabled: true, notes: '',
  key_count: 0, model_count: 2, keys: [], models: [PRIMARY_ROW, SECONDARY_ROW],
};

describe('AdminPanel engine + links', () => {
  let fixture: ComponentFixture<AdminPanel>;
  let panel: AdminPanel;
  let http: HttpTestingController;

  /** ngOnInit loads providers + settings + engine status; every test starts past that.
      `whenStable()` is not decoration: NgModel writes the initial value in a microtask, so the
      inputs are still empty on the same tick the settings signal lands. */
  async function boot(
    settings: AdminSettings = SETTINGS,
    providers: ProviderView[] = [],
    status: N8nStatus = REACHABLE,
  ): Promise<void> {
    fixture.detectChanges();
    http.expectOne('/api/providers').flush(providers);
    http.expectOne('/api/settings').flush(settings);
    http.expectOne('/api/n8n/status').flush(status);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AdminPanel],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(AdminPanel);
    panel = fixture.componentInstance;
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  const findInput = (name: string): HTMLInputElement | null =>
    fixture.nativeElement.querySelector(`input[name="${name}"]`) as HTMLInputElement | null;
  const input = (name: string): HTMLInputElement => findInput(name)!;
  const buttons = (): HTMLButtonElement[] =>
    Array.prototype.slice.call(fixture.nativeElement.querySelectorAll('button')) as HTMLButtonElement[];
  const button = (label: string): HTMLButtonElement =>
    buttons().find((b) => (b.textContent ?? '').trim() === label)!;
  const type = (name: string, value: string): void => {
    const el = input(name);
    el.value = value;
    el.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  };

  it('shows the stored engine target and its reachability', async () => {
    await boot();
    expect(input('n8n_url').value).toBe('http://127.0.0.1:5678');
    expect(input('webhook_path').value).toBe('/webhook/ai-detect-ai');
    expect(fixture.nativeElement.textContent).toContain('engine reachable');
    // the address the run is posted to, in the status line rather than in a sentence about it
    expect(fixture.nativeElement.textContent).toContain('posting to http://127.0.0.1:5678');
  });

  it('names the built-in default when the setting is blank', async () => {
    await boot({ ...SETTINGS, n8n_url: '', webhook_path: '' });
    // A blank field is not an unconfigured install: the default the backend falls back to has to be
    // readable without a round trip, and the cure for a target that is not up has to be named.
    expect(input('n8n_url').placeholder).toBe('http://localhost:5678');
    expect(input('webhook_path').placeholder).toBe('/webhook/ai-detect-ai');
    expect(fixture.nativeElement.textContent).toContain('docker compose up n8n');
  });

  /** The packaged install answers `/detect` in its own process, so the workflow target it still
      carries in `settings` is a row nothing reads. Left on the page it is an instruction to go and
      fix n8n -- software that is not installed and never was -- on a machine that has already
      scored a paragraph. `/n8n/status` is what says which case this is, and the half of the card it
      makes redundant goes with it. On an ACCOUNTS deploy the other half stays: a hosted admin owns
      the citation, the gateway allowlist and the guides every visitor reads. */
  it('drops the workflow target from an install that scores in its own process', async () => {
    await boot(SETTINGS, [], {
      n8n_url: 'http://127.0.0.1:5678',
      workflow_id: 'ai-detection-ai-only',
      db_readable: false,
      on_request_path: false,
      note: 'this process answers /detect with the python-native engine; the n8n workflow is not contacted',
    });
    const text = fixture.nativeElement.textContent;
    expect(text).toContain('scores in this process');
    // a red "engine unreachable" would be a claim about a server this install does not dial
    expect(text).not.toContain('engine unreachable');
    expect(findInput('n8n_url')).toBe(null);
    expect(findInput('webhook_path')).toBe(null);
    expect(text).not.toContain('Save engine target');
    expect(text).not.toContain('docker compose up n8n');
    // and the fields a hosted admin owns for every user are untouched by which engine answers
    expect(text).toContain('Ranking page links and key guides');
    expect(input('paper_url').value).toBe('https://zenodo.org/doi/10.5281/zenodo.000');
  });

  /** Both answers a packaged install would be asked to give here — where the citation points, which
      gateway hosts may be dialled, and the per-provider key guides — are things the BUILD ships
      with, not things one person's machine configures. So on a single-owner install the card has no
      reason to exist, and the absence is the statement: there is nothing on this surface to fix. */
  it('leaves the whole card off a single-owner install', async () => {
    TestBed.inject(AuthService).mode.set('local');
    await boot(SETTINGS, [], {
      n8n_url: 'http://127.0.0.1:5678',
      workflow_id: 'ai-detection-ai-only',
      db_readable: false,
      on_request_path: false,
      note: 'this process answers /detect with the python-native engine',
    });
    const text = fixture.nativeElement.textContent;
    expect(findInput('paper_url')).toBeNull();
    expect(findInput('allowed_llm_hosts')).toBeNull();
    expect(text).not.toContain('Ranking page links and key guides');
    expect(text).not.toContain('Key guides');
  });

  it('refuses a scheme-less engine URL before sending it', async () => {
    await boot();
    type('n8n_url', 'localhost:5678');
    expect(button('Save engine target').disabled).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('absolute http(s) URL');

    type('n8n_url', 'http://n8n.example.org:5678');
    expect(button('Save engine target').disabled).toBe(false);
    button('Save engine target').click();
    fixture.detectChanges();
    const req = http.expectOne('/api/settings');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      n8n_url: 'http://n8n.example.org:5678',
      webhook_path: '/webhook/ai-detect-ai',
    });
    req.flush({ ok: true, written: { ...SETTINGS, n8n_url: 'http://n8n.example.org:5678' } });
    fixture.detectChanges();
    // Saving re-reads the engine, so the pill reflects the target that was just written.
    http.expectOne('/api/n8n/status').flush({ ...REACHABLE, n8n_url: 'http://n8n.example.org:5678' });
    http.expectOne('/api/providers').flush([] as ProviderView[]);
    http.expectOne('/api/settings').flush({ ...SETTINGS, n8n_url: 'http://n8n.example.org:5678' });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('n8n.example.org:5678');
  });

  // One act writes all three blocks: saveLinks() posts the paper link, the gateway list and every
  // guide row as a single settings document, so the page draws a single button at the end of the
  // whole group and joins it to the fields above with form="linksForm". Both halves of that move are
  // what these assertions hold down — the relocated field still reaches the payload, and a button
  // that is no longer a descendant of the form still submits it.
  it('saves the links, the gateway list and the guides with the one button at the end', async () => {
    await boot();
    expect(input('paper_url').value).toContain('zenodo.org');
    expect(input('guide_p_0').value).toBe('nvidia');
    // Typing into a guide row has to reach the payload. The rows live inside a signal, so an
    // in-place mutation would be invisible to the next read under zoneless.
    type('guide_u_0', 'https://build.nvidia.com/playground?tab=keys');
    type('paper_url', '');
    type('allowed_llm_hosts', 'gateway.example.org');
    const save = button('Save links and guides');
    expect(save.closest('form'), 'the act stands outside the field row it saves').toBe(null);
    save.click();
    fixture.detectChanges();
    const req = http.expectOne('/api/settings');
    expect(req.request.method).toBe('POST');
    expect(req.request.body.paper_url).toBe('');
    expect(req.request.body.allowed_llm_hosts).toBe('gateway.example.org');
    expect(req.request.body.api_key_guides).toEqual([
      { provider: 'nvidia', url: 'https://build.nvidia.com/playground?tab=keys', steps: 'sign up' },
    ]);
    req.flush({ ok: true, written: { ...SETTINGS, paper_url: '' } });
    fixture.detectChanges();
    http.expectOne('/api/n8n/status').flush(REACHABLE);
    http.expectOne('/api/providers').flush([] as ProviderView[]);
    http.expectOne('/api/settings').flush({ ...SETTINGS, paper_url: '' });
    expect(panel.settingsBlocked()).toBe(false);
  });

  it('adds and removes a guide row', async () => {
    await boot();
    button('Add guide').click();
    fixture.detectChanges();
    expect(findInput('guide_p_1')).not.toBeNull();
    type('guide_p_1', 'openrouter');
    type('guide_u_1', 'https://openrouter.ai/settings/keys');
    button('Remove').click(); // first row's button; there are now two
    fixture.detectChanges();
    button('Remove').click();
    fixture.detectChanges();
    expect(findInput('guide_p_0')).toBeNull();
  });

  it('renders as admin-only when GET /settings answers 403', () => {
    fixture.detectChanges();
    http.expectOne('/api/providers').flush([] as ProviderView[]);
    http.expectOne('/api/n8n/status').flush(REACHABLE);
    http.expectOne('/api/settings').flush({ error: 'ADMIN_REQUIRED' }, { status: 403, statusText: 'Forbidden' });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('admin-only');
    expect(input('n8n_url')).toBeNull();
  });

});
