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

  it('lists the benchmarked model first in both lanes', () => {
    expect(LANE_MODELS.primary[0].id).toBe(BENCHMARKED_PAIR.primary);
    expect(LANE_MODELS.secondary[0].id).toBe(BENCHMARKED_PAIR.secondary);
    expect(LANE_MODELS.primary[0].benchmarked).toBe(true);
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
