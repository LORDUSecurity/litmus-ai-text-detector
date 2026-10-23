import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import { DetectRail } from './detect-rail';
import { AuthService } from '../../core/auth.service';
import { BackendService } from '../../core/backend.service';
import { DetectionFormState } from '../../core/form-state';
import type { DetectionResult } from '../../core/detection.types';

/**
 * The rail beside the input: everything that starts a run, and the score it produced.
 *
 * The Detect press, the sample buttons, the document loader and the picked-model line used to sit
 * on top of the text field, and the verdict sat below the analysis, so a reader who wanted the
 * number had to scroll past the evidence to find it — and one who wanted to try a sample had to
 * hunt the header above the field. Both now live in one column, which is also where the score is
 * printed. These specs pin that the rail writes into the *same* text signal the input column shows
 * (a sample that filled a private copy would be the worst kind of dead button), that a press is
 * forwarded rather than half-handled here, and that the verdict appears exactly once.
 */
const RESULT = {
  classification: 'AI_GENERATED',
  ai_probability: 0.91,
  confidence: 0.84,
  method: 'single_lane',
  review_required: false,
} as unknown as DetectionResult;

const OK = { ok: true, text: '', chars: 0, truncated: false, kind: 'txt', filename: '' };

describe('DetectRail', () => {
  /** The backend is only reached by the document loader, so every case supplies its own answer. */
  let extract: (file: File) => any;

  function render(over: Record<string, unknown> = {}): {
    f: ComponentFixture<DetectRail>;
    state: DetectionFormState;
  } {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [DetectRail],
      providers: [
        { provide: AuthService, useValue: { theme: () => 'dark' as const } },
        { provide: BackendService, useValue: { extractFile: (file: File) => extract(file) } },
      ],
    }).compileComponents();
    const f = TestBed.createComponent(DetectRail);
    const state = new DetectionFormState();
    f.componentRef.setInput('state', state);
    for (const [k, v] of Object.entries(over)) f.componentRef.setInput(k, v);
    f.detectChanges();
    return { f, state };
  }

  /** Every case that does not test the loader still needs one answer for it to be wired at all. */
  const idle = (): void => {
    extract = () => of(OK);
  };

  const find = (f: ComponentFixture<DetectRail>, text: string): HTMLButtonElement | undefined =>
    Array.from<HTMLButtonElement>(f.nativeElement.querySelectorAll('button')).find(
      (b) => (b.textContent ?? '').trim().toLowerCase().includes(text),
    );

  const press = (f: ComponentFixture<DetectRail>, text: string): void => {
    const b = find(f, text);
    if (!b) throw new Error(`no button containing "${text}"`);
    b.click();
    f.detectChanges();
  };

  /** Choose a file for the hidden `<input type=file>` and fire the change the loader listens to. */
  async function chooseFile(f: ComponentFixture<DetectRail>, name: string): Promise<void> {
    const input = f.nativeElement.querySelector('input[type=file]') as HTMLInputElement;
    const file = new File(['the report body'], name, { type: 'text/plain' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change'));
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
  }

  it('asks for a run when Detect is pressed', () => {
    idle();
    const { f } = render();
    let runs = 0;
    f.componentInstance.run.subscribe(() => runs++);
    press(f, 'detect');
    expect(runs).toBe(1);
  });

  it('refuses a second press while a run is in flight, and says how long it has waited', () => {
    idle();
    const { f } = render({ pending: true, elapsed: 12, inflightHint: 'waiting for the lane' });
    let runs = 0;
    f.componentInstance.run.subscribe(() => runs++);
    const b = find(f, 'detect');
    expect(b!.disabled, 'a second press must not start a second run').toBe(true);
    expect(b!.textContent).toContain('12s');
    expect(f.nativeElement.textContent).toContain('waiting for the lane');
    expect(runs).toBe(0);
  });

  it('writes a sample into the text the input column is showing', () => {
    idle();
    const { f, state } = render();
    press(f, 'ai-flavoured');
    expect(state.text()).toContain('leveraging cloud-native architectures');
    press(f, 'human forum');
    expect(state.text()).toContain('impeller was stuffed with hair');
    expect(state.text(), 'the last press wins — samples do not append').not.toContain(
      'cloud-native',
    );
  });

  it('clears the shared text', () => {
    idle();
    const { f, state } = render();
    state.text.set('something already typed');
    press(f, 'clear');
    expect(state.text()).toBe('');
  });

  it('puts a readable document in the input and says which one landed', async () => {
    extract = () =>
      of({
        ok: true,
        text: 'Extracted paragraphs from the report.',
        chars: 36,
        truncated: false,
        kind: 'pdf',
        filename: 'report.pdf',
      });
    const { f, state } = render();
    await chooseFile(f, 'report.pdf');
    expect(state.text()).toBe('Extracted paragraphs from the report.');
    const status = f.nativeElement.querySelector('.doc-status');
    expect(status.textContent).toContain('report.pdf');
    expect(status.textContent).toContain('36 chars');
    expect(status.classList.contains('bad'), 'a read is not a failure').toBe(false);
  });

  it('lets the field keep its evidence paint for text that arrived as a document', async () => {
    extract = () => of({ ...OK, text: 'Extracted paragraphs from the report.', chars: 36 });
    const { f, state } = render();
    await chooseFile(f, 'report.pdf');
    expect(state.textFromDocument(), 'reading a file is not typing it').toBe(true);
  });

  it('puts the field back to being typed text on a sample or a Clear', () => {
    idle();
    const { f, state } = render();
    state.textFromDocument.set(true);
    press(f, 'ai-flavoured');
    expect(state.textFromDocument()).toBe(false);

    state.textFromDocument.set(true);
    press(f, 'clear');
    expect(state.textFromDocument(), 'an emptied field holds no document').toBe(false);
  });

  it('names the file it could not read and leaves the text alone', async () => {
    extract = () => throwError(() => ({ status: 415, error: { error: 'This file type cannot be read.' } }));
    const { f, state } = render();
    state.text.set('untouched');
    await chooseFile(f, 'drawing.dwg');
    expect(state.text()).toBe('untouched');
    const status = f.nativeElement.querySelector('.doc-status');
    expect(status.classList.contains('bad'), 'a failure is not a success with different words').toBe(
      true,
    );
    expect(status.textContent).toContain('This file type cannot be read.');
  });

  it('says which model the next run will score with, and how to stop', () => {
    idle();
    const { f } = render({ pickedModel: 'z-ai/glm-5.3-flash' });
    expect(f.nativeElement.textContent).toContain('z-ai/glm-5.3-flash');
    expect(f.nativeElement.textContent).not.toContain('second opinion is off');
    let cleared = 0;
    f.componentInstance.clearPicked.subscribe(() => cleared++);
    press(f, 'use the deployed lane');
    expect(cleared).toBe(1);
  });

  it('warns when the picked model is also the deployed tie-break', () => {
    idle();
    const { f } = render({ pickedModel: 'kiro/claude-sonnet-4.5', pickedIsTieBreak: true });
    expect(f.nativeElement.textContent).toContain('second opinion is off');
  });

  it('prints the verdict in the rail, and only once per run', () => {
    idle();
    const { f: bare } = render();
    expect(bare.nativeElement.querySelector('app-score-readout'), 'no run yet').toBe(null);

    const { f } = render({ result: RESULT, elapsed: 8 });
    const readout = f.nativeElement.querySelector('app-score-readout');
    expect(
      readout,
      'the score belongs in the rail, beside the button that produced it',
    ).toBeTruthy();
    expect(readout.querySelector('.verdict').textContent).toContain('AI_GENERATED');
    expect(f.nativeElement.querySelectorAll('app-score-readout').length).toBe(1);
  });
});
