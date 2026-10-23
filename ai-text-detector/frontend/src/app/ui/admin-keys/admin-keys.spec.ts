import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AdminKeys } from './admin-keys';
import type { ApiKeyView, ProviderView } from '../../core/backend.types';

/**
 * The shared credentials, on the page that draws the logos they light up.
 *
 * A key stored here decides whether an endpoint's mark is lit on `Models & keys`, so the table and
 * the mark cannot live several screens apart — that is how "I saved a key and nothing changed"
 * happens. These specs drive the DOM, because the failures worth catching are a paste that is sent
 * without the character check, and a row that stays lit after the delete.
 */
const KEY: ApiKeyView = {
  id: 7, provider_id: 1, label: 'primary', key_hint: 'nvapi-...(41 chars)',
  is_active: true, last_used_at: '2026-10-06 20:04:11.512',
  created_at: '2026-10-06T20:00:00Z', updated_at: '2026-10-06T20:00:00Z',
};

const PROVIDER: ProviderView = {
  id: 1, name: 'nvidia', label: 'NVIDIA NIM', base_url: 'https://integrate.api.nvidia.com/v1',
  auth_header: 'Authorization', auth_prefix: 'Bearer ', free_tier: true, enabled: true, notes: '',
  key_count: 1, model_count: 0, keys: [KEY], models: [],
};

