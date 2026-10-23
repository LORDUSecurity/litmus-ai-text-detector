import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TieBreakSwitch } from './tie-break-switch';
import type { ModelView, N8nStatus, ProviderView } from '../../core/backend.types';

/**
 * The tie-break switch, on the surface that spends the models.
 *
 * It is a shared setting, not a browser preference: `detect()` reads `models.enabled` to decide
 * whether the second lane may run at all, and a request body can only ever switch it off. So the
 * only way an owner gets the second opinion back is a write to the row that owns it — which is what
 * these specs prove by driving the checkbox rather than poking the component.
 *
 * It used to sit at the bottom of Advanced parameters, several screens from the model tables whose
 * lane column it depends on. The card moved; the write it performs did not.
 */
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

/** `on_request_path: true` is the hosted case: the workflow is dialled, and it rejects an unknown
    `lane_config` field, so a leg that gives no answer is not replaced. */
const WORKFLOW: N8nStatus = {
  n8n_url: 'http://127.0.0.1:5678',
  workflow_id: 'ai-detection-ai-only',
  reachable: true,
  healthz: 200,
  db_readable: true,
  published: true,
  webhook_registered: true,
  on_request_path: true,
};

const NATIVE: N8nStatus = {
  n8n_url: 'http://127.0.0.1:5678',
  workflow_id: 'ai-detection-ai-only',
  db_readable: false,
  on_request_path: false,
  note: 'this process answers /detect with the python-native engine',
};

describe('TieBreakSwitch', () => {
  let fixture: ComponentFixture<TieBreakSwitch>;
  let http: HttpTestingController;

  const providerWithLane = (enabled: boolean): ProviderView => ({
    ...PROVIDER, models: [PRIMARY_ROW, { ...SECONDARY_ROW, enabled }],
  });

  async function boot(
    providers: ProviderView[] = [PROVIDER],
    status: N8nStatus = WORKFLOW,
  ): Promise<void> {
    fixture.detectChanges();
    http.expectOne('/api/providers').flush(providers);
    http.expectOne('/api/n8n/status').flush(status);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TieBreakSwitch],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(TieBreakSwitch);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  const input = (name: string): HTMLInputElement =>
    fixture.nativeElement.querySelector(`input[name="${name}"]`) as HTMLInputElement;
  const findInput = (name: string): HTMLInputElement | null =>
    fixture.nativeElement.querySelector(`input[name="${name}"]`) as HTMLInputElement | null;
  const note = (): string =>
    (fixture.nativeElement.querySelector('.lane-note')?.textContent ?? '')
      .replace(/\s+/g, ' ').trim();

  /** The write is the answer: the switch was set optimistically from the click and the row it wrote
      is the same row this card read, so a confirmation does not need a second GET to be honest. What
      a refused write DOES need is the rollback, which the failure case below asserts. */
  function confirmWrite(laneEnabled: boolean): void {
    fixture.detectChanges();
    const req = http.expectOne((r) => r.url === '/api/models/44' && r.method === 'PATCH');
    expect(req.request.body).toEqual({ enabled: laneEnabled });
    req.flush({ ...SECONDARY_ROW, enabled: laneEnabled });
    fixture.detectChanges();
  }

  it('binds the switch to the row that owns the lane, at the state the server has', async () => {
    await boot();
    const box = input('secondary_lane');
    expect(box.checked).toBe(false);
    expect(box.disabled).toBe(false);
    expect(fixture.nativeElement.querySelector('.lane-card')?.textContent)
      .toContain('google/diffusiongemma-26b-a4b-it');
  });

  it('promises the walk to the install that scores in its own process', async () => {
    await boot([PROVIDER], NATIVE);
    expect(note()).toContain('right below');
    expect(note()).toContain('keeps going down the pool');
    expect(note()).not.toContain('does not get replaced');
  });

  it('does not promise a walk to a deploy that scores through the n8n workflow', async () => {
    await boot();
    expect(note()).toContain('right below');
    expect(note()).toContain('does not get replaced');
    expect(note()).not.toContain('keeps going down the pool');
  });

  /** The note used to read as if the pool always supplied the second leg. It does not: a primary
      named by the caller leaves the ranking unread and dials the row that owns the lane, which is
      how a "test this model" run gets a tie-break by a model the user never pooled. */
  it('names the run that does not follow the ranking, and the model it dials instead', async () => {
    await boot([PROVIDER], NATIVE);
    expect(note()).toContain('Score with this model');
    expect(note()).toContain('skips the pool');
    expect(note()).toContain('google/diffusiongemma-26b-a4b-it');
    expect(note()).not.toContain('always follows');
  });

  it('writes the row when the switch is turned on, then reads the server answer back', async () => {
    await boot();
    input('secondary_lane').click();
    confirmWrite(true);
    expect(input('secondary_lane').checked).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('tie-break lane is on');
  });

  it('writes the same field false when the lane is turned back off', async () => {
    await boot([providerWithLane(true)]);
    expect(input('secondary_lane').checked).toBe(true);
    input('secondary_lane').click();
    confirmWrite(false);
    expect(input('secondary_lane').checked).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('REVIEW');
  });

  it('puts the switch back where the server left it when the write is refused', async () => {
    await boot([providerWithLane(true)]);
    input('secondary_lane').click();
    fixture.detectChanges();
    const req = http.expectOne((r) => r.url === '/api/models/44' && r.method === 'PATCH');
    req.flush({ error: 'admin role required', error_code: 'FORBIDDEN' },
              { status: 403, statusText: 'Forbidden' });
    fixture.detectChanges();
    // Nothing was written, so the box may not sit there looking enabled: the next detection would
    // still run the lane the server actually has.
    expect(input('secondary_lane').checked).toBe(true);
    expect(note()).not.toContain('admin role required');
    expect(fixture.nativeElement.textContent).toContain('not changed');
  });

  it('explains why there is no switch when no row owns the lane', async () => {
    await boot([{ ...PROVIDER, models: [PRIMARY_ROW] }]);
    expect(findInput('secondary_lane')).toBeNull();
    expect(note()).toContain('no model row is tagged secondary');
  });
});
