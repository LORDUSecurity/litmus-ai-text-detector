import { ComponentFixture, TestBed } from '@angular/core/testing';

import { TextPanel } from './text-panel';
import { AuthService } from '../../core/auth.service';
import { DetectionFormState } from '../../core/form-state';
import type { DetectionResult } from '../../core/detection.types';

/**
 * The evidence painted inside the writing surface itself.
 *
 * The field is a contenteditable, and the verdict is worn on its own words by a clone of the
 * field's markup sitting underneath it. Four things have to hold here that no other painted surface
 * needs, and they are the reason this file is long:
 *
 *  - the plate layer must be the SAME characters the reader sees, in the SAME boxes, so a quote
 *    cannot land on the word after the one the model picked;
 *  - the characters handed to the shared form state must be what the field holds, including the
 *    line breaks a paragraph or a list item stands for — `textContent` would run them together and
 *    shift every offset the detector gets back;
 *  - the paint gets out of the way while the reader is working in the field, and stops entirely
 *    when the characters are no longer the characters that were scored;
 *  - a value written from outside (a sample, a document, a history reopen) has to reach the field,
 *    because the field, not the signal, is what the reader looks at.
 *
 * `styles.css` and this component's own styles do not apply under jsdom, so what is asserted here is
 * the structure and the inline styles the painter sets. The geometry the two layers share is proven
 * live, in the browser, where the pixels actually exist.
 */
const AI_PHRASE = 'particularly vulnerable';
const HUMAN_PHRASE = 'plain-speaking clause';

const SCORED = `Those types of funds are ${AI_PHRASE}, and the ${HUMAN_PHRASE} closes that gap.`;

const VERDICT = {
  classification: 'AI_GENERATED',
  ai_probability: 0.85,
  confidence: 0.82,
  method: 'single_lane',
  review_required: false,
  component_scores: { deciding_lane: 'primary' },
  evidence: {
    models: [
      {
        lane: 'primary',
        ok: true,
        result: {
          ai_probability: 0.85,
          confidence: 0.82,
          signals: [],
          phrases: [
            { quote: AI_PHRASE, direction: 'AI', strength: 0.8 },
            { quote: HUMAN_PHRASE, direction: 'HUMAN', strength: 0.6 },
          ],
        },
      },
    ],
  },
} as unknown as DetectionResult;

const NO_PHRASES = {
  ...VERDICT,
  evidence: { models: [{ ...VERDICT.evidence!.models![0], result: { phrases: [] } }] },
} as unknown as DetectionResult;

function render(opts: {
  text?: string;
  markup?: string;
  result?: DetectionResult | null;
  scoredText?: string;
}): { f: ComponentFixture<TextPanel>; state: DetectionFormState } {
  // Markup is what the reader left in the field, so the field is what the component has to read:
  // the signal starts empty and the `input` event is what puts the characters in it. Starting from
  // the already-read string would make that write a no-op and no repaint would be asked for.
  const state = new DetectionFormState();
  state.text.set(opts.markup === undefined ? (opts.text ?? SCORED) : '');

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    imports: [TextPanel],
    providers: [{ provide: AuthService, useValue: { theme: () => 'dark' } }],
  });

  const f = TestBed.createComponent(TextPanel);
  f.componentRef.setInput('state', state);
  f.componentRef.setInput('result', opts.result === undefined ? VERDICT : opts.result);
  f.componentRef.setInput('scoredText', opts.scoredText ?? SCORED);
  f.detectChanges();
  if (opts.markup !== undefined) {
    field(f).innerHTML = opts.markup;
    field(f).dispatchEvent(new Event('input'));
    f.detectChanges();
  }
  return { f, state };
}

/** `fixture.nativeElement` is typed as a plain node, so the query helpers cast the root once rather
    than at every call site. */
const root = (f: ComponentFixture<TextPanel>): HTMLElement => f.nativeElement as HTMLElement;

const field = (f: ComponentFixture<TextPanel>): HTMLElement =>
  root(f).querySelector<HTMLElement>('.ed-surface')!;

const layer = (f: ComponentFixture<TextPanel>): HTMLElement =>
  root(f).querySelector<HTMLElement>('.ed-plates')!;

const platesIn = (f: ComponentFixture<TextPanel>): HTMLElement[] =>
  Array.from(root(f).querySelectorAll<HTMLElement>('.ed-plates .ev-plate'));

/** `blur` decides where the paint goes by looking at what took the focus, and the browser only has
    that answer on the next task — so the specs that leave the field wait one macrotask, which is
    what the reader experiences. */