describe('AdminKeys', () => {
  let fixture: ComponentFixture<AdminKeys>;
  let panel: AdminKeys;
  let http: HttpTestingController;

  async function boot(providers: ProviderView[] = [PROVIDER]): Promise<void> {
    fixture.detectChanges();
    http.expectOne('/api/providers').flush(providers);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AdminKeys],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(AdminKeys);
    panel = fixture.componentInstance;
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  const text = (): string => fixture.nativeElement.textContent ?? '';
  const findInput = (name: string): HTMLInputElement | null =>
    fixture.nativeElement.querySelector(`input[name="${name}"], select[name="${name}"]`);
  const input = (name: string): HTMLInputElement => findInput(name) as HTMLInputElement;
  const button = (label: string): HTMLButtonElement =>
    (Array.prototype.slice.call(fixture.nativeElement.querySelectorAll('button')) as HTMLButtonElement[])
      .find((b) => (b.textContent ?? '').trim() === label)!;
  /** NgModel's first write lands on a microtask, so a form driven without settling here keeps its
      empty draft and the click that follows answers "Pick an endpoint first" about a field the DOM
      plainly shows filled. Every type/open below goes through this. */
  async function type(name: string, value: string): Promise<void> {
    const el = input(name);
    el.value = value;
    el.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function open(which: 'addingKey' | 'addingProvider'): Promise<void> {
    button(which === 'addingKey' ? 'Add shared key' : 'Add endpoint').click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  /** The row-actions toggle and the form's submit button can share a label, and both forms can be
      open at once, so the submit is taken from the form that holds the named field. */
  const submitIn = (field: string): HTMLButtonElement => {
    const form = Array.prototype.slice.call(fixture.nativeElement.querySelectorAll('form'))
      .find((f: HTMLFormElement) => f.querySelector(`[name="${field}"]`)) as HTMLFormElement;
    return form.querySelector('button[type="submit"]') as HTMLButtonElement;
  };

  async function submit(field: string): Promise<void> {
    submitIn(field).click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('lists every stored key with the endpoint it belongs to and when it last served a run', async () => {
    await boot();
    const row = fixture.nativeElement.querySelector('tbody tr') as HTMLElement;
    expect(row.textContent).toContain('NVIDIA NIM');
    expect(row.textContent).toContain('nvapi-...(41 chars)');
    // The hint is all the page ever gets, and the row has to say which endpoint it is for.
    expect(row.textContent).not.toContain('nvapi-abcdefghijklmnopqrstuvwxyz');
    expect(row.querySelector('button')?.textContent).toContain('deactivate');
  });

  it('says what happens when no shared key exists', async () => {
    await boot([{ ...PROVIDER, key_count: 0, keys: [] }]);
    expect(text()).toContain('PROVIDER_KEY_MISSING');
  });

  it('refuses a key with a character the backend rejects, and sends nothing', async () => {
    await boot();
    await open('addingKey');
    await type('ak_value', 'abc def\n');
    await submit('ak_value');
    expect(text()).toContain('character the backend rejects');
    expect(http.match(() => true)).toEqual([]);
  });

  it('stores a key with the endpoint the picker holds, and reloads the table', async () => {
    await boot();
    await open('addingKey');
    await type('ak_value', 'nvapi-abcdef123456');
    // The endpoint is whatever the form opened pointing at — no select was touched, and the row
    // still has to be written to provider 1 rather than the "— choose —" placeholder.
    await submit('ak_value');
    const req = http.expectOne('/api/providers/1/keys');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ api_key: 'nvapi-abcdef123456', label: 'key' });
    const stored = { ...KEY, id: 9, key_hint: 'nvapi-a...(16 chars)' };
    req.flush(stored);
    http.expectOne('/api/providers').flush([{ ...PROVIDER, key_count: 2, keys: [KEY, stored] }]);
    fixture.detectChanges();
    const hints = Array.prototype.slice.call(fixture.nativeElement.querySelectorAll('tbody tr td.mono'))
      .map((td: HTMLElement) => td.textContent);
    expect(hints).toEqual(['nvapi-...(41 chars)', 'nvapi-a...(16 chars)']);
  });

  it('keeps the paste when the backend refuses the key', async () => {
    await boot();
    await open('addingKey');
    await type('ak_value', 'nvapi-abcdef123456');
    await submit('ak_value');
    const req = http.expectOne('/api/providers/1/keys');
    req.flush({ error: 'ENCRYPT_FAILED' }, { status: 500, statusText: 'Server error' });
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(input('ak_value').value).toBe('nvapi-abcdef123456');
    expect(text()).toContain('Key not stored');
  });

  it('adds an endpoint so its logo can be lit, and names the row it wrote', async () => {
    await boot();
    await open('addingProvider');
    await type('ap_name', 'deepseek');
    await type('ap_label', 'DeepSeek');
    await type('ap_url', 'api.deepseek.com/v1');
    await submit('ap_url');
    expect(text()).toContain('absolute http(s) URL');
    expect(http.match(() => true)).toEqual([]);

    await type('ap_url', 'https://api.deepseek.com/v1');
    await submit('ap_url');
    const req = http.expectOne('/api/providers');
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      name: 'deepseek', base_url: 'https://api.deepseek.com/v1', label: 'DeepSeek', notes: '',
    });
    req.flush({ id: 5, name: 'deepseek', label: 'DeepSeek' });
    http.expectOne('/api/providers').flush([PROVIDER]);
    fixture.detectChanges();
    expect(text()).toContain('Endpoint "deepseek" added');
  });

  it('tells the page that drew the logos that something about paying changed', async () => {
    await boot();
    let seen = 0;
    panel.changed.subscribe(() => (seen += 1));
    await open('addingKey');
    await type('ak_value', 'nvapi-abcdef123456');
    await submit('ak_value');
    http.expectOne('/api/providers/1/keys').flush(KEY);
    http.expectOne('/api/providers').flush([PROVIDER]);
    fixture.detectChanges();
    expect(seen).toBe(1);
  });

  it('reads back the deactivate it wrote instead of guessing the row away', async () => {
    await boot();
    const toggle = fixture.nativeElement.querySelector('tbody tr button') as HTMLButtonElement;
    toggle.click();
    fixture.detectChanges();
    const req = http.expectOne('/api/keys/7');
    expect(req.request.method).toBe('PATCH');
    expect(req.request.body).toEqual({ is_active: false });
    req.flush({ ...KEY, is_active: false });
    http.expectOne('/api/providers').flush([{ ...PROVIDER, keys: [{ ...KEY, is_active: false }] }]);
    fixture.detectChanges();
    expect(panel.rows()[0].is_active).toBe(false);
    expect(fixture.nativeElement.querySelector('tbody tr button')?.textContent)
      .toContain('activate');
  });
});
