import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';

import { HistoryView } from './history-view';
import { AuthService } from '../../core/auth.service';
import { BackendService } from '../../core/backend.service';
import type { DetectionRow } from '../../core/backend.types';

/**
 * Cover for how a saved run renders — including a run that never got a score.
 *
 * On 2026-10-16 a live detect timed out through the app and the History row for it showed
 * "0% AI prob." next to a ring, "?" chars and "120 ms". Each of those was wrong in a way a
 * reader would act on: 0% is a confident HUMAN verdict, not a missing one; "?" told the user
 * nothing about a 42-character text; and 120 ms was really 120 s (the backend stored seconds on
 * the failure path). These specs pin the render for both shapes, and the reopen guard, because
 * clicking a row whose payload is an error body used to hand that body to the Detect tab.
 */
const SCORED: DetectionRow = {
  id: 1,
  created_at: '2026-10-16T10:00:00',
  document_id: 'doc-1',
  text_length: 42,
  classification: 'AI_GENERATED',
  ai_probability: 0.85,
  confidence: 0.85,
  method: 'single_lane',
  review_required: 0,
  deciding_lane: 'primary',
  primary_model: 'kiro/claude-sonnet-4.5',
  secondary_model: null,
  latency_ms: 4500,
  http_status: 200,
  error_code: null,
};

const TIMED_OUT: DetectionRow = {
  ...SCORED,
  id: 125,
  text_length: null,
  classification: 'DETECTION_UNAVAILABLE',
  ai_probability: null,
  confidence: null,
  method: null,
  deciding_lane: null,
  latency_ms: 120023,
  http_status: 502,
  error_code: 'timeout',
};

/** The writing that run scored, and the verdict the workflow answered with. Since schema v7 the
    backend keeps the text next to the scores, so a saved run can be re-read with its evidence
    painted back onto the sentences it quotes — which is the whole point of the sheet. */
const ANALYZED =
  'Those types of funds are particularly vulnerable, and the plain-speaking clause closes that gap.';

const skipped = { checked: false, reason: 'not applicable' };

const VERDICT = {
  classification: 'AI_GENERATED',
  ai_probability: 0.85,
  confidence: 0.82,
  method: 'TIE_BREAK_AI',
  review_required: false,
  component_scores: { deciding_lane: 'primary' },
  evidence: {
    provenance: skipped,
    watermark: skipped,
    statistics: skipped,
    models: [
      {
        lane: 'primary',
        model: 'z-ai/glm-5.3-flash',
        ok: true,
        skipped: false,
        error_code: null,
        error_detail: null,
        salvaged: false,
        truncated: false,
        result: {
          ai_probability: 0.85,
          confidence: 0.82,
          signals: [],
          phrases: [
            { quote: 'those types of funds are', direction: 'AI', strength: 0.9 },
            { quote: 'plain-speaking clause', direction: 'HUMAN', strength: 0.6 },
          ],
        },
      },
    ],
  },
};

