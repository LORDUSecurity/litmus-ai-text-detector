import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AdminPanel } from './admin-panel';
import type { AdminSettings, N8nStatus, ProviderView } from '../../core/backend.types';

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
};

describe('AdminPanel engine + links', () => {
  let fixture: ComponentFixture<AdminPanel>;
  let panel: AdminPanel;
  let http: HttpTestingController;

  /** ngOnInit loads providers + settings + engine status; every test starts past that.
      `whenStable()` is not decoration: NgModel writes the initial value in a microtask, so the
      inputs are still empty on the same tick the settings signal lands. */
  async function boot(settings: AdminSettings = SETTINGS): Promise<void> {
    fixture.detectChanges();
    http.expectOne('/api/providers').flush([] as ProviderView[]);
    http.expectOne('/api/settings').flush(settings);
    http.expectOne('/api/n8n/status').flush(REACHABLE);
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
    expect(fixture.nativeElement.textContent).toContain('engine reachable');
    expect(fixture.nativeElement.textContent).toContain('/webhook/ai-detect-ai');
  });

  it('names the built-in default when the setting is blank', async () => {
    await boot({ ...SETTINGS, n8n_url: '', webhook_path: '' });
    // The packaged app ships with both blank, so the card has to state what will be used.
    expect(fixture.nativeElement.textContent).toContain('http://localhost:5678/webhook/ai-detect-ai');
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

  it('saves the ranking-page links and the guides together', async () => {
    await boot();
    expect(input('paper_url').value).toContain('zenodo.org');
    expect(input('guide_p_0').value).toBe('nvidia');
    // Typing into a guide row has to reach the payload. The rows live inside a signal, so an
    // in-place mutation would be invisible to the next read under zoneless.
    type('guide_u_0', 'https://build.nvidia.com/playground?tab=keys');
    type('paper_url', '');
    button('Save links').click();
    fixture.detectChanges();
    const req = http.expectOne('/api/settings');
    expect(req.request.body.paper_url).toBe('');
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
