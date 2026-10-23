import { Component, OnInit, computed, inject, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import type { ApiKeyView, ProviderView } from '../../core/backend.types';

/** One shared key plus the endpoint that holds it. The nested rows carry no provider name of their
    own, and a table of twenty `nvapi-…` hints with no endpoint beside them answers no question. */
interface KeyRow extends ApiKeyView {
  provider: string;
  providerId: number;
}

interface NewProvider {
  name: string;
  base_url: string;
  label: string;
}

interface NewKey {
  providerId: number;
  api_key: string;
  label: string;
}

/** Same rule the backend applies on POST /admin/providers/:id/keys — spelled here so a paste with a
    stray space is refused before anything is sent, rather than after. */
const KEY_PATTERN = /^[A-Za-z0-9._:+/-]+$/;
const PROVIDER_PATTERN = /^[a-z0-9_-]{2,40}$/;

/**
 * The credentials this install dials when a detection has no key of its own, and the endpoints those
 * keys belong to.
 *
 * It moved here from the bottom of Advanced parameters because that is where the other half of the
 * subject already lives: the endpoint stickers, their verified marks, and the user's own key. One
 * surface answers "what can this install pay for", whether the answer is a shared key or his.
 * Advanced keeps the model chooser — the per-model bands and which lane a row serves — because those
 * are the calibrated parameters a ranking cites, not credentials.
 *
 * Plaintext never comes back: the backend masks at the source (`key_hint` is six characters plus a
 * length), and this panel shows what it is told.
 */
@Component({
  selector: 'app-admin-keys',
  imports: [FormsModule],
  template: `
    @if (message(); as m) {
      <p class="doc-status" [class.bad]="m.tone === 'bad'" aria-live="polite">{{ m.text }}</p>
    }

    <div class="scroll-x">
      <table class="grid">
        <thead>
          <tr><th>endpoint</th><th>name</th><th>hint</th><th>last used</th><th>active</th><th></th></tr>
        </thead>
        <tbody>
          @for (k of rows(); track k.id) {
            <tr>
              <td>{{ k.provider }}</td>
              <td>{{ k.label }}</td>
              <td class="mono">{{ k.key_hint }}</td>
              <td class="hint">{{ used(k.last_used_at) }}</td>
              <td>
                <button type="button" (click)="setActive(k.id, !k.is_active)">
                  {{ k.is_active ? 'deactivate' : 'activate' }}
                </button>
              </td>
              <td><button type="button" class="danger" (click)="remove(k.id)">delete</button></td>
            </tr>
          } @empty {
            <tr>
              <td colspan="6" class="hint">
                No shared key yet. Detection for these endpoints then runs on the key each user
                supplies himself, and a run with none fails with <code>PROVIDER_KEY_MISSING</code>.
              </td>
            </tr>
          }
        </tbody>
      </table>
    </div>

    @if (addingKey()) {
      <form class="inline-form" (ngSubmit)="addKey(); $event.preventDefault()">
        <label>endpoint
          <select name="ak_provider" [ngModel]="keyForm().providerId"
                  (ngModelChange)="keyForm.set({ ...keyForm(), providerId: $any($event) })">
            <option [ngValue]="0">— choose —</option>
            @for (p of providers(); track p.id) {
              <option [ngValue]="p.id">{{ p.label || p.name }}</option>
            }
          </select>
        </label>
        <label>name
          <input name="ak_label" [ngModel]="keyForm().label"
                 (ngModelChange)="keyForm.set({ ...keyForm(), label: $any($event) })" />
        </label>
        <label>api key
          <input name="ak_value" type="password" size="40" autocomplete="off"
                 [ngModel]="keyForm().api_key"
                 (ngModelChange)="keyForm.set({ ...keyForm(), api_key: $any($event) })"
                 placeholder="nvapi-… / sk-or-…" />
        </label>
        <button class="primary" type="submit" [disabled]="busy()">Store encrypted</button>
        <button type="button" (click)="addingKey.set(false)">Cancel</button>
      </form>
    }

    @if (addingProvider()) {
      <form class="inline-form" (ngSubmit)="addProvider(); $event.preventDefault()">
        <label>slug
          <input name="ap_name" size="14" [ngModel]="providerForm().name" placeholder="nvidia"
                 (ngModelChange)="providerForm.set({ ...providerForm(), name: $any($event) })" />
        </label>
        <label>label
          <input name="ap_label" size="14" [ngModel]="providerForm().label" placeholder="NVIDIA"
                 (ngModelChange)="providerForm.set({ ...providerForm(), label: $any($event) })" />
        </label>
        <label>base URL
          <input name="ap_url" size="34" [ngModel]="providerForm().base_url"
                 placeholder="https://integrate.api.nvidia.com/v1"
                 (ngModelChange)="providerForm.set({ ...providerForm(), base_url: $any($event) })" />
        </label>
        <button class="primary" type="submit" [disabled]="busy()">Add endpoint</button>
        <button type="button" (click)="addingProvider.set(false)">Cancel</button>
      </form>
    }

    <div class="row-actions">
      <button type="button" class="primary" data-act="add-key"
              [attr.aria-expanded]="addingKey()" (click)="toggle('addingKey')">
        {{ addingKey() ? 'Cancel' : 'Add shared key' }}
      </button>
      <button type="button" data-act="add-endpoint"
              [attr.aria-expanded]="addingProvider()" (click)="toggle('addingProvider')">
        {{ addingProvider() ? 'Cancel' : 'Add endpoint' }}
      </button>
      <span class="hint">
        The plaintext goes once over loopback, is stored with Fernet, and no endpoint returns it.
      </span>
    </div>
  `,
})
export class AdminKeys implements OnInit {
  private readonly backend = inject(BackendService);

  /** Fired after anything that changes what an endpoint can be paid for, so the page that shows the
      verified marks can re-read them instead of leaving a lit logo over a deleted key. */
  readonly changed = output<void>();

  readonly providers = signal<ProviderView[]>([]);
  readonly busy = signal(false);
  readonly message = signal<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  readonly addingKey = signal(false);
  readonly addingProvider = signal(false);
  readonly keyForm = signal<NewKey>({ providerId: 0, api_key: '', label: 'key' });
  readonly providerForm = signal<NewProvider>({ name: '', base_url: '', label: '' });

  readonly rows = computed<KeyRow[]>(() =>
    this.providers().flatMap((p) =>
      p.keys.map((k) => ({ ...k, provider: p.label || p.name, providerId: p.id }))),
  );

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.backend.providers().subscribe({
      next: (rows) => this.providers.set(rows),
      error: (err) => this.fail('Could not read the stored keys.', err),
    });
  }

  toggle(which: 'addingKey' | 'addingProvider'): void {
    const next = !this[which]();
    this[which].set(next);
    // The form opens already pointing at an endpoint: with nothing chosen its first click would only
    // answer "Pick an endpoint first", and the select below already shows that one as the default.
    const first = this.providers()[0];
    if (which === 'addingKey' && next && !this.keyForm().providerId && first) {
      this.keyForm.update((f) => ({ ...f, providerId: first.id }));
    }
  }

  addKey(): void {
    const k = this.keyForm();
    if (!k.providerId) {
      this.message.set({ tone: 'bad', text: 'Pick an endpoint first.' });
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
        this.keyForm.set({ providerId: k.providerId, api_key: '', label: 'key' });
        this.addingKey.set(false);
        this.ok(`Key stored encrypted as "${row.key_hint}". The plaintext is not kept anywhere else.`);
      },
      error: (err) => {
        this.busy.set(false);
        // His paste stays in the field: closing the form here would throw away what he typed and
        // blame him for the backend's answer.
        this.fail('Key not stored.', err);
      },
    });
  }

  addProvider(): void {
    const p = this.providerForm();
    if (!PROVIDER_PATTERN.test(p.name.trim())) {
      this.message.set({ tone: 'bad', text: 'The slug must be 2-40 characters of [a-z0-9_-].' });
      return;
    }
    if (!/^https?:\/\//i.test(p.base_url.trim())) {
      this.message.set({ tone: 'bad', text: 'The base URL must be an absolute http(s) URL.' });
      return;
    }
    this.busy.set(true);
    this.backend
      .createProvider({ name: p.name.trim(), base_url: p.base_url.trim(), label: p.label, notes: '' })
      .subscribe({
        next: (row) => {
          this.busy.set(false);
          this.providerForm.set({ name: '', base_url: '', label: '' });
          this.addingProvider.set(false);
          this.ok(`Endpoint "${row.name}" added. Store a key for it and its logo lights up.`);
        },
        error: (err) => {
          this.busy.set(false);
          this.fail('Endpoint not added.', err);
        },
      });
  }

  setActive(keyId: number, active: boolean): void {
    this.backend.setKeyActive(keyId, active).subscribe({
      next: () => this.ok(active ? 'Key activated.' : 'Key deactivated.'),
      error: (err) => this.fail('Could not change that key.', err),
    });
  }

  remove(keyId: number): void {
    this.backend.deleteKey(keyId).subscribe({
      next: () => this.ok('Key deleted from the database.'),
      error: (err) => this.fail('Could not delete that key.', err),
    });
  }

  /** SQLite writes UTC with a space and no offset, so the Z is what makes it not local time. */
  used(at: string | null): string {
    if (!at) return 'never';
    const d = new Date(`${at.replace(' ', 'T')}Z`);
    return Number.isNaN(d.getTime()) ? at : d.toLocaleString();
  }

  private ok(text: string): void {
    this.message.set({ tone: 'ok', text });
    this.reload();
    this.changed.emit();
  }

  private fail(prefix: string, err: unknown): void {
    const detail =
      err instanceof HttpErrorResponse
        ? ((err.error as { error?: string })?.error ?? err.message)
        : String(err);
    this.message.set({ tone: 'bad', text: `${prefix} ${detail}` });
  }
}
