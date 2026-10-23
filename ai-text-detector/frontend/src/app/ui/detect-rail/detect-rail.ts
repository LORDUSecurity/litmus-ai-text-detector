import { ChangeDetectionStrategy, Component, inject, input, output, signal } from '@angular/core';
import { MAX_TEXT_LENGTH } from '../../core/detection.service';
import { BackendService } from '../../core/backend.service';
import type { DetectionFormState } from '../../core/form-state';
import type { DetectionResult } from '../../core/detection.types';
import { ScoreReadout } from '../score-readout/score-readout';

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

/**
 * The column a run is started from, and the score it produced.
 *
 * Everything that answers "what happens when I press this" — the samples, the document reader, the
 * model this press will use, the press itself — sits beside the text field instead of on top of it,
 * and the verdict lands directly under the button that earned it rather than below the analysis it
 * is a summary of. The writing stays on the left, the reading on the right, and the evidence panel
 * spans the whole width underneath both because it quotes the writing at length.
 *
 * The samples and the loader write into the `DetectionFormState` signal the textarea is already
 * bound to, which is why they live here at all: a copy of its own would make both dead.
 */
@Component({
  selector: 'app-detect-rail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ScoreReadout],
  templateUrl: './detect-rail.html',
})
export class DetectRail {
  readonly state = input.required<DetectionFormState>();
  readonly pending = input(false);
  readonly elapsed = input(0);
  readonly inflightHint = input('');
  /** A ranked model chosen on the Ranking tab, or null when the deployed lane is used. */
  readonly pickedModel = input<string | null>(null);
  readonly pickedIsTieBreak = input(false);
  readonly result = input<DetectionResult | null>(null);

  /** Pressing Detect is the parent's job — it owns the request, the timer and the notice banner. */
  readonly run = output<void>();
  readonly clearPicked = output<void>();

  private readonly backend = inject(BackendService);

  readonly samples = SAMPLES;
  readonly max = MAX_TEXT_LENGTH;

  /** One-line transcript of the last document read: what came in, or why it didn't. */
  readonly docStatus = signal<{ text: string; bad: boolean } | null>(null);
  readonly extracting = signal(false);

  load(text: string): void {
    this.state().text.set(text);
    this.state().textFromDocument.set(false);
    this.docStatus.set(null);
  }

  clear(): void {
    this.state().text.set('');
    this.state().textFromDocument.set(false);
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
        this.state().text.set(r.text);
        this.state().textFromDocument.set(true);
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
