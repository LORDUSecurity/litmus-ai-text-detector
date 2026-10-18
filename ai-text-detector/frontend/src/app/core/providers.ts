import type { Lane } from './detection.types';

export interface ProviderPreset {
  id: string;
  label: string;
  host: string;
  port: string;
  scheme: 'http' | 'https';
  path: string;
  note: string;
}

export interface ModelOption {
  id: string;
  note: string;
  benchmarked: boolean;
}

export const PROVIDERS: ProviderPreset[] = [
  {
    id: 'omniroute',
    label: 'OmniRoute (local gateway)',
    host: 'localhost',
    port: '20128',
    scheme: 'http',
    path: '/v1',
    note: 'Self-hosted gateway. Change the port if yours is not 20128.',
  },
  {
    id: 'nvidia',
    label: 'NVIDIA NIM',
    host: 'integrate.api.nvidia.com',
    port: '443',
    scheme: 'https',
    path: '/v1',
    note: 'The provider the thresholds were calibrated against.',
  },
  {
    id: 'openrouter',
    label: 'OpenRouter',
    host: 'openrouter.ai',
    port: '443',
    scheme: 'https',
    path: '/api/v1',
    note: 'Needs its own key; model ids differ from NVIDIA\'s.',
  },
];

/** Hosts the UI recognises. Used only for a cosmetic warning — the server
    decides via ALLOWED_LLM_HOSTS, and this list cannot widen it. */
export const KNOWN_HOSTS = PROVIDERS.map((p) => p.host);

/** The listed ids are NVIDIA NIM ids. Other gateways keep their own naming and
    nothing here claims an id exists on them — the provider returns 404 if it
    doesn't, and that is shown verbatim. */
export const MODEL_ID_NOTE =
  'Model ids below are NVIDIA NIM ids. Other providers use their own — type the id that provider accepts.';

/** The pair the shipped thresholds were measured on: 2026-09-29 calibration for the
    primary, 2026-10-06 HC3 replay for the tie-break lane. SECONDARY_DECISION_THRESHOLD
    is calibrated for THIS secondary — a different model sits on a different scale, so
    changing only the model without re-fitting the cut changes what the tie-break means.
    The primary band is just as lane-specific: 0.69/0.40 straddles the 550B's own spread
    (median 0.85 AI / 0.25 human), while muse-glimmer's human median is ~0.68 and needs
    0.75/0.40 (47/48 there, 45/48 at the shipped band). */
export const BENCHMARKED_PAIR: Record<Lane, string> = {
  primary: 'nvidia/nemotron-3-ultra-550b-a55b',
  secondary: 'google/diffusiongemma-26b-a4b-it',
};

/** The order of each lane's list is not decoration: choosing a provider preset puts entry [0] in
    the Model field. That is how a retired lane came back — the 550B sat at [0], so every fresh
    form and every `applyPreset('nvidia')` named it, and the request-override bug made that the
    lane every plain user hit. Entry [0] is the model the shipped defaults actually deploy
    (`backend/seed.py` DEFAULT_LANES); the models below it are alternatives an admin names on
    purpose. */
export const LANE_MODELS: Record<Lane, ModelOption[]> = {
  primary: [
    { id: 'z-ai/glm-5.3-flash', note: 'deployed primary (the shipped default) · AUC 1.000 dirty, 0 wrong · band 0.80/0.55 · p50 ~8 s', benchmarked: true },
    { id: BENCHMARKED_PAIR.primary, note: 'retired as the shipped lane · 404 then 503 from NVIDIA · 0.69/0.40 was calibrated on it', benchmarked: true },
    { id: 'moonshotai/kimi-k3', note: 'delisted: NVIDIA answers HTTP 410 since 2026-10-15 · AUC 1.000 with 0 wrong while it served', benchmarked: true },
    { id: 'inclusionai/ling-3.1-flash', note: 'OpenRouter id · best free OR lane (47/48 dirty, 0 wrong at its own 0.63/0.52 band) — pick the OpenRouter provider', benchmarked: true },
    { id: 'meta/muse-glimmer-30b', note: 'AUC 0.986 · strongest clean-text score, but slowest lane', benchmarked: true },
  ],
  secondary: [
    { id: BENCHMARKED_PAIR.secondary, note: 'deployed tie-break row (shipped switched off) · AUC 0.953 · cut 0.90 is calibrated for it', benchmarked: true },
    { id: 'meta/muse-glimmer-30b', note: 'AUC 0.986 · holdout wanted cut 0.70, not 0.90', benchmarked: true },
    { id: BENCHMARKED_PAIR.primary, note: 'AUC 0.934 · holdout wanted cut 0.30-0.65, unstable · retired as a primary', benchmarked: true },
  ],
};

export function presetById(id: string): ProviderPreset {
  return PROVIDERS.find((p) => p.id === id) ?? PROVIDERS[0];
}

export function providerFor(host: string, port: string): ProviderPreset | null {
  const h = host.trim().toLowerCase();
  return (
    PROVIDERS.find((p) => p.host === h && String(p.port) === String(port).trim()) ?? null
  );
}

/** A preset is a convenience, not a constraint: every field stays editable. */
export function composeBaseUrl(preset: Pick<ProviderPreset, 'scheme' | 'host' | 'port' | 'path'>): string {
  const host = preset.host.trim().toLowerCase();
  // No host is not a malformed URL, it is the absence of an override — and this string is what
  // the lane card prints under the Host field, so composing `https://` here would advertise an
  // endpoint nobody chose.
  if (!host) return '';
  const port = preset.port.trim();
  const bracketed = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
  const defaultPort = (preset.scheme === 'http' && port === '80') || (preset.scheme === 'https' && port === '443');
  const origin = `${preset.scheme}://${bracketed}${port && !defaultPort ? ':' + port : ''}`;
  return origin + (preset.path.trim() === '/' ? '' : preset.path.trim());
}

/** Splits a composed base_url back into the four UI fields. */
export function decomposeBaseUrl(
  url: string,
): { scheme: 'http' | 'https'; host: string; port: string; path: string } | null {
  const m = /^(https?):\/\/([^/?#]+)(.*)$/i.exec(url.trim());
  if (!m) return null;
  const scheme = m[1].toLowerCase() as 'http' | 'https';
  const authority = m[2];
  const path = m[3] || '/';
  let host = authority;
  let port = '';
  if (authority.startsWith('[')) {
    const close = authority.indexOf(']');
    if (close === -1) return null;
    host = authority.slice(0, close + 1);
    port = authority.slice(close + 1).replace(/^:/, '');
  } else {
    const colon = authority.lastIndexOf(':');
    if (colon !== -1) {
      host = authority.slice(0, colon);
      port = authority.slice(colon + 1);
    }
  }
  return { scheme, host, port: port || (scheme === 'https' ? '443' : '80'), path };
}

export function isKnownHost(host: string): boolean {
  const h = host.trim().toLowerCase().replace(/^\[|\]$/g, '');
  return KNOWN_HOSTS.includes(h) || h === 'localhost' || h === '127.0.0.1' || h === '::1';
}

export function isBenchmarkedPair(primaryModel: string, secondaryModel: string): boolean {
  return primaryModel === BENCHMARKED_PAIR.primary && secondaryModel === BENCHMARKED_PAIR.secondary;
}

export const MODEL_PATTERN = /^[A-Za-z0-9._:/@+-]+$/;
