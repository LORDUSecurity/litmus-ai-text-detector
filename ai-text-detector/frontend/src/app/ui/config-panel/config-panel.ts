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
  type LaneState,
} from '../../core/form-state';

/** v2: the v1 record stored the lane fields *including the baked defaults this panel used to
    start with*, so an admin who ever opened this tab kept a nemotron override pinned to every
    later request from that browser — restored after the shipped code stopped pre-filling it.
    The version bump is the migration: stale prefs are simply not read. Keys were never stored. */
const PREFS_KEY = 'aidetect.prefs.v2';
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

  /** Whether this process answers `/detect` inside itself. Read from the engine status the backend
      already sends, not from a name written into the template, because the purge footer's subject --
      execution rows a workflow wrote -- does not exist on a packaged install, and a red dot there
      would accuse the user's own machine of leaving a key on a disk it never wrote. */
  readonly nativeEngine = computed(() => this.health()?.n8n?.on_request_path === false);

  /** Who refuses a host outside `ALLOWED_LLM_HOSTS`. The warning has to name the thing that will
      actually reject the call, or a local user goes looking for a workflow that was never installed. */
  readonly hostChecker = computed(() => {
    if (!this.health()) return 'The backend';
    return this.nativeEngine() ? 'The engine in this process' : 'The workflow';
  });

  /** True when at least one lane field carries an override. Nothing does on a fresh form: the
      deployed row and the user's own pool are the authority, and this panel exists to deviate
      from them deliberately, not by accident. */
  readonly overridden = computed(() => Object.keys(this.state.laneConfig()).length > 0);

  readonly pairIsBenchmarked = computed(() => {
    const p = this.state.primary.model().trim();
    const s = this.state.secondary.model().trim();
    return p === BENCHMARKED_PAIR.primary && s === BENCHMARKED_PAIR.secondary;
  });

  /** computed, not a direct signal reference: @Input `state` only exists after
      construction, so a field initializer would read undefined here. */
  readonly secondaryEnabled = computed(() => this.state.secondaryEnabled());

  /** An empty host is "no override", not a suspicious host — warning about it would train the
      admin to ignore the one warning that matters. */
  readonly warnPrimaryHost = computed(() => {
    const host = this.state.primary.host().trim();
    return host !== '' && !isKnownHost(host);
  });
  readonly warnSecondaryHost = computed(() => {
    const host = this.state.secondary.host().trim();
    return host !== '' && !isKnownHost(host);
  });

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
    // The first option is "no override": choosing it must clear the lane, not compose a URL out
    // of a preset the admin never picked.
    if (!id) state.clear();
    else state.applyPreset(id);
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
    const host = state.host().trim();
    if (!host) return 'no override — the deployed lane and your pool decide';
    const preset = providerFor(host, state.port());
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

  /** Clears both overrides, which is what the button is for: an admin who wants a specific pair
      can name it in the fields. "Reset" used to write the retired NVIDIA pair back into the form,
      i.e. it installed an override under a label that promised the opposite. */
  resetLanes(): void {
    this.state.primary.clear();
    this.state.secondary.clear();
    this.save();
  }

  clearKeys(): void {
    this.state.primaryApiKey.set('');
    this.state.secondaryApiKey.set('');
  }
}
