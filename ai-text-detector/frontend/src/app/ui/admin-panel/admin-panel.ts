import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import type {
  AdminSettings,
  KeyGuide,
  ModelView,
  N8nStatus,
  ProbeResult,
  ProviderView,
} from '../../core/backend.types';

const MODEL_PATTERN = /^[A-Za-z0-9._:/@+-]+$/;
const KEY_PATTERN = /^[A-Za-z0-9._:+/-]+$/;

/** What the backend falls back to when the `n8n_url` / `webhook_path` settings are blank. The UI
 *  shows these as placeholders so an admin can see the effective target without a round trip. */
const DEFAULT_ENGINE_URL = 'http://localhost:5678';
const DEFAULT_WEBHOOK_PATH = '/webhook/ai-detect-ai';
const ABSOLUTE_URL = /^https?:\/\/[^\s/?#]+/i;

interface EngineDraft {
  n8n_url: string;
  webhook_path: string;
  paper_url: string;
  paper_title: string;
  allowed_llm_hosts: string;
}

interface NewProvider {
  name: string;
  base_url: string;
  label: string;
  notes: string;
}

interface NewKey {
  providerId: number;
  api_key: string;
  label: string;
}

interface NewModel {
  provider_id: number;
  model_id: string;
  lane: 'primary' | 'secondary' | null;
  notes: string;
}

@Component({
  selector: 'app-admin-panel',
  imports: [FormsModule],
  templateUrl: './admin-panel.html',
})
export class AdminPanel implements OnInit {
  private readonly backend = inject(BackendService);

  readonly providers = signal<ProviderView[]>([]);
  readonly models = signal<ModelView[]>([]);
  readonly busy = signal(false);
  readonly message = signal<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  readonly probe = signal<ProbeResult | null>(null);

  readonly showNewProvider = signal(false);
  readonly newProvider = signal<NewProvider>({ name: '', base_url: '', label: '', notes: '' });
  readonly newKey = signal<NewKey>({ providerId: 0, api_key: '', label: 'key' });
  readonly showNewKey = signal(false);
  readonly newModel = signal<NewModel>({ provider_id: 0, model_id: '', lane: null, notes: '' });
  readonly showNewModel = signal(false);

  /** Local edits to a model's tuned parameters, keyed by model id, so typing in a
      field does not rewrite the row until it is saved. */
  readonly drafts = signal<Record<number, Partial<ModelView>>>({});

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.backend.providers().subscribe({
      next: (rows) => {
        this.providers.set(rows);
        this.models.set(rows.flatMap((p) => p.models));
      },
      error: (err) => this.fail('Could not load configuration from the backend.', err),
    });
    this.reloadSettings();
  }

  // ---- engine + site settings -------------------------------------------

  readonly settings = signal<AdminSettings | null>(null);
  /** Set only when GET /settings answers — a 403, not a slow load. Shows the card as
      admin-only instead of five empty fields that read like an unconfigured install. */
  readonly settingsBlocked = signal(false);
  readonly draft = signal<EngineDraft>({
    n8n_url: '', webhook_path: '', paper_url: '', paper_title: '', allowed_llm_hosts: '',
  });
  readonly guides = signal<KeyGuide[]>([]);
  readonly engine = signal<N8nStatus | null>(null);

  /** Placeholders, so the effective default is visible in the field itself. */
  readonly defaultEngineUrl = DEFAULT_ENGINE_URL;
  readonly defaultWebhookPath = DEFAULT_WEBHOOK_PATH;

  /** The target `/detect` will actually post to: the stored setting, or what the backend falls
      back to when it is blank. An admin has to see the effective value, not an empty box. */
  readonly engineTarget = computed(
    () => `${this.draft().n8n_url.trim() || DEFAULT_ENGINE_URL}${
      this.draft().webhook_path.trim() || DEFAULT_WEBHOOK_PATH}`,
  );

  /** Mirrors the server's write-time check (see POST /settings) so a typo is caught before the
      request, with the same wording the server would answer with. */
  readonly draftProblem = computed(() => {
    const d = this.draft();
    const url = d.n8n_url.trim();
    if (url && !ABSOLUTE_URL.test(url)) {
      return 'Detection engine URL must be an absolute http(s) URL with a host, e.g. http://localhost:5678 — blank means the built-in default.';
    }
    const path = d.webhook_path.trim();
    if (path && !path.startsWith('/')) return 'Webhook path must start with "/", e.g. /webhook/ai-detect-ai.';
    const paper = d.paper_url.trim();
    if (paper && !ABSOLUTE_URL.test(paper)) {
      return 'Paper link must be an absolute http(s) URL (a relative one renders as a link on this site, not on Zenodo).';
    }
    return null;
  });

  reloadSettings(): void {
    this.backend.settings().subscribe({
      next: (s) => {
        this.settingsBlocked.set(false);
        this.settings.set(s);
        this.draft.set({
          n8n_url: s.n8n_url ?? '',
          webhook_path: s.webhook_path ?? '',
          paper_url: s.paper_url ?? '',
          paper_title: s.paper_title ?? '',
          allowed_llm_hosts: s.allowed_llm_hosts ?? '',
        });
        this.guides.set(Array.isArray(s.api_key_guides) ? s.api_key_guides : []);
      },
      // A non-admin reaching this view gets 403 here; the card then says so instead of
      // showing five empty fields that look like the settings were never configured.
      error: () => this.settingsBlocked.set(true),
    });
    this.checkEngine();
  }

  checkEngine(): void {
    this.backend.n8nStatus().subscribe({
      next: (s) => this.engine.set(s),
      error: () => this.engine.set(null),
    });
  }

  editSetting(field: keyof EngineDraft, raw: string): void {
    this.draft.update((d) => ({ ...d, [field]: raw }));
  }

  editGuide(index: number, field: keyof KeyGuide, raw: string): void {
    this.guides.update((g) => g.map((row, i) => (i === index ? { ...row, [field]: raw } : row)));
  }

  addGuide(): void {
    this.guides.update((g) => [...g, { provider: '', url: '', steps: '' }]);
  }

  removeGuide(index: number): void {
    this.guides.update((g) => g.filter((_, i) => i !== index));
  }

  /** Write only the two fields that decide where a verdict comes from, then re-read the engine
      status so the card shows whether the target that was just saved is actually reachable. */
  saveEngine(): void {
    const problem = this.draftProblem();
    if (problem) {
      this.message.set({ tone: 'bad', text: problem });
      return;
    }
    const d = this.draft();
    this.busy.set(true);
    this.backend
      .saveSettings({ n8n_url: d.n8n_url.trim(), webhook_path: d.webhook_path.trim() })
      .subscribe({
        next: () => {
          this.busy.set(false);
          // ok() reloads providers *and* settings, and reloadSettings() re-checks the engine — so
          // the pill under the button reflects the target that was just written, without a second
          // status call from here.
          this.ok(`Engine target saved. Detections now post to ${this.engineTarget()}.`);
        },
        error: (err) => {
          this.busy.set(false);
          this.fail('Engine target not saved.', err);
        },
      });
  }

  saveLinks(): void {
    const problem = this.draftProblem();
    if (problem) {
      this.message.set({ tone: 'bad', text: problem });
      return;
    }
    const d = this.draft();
    this.busy.set(true);
    this.backend
      .saveSettings({
        paper_url: d.paper_url.trim(),
        paper_title: d.paper_title.trim(),
        allowed_llm_hosts: d.allowed_llm_hosts.trim(),
        api_key_guides: this.guides().filter((g) => g.provider.trim() && g.url.trim()),
      })
      .subscribe({
        next: (r) => {
          this.busy.set(false);
          const guides = Array.isArray(r.written?.api_key_guides)
            ? r.written.api_key_guides.length
            : this.guides().length;
          this.ok(`Ranking page links saved (paper ${d.paper_url.trim() || 'blank'}, ${guides} key guides).`);
        },
        error: (err) => {
          this.busy.set(false);
          this.fail('Links not saved.', err);
        },
      });
  }


  private fail(prefix: string, err: unknown): void {
    const detail =
      err instanceof HttpErrorResponse
        ? ((err.error as { error?: string })?.error ?? err.message)
        : String(err);
    this.message.set({ tone: 'bad', text: `${prefix} ${detail}` });
  }

  private ok(text: string): void {
    this.message.set({ tone: 'ok', text });
    this.reload();
  }

  // ---- providers --------------------------------------------------------

  createProvider(): void {
    const p = this.newProvider();
    if (!/^[a-z0-9_-]{2,40}$/.test(p.name)) {
      this.message.set({ tone: 'bad', text: 'Provider name must be 2-40 chars of [a-z0-9_-].' });
      return;
    }
    if (!/^https?:\/\//i.test(p.base_url.trim())) {
      this.message.set({ tone: 'bad', text: 'base_url must be an absolute http(s) URL.' });
      return;
    }
    this.busy.set(true);
    this.backend
      .createProvider({ name: p.name.trim(), base_url: p.base_url.trim(), label: p.label, notes: p.notes })
      .subscribe({
        next: (row) => {
          this.busy.set(false);
          this.newProvider.set({ name: '', base_url: '', label: '', notes: '' });
          this.showNewProvider.set(false);
          this.ok(`Provider "${row.name}" saved to the database.`);
        },
        error: (err) => {
          this.busy.set(false);
          this.fail('Provider not created.', err);
        },
      });
  }

  updateProvider(p: ProviderView, field: 'base_url' | 'enabled', value: string | boolean): void {
    this.backend.updateProvider(p.id, { [field]: value } as Partial<ProviderView>).subscribe({
      next: () => this.ok(`Provider "${p.name}" updated.`),
      error: (err) => this.fail('Update failed.', err),
    });
  }

  deleteProvider(p: ProviderView): void {
    this.backend.deleteProvider(p.id).subscribe({
      next: () => this.ok(`Provider "${p.name}" deleted, with its keys and models.`),
      error: (err) => this.fail('Delete failed.', err),
    });
  }

  // ---- keys -------------------------------------------------------------

  addKey(): void {
    const k = this.newKey();
    if (!k.providerId) {
      this.message.set({ tone: 'bad', text: 'Pick a provider first.' });
      return;
    }
    if (!KEY_PATTERN.test(k.api_key) || k.api_key.length > 512) {
      this.message.set({
        tone: 'bad',
        text: 'That key has a character the backend rejects (letters, digits and . _ : + / - only). Nothing was sent.',
      });
      return;
    }
    this.busy.set(true);
    this.backend.addKey(k.providerId, k.api_key, k.label || 'key').subscribe({
      next: (row) => {
        this.busy.set(false);
        this.newKey.set({ providerId: k.providerId, api_key: '', label: 'key' });
        this.showNewKey.set(false);
        this.ok(`Key stored encrypted as "${row.key_hint}". The plaintext is not kept anywhere else.`);
      },
      error: (err) => {
        this.busy.set(false);
        this.fail('Key not stored.', err);
      },
    });
  }

  toggleKey(keyId: number, active: boolean): void {
    this.backend.setKeyActive(keyId, active).subscribe({
      next: () => this.ok(active ? 'Key activated.' : 'Key deactivated.'),
      error: (err) => this.fail('Could not change that key.', err),
    });
  }

  /** SQLite writes UTC with a space and no offset, so the Z is what makes it not local time. */
  keyUsed(at: string | null): string {
    if (!at) return 'never';
    const d = new Date(`${at.replace(' ', 'T')}Z`);
    return Number.isNaN(d.getTime()) ? at : d.toLocaleString();
  }

  deleteKey(keyId: number): void {
    this.backend.deleteKey(keyId).subscribe({
      next: () => this.ok('Key deleted from the database.'),
      error: (err) => this.fail('Could not delete that key.', err),
    });
  }

  // ---- models + tuned parameters ----------------------------------------

  hasDraft(m: ModelView): boolean {
    return this.drafts()[m.id] !== undefined;
  }

  editDraft(m: ModelView, field: keyof ModelView, raw: string): void {
    const value = raw === '' ? null : Number(raw);
    this.drafts.update((all) => ({ ...all, [m.id]: { ...all[m.id], [field]: value } }));
  }

  saveModel(m: ModelView): void {
    const draft = this.drafts()[m.id];
    if (!draft) {
      this.message.set({ tone: 'ok', text: 'Nothing changed.' });
      return;
    }
    this.backend.updateModel(m.id, draft).subscribe({
      next: (row) => {
        this.drafts.update((all) => {
          const next = { ...all };
          delete next[m.id];
          return next;
        });
        this.ok(`Tuned parameters saved for ${row.model_id}.`);
      },
      error: (err) => this.fail('Could not save those parameters.', err),
    });
  }

  createModel(): void {
    const m = this.newModel();
    if (!MODEL_PATTERN.test(m.model_id.trim())) {
      this.message.set({
        tone: 'bad',
        text: 'Model id must use letters, digits and . _ : / @ + - only.',
      });
      return;
    }
    this.busy.set(true);
    this.backend
      .createModel({ provider_id: m.provider_id, model_id: m.model_id.trim(), lane: m.lane, notes: m.notes })
      .subscribe({
        next: (row) => {
          this.busy.set(false);
          this.newModel.set({ provider_id: m.provider_id, model_id: '', lane: null, notes: '' });
          this.showNewModel.set(false);
          this.ok(`Model ${row.model_id} saved${row.lane ? ` as the ${row.lane} lane` : ''}.`);
        },
        error: (err) => {
          this.busy.set(false);
          this.fail('Model not created.', err);
        },
      });
  }

  assign(m: ModelView, lane: 'primary' | 'secondary'): void {
    this.backend.assignLane(m.id, lane).subscribe({
      next: (r) => this.ok(`Lanes are now primary=${r.primary ?? 'none'}, secondary=${r.secondary ?? 'none'}.`),
      error: (err) => this.fail('Could not assign that lane.', err),
    });
  }

  deleteModel(m: ModelView): void {
    this.backend.deleteModel(m.id).subscribe({
      next: () => this.ok(`Model ${m.model_id} deleted.`),
      error: (err) => this.fail('Delete failed.', err),
    });
  }

  runProbe(p: ProviderView, modelId: string): void {
    this.probe.set(null);
    this.busy.set(true);
    this.message.set({ tone: 'ok', text: `Probing ${modelId} on ${p.name}…` });
    this.backend.probe(p.id, modelId).subscribe({
      next: (r) => {
        this.busy.set(false);
        this.probe.set(r);
        this.message.set({
          tone: r.ok ? 'ok' : 'bad',
          text: r.ok
            ? `${r.model} answered HTTP ${r.status} in ${r.latency_ms}ms.`
            : `${r.model} failed: ${r.error_code ?? `HTTP ${r.status ?? 'n/a'}`} ${r.error ?? r.detail ?? ''}`,
        });
      },
      error: (err) => {
        this.busy.set(false);
        this.fail('Probe did not complete.', err);
      },
    });
  }
}
