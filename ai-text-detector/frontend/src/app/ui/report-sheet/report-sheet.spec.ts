import { ComponentFixture, TestBed } from '@angular/core/testing';

import { ReportSheet } from './report-sheet';

/**
 * The report read beside the ranking instead of in another tab.
 *
 * Two claims worth pinning. The framed page is the bundle's own copy at a constant same-origin path —
 * never `paper_url`, which is an outside archive and would render as a blank frame because archives
 * send X-Frame-Options. And the record line appears only when there is an outside record to name: an
 * install with no `paper_url` set must not print a DOI it does not have.
 */
async function build(inputs: { paperUrl?: string; paperTitle?: string } = {}) {
  await TestBed.configureTestingModule({ imports: [ReportSheet] }).compileComponents();
  const f = TestBed.createComponent(ReportSheet);
  if (inputs.paperUrl !== undefined) f.componentRef.setInput('paperUrl', inputs.paperUrl);
  if (inputs.paperTitle !== undefined) f.componentRef.setInput('paperTitle', inputs.paperTitle);
  f.detectChanges();
  await f.whenStable();
  return f;
}

const text = (f: ComponentFixture<ReportSheet>) => f.nativeElement.textContent ?? '';
const panel = (f: ComponentFixture<ReportSheet>) =>
  f.nativeElement.querySelector('.sheet') as HTMLElement;
const frame = (f: ComponentFixture<ReportSheet>) =>
  f.nativeElement.querySelector('.report-frame') as HTMLIFrameElement;
const record = (f: ComponentFixture<ReportSheet>) =>
  f.nativeElement.querySelector('.report-record') as HTMLAnchorElement | null;

describe('ReportSheet', () => {
  it('frames the copy that ships inside this bundle', async () => {
    const f = await build({ paperUrl: 'https://doi.org/10.5281/zenodo.23460013' });
    expect(frame(f).getAttribute('src')).toBe('/research/report.html');
    // The archive is linked, never framed — a frame on zenodo.org is an empty box.
    expect(record(f)?.getAttribute('href')).toBe('https://doi.org/10.5281/zenodo.23460013');
    expect(record(f)?.getAttribute('target')).toBe('_blank');
    expect(frame(f).getAttribute('src')).not.toContain('zenodo');
  });

  it('is a modal dialog that says what it is and how to leave it', async () => {
    const f = await build({ paperTitle: 'Read the paper' });
    expect(panel(f).getAttribute('role')).toBe('dialog');
    expect(panel(f).getAttribute('aria-modal')).toBe('true');
    expect(panel(f).getAttribute('aria-label')).toBe('Technical report');
    expect(f.nativeElement.querySelector('.sheet-head h3')?.textContent).toContain('Read the paper');
    expect(f.nativeElement.querySelector('.sheet-scrim')).toBeTruthy();
  });

  it('falls back to a plain title when the admin left one blank', async () => {
    const f = await build({ paperTitle: '   ' });
    expect(f.nativeElement.querySelector('.sheet-head h3')?.textContent).toContain('Technical report');
  });

  it('closes on the scrim, the Close button and Escape', async () => {
    const f = await build();
    const closed: unknown[] = [];
    f.componentInstance.closed.subscribe(() => closed.push(1));
    f.nativeElement.querySelector('.sheet-scrim')!.dispatchEvent(new Event('click'));
    (f.nativeElement.querySelector('.sheet-close') as HTMLButtonElement).click();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(closed.length).toBe(3);
  });

  it('does not invent a record when none is stored', async () => {
    const f = await build({ paperUrl: '' });
    expect(record(f)).toBe(null);
    expect(text(f)).toContain('/research/report.html');
    expect(text(f)).not.toContain('DOI:');
  });

  it('keeps the record out of the frame when it points back at this site', async () => {
    const f = await build({ paperUrl: '/research/report.html' });
    expect(record(f)).toBe(null);
    expect(frame(f).getAttribute('src')).toBe('/research/report.html');
  });
});
