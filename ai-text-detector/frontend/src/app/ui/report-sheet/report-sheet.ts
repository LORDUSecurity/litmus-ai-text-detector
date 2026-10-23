import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  viewChild,
} from '@angular/core';
import { DomSanitizer, type SafeResourceUrl } from '@angular/platform-browser';

/** The study behind the numbers, opened beside the page that cites it.
 *
    The report ships inside this bundle (`public/research/`), so it can be read here instead of being
    thrown into another browser tab — and the ranking stays where the reader left it, with the row
    they were comparing still on screen. What is *not* the bundle's own copy is the archival record
    (`paper_url`, a DOI): an archive refuses to render inside another site's frame, so that one is
    named and linked, never framed. Pretending otherwise would put an empty panel where the citation
    should be. */
@Component({
  selector: 'app-report-sheet',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '(document:keydown.escape)': 'closed.emit()' },
  template: `
    <div class="sheet-scrim" (click)="closed.emit()"></div>
    <section class="sheet sheet--side" role="dialog" aria-modal="true" aria-label="Technical report"
             tabindex="-1" #panel>
      <header class="sheet-head">
        <h3>{{ title() }}</h3>
        <button type="button" class="ghost sheet-close" (click)="closed.emit()">Close</button>
      </header>

      <iframe class="report-frame" [src]="frame()" title="Technical report"></iframe>

      @if (record(); as r) {
        <p class="report-cite">
          The archival copy, with its DOI:
          <a class="report-record" [href]="r" target="_blank" rel="noopener noreferrer">{{ r }}</a>
        </p>
        <p class="hint">
          A research archive will not render inside another site's frame, so the record opens in its
          own tab. The page above is the copy this deployment ships.
        </p>
      } @else {
        <p class="report-cite">
          This copy is the one the app serves, at <code>{{ reportPath }}</code>. No external record is
          set on the Admin tab, so there is nothing to cite a DOI against yet.
        </p>
      }
    </section>
  `,
})
export class ReportSheet {
  /** `paper_url` as stored: an absolute http(s) address of the archival record, or blank. */
  readonly paperUrl = input('');
  readonly paperTitle = input('Technical report');

  readonly closed = output<void>();

  private readonly sanitizer = inject(DomSanitizer);
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  /** The report's own copy, straight out of this bundle. Same-origin and a constant, so the only
      thing the resource-URL check here guards against is Angular refusing a URL it did not mint —
      an iframe `src` is a resource context, and no literal string passes it without this. */
  readonly reportPath = '/research/report.html';
  readonly frame = computed<SafeResourceUrl>(() =>
    this.sanitizer.bypassSecurityTrustResourceUrl(this.reportPath));

  readonly title = computed(() => this.paperTitle().trim() || 'Technical report');

  /** The record only when it is actually an outside address. The view's fallback is the same path
      this sheet already frames, and calling that "the archival copy with its DOI" would be a lie
      about a deposit that has not been made. */
  readonly record = computed(() => {
    const u = this.paperUrl().trim();
    return /^https?:\/\//i.test(u) && u !== this.reportPath ? u : null;
  });

  constructor() {
    // Same rule as the run sheet: focus has to land inside the dialog, or Escape closes whatever the
    // scrim is covering instead of the thing the reader is looking at.
    afterNextRender(() => this.panel().nativeElement.focus());
  }
}