const leave = async (f: ComponentFixture<TextPanel>): Promise<void> => {
  field(f).dispatchEvent(new Event('blur'));
  await new Promise((r) => setTimeout(r, 0));
  f.detectChanges();
};

describe('TextPanel evidence paint', () => {
  it('paints nothing before there is a run', () => {
    const { f } = render({ result: null });
    expect(platesIn(f)).toHaveLength(0);
  });

  it('puts one plate behind each phrase the model quoted', () => {
    const { f } = render({});
    expect(platesIn(f).map((p) => p.textContent)).toEqual([AI_PHRASE, HUMAN_PHRASE]);
    expect(platesIn(f)[0].style.backgroundColor).not.toBe('');
    expect(platesIn(f)[1].style.backgroundColor).not.toBe(platesIn(f)[0].style.backgroundColor);
  });

  /** Asserted as inline styles because a stylesheet cannot be seen here — and because a stylesheet
      is exactly what let the doubled-glyph bug through once: leaving the ink unset is not the same
      as making it transparent, and the plate layer's own colour can be outranked. */
  it('draws no letters of its own', () => {
    const { f } = render({});
    platesIn(f).forEach((p) => expect(p.style.color).toBe('transparent'));
  });

  it('is the field, character for character', () => {
    const { f } = render({});
    expect(layer(f).textContent).toBe(field(f).textContent);
  });

  it('lands the plates on the words, not on the paragraph breaks between them', () => {
    // Two paragraphs, the model quoting one phrase from each. A painter that counted text nodes
    // instead of logical characters would put the second plate a character short of its own words.
    const both = `first ${AI_PHRASE} end\n\nsecond ${HUMAN_PHRASE} end`;
    const { f } = render({
      scoredText: both,
      markup: `<div>first ${AI_PHRASE} end</div><div><br></div><div>second ${HUMAN_PHRASE} end</div>`,
    });
    expect(platesIn(f).map((p) => p.textContent)).toEqual([AI_PHRASE, HUMAN_PHRASE]);
  });

  it('splits a quote that straddles a bold run instead of moving it', () => {
    // "particularly vulnerable" is the quote. Five of its characters sit inside the <b> and the rest
    // sit outside it, so the honest paint is two plates that together cover exactly those
    // characters — not one plate that reaches back over the words before the quote.
    const written =
      'Those types of funds are particularly vulnerable, and the plain-speaking clause closes it.';
    const { f } = render({
      scoredText: written,
      markup: `<div>Those <b>types of funds are parti</b>cularly vulnerable, and the ${HUMAN_PHRASE} closes it.</div>`,
    });
    expect(platesIn(f).map((p) => p.textContent)).toEqual([
      'parti',
      'cularly vulnerable',
      'plain-speaking clause',
    ]);
  });

  it('gets out of the way while the reader is in the field', () => {
    const { f } = render({});
    expect(platesIn(f)).toHaveLength(2);
    field(f).dispatchEvent(new Event('focus'));
    f.detectChanges();
    expect(platesIn(f)).toHaveLength(0);
  });

  it('comes back when the field is left again', async () => {
    const { f } = render({});
    field(f).dispatchEvent(new Event('focus'));
    f.detectChanges();
    await leave(f);
    expect(platesIn(f)).toHaveLength(2);
  });

  it('stops painting the moment the field is no longer what was scored', () => {
    const { f, state } = render({});
    state.text.set(`${SCORED} — and a sentence I just wrote.`);
    f.detectChanges();
    expect(platesIn(f)).toHaveLength(0);
  });

  it('paints nothing for a run that quoted nothing', () => {
    const { f } = render({ result: NO_PHRASES });
    expect(platesIn(f)).toHaveLength(0);
  });
});

describe('TextPanel text path', () => {
  it('reads the field, with the paragraph break the markup stands for', () => {
    const { f, state } = render({ result: null, markup: '<div>one</div><div>two</div>' });
    expect(state.text()).toBe('one\ntwo');
    expect(root(f).querySelector('.counter')!.textContent).toContain('7 chars');
  });

  it('keeps a forced line break as a break, and a bold tag as nothing', () => {
    // The second paragraph starts a line box of its own even though a <br> already broke the first,
    // which is what the browser paints. A <b> is a style on words, not a line, and leaves no mark.
    const { f, state } = render({
      result: null,
      markup: '<div>a<br>b</div><div><b>cd</b></div>',
    });
    expect(state.text()).toBe('a\nb\ncd');
  });

  it('turns the spaces the browser protects into ordinary spaces', () => {
    const { f, state } = render({ result: null, markup: '<div>\u00a0x\u00a0</div>' });
    expect(state.text()).toBe(' x ');
  });

  it('carries a value written from outside into the field', () => {
    const { f, state } = render({ result: null });
    state.text.set('loaded out of a document');
    f.detectChanges();
    expect(field(f).textContent).toBe('loaded out of a document');
  });

  it('leaves the field alone when the value it just reported comes back round', () => {
    const { f, state } = render({ result: null, markup: '<div>typed here</div>' });
    expect(field(f).innerHTML).toBe('<div>typed here</div>');
    state.text.set('typed here');
    f.detectChanges();
    expect(field(f).innerHTML).toBe('<div>typed here</div>');
  });
});

