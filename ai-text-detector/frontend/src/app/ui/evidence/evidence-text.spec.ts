import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AuthService } from '../../core/auth.service';
import type { ModelPhrase } from '../../core/detection.types';
import { buildSegments } from './evidence-segments';
import { EvidenceText } from './evidence-text';

/**
 * The paint, in a browser-shaped DOM.
 *
 * `evidence-color.spec.ts` proves the maths on the hexes the ramp returns; this proves the surface
 * that uses them is handed a solid plate to read on. An rgba wash over a translucent evidence panel
 * has no definite colour, so nothing about legibility could be proven — and the strength scale would
 * be carried by alpha over an unknown backdrop.
 *
 * Attribution after a failover is tested in ../score-readout/score-readout.spec.ts, where the verdict
 * is actually printed.
 */
const PHRASES: ModelPhrase[] = [
  { quote: 'ubiquitous leverage', direction: 'AI', strength: 0.9 },
  { quote: 'plain-speaking clause', direction: 'HUMAN', strength: 0.6 },
];

const TEXT = 'The report leaned on ubiquitous leverage before a plain-speaking clause.';

async function render(text: string, phrases: ModelPhrase[]): Promise<ComponentFixture<EvidenceText>> {
  // The ramp is theme-dependent, so the paint reads the signed-in theme. AuthService owns the
  // session and talks to the backend, so this spec pins it to one dark answer instead of letting a
  // component test reach for HTTP.
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [EvidenceText],
    providers: [{ provide: AuthService, useValue: { theme: () => 'dark' as const } }],
  }).compileComponents();
  const f = TestBed.createComponent(EvidenceText);
  f.componentRef.setInput('segments', buildSegments(text, phrases).segments);
  f.detectChanges();
  await f.whenStable();
  f.detectChanges();
  return f;
}

const hits = (f: ComponentFixture<EvidenceText>): HTMLElement[] =>
  Array.from((f.nativeElement as HTMLElement).querySelectorAll<HTMLElement>('mark.ev-hit'));

describe('EvidenceText paint', () => {
  it('hands each quoted phrase an opaque background and its own ink', async () => {
    const f = await render(TEXT, PHRASES);
    const marks = hits(f);
    expect(marks.length).toBe(2);
    const declared = (el: HTMLElement, prop: 'backgroundColor' | 'color'): string =>
      getComputedStyle(el)[prop] || el.style[prop] || '';
    for (const m of marks) {
      const bg = declared(m, 'backgroundColor');
      const ink = declared(m, 'color');
      expect(bg, 'the phrase must carry a background').not.toBe('');
      // Opacity is a property of the colour's arity, not of its last digit: an earlier version of
      // this assertion rejected anything ending in `0)`, which the shipped ramp paints at strength
      // 0.9 in dark mode (#745900 — a gold with a genuinely zero blue channel). An alpha channel
      // arrives as `rgba(...)` or as a `/`-separated component; neither means "opaque plate".
      const hasAlpha = /^rgba\(/.test(bg) || /\//.test(bg);
      expect(hasAlpha, `must be opaque, not an alpha wash: ${bg}`).toBe(false);
      expect(ink, 'the ink is part of the ramp, not inherited from the panel').not.toBe('');
      expect(ink).not.toBe(bg);
    }
    expect(marks[0].textContent).toContain('ubiquitous leverage');
    expect(marks[1].textContent).toContain('plain-speaking clause');
  });

  it('keeps every character of the text it was given, quoted or not', async () => {
    const f = await render(TEXT, PHRASES);
    const runs = Array.from(
      (f.nativeElement as HTMLElement).querySelectorAll<HTMLElement>('mark.ev-hit, span.ev-plain'),
    )
      .map((el) => el.textContent)
      .join('');
    expect(runs).toBe(TEXT);
  });

  it('paints the plate but no letters when the caller only wants the ground', async () => {
    // The layer underneath a writing surface supplies its own words: a second copy of them showing
    // through a plate reads as blurred text, so `platesOnly` has to answer the user-agent's own
    // `mark` colour and the reading ink both, on the node itself.
    const f = await render(TEXT, PHRASES);
    f.componentRef.setInput('platesOnly', true);
    f.detectChanges();
    hits(f).forEach((m) => expect(m.style.color).toBe('transparent'));
    const plain = (f.nativeElement as HTMLElement).querySelector('span.ev-plain');
    expect(plain!.getAttribute('style')).toContain('transparent');
  });
});
