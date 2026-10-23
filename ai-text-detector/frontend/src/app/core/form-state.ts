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

  /** Back to "no override": the deployed row, or this user's own pool, decides the lane. */
  clear(): void {
    this.setFields(defaultLaneFields());
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

/** What an untouched lane holds: nothing. A blank field means "send no override", so the
    backend resolves the lane from the admin's deployed row and, failing that, from the signed-in
    user's own pool — which is the only way "the admin sets the models for everybody" and "auto
    switching based on the order of the user" can both be true.

    These fields used to start pre-filled with the pair this file was written against, and
    `laneConfig()` reads a filled field as an override. Every plain user's Detect therefore named
    `nvidia/nemotron-3-ultra-550b-a55b` in the request, the backend honoured it
    (`lane_source: "request"`), the deployed `z-ai/glm-5.3-flash` row was never consulted, and the
    shared page answered `DETECTION_UNAVAILABLE` / 503 in 7 s — measured 2026-10-17 with a
    non-admin account, whose form renders no lane fields at all, so nothing on screen could have
    produced that model id. A non-admin cannot see or edit these fields, so "blank means don't
    send" only protects a user if blank is also where the form begins. */
export function defaultLaneFields(): LaneFields {
  return { provider: '', scheme: 'https', host: '', port: '', path: '', model: '' };
}

export class DetectionFormState {
  readonly primary = new LaneState('primary', defaultLaneFields());
  readonly secondary = new LaneState('secondary', defaultLaneFields());
  readonly text = signal('');

  /** True when the text now in the field arrived from a document rather than being typed or
      pasted. The Detect field paints the winning lane's quotes under its own words, and it lifts
      that paint out of the way while the reader has the box selected — because a highlight under
      the caret is in the way of editing. Loaded text is not being edited: opening a document and
      reading where its evidence fell is the reason it was loaded, so selecting inside it is how you
      scroll and search, and the paint stays. Any change to the characters ends the paint on its
      own, since the field no longer holds what was scored. */
  readonly textFromDocument = signal(false);

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
