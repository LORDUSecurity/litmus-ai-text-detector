import { computed, signal, type Signal, type WritableSignal } from '@angular/core';
import type { ByokKeys, Lane, LaneConfig } from './detection.types';
import {
  LANE_MODELS,
  composeBaseUrl,
  decomposeBaseUrl,
  presetById,
  type ProviderPreset,
} from './providers';

export interface LaneFields {
  provider: string;
  scheme: 'http' | 'https';
  host: string;
  port: string;
  path: string;
  model: string;
}

export class LaneState {
  readonly provider: WritableSignal<string>;
  readonly scheme: WritableSignal<'http' | 'https'>;
  readonly host: WritableSignal<string>;
  readonly port: WritableSignal<string>;
  readonly path: WritableSignal<string>;
  readonly model: WritableSignal<string>;

  readonly baseUrl = computed(() =>
    composeBaseUrl({
      scheme: this.scheme(),
      host: this.host(),
      port: this.port(),
      path: this.path(),
    }),
  );

  readonly models: Signal<{ id: string; note: string; benchmarked: boolean }[]>;

  constructor(readonly lane: Lane, fields: LaneFields) {
    this.provider = signal(fields.provider);
    this.scheme = signal(fields.scheme);
    this.host = signal(fields.host);
    this.port = signal(fields.port);
    this.path = signal(fields.path);
    this.model = signal(fields.model);
    this.models = computed(() => LANE_MODELS[this.lane]);
  }

  applyPreset(id: string): void {
    const preset: ProviderPreset = presetById(id);
    const parsed = decomposeBaseUrl(composeBaseUrl(preset));
    this.provider.set(id);
    this.scheme.set(parsed ? parsed.scheme : preset.scheme);
    this.host.set(parsed ? parsed.host : preset.host);
    this.port.set(parsed ? parsed.port : preset.port);
    this.path.set(preset.path);
    const stillListed = LANE_MODELS[this.lane].some((m) => m.id === this.model());
    if (!stillListed) this.model.set(LANE_MODELS[this.lane][0].id);
  }

  fields(): LaneFields {
    return {
      provider: this.provider(),
      scheme: this.scheme(),
      host: this.host(),
      port: this.port(),
      path: this.path(),
      model: this.model(),
    };
  }

  setFields(fields: LaneFields): void {
    this.provider.set(fields.provider);
    this.scheme.set(fields.scheme);
    this.host.set(fields.host);
    this.port.set(fields.port);
    this.path.set(fields.path);
    this.model.set(fields.model);
  }
}

const NVIDIA_PRESET = presetById('nvidia');

function laneFields(lane: Lane): LaneFields {
  return {
    provider: NVIDIA_PRESET.id,
    scheme: NVIDIA_PRESET.scheme,
    host: NVIDIA_PRESET.host,
    port: NVIDIA_PRESET.port,
    path: NVIDIA_PRESET.path,
    model: LANE_MODELS[lane][0].id,
  };
}

/** The defaults are the calibrated configuration: NVIDIA NIM with the
    benchmarked 550B/120B pair and the env keys. Anything else is a deviation
    the user asked for, and the result card says so. */
export function defaultLaneFields(lane: Lane): LaneFields {
  return laneFields(lane);
}

export class DetectionFormState {
  readonly primary = new LaneState('primary', laneFields('primary'));
  readonly secondary = new LaneState('secondary', laneFields('secondary'));
  readonly text = signal('');
  readonly primaryApiKey = signal('');
  readonly secondaryApiKey = signal('');

  /** The tie-break lane is optional. Off means the request sends
      lane_config.secondary = { enabled: false } and nothing about that lane —
      a gray-zone primary answer becomes REVIEW instead of buying a second call. */
  readonly secondaryEnabled = signal(true);

  /** A blank field means "use whatever the workflow already has from its .env",
      so the object stays empty when the user has changed nothing to send. */
  laneConfig(): LaneConfig {
    const out: LaneConfig = {};
    ([
      ['primary', this.primary],
      ['secondary', this.secondary],
    ] as const).forEach(([lane, state]) => {
      if (lane === 'secondary' && !this.secondaryEnabled()) return;
      const entry: { base_url?: string; model?: string } = {};
      if (state.host().trim()) {
        entry.base_url = state.baseUrl();
      }
      if (state.model().trim()) {
        entry.model = state.model().trim();
      }
      if (Object.keys(entry).length) out[lane] = entry;
    });
    if (!this.secondaryEnabled()) {
      out.secondary = { enabled: false };
    }
    return out;
  }

  keys(): ByokKeys {
    return {
      primary: this.primaryApiKey(),
      secondary: this.secondaryEnabled() ? this.secondaryApiKey() : '',
    };
  }
}
