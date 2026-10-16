import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { DetectionService } from './detection.service';
import { DetectionFormState } from './form-state';
import type { LaneConfig } from './detection.types';

const PRIMARY_KEY = 'nvapi-PRIMARYsecret123';
const SECONDARY_KEY = 'sk-or-SECONDARYsecret456';

describe('DetectionService', () => {
  let service: DetectionService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(DetectionService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('posts to the gateway path, not to n8n directly', () => {
    const form = new DetectionFormState();
    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.method).toBe('POST');
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('sends lane_config with a composed base_url and the model per lane', () => {
    const form = new DetectionFormState();
    form.primary.applyPreset('omniroute');
    form.primary.model.set('nvidia/nemotron-3-ultra-550b-a55b');
    form.secondary.applyPreset('nvidia');

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    const body = req.request.body as { text: string; lane_config: LaneConfig };
    expect(body.text).toBe('some text to judge');
    expect(body.lane_config.primary?.base_url).toBe('http://localhost:20128/v1');
    expect(body.lane_config.primary?.model).toBe('nvidia/nemotron-3-ultra-550b-a55b');
    expect(body.lane_config.secondary?.base_url).toBe('https://integrate.api.nvidia.com/v1');
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('omits lane_config entirely when every override field is blank', () => {
    const form = new DetectionFormState();
    form.primary.host.set('');
    form.primary.model.set('   ');
    form.secondary.host.set('');
    form.secondary.model.set('');

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect((req.request.body as Record<string, unknown>)['lane_config']).toBeUndefined();
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('sets both key headers when both keys are present', () => {
    const form = new DetectionFormState();
    form.primaryApiKey.set(PRIMARY_KEY);
    form.secondaryApiKey.set(SECONDARY_KEY);

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.headers.get('x-primary-api-key')).toBe(PRIMARY_KEY);
    expect(req.request.headers.get('x-secondary-api-key')).toBe(SECONDARY_KEY);
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('omits a key header when only one lane has a key', () => {
    const form = new DetectionFormState();
    form.secondaryApiKey.set(SECONDARY_KEY);

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.headers.has('x-primary-api-key')).toBe(false);
    expect(req.request.headers.get('x-secondary-api-key')).toBe(SECONDARY_KEY);
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('omits both key headers when no key is typed', () => {
    const form = new DetectionFormState();

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.headers.has('x-primary-api-key')).toBe(false);
    expect(req.request.headers.has('x-secondary-api-key')).toBe(false);
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('never puts a key value anywhere in the serialized body', () => {
    const form = new DetectionFormState();
    form.primaryApiKey.set(PRIMARY_KEY);
    form.secondaryApiKey.set(SECONDARY_KEY);

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    const serialized = JSON.stringify(req.request.body);
    expect(serialized).not.toContain(PRIMARY_KEY);
    expect(serialized).not.toContain(SECONDARY_KEY);
    expect(serialized.toLowerCase()).not.toContain('api-key');
    expect(serialized.toLowerCase()).not.toContain('apikey');
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('trims whitespace around a key before sending it', () => {
    const form = new DetectionFormState();
    form.primaryApiKey.set('  ' + PRIMARY_KEY + '  ');

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.headers.get('x-primary-api-key')).toBe(PRIMARY_KEY);
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('sends secondary:{enabled:false} and no secondary key when the tie-break is off', () => {
    const form = new DetectionFormState();
    form.primary.applyPreset('nvidia');
    form.secondaryApiKey.set(SECONDARY_KEY);
    form.secondaryEnabled.set(false);

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    const body = req.request.body as { lane_config: LaneConfig };
    expect(body.lane_config.secondary).toEqual({ enabled: false });
    expect(body.lane_config.primary?.base_url).toBe('https://integrate.api.nvidia.com/v1');
    expect(req.request.headers.has('x-secondary-api-key')).toBe(false);
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('keeps the off-flag even when the off lane has no other fields', () => {
    const form = new DetectionFormState();
    form.primary.host.set('');
    form.primary.model.set('');
    form.secondary.host.set('');
    form.secondary.model.set('');
    form.secondaryEnabled.set(false);

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect((req.request.body as { lane_config?: LaneConfig }).lane_config)
      .toEqual({ secondary: { enabled: false } });
    req.flush({ classification: 'HUMAN_LIKELY' });
  });

  it('scores with a model picked on the Ranking tab, in place of the panel primary', () => {
    const form = new DetectionFormState();
    form.primary.applyPreset('nvidia');
    form.primary.model.set('nvidia/nemotron-3-ultra-550b-a55b');

    service.usePickedModel('  google/gemma-4-26b-a4b-it:free  ');
    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    const body = req.request.body as { lane_config: LaneConfig };
    expect(body.lane_config.primary?.model).toBe('google/gemma-4-26b-a4b-it:free');
  });

  it('drops the panel base_url when a ranked model is picked, so it cannot be sent to the wrong gateway', () => {
    // The point of the pick is that the backend resolves the endpoint from the model's own
    // stored row. Sending the previously-configured host with a different provider's model id
    // would 404 (or worse, answer from a model nobody ranked).
    const form = new DetectionFormState();
    form.primary.applyPreset('nvidia');
    form.primary.model.set('nvidia/nemotron-3-ultra-550b-a55b');

    service.usePickedModel('openai/gpt-oss-120b');
    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    const body = req.request.body as { lane_config: LaneConfig };
    expect(body.lane_config.primary).toEqual({ model: 'openai/gpt-oss-120b' });
  });

  it('still honours a tie-break switched off while scoring with a picked model', () => {
    const form = new DetectionFormState();
    form.secondaryEnabled.set(false);
    service.usePickedModel('moonshotai/kimi-k2.5');

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.body).toEqual({
      text: 'some text to judge',
      lane_config: { secondary: { enabled: false }, primary: { model: 'moonshotai/kimi-k2.5' } },
    });
  });

  it('sends no lane_config primary once the pick is cleared', () => {
    const form = new DetectionFormState();
    form.primary.host.set('');
    form.primary.model.set('');
    form.secondary.host.set('');
    form.secondary.model.set('');

    service.usePickedModel('z-ai/glm-5.2');
    service.clearPickedModel();
    expect(service.pickedModel()).toBeNull();

    service.detect('some text to judge', form.laneConfig(), form.keys()).subscribe();

    const req = http.expectOne('/api/detect');
    expect((req.request.body as Record<string, unknown>)['lane_config']).toBeUndefined();
  });

  it('clears the pick when handed blank text, and never puts the model id in a header', () => {
    service.usePickedModel('   ');
    expect(service.pickedModel()).toBeNull();

    service.usePickedModel('nvidia/nemotron-3-nano-30b-a1b');
    service.detect('some text to judge', new DetectionFormState().laneConfig(),
      { primary: PRIMARY_KEY, secondary: '' }).subscribe();

    const req = http.expectOne('/api/detect');
    expect(req.request.headers.get('x-primary-api-key')).toBe(PRIMARY_KEY);
    expect((req.request.body as { lane_config?: LaneConfig }).lane_config?.primary?.model)
      .toBe('nvidia/nemotron-3-nano-30b-a1b');
    req.flush({ classification: 'HUMAN_LIKELY' });
  });
});
