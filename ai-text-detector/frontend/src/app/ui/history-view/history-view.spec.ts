import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';

import { HistoryView } from './history-view';
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

describe('HistoryView rows', () => {
  let fixture: ComponentFixture<HistoryView>;
  let rows: DetectionRow[];
  let storedPayload: unknown;

  const rowEls = () =>
    [...fixture.nativeElement.querySelectorAll('.history-row')] as HTMLElement[];
  const whenText = (i = 0) =>
    (rowEls()[i].querySelector('.h-when') as HTMLElement).textContent?.replace(/\s+/g, ' ').trim();

  beforeEach(async () => {
    rows = [SCORED, TIMED_OUT];
    storedPayload = null;
    const backend = {
      detections: () => of(rows),
      detection: (id: number) =>
        of({ ...rows.find((r) => r.id === id), payload: storedPayload }),
    };

    await TestBed.configureTestingModule({
      imports: [HistoryView],
      providers: [{ provide: BackendService, useValue: backend }],
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

  it('does not reopen a run whose stored payload is an error body', () => {
    rows = [TIMED_OUT];
    storedPayload = { error: 'n8n unreachable', detail: 'timed out', config_used: {} };
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const emitted: unknown[] = [];
    fixture.componentInstance.reopen.subscribe((p) => emitted.push(p));
    rowEls()[0].click();
    fixture.detectChanges();
    expect(emitted.length).toBe(0);
  });

  it('reopens a scored run with its stored verdict', () => {
    rows = [SCORED];
    storedPayload = { ...SCORED, review_required: false };
    fixture.componentInstance.reload();
    fixture.detectChanges();

    const emitted: unknown[] = [];
    fixture.componentInstance.reopen.subscribe((p) => emitted.push(p));
    rowEls()[0].click();
    fixture.detectChanges();
    expect(emitted.length).toBe(1);
  });
});
