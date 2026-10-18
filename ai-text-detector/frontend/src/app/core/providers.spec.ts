import {
  BENCHMARKED_PAIR,
  LANE_MODELS,
  MODEL_PATTERN,
  composeBaseUrl,
  decomposeBaseUrl,
  isBenchmarkedPair,
  isKnownHost,
  presetById,
} from './providers';

describe('composeBaseUrl', () => {
  it('builds the two URLs the benchmark ran on', () => {
    expect(composeBaseUrl(presetById('nvidia'))).toBe('https://integrate.api.nvidia.com/v1');
    expect(composeBaseUrl(presetById('omniroute'))).toBe('http://localhost:20128/v1');
    expect(composeBaseUrl(presetById('openrouter'))).toBe('https://openrouter.ai/api/v1');
  });

  it('composes nothing when there is no host', () => {
    // An empty lane means "no override", and the panel prints this value — a bare `https://`
    // under an empty Host field reads like a half-typed endpoint the user did not choose.
    expect(composeBaseUrl({ scheme: 'https', host: '', port: '', path: '' })).toBe('');
    expect(composeBaseUrl({ scheme: 'http', host: '  ', port: '20128', path: '/v1' })).toBe('');
  });

  it('drops the default port for the scheme', () => {
    expect(
      composeBaseUrl({ scheme: 'https', host: 'openrouter.ai', port: '443', path: '/api/v1' }),
    ).toBe('https://openrouter.ai/api/v1');
    expect(
      composeBaseUrl({ scheme: 'http', host: 'localhost', port: '80', path: '/v1' }),
    ).toBe('http://localhost/v1');
  });

  it('keeps a non-default port — that is the whole point of the field', () => {
    expect(
      composeBaseUrl({ scheme: 'http', host: 'localhost', port: '3001', path: '/v1' }),
    ).toBe('http://localhost:3001/v1');
  });

  it('brackets a bare IPv6 host', () => {
    expect(
      composeBaseUrl({ scheme: 'http', host: '::1', port: '20128', path: '/v1' }),
    ).toBe('http://[::1]:20128/v1');
  });

  it('normalises an empty or slash-only path', () => {
    expect(
      composeBaseUrl({ scheme: 'http', host: 'localhost', port: '20128', path: '/' }),
    ).toBe('http://localhost:20128');
    expect(
      composeBaseUrl({ scheme: 'http', host: 'localhost', port: '20128', path: '' }),
    ).toBe('http://localhost:20128');
  });

  it('survives a round trip through the four fields', () => {
    const url = 'http://localhost:20128/v1';
    const parts = decomposeBaseUrl(url);
    expect(parts).not.toBeNull();
    expect(composeBaseUrl({ ...parts!, host: parts!.host })).toBe(url);
  });
});

describe('host recognition', () => {
  it('knows the three preset hosts and loopback', () => {
    expect(isKnownHost('integrate.api.nvidia.com')).toBe(true);
    expect(isKnownHost('OpenRouter.AI')).toBe(true);
    expect(isKnownHost('localhost')).toBe(true);
    expect(isKnownHost('[::1]')).toBe(true);
  });

  it('does not let a suffix imitate a known host', () => {
    expect(isKnownHost('openrouter.ai.evil.com')).toBe(false);
    expect(isKnownHost('evil.com')).toBe(false);
  });
});

describe('benchmark pair', () => {
  it('only accepts the exact calibrated combination', () => {
    expect(isBenchmarkedPair(BENCHMARKED_PAIR.primary, BENCHMARKED_PAIR.secondary)).toBe(true);
    expect(isBenchmarkedPair(BENCHMARKED_PAIR.secondary, BENCHMARKED_PAIR.primary)).toBe(false);
    expect(
      isBenchmarkedPair(BENCHMARKED_PAIR.primary, 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning'),
    ).toBe(false);
  });

  it('auto-fills the lane with the model the shipped defaults deploy', () => {
    // Choosing a provider preset writes entry [0] into the Model field, so [0] has to be the lane
    // a fresh install actually runs — not the pair the thresholds were once measured on. The 550B
    // sitting here is what made every untouched form name a retired NVIDIA lane.
    expect(LANE_MODELS.primary[0].id).toBe('z-ai/glm-5.3-flash');
    expect(LANE_MODELS.secondary[0].id).toBe(BENCHMARKED_PAIR.secondary);
    expect(LANE_MODELS.primary[0].benchmarked).toBe(true);
    expect(LANE_MODELS.primary[0].id).not.toBe(BENCHMARKED_PAIR.primary);
  });

  it('says so on the model it retired', () => {
    const retired = LANE_MODELS.primary.find((m) => m.id === BENCHMARKED_PAIR.primary);
    expect(retired?.note).toMatch(/retired/i);
  });
});

describe('MODEL_PATTERN', () => {
  it('accepts the ids actually in use', () => {
    ['nvidia/nemotron-3-ultra-550b-a55b', 'llama-3.3-70b-instruct', 'meta/llama-4:scout',
      'anthropic/claude-sonnet-4@20250514', 'gpt-4o-mini'].forEach((id) =>
      expect(MODEL_PATTERN.test(id)).toBe(true),
    );
  });

  it('rejects injection attempts and whitespace', () => {
    ['x"} , {"role":"system', 'has space', 'tab\there', 'a\nb', 'back\\slash', 'quote"q'].forEach(
      (id) => expect(MODEL_PATTERN.test(id)).toBe(false),
    );
  });
});
