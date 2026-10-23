import { Component, computed, input } from '@angular/core';

export type MetricTone = 'neutral' | 'green' | 'red' | 'amber';

/** A circular percentile gauge.

    Pure SVG, no dependency: a background track ring plus a value arc whose length is the fraction.
    An untoned arc is the instrument's own grey, because a measurement with no verdict attached to it
    has no business borrowing a hue; `tone` is what states one (human-green, ai-red, warn-amber, and
    in the ranking the availability of a key). The number in the middle is the percentile (0-100).

    Every field the view reads is a signal input, because the surfaces that answer a run — the Detect
    rail above all — outlive the run they were built for and are handed the next verdict as a new
    value on the same instance. Under zoneless change detection a computed over a plain field is
    evaluated once and cached forever, so a second run would have redrawn the layout and kept the
    first run's arc. */
@Component({
  selector: 'app-circular-metric',
  standalone: true,
  template: `
    <div class="ring" [class.compact]="compact()" [attr.data-tone]="tone()" role="img"
         [attr.aria-label]="label() + ': ' + (fraction() * 100).toFixed(0) + '%'">
      <svg viewBox="0 0 120 120" class="ring-svg">
        <circle class="ring-track" cx="60" cy="60" [attr.r]="radius" />
        <circle class="ring-value" cx="60" cy="60" [attr.r]="radius"
                [attr.stroke-dasharray]="circumference"
                [attr.stroke-dashoffset]="offset()" />
      </svg>
      <div class="ring-center">
        <span class="ring-num">{{ display() }}</span>
        @if (unit()) { <span class="ring-unit">{{ unit() }}</span> }
      </div>
    </div>
    @if (label()) { <div class="ring-label">{{ label() }}</div> }
  `,
})
export class CircularMetric {
  /** 0..1 fraction of the ring to fill. Values outside are clamped, and a value that is not a number
      fills nothing. */
  readonly value = input<number | null | undefined>(0);

  /** Text shown in the middle. Defaults to the percentage; override for e.g. "12/48", or for a
      measurement that has a decimal the rounded arc cannot carry. */
  readonly displayText = input<string | undefined>(undefined);
  readonly unit = input('%');
  readonly label = input('');
  readonly tone = input<MetricTone>('neutral');
  readonly compact = input(false);

  readonly radius = 52;
  readonly circumference = 2 * Math.PI * this.radius;

  readonly fraction = computed(() => {
    const v = this.value();
    return typeof v === 'number' && isFinite(v) ? Math.max(0, Math.min(1, v)) : 0;
  });
  readonly offset = computed(() => this.circumference * (1 - this.fraction()));
  readonly display = computed(() =>
    this.displayText() ?? String(Math.round(this.fraction() * 100)),
  );
}
