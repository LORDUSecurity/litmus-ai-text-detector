import { Component, Input, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { BackendHealth } from '../../core/backend.types';
import { BackendService } from '../../core/backend.service';
import { DetectionService } from '../../core/detection.service';
import {
  BENCHMARKED_PAIR,
  MODEL_ID_NOTE,
  MODEL_PATTERN,
  PROVIDERS,
  isKnownHost,
  providerFor,
} from '../../core/providers';
import {
  DetectionFormState,
  defaultLaneFields,
  type LaneState,
} from '../../core/form-state';

const PREFS_KEY = 'aidetect.prefs.v1';
const KEY_PATTERN = /^[A-Za-z0-9._:+\/-]+$/;

interface LanePrefs {
  provider: string;
  scheme: 'http' | 'https';
  host: string;
  port: string;
  path: string;
  model: string;
}

@Component({
  selector: 'app-config-panel',
  imports: [FormsModule],
  templateUrl: './config-panel.html',
})
export class ConfigPanel implements OnInit {
  @Input({ required: true }) state!: DetectionFormState;

  private readonly service = inject(DetectionService);
  private readonly backend = inject(BackendService);

  /** The tie-break row this panel is allowed to switch for everybody, and whether the write
      landed. `models.enabled` is the authority the backend reads: a request body can only ever
      switch the lane OFF, never on (see detect() in backend/app.py), so an admin who wants the
      second opinion back has to write the row -- which is what this switch now does. */
  private secondaryRowId: number | null = null;
  readonly laneSwitchFailed = signal(false);
  /** True until the server has answered: `null` means "the shared switch is unknown", and the
      card must not present an unverified state as if the admin were looking at the real one. */
  readonly laneStateLoaded = signal(false);

  readonly providers = PROVIDERS;
  readonly modelIdNote = MODEL_ID_NOTE;
  readonly benchmarkedPair = BENCHMARKED_PAIR;

  readonly health = signal<BackendHealth | null>(null);
  readonly healthFailed = signal(false);

  readonly pairIsBenchmarked = computed(() => {
    const p = this.state.primary.model().trim();
    const s = this.state.secondary.model().trim();
    return p === BENCHMARKED_PAIR.primary && s === BENCHMARKED_PAIR.secondary;
  });

  /** computed, not a direct signal reference: @Input `state` only exists after
      construction, so a field initializer would read undefined here. */
  readonly secondaryEnabled = computed(() => this.state.secondaryEnabled());

  readonly warnPrimaryHost = computed(() => !isKnownHost(this.state.primary.host()));
  readonly warnSecondaryHost = computed(() => !isKnownHost(this.state.secondary.host()));

  /** The primary card always renders; the secondary card is an OPTIONAL expansion.
      Off is the honest single-lane mode: gray-zone answers become REVIEW instead
      of buying a second call, and nothing about the lane leaves this page. */
  readonly lanes = computed(() => {
    const primary = {
      name: 'primary' as const,
      title: 'Primary lane',
      hint: 'Scores every text. Its verdict decides unless it lands in the gray zone.',
      state: this.state.primary,
      warnHost: this.warnPrimaryHost(),
      key: this.state.primaryApiKey,
    };
    if (!this.state.secondaryEnabled()) return [primary];
    return [
      primary,
      {
        name: 'secondary' as const,
        title: 'Secondary lane (tie-break)',
        hint: 'Only asked when the primary defers, and only believed at its own ≥0.90.',
        state: this.state.secondary,
        warnHost: this.warnSecondaryHost(),
        key: this.state.secondaryApiKey,
      },
    ];
  });

  toggleSecondary(on: boolean): void {
    this.state.secondaryEnabled.set(on);
    this.save();
    // Write the shared row, not just this browser: the backend reads `models.enabled` to decide
    // whether the lane may run at all, and a request body can only switch it off. Without this
    // call the admin's switch would look saved while every detection kept the shipped default.
    this.laneSwitchFailed.set(false);
    if (this.secondaryRowId !== null) {
      this.backend.updateModel(this.secondaryRowId, { enabled: on }).subscribe({
        error: () => this.laneSwitchFailed.set(true),
      });
    }
  }

  onProvider(state: LaneState, id: string): void {
    state.applyPreset(id);
    this.save();
  }

  /** Inline hints only. The Detect button runs the authoritative pre-flight and
      refuses to send, so an illegal key never reaches n8n's database. */
  modelError(state: LaneState): string | null {
    const model = state.model().trim();
    if (!model) return null;
    if (model.length > 200) return `${model.length} characters, limit is 200`;
    if (!MODEL_PATTERN.test(model)) return 'letters, digits and . _ : / @ + - only';
    return null;
  }

  keyError(value: string): string | null {
    if (!value) return null;
    if (value.length > 512) return `${value.length} characters, limit is 512`;
    if (!KEY_PATTERN.test(value)) return 'no spaces, no line breaks, no other symbols';
    return null;
  }

  hostLabel(state: LaneState): string {
    const preset = providerFor(state.host(), state.port());
    return preset ? preset.label : 'custom endpoint';
  }

  ngOnInit(): void {
    this.restore();
    this.service.health().subscribe({
      next: (h) => this.health.set(h),
      error: () => this.healthFailed.set(true),
    });
    // The shared lane state outranks anything this browser remembered: the switch on this card
    // is a setting for every user, so it has to show what the server actually has enabled.
    this.backend.models().subscribe({
      next: (rows) => {
        const sec = rows.find((r) => r.lane === 'secondary') ?? null;
        this.secondaryRowId = sec ? sec.id : null;
        this.state.secondaryEnabled.set(sec ? sec.enabled : false);
        this.laneStateLoaded.set(true);
        this.save();
      },
      error: () => {
        this.laneStateLoaded.set(true);
        this.laneSwitchFailed.set(true);
      },
    });
  }

  /** Only non-secret fields are remembered. The two BYOK keys are never written
      to localStorage, sessionStorage or any service. */
  private restore(): void {
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(PREFS_KEY);
    } catch {
      return;
    }
    if (!raw) return;
    let prefs: { primary?: LanePrefs; secondary?: LanePrefs; secondaryEnabled?: boolean };
    try {
      prefs = JSON.parse(raw);
    } catch {
      return;
    }
    if (prefs.secondaryEnabled === false) this.state.secondaryEnabled.set(false);
    ([
      ['primary', this.state.primary],
      ['secondary', this.state.secondary],
    ] as const).forEach(([lane, state]) => {
      const saved = prefs[lane];
      if (!saved || typeof saved.host !== 'string' || typeof saved.model !== 'string') return;
      state.setFields({
        provider: typeof saved.provider === 'string' ? saved.provider : state.provider(),
        scheme: saved.scheme === 'http' || saved.scheme === 'https' ? saved.scheme : state.scheme(),
        host: saved.host,
        port: typeof saved.port === 'string' ? saved.port : state.port(),
        path: typeof saved.path === 'string' ? saved.path : state.path(),
        model: saved.model,
      });
    });
  }

  save(): void {
    const prefs = {
      primary: this.state.primary.fields(),
      secondary: this.state.secondary.fields(),
      secondaryEnabled: this.state.secondaryEnabled(),
    };
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* private mode or a full quota: the form still works, nothing persists */
    }
  }

  resetLanes(): void {
    this.state.primary.setFields(defaultLaneFields('primary'));
    this.state.secondary.setFields(defaultLaneFields('secondary'));
    this.save();
  }

  clearKeys(): void {
    this.state.primaryApiKey.set('');
    this.state.secondaryApiKey.set('');
  }
}
