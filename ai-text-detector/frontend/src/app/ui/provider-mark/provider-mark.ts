import { Component, computed, input } from '@angular/core';
import { PROVIDER_HUES, PROVIDER_LOGOS } from '../../core/provider-brand';

/** The endpoint's own mark: its published logo, grey at rest, and lit in the vendor's colour when
    that endpoint is one the caller can actually spend.

    The artwork is the vendor's file, from `PROVIDER_LOGOS`, drawn as an image rather than retyped:
    a letter in a circle is not a logo, and it tells the user nothing they cannot read in the name
    beside it. At rest the mark is the logo's own shape drained of colour; lit, it is the logo in
    the brand's colour with a halo of the same hue behind it.

    The halo is painted, not filtered. The visual language bans blur and drop-shadow as glass, and a
    filter would also turn the mark into a containing block for anything fixed — so the light is a
    radial-gradient on an unclipped box sitting behind the image. The only rounding in this file is
    50 per cent on the fallback dot for an endpoint that has no published mark.

    Neither the colour nor the shape is invented here: the hue comes from `PROVIDER_HUES` and is
    written onto the element as `--neon`, so the stylesheet stays vendor-free, and an endpoint with
    no published hue falls back to the app's own accent rather than to a hex somebody guessed. */
@Component({
  selector: 'app-provider-mark',
  standalone: true,
  // The mark is decoration and the name is either beside it or absent, so the composite is labelled
  // as one thing: a screen reader on "nvidia, spendable" needs a name, not an image with no text.
  host: { role: 'img', '[attr.aria-label]': 'name()' },
  styles: `
    :host {
      display: inline-flex;
      align-items: center;
      gap: var(--s-2);
      min-width: 0;
    }
    .mark {
      position: relative;
      flex: none;
      display: grid;
      place-items: center;
      width: 22px;
      height: 22px;
    }
    /* Rest: no light at all. The gradient is fully transparent until the class lands, so a page of
       unlit endpoints stays a page of grey marks and the lit ones are the only thing that glows. */
    .halo {
      position: absolute;
      inset: -4px;
      opacity: 0;
      background-image: radial-gradient(circle at 50% 50%,
        color-mix(in oklab, var(--neon, var(--accent)) 45%, transparent) 0%,
        color-mix(in oklab, var(--neon, var(--accent)) 16%, transparent) 52%,
        transparent 74%);
      transition: opacity var(--t-fast) linear;
    }
    .mark.lit .halo {
      opacity: 1;
    }
    .logo {
      position: relative;
      width: 19px;
      height: 19px;
      object-fit: contain;
      opacity: 0.5;
      filter: grayscale(1);
      transition: opacity var(--t-fast) linear, filter var(--t-fast) linear;
    }
    .mark.lit .logo {
      opacity: 1;
      filter: grayscale(0);
    }
    /* An endpoint with no published mark still gets a presence: a bare LED, deliberately letterless. */
    .dot {
      position: relative;
      display: grid;
      place-items: center;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      border: 1px solid var(--edge);
      background-color: transparent;
      transition: background-color var(--t-fast) linear, border-color var(--t-fast) linear, box-shadow var(--t-fast) linear;
    }
    .mark.lit .dot {
      border-color: var(--neon, var(--accent));
      background-color: color-mix(in oklab, var(--neon, var(--accent)) 22%, transparent);
      box-shadow:
        0 0 0 1px var(--neon, var(--accent)),
        0 0 6px var(--neon, var(--accent)),
        0 0 16px color-mix(in oklab, var(--neon, var(--accent)) 55%, transparent);
    }
    /* The big size is for a sticker: the keys page asks "which endpoint", so its marks are the
       control, not a decoration beside a name. Only the artwork scales — rest/lit, hue and halo are
       the same contract at either size. (No backticks in this comment: it lives inside a template
       literal.) */
    .mark.big {
      width: 40px;
      height: 40px;
    }
    .mark.big .logo {
      width: 34px;
      height: 34px;
    }
    .mark.big .dot {
      width: 14px;
      height: 14px;
    }
    .name {
      font-size: var(--fs-meta);
      color: var(--ink);
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .name.big {
      font-size: var(--fs-body);
    }
  `,
  template: `
    <span class="mark" [class.lit]="lit()" [class.unknown]="!logo()" [class.big]="big()"
          [style.--neon]="lit() ? hue() : null" aria-hidden="true">
      <span class="halo"></span>
      @if (logo()) {
        <img class="logo" [src]="logo()" alt="" draggable="false" />
      } @else {
        <span class="dot"></span>
      }
    </span>
    @if (name()) {
      <span class="name" [class.big]="big()">{{ name() }}</span>
    }
  `,
})
export class ProviderMark {
  /** The provider id, as the backend stores it (`nvidia`, `openrouter`, or whatever an admin typed
      in). Case-insensitive for both lookups; the label is what the user reads. */
  readonly provider = input('');
  /** Display name. Falls back to the provider id, because a mark with no name at all is a logo a
      screen reader cannot describe. */
  readonly label = input('');
  /** Whether the endpoint is spendable for this caller. Grey at rest is the point: a page of lit
      logos is decoration, and "which of these can I pay for" is the question the mark answers. */
  readonly lit = input(false);
  /** Sticker size, for the surfaces where the mark IS the control. */
  readonly big = input(false);

  readonly hue = computed(() => {
    const p = this.provider().toLowerCase();
    return PROVIDER_HUES[p];
  });

  /** The vendor's published file, or nothing — and nothing renders a dot, never an initial. */
  readonly logo = computed(() => {
    const p = this.provider().toLowerCase();
    return PROVIDER_LOGOS[p];
  });

  readonly name = computed(() => this.label() || this.provider());
}