describe('TextPanel format strip', () => {
  /** jsdom has no editing commands, so the strip is proven by what it asks the browser to do. The
      formatting never reaches `state.text` — the detector is given characters, and the plates would
      have nothing to contradict — which is what the note on the strip says. */
  it('offers the marks it claims, each named after what it does', () => {
    const { f } = render({ result: null });
    const tools = Array.from(root(f).querySelectorAll<HTMLElement>('.ed-bar button'));
    expect(tools.map((t) => t.getAttribute('data-tool'))).toEqual([
      'bold',
      'italic',
      'underline',
      'strikeThrough',
      'insertUnorderedList',
      'insertOrderedList',
      'removeFormat',
    ]);
    tools.forEach((t) => expect(t.getAttribute('aria-label')).toBe(t.getAttribute('title')));
  });

  it('says on the strip that the marks are not scored', () => {
    const { f } = render({ result: null });
    expect(f.nativeElement.querySelector('.ed-note')!.textContent).toMatch(/out of the analysis/i);
  });

  it('applies a command without rewriting the field out from under the caret', () => {
    const calls: string[] = [];
    document.execCommand = ((cmd: string) => {
      calls.push(cmd);
      return true;
    }) as typeof document.execCommand;

    const { f } = render({ result: null, markup: '<div>hello there</div>' });
    f.componentRef.instance.apply('bold');
    expect(calls).toEqual(['bold']);
    expect(field(f).innerHTML).toBe('<div>hello there</div>');
  });

  it('pastes as plain text, so a document cannot bring its own fonts in', () => {
    const pasted: string[] = [];
    document.execCommand = ((cmd: string, _ui: boolean, value?: string) => {
      if (cmd === 'insertText') pasted.push(value ?? '');
      return true;
    }) as typeof document.execCommand;

    const { f } = render({ result: null });
    const ev = new Event('paste') as ClipboardEvent;
    Object.defineProperty(ev, 'clipboardData', { value: { getData: () => 'some words' } });
    field(f).dispatchEvent(ev);
    expect(pasted).toEqual(['some words']);
  });
});

describe('TextPanel field frame', () => {
  it('names the field on the control itself, with no title standing above it', () => {
    const { f } = render({ result: null });
    expect(f.nativeElement.querySelector('.panel-head')).toBeNull();
    expect(f.nativeElement.querySelector('h2')).toBeNull();
    expect(field(f).getAttribute('aria-label')).toMatch(/text/i);
    expect(field(f).getAttribute('contenteditable')).toBe('true');
  });

  it('marks the empty field with the placeholder, and says a document is a document', () => {
    const { f, state } = render({ text: '', result: null });
    expect(field(f).classList.contains('empty')).toBe(true);
    expect(field(f).getAttribute('data-placeholder')).toMatch(/paste or write/i);
    state.textFromDocument.set(true);
    f.detectChanges();
    expect(field(f).getAttribute('data-placeholder')).toMatch(/loaded document/i);
  });

  it('folds the counters away once the field holds a usable amount of text', () => {
    const { f } = render({ text: SCORED, result: null });
    expect(f.nativeElement.querySelector('.counter')).toBeNull();
  });

  it('counts both characters and words while there is nothing to read yet', () => {
    const { f } = render({ text: '', result: null });
    const line = f.nativeElement.querySelector('.counter');
    expect(line).not.toBeNull();
    expect(line.textContent).toContain('0 chars');
    expect(line.textContent).toContain('0 words');
  });

  it('keeps the counters when what is in the field cannot be scored', () => {
    const { f } = render({ text: 'hi there', result: null });
    const line = f.nativeElement.querySelector('.counter')!;
    expect(line.textContent).toContain('8 chars');
    expect(line.textContent).toContain('2 words');
    expect(line.classList.contains('bad')).toBe(true);
  });
});