describe('HistoryView rows', () => {
  let fixture: ComponentFixture<HistoryView>;
  let rows: DetectionRow[];
  let storedPayload: unknown;
  let storedText: string | null;

  const rowEls = () =>
    [...fixture.nativeElement.querySelectorAll('.history-row')] as HTMLElement[];
  const whenText = (i = 0) =>
    (rowEls()[i].querySelector('.h-when') as HTMLElement).textContent?.replace(/\s+/g, ' ').trim();
  const sheetEl = () => fixture.nativeElement.querySelector('.sheet') as HTMLElement | null;
  const openSheet = (rowIndex = 0) => {
    rowEls()[rowIndex].click();
    fixture.detectChanges();
    return sheetEl();
  };

  beforeEach(async () => {
    rows = [SCORED, TIMED_OUT];
    storedPayload = null;
    storedText = null;
    const backend = {
      detections: () => of(rows),
      detection: (id: number) =>
        of({ ...rows.find((r) => r.id === id), payload: storedPayload, text: storedText }),
    };

    await TestBed.configureTestingModule({
      imports: [HistoryView],
      providers: [
        { provide: BackendService, useValue: backend },
        { provide: AuthService, useValue: { theme: () => 'dark' as const } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(HistoryView);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  it('draws the ring for a scored run and reads its duration in seconds', () => {
    rows = [SCORED];
    storedPayload = null;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    expect(rowEls().length).toBe(1);
    expect(rowEls()[0].querySelector('app-circular-metric')).toBeTruthy();
    expect(whenText()).toContain('4.5 s');
    expect(whenText()).toContain('42 chars');
    // decided by the primary: the ring number is on the primary's scale, so no extra label
    expect(whenText()).not.toContain('tie-break');
  });

  it('labels a row decided on the tie-break scale, whose percentage means something else', () => {
    rows = [{ ...SCORED, id: 7, deciding_lane: 'secondary',
              method: 'SINGLE_MODEL_SECONDARY_HUMAN', classification: 'HUMAN_LIKELY' }];
    fixture.componentInstance.reload();
    fixture.detectChanges();

    expect(whenText()).toContain('tie-break scale');
    // the ring itself is unchanged: this is about naming the scale, not hiding the score
    expect(rowEls()[0].querySelector('app-circular-metric')).toBeTruthy();
  });

  it('shows "not scored" instead of a 0% ring when the run never reached a model', () => {
    rows = [TIMED_OUT];
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const row = rowEls()[0];
    expect(row.querySelector('app-circular-metric')).toBe(null);
    expect(row.querySelector('.no-score')).toBeTruthy();
    expect(row.querySelector('.ns-text')?.textContent?.trim()).toBe('not scored');
    expect(row.textContent).not.toContain('0%');
  });

  it('says the length was not recorded and names the HTTP status on an un-scored run', () => {
    rows = [TIMED_OUT];
    fixture.componentInstance.reload();
    fixture.detectChanges();

    expect(whenText()).toContain('length not recorded');
    expect(whenText()).toContain('HTTP 502');
    expect(whenText()).not.toContain('?');
  });

  it('reads a two-minute timeout as minutes, not as milliseconds', () => {
    expect(fixture.componentInstance.dur(812)).toBe('812 ms');
    expect(fixture.componentInstance.dur(4500)).toBe('4.5 s');
    expect(fixture.componentInstance.dur(120023)).toBe('2 min 0 s');
  });

  it('opens a sheet over the list instead of moving the run onto the Detect tab', () => {
    rows = [SCORED];
    storedPayload = VERDICT;
    storedText = ANALYZED;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const emitted: unknown[] = [];
    fixture.componentInstance.reopen.subscribe((p) => emitted.push(p));

    const sheet = openSheet();
    expect(sheet, 'a row click must read the stored run back in place').toBeTruthy();
    expect(sheet!.getAttribute('role')).toBe('dialog');
    expect(emitted.length).toBe(0);
  });

  /* A row announces itself as a button, and a button answers to the space bar as well as to Enter.
     The page is keyboard-operable or it is not: a ring that arrives on a row that then ignores the
     key the ring promised is worse than no ring, because it teaches the reader that focus means
     nothing here. */
  it('opens a row from the keyboard on space, not only on enter', () => {
    rows = [SCORED];
    storedPayload = VERDICT;
    storedText = ANALYZED;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    expect(rowEls()[0].getAttribute('tabindex'), 'reachable by Tab at all').toBe('0');
    rowEls()[0].dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));
    fixture.detectChanges();
    expect(sheetEl(), 'space must open the same sheet a click opens').toBeTruthy();
  });

  it('shows the rings, the models that answered and the painted text in the sheet', () => {
    rows = [SCORED];
    storedPayload = VERDICT;
    storedText = ANALYZED;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const sheet = openSheet()!;
    // The number the row was saved with, on the dial — not recomputed from the payload, which can
    // disagree with the row on a run stored by an older engine.
    expect(sheet.querySelector('.ring-lead .ring-num')?.textContent).toBe('85.0');
    // Confidence is the other magnitude the run reported, and the reader of a stored row needs it
    // beside the probability the same way the live rail shows it.
    const figures = [...sheet.querySelectorAll('.ring-num')].map((e) => e.textContent?.trim());
    expect(figures).toEqual(['85.0', '82']);
    expect(sheet.querySelector('.ring-lead .ring')?.getAttribute('data-tone')).toBe('red');
    // Which model made this verdict is part of reading it a week later, not an internal detail.
    const lanes = [...sheet.querySelectorAll('.readout-models li')].map((e) =>
      e.textContent?.replace(/\s+/g, ' ').trim(),
    );
    expect(lanes.length).toBe(1);
    expect(lanes[0]).toContain('z-ai/glm-5.3-flash');
    expect(lanes[0]).toContain('decided');
    const marks = [...sheet.querySelectorAll('mark.ev-hit')] as HTMLElement[];
    expect(marks.length).toBe(2);
    expect(marks[0].textContent).toContain('Those types of funds are');
    expect(marks[1].textContent).toContain('plain-speaking clause');
    // The sheet is the short read: the sampling parameters, the endpoints and the raw payload are
    // not part of an answer, and no fold is offered for them.
    expect(sheet.querySelector('details')).toBe(null);
    expect(sheet.querySelector('.raw')).toBe(null);
    expect(sheet.textContent).not.toContain('temperature');
  });

  it('names the reason a stored run got no answer, in the sheet', () => {
    rows = [TIMED_OUT];
    storedPayload = {
      ...VERDICT,
      classification: 'DETECTION_UNAVAILABLE',
      ai_probability: null,
      method: 'ALL_MODELS_FAILED',
      error: 'All configured models failed to return a usable verdict.',
      evidence: {
        ...VERDICT.evidence,
        models: [
          {
            lane: 'primary',
            model: 'nvidia/nemotron-3-ultra-550b-a55b',
            ok: false,
            skipped: false,
            error_code: '401',
            error_detail: 'Invalid API key provided',
            salvaged: false,
            truncated: false,
            result: null,
          },
        ],
      },
    };
    storedText = null;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const sheet = openSheet()!;
    // A failed run's reason IS its output — the sheet would otherwise show an empty panel over a row
    // the reader clicked on for an explanation.
    expect(sheet.querySelector('.readout-failures')?.textContent).toContain('Invalid API key provided');
    expect(sheet.querySelector('.readout-failures')?.textContent).toContain('401');
    expect(sheet.querySelector('.no-score')).toBeTruthy();
  });

  it('says a run predates text storage rather than opening an empty sheet', () => {
    rows = [SCORED];
    storedPayload = VERDICT;
    storedText = null;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const sheet = openSheet()!;
    expect(sheet.querySelector('app-circular-metric')).toBeTruthy();
    expect(sheet.querySelectorAll('mark.ev-hit').length).toBe(0);
    expect(sheet.textContent).toContain('before the analyzed text was stored');
  });

  it('closes the sheet on the close button and on Escape, leaving the list', () => {
    rows = [SCORED];
    storedPayload = VERDICT;
    storedText = ANALYZED;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    openSheet();
    (sheetEl()!.querySelector('.sheet-close') as HTMLElement).click();
    fixture.detectChanges();
    expect(sheetEl()).toBe(null);

    openSheet();
    expect(sheetEl()).toBeTruthy();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    fixture.detectChanges();
    expect(sheetEl()).toBe(null);
  });

  it('still hands the stored verdict to the Detect tab from the sheet, not from the row', () => {
    rows = [SCORED];
    storedPayload = VERDICT;
    storedText = ANALYZED;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const emitted: unknown[] = [];
    fixture.componentInstance.reopen.subscribe((p) => emitted.push(p));
    openSheet();
    (sheetEl()!.querySelector('.sheet-reopen') as HTMLElement).click();
    fixture.detectChanges();
    expect(emitted).toEqual([{ result: VERDICT, text: ANALYZED }]);
  });

  it('offers no reopen for a run whose stored payload is an error body', () => {
    rows = [TIMED_OUT];
    storedPayload = { error: 'n8n unreachable', detail: 'timed out', config_used: {} };
    storedText = null;
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const emitted: unknown[] = [];
    fixture.componentInstance.reopen.subscribe((p) => emitted.push(p));
    const sheet = openSheet();
    expect(sheet).toBeTruthy();
    expect(sheet!.querySelector('.sheet-reopen')).toBe(null);
    expect(sheet!.querySelector('app-circular-metric')).toBe(null);
    expect(emitted.length).toBe(0);
  });
});
