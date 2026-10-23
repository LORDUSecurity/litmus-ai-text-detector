import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ProviderMark } from './provider-mark';
import { PROVIDER_HUES, PROVIDER_LOGOS } from '../../core/provider-brand';

/**
 * The endpoint's own logo: grey until that endpoint is one the caller can actually spend, then it
 * lights in the brand's colour.
 *
 * Three things this has to keep true. The artwork is the vendor's published file, not a letter
 * invented here — an initial inside a circle reads as a brand and is not one. The colour is the
 * PROVIDER's, not the app's: the design brief spends its gold on interaction and green/yellow on
 * verdicts, so an endpoint cannot get a hue of its own from the palette; it comes from a brand map
 * and is written as a custom property, which is the only place a brand hue enters this app. And at
 * rest it is grey: a page of six lit logos is decoration, and "which of these can I pay for right
 * now" is the question the mark answers.
 *
 * The halo is a radial-gradient behind an unclipped box, not a filter: `check_visual_language.py`
 * bans blur/drop-shadow as glass, and a clip-path erases anything drawn outside the edge it cuts.
 */
async function build(inputs: { provider: string; label?: string; lit?: boolean }): Promise<ComponentFixture<ProviderMark>> {
  await TestBed.configureTestingModule({ imports: [ProviderMark] }).compileComponents();
  const f = TestBed.createComponent(ProviderMark);
  f.componentRef.setInput('provider', inputs.provider);
  if (inputs.label !== undefined) f.componentRef.setInput('label', inputs.label);
  if (inputs.lit !== undefined) f.componentRef.setInput('lit', inputs.lit);
  f.detectChanges();
  return f;
}

const mark = (f: ComponentFixture<ProviderMark>) =>
  f.nativeElement.querySelector('.mark') as HTMLElement;

const logo = (f: ComponentFixture<ProviderMark>) =>
  f.nativeElement.querySelector('img.logo') as HTMLImageElement | null;

describe('ProviderMark', () => {
  it('draws the vendor’s published logo file', async () => {
    const f = await build({ provider: 'nvidia', label: 'NVIDIA' });
    expect(logo(f)?.getAttribute('src')).toBe(PROVIDER_LOGOS['nvidia']);
    // And nothing of the old letter mark survives: the circle held a glyph, the logo holds none.
    expect(mark(f).textContent?.trim()).toBe('');
  });

  it('looks the logo up case-insensitively, because the id is typed by an admin', async () => {
    const f = await build({ provider: 'OpenRouter', label: 'OpenRouter' });
    expect(logo(f)?.getAttribute('src')).toBe(PROVIDER_LOGOS['openrouter']);
  });

  it('carries the brand hue as a custom property so the stylesheet never hardcodes a vendor', async () => {
    const f = await build({ provider: 'nvidia', lit: true });
    const hue = mark(f).style.getPropertyValue('--neon').toLowerCase();
    expect(hue).toBe(PROVIDER_HUES['nvidia'].toLowerCase());
  });

  it('never lights a logo whose hue it does not know', () => {
    // A file in the logo map with no hue would glow in the app's own accent, which is a claim about
    // the app, not the vendor — so the two maps have to be kept in step.
    for (const slug of Object.keys(PROVIDER_LOGOS)) {
      expect(PROVIDER_HUES[slug], `${slug} has a logo but no published hue`).toBeTruthy();
    }
  });

  it('is grey at rest', async () => {
    const f = await build({ provider: 'nvidia', lit: false });
    expect(mark(f).classList.contains('lit')).toBe(false);
    // The grey is the sheet's own dim ink, not a colour invented here: no hue is painted until lit.
    expect(mark(f).style.getPropertyValue('--neon')).toBe('');
  });

  it('lights when the endpoint is one the caller can spend', async () => {
    const f = await build({ provider: 'nvidia', lit: true });
    expect(mark(f).classList.contains('lit')).toBe(true);
    expect(mark(f).style.getPropertyValue('--neon').toLowerCase())
      .toBe(PROVIDER_HUES['nvidia'].toLowerCase());
  });

  it('falls back to a bare LED for an endpoint with no published mark, and never to a letter', async () => {
    // An admin adds a gateway with a name nobody predicted. A missing brand must not become a
    // missing control, and it must not become a fake logo either: the dot is the whole affordance.
    const f = await build({ provider: 'kiro-moxin', label: 'Kiro Moxin' });
    expect(logo(f)).toBeNull();
    expect(f.nativeElement.querySelector('.dot')).toBeTruthy();
    expect(mark(f).textContent?.trim()).toBe('');
    expect(mark(f).classList.contains('unknown')).toBe(true);
    expect(mark(f).style.getPropertyValue('--neon')).toBe('');
  });

  it('names the endpoint to assistive tech, because a logo alone says nothing', async () => {
    const f = await build({ provider: 'openrouter', label: 'OpenRouter' });
    // The fixture's nativeElement IS the host: app-provider-mark is the outermost node, so looking
    // for it *inside* itself finds nothing and the test would pass on a component that says nothing.
    const host = f.nativeElement as HTMLElement;
    expect(host.getAttribute('aria-label')).toBe('OpenRouter');
    expect(mark(f).getAttribute('aria-hidden')).toBe('true');
    // The image is decoration beside that name; announcing it twice is noise.
    expect(logo(f)?.getAttribute('alt')).toBe('');
  });
});
