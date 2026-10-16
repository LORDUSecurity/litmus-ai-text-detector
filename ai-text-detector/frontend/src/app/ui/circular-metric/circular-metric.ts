import { Component, Input, computed } from '@angular/core';

/** A circular percentile gauge — the "circular metrics percentile" the design calls for.

    Pure SVG, no dependency: a background track ring plus a value arc whose length is the
    fraction, stroked in the tune colour (blue) by default. `tone` swaps the arc colour for
    the semantic cases (human-green, ai-red, warn-amber) so the same gauge reads as a score,
    a coverage bar or a risk dial. The number in the middle is the percentile (0-100). */
@Component({
  selector: 'app-circular-metric',
  standalone: true,
  template: `
    <div class="ring" [class.compact]="compact" [attr.data-tone]="tone" role="img"
         [attr.aria-label]="label + ': ' + (fraction() * 100).toFixed(0) + '%'">
      <svg viewBox="0 0 120 120" class="ring-svg">
        <circle class="ring-track" cx="60" cy="60" [attr.r]="radius" />
        <circle class="ring-value" cx="60" cy="60" [attr.r]="radius"
                [attr.stroke-dasharray]="circumference"
                [attr.stroke-dashoffset]="offset()" />
      </svg>
      <div class="ring-center">
        <span class="ring-num">{{ display() }}</span>
        @if (unit) { <span class="ring-unit">{{ unit }}</span> }
      </div>
    </div>
    @if (label) { <div class="ring-label">{{ label }}</div> }
  `,
})
export class CircularMetric {
  /** 0..1 fraction of the ring to fill. Values outside are clamped. */
  @Input() set value(v: number | null | undefined) {
    this._value = typeof v === 'number' && isFinite(v) ? v : 0;
  }
  private _value = 0;

  /** Text shown in the middle. Defaults to the percentage; override for e.g. "12/48". */
  @Input() displayText?: string;
  @Input() unit = '%';
  @Input() label = '';
  @Input() tone: 'blue' | 'green' | 'red' | 'amber' | 'violet' = 'blue';
  @Input() compact = false;

  readonly radius = 52;
  readonly circumference = 2 * Math.PI * this.radius;

  readonly fraction = computed(() => Math.max(0, Math.min(1, this._value)));
  readonly offset = computed(() => this.circumference * (1 - this.fraction()));
  readonly display = computed(() =>
    this.displayText != null
      ? this.displayText
      : Math.round(this.fraction() * 100).toString(),
  );
}
