import { Component, Input, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MAX_TEXT_LENGTH, MIN_TEXT_LENGTH } from '../../core/detection.service';
import { BackendService } from '../../core/backend.service';
import type { DetectionFormState } from '../../core/form-state';

const SAMPLES: { id: string; label: string; text: string }[] = [
  {
    id: 'ai',
    label: 'AI-flavoured paragraph',
    text:
      'In today’s rapidly evolving digital landscape, organizations must embrace transformative ' +
      'technologies to remain competitive. Moreover, by leveraging cloud-native architectures, ' +
      'businesses can unlock unprecedented agility, drive measurable outcomes, and foster a culture ' +
      'of continuous innovation. It is not merely a matter of efficiency; it is a strategic imperative ' +
      'that will shape the future of the enterprise for years to come.',
  },
  {
    id: 'human',
    label: 'Human forum post',
    text:
      'tried this last week after my pump started making that clicking noise again. turned out the ' +
      'impeller was stuffed with hair, which yeah. took me 40 mins and a flathead. if yours does the ' +
      'same thing dont bother buying a new one, just pull it apart.',
  },
  {
    id: 'gray',
    label: 'Hybrid / short',
    text: 'Please find attached the report. Let me know if anything looks off and I will adjust it.',
  },
];

@Component({
  selector: 'app-text-panel',
  imports: [FormsModule, DecimalPipe],
  templateUrl: './text-panel.html',
})
export class TextPanel {
  @Input({ required: true }) state!: DetectionFormState;

  private readonly backend = inject(BackendService);

  readonly samples = SAMPLES;
  readonly min = MIN_TEXT_LENGTH;
  readonly max = MAX_TEXT_LENGTH;

  /** One-line transcript of the last document read: what came in, or why it didn't. */
  readonly docStatus = signal<{ text: string; bad: boolean } | null>(null);
  readonly extracting = signal(false);

  readonly length = computed(() => this.state.text().length);
  readonly tooShort = computed(() => {
    const n = this.length();
    return n > 0 && n < this.min;
  });
  readonly tooLong = computed(() => this.length() > this.max);

  load(text: string): void {
    this.state.text.set(text);
    this.docStatus.set(null);
  }

  clear(): void {
    this.state.text.set('');
    this.docStatus.set(null);
  }

  /** The hidden <input type=file> behind the "Load document" label fires this. */
  pickFile(ev: Event): void {
    const input = ev.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ''; // let the same file be picked again after an edit
    if (!file) {
      return;
    }
    this.extracting.set(true);
    this.docStatus.set({ text: `Reading ${file.name}…`, bad: false });
    this.backend.extractFile(file).subscribe({
      next: (r) => {
        this.extracting.set(false);
        this.state.text.set(r.text);
        const cut = r.truncated ? ` · cut at ${this.max.toLocaleString()} chars` : '';
        this.docStatus.set({
          text: `${r.filename} → ${r.chars.toLocaleString()} chars${cut}`,
          bad: false,
        });
      },
      error: (e: unknown) => {
        this.extracting.set(false);
        const err = e as { status?: number; error?: { error?: string } };
        let msg: string;
        if (err.status === 0) {
          msg = `Backend not answering on 127.0.0.1:8787 — start it with python -m backend.app`;
        } else {
          msg = err.error?.error ?? `Could not read ${file.name} (HTTP ${err.status}).`;
        }
        this.docStatus.set({ text: msg, bad: true });
      },
    });
  }
}
