import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { AuthService } from '../../core/auth.service';
import { evidencePaint, type EvidenceTheme } from './evidence-color';
import type { EvidenceSegment } from './evidence-segments';

/** The phrase paint, once. Every surface that shows a model's quoted evidence — the result
    card's fold, the history sheet, and the highlight underneath the input box — draws these same
    `<mark>` runs, so the plate, the ink and the hover strength cannot drift apart between them.

    It renders bare siblings and no box of its own: `display: contents` leaves the marks as
    children of whatever the caller supplies, because the container is the part that differs. The
    evidence fold wants a scrolling paragraph in the app's reading font; the input mirror wants a
    layer that matches the textarea's metrics character for character. Neither belongs in here.

    A segment carries the text's own characters, sliced from the original, so the caller's
    container must preserve whitespace (`white-space: pre-wrap`) — and the loop keeps every run
    on one line so no template indentation can leak into the text. */
@Component({
  selector: 'app-evidence-text',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [],
  template: `
    @for (s of segments(); track $index) { @if (s.direction === 'HUMAN') { @let hp = paint('HUMAN', s.strength); <mark class="ev-hit human" [style.background-color]="hp.background" [style.color]="platesOnly() ? 'transparent' : hp.ink" [title]="'human evidence · strength ' + pct(s.strength)">{{ s.text }}</mark> } @else if (s.direction === 'AI') { @let ap = paint('AI', s.strength); <mark class="ev-hit ai" [style.background-color]="ap.background" [style.color]="platesOnly() ? 'transparent' : ap.ink" [title]="'AI evidence · strength ' + pct(s.strength)">{{ s.text }}</mark> } @else { <span class="ev-plain" [style.color]="platesOnly() ? 'transparent' : null">{{ s.text }}</span> } }
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class EvidenceText {
  /** The runs to draw, in text order, from `buildSegments()`. A signal input because this list is
      recomputed every time the scored text or the deciding lane changes, and the view must follow
      it without a zone to tell it to. */
  readonly segments = input.required<EvidenceSegment[]>();

  /** Draw the plates and nothing else: every glyph — quoted or not — gets transparent ink, so
      whatever is on top of this layer supplies the letters. Used by the layer underneath the input
      box, which is a second copy of the same characters sitting exactly behind them. This has to be
      an explicit `transparent` rather than simply omitting a colour, because both the user-agent's
      own `mark` rule and the reading ink for an unquoted run outrank the transparent the mirror
      container asks for, and a doubled glyph shows through as a darker edge on the quoted phrases. */
  readonly platesOnly = input(false);

  /** The ramp is theme-dependent: a plate that is legible on absolute black has no edge against
      absolute white. The signed-in theme is the only authority for which one to draw. */
  private readonly auth = inject(AuthService);
  readonly theme = computed<EvidenceTheme>(() => (this.auth.theme() === 'light' ? 'light' : 'dark'));

  paint(direction: 'AI' | 'HUMAN', strength: number) {
    return evidencePaint(direction, strength, this.theme());
  }

  pct(value: number): string {
    return (Math.max(0, Math.min(1, value)) * 100).toFixed(1) + '%';
  }
}
