import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { AuthService } from '../../core/auth.service';
import { MAX_TEXT_LENGTH, MIN_TEXT_LENGTH } from '../../core/detection.service';
import type { DetectionFormState } from '../../core/form-state';
import type { DetectionResult } from '../../core/detection.types';
import { evidencePaint, type EvidenceTheme } from '../evidence/evidence-color';
import { paintEvidence, type EvidenceSegment } from '../evidence/evidence-segments';

/** The buttons on the strip. Each one is an `execCommand` name plus the glyph it draws itself —
    no icon font and no SVG per button, because bold and italic ARE their own icons and rendering
    the letter in the style it applies is both the cheapest and the clearest thing available. The
    style is keyed off `data-tool` rather than a class binding so the static `ed-tool` class and
    the per-tool style cannot replace each other. */
interface Tool {
  cmd: string;
  label: string;
  title: string;
}

const TOOLS: Tool[] = [
  { cmd: 'bold', label: 'B', title: 'Bold' },
  { cmd: 'italic', label: 'I', title: 'Italic' },
  { cmd: 'underline', label: 'U', title: 'Underline' },
  { cmd: 'strikeThrough', label: 'S', title: 'Strikethrough' },
  { cmd: 'insertUnorderedList', label: '•', title: 'Bulleted list' },
  { cmd: 'insertOrderedList', label: '1.', title: 'Numbered list' },
  { cmd: 'removeFormat', label: '⌫', title: 'Clear formatting' },
];

/** Tags that own a line of their own. `innerText` is what would normally answer "what did the
    reader write", but it is a rendering-dependent API — jsdom has none, so a spec would read
    `undefined` off it and the field would silently stop syncing its text. This walker is the same
    rule spelled out for the two layers that need to agree, and it runs identically in the browser
    and in the test environment. */
const LINE_TAGS = new Set(['div', 'p', 'li', 'ul', 'ol', 'blockquote', 'pre', 'h1', 'h2', 'h3', 'h4']);
const SKIP_TAGS = new Set(['style', 'script', 'head']);

/**
 * The writing — a document you can mark up, that also wears the verdict on its own words.
 *
 * It is a `contenteditable`, not a `<textarea>`, for two reasons that turn out to be the same
 * reason. A field cannot hold a bold word, and a field cannot hold a coloured background behind
 * one range of its own text. The surface below solves both with one mechanism: the caret lives in
 * an element whose markup the app never rewrites, and the evidence is painted by a clone of that
 * exact markup sitting underneath it, transparent letters and gold plates only.
 *
 * The clone is why the highlight is the input text rather than a picture of it. It is regenerated
 * from `surface.innerHTML` on every paint, so a bold run, a list item and a line break all occupy
 * the same pixels in both layers — the plates cannot drift, because they are drawn around the same
 * tags the editor is holding, and both layers share one scrollport so they cannot drift vertically
 * either.
 *
 * The paint is a reading aid, not an edit aid, so it lifts the moment the reader goes to work:
 * while the caret is in the surface or the format strip has been pressed, and whenever the
 * characters in the field are no longer the characters that were scored. A highlight under a caret
 * is in the way of the edit that caret is making, and a highlight that has drifted off its words
 * would be a lie about which sentences the model quoted.
 */
@Component({
  selector: 'app-text-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe],
  host: { class: 'text-panel-host' },
  template: `
    <div class="ed" [class.focused]="editing()">
      <!-- The strip belongs to the surface, so it sits inside the same frame rather than on top of
           it: a toolbar outside the box reads as a toolbar for the page. -->
      <div class="ed-bar" role="toolbar" aria-label="Text formatting">
        @for (t of tools; track t.cmd) {
          <button type="button" class="ed-tool" [attr.data-tool]="t.cmd" [title]="t.title"
                  [attr.aria-label]="t.title"
                  (pointerdown)="engaged($event)" (click)="apply(t.cmd)">{{ t.label }}</button>
        }
        <span class="ed-bar-rule" aria-hidden="true"></span>
        <!-- The formatting is not scored: the model reads the characters. Saying so on the strip
             that adds it is the one place a reader needs to know. -->
        <span class="ed-note">marks are kept out of the analysis</span>
      </div>

      <div class="ed-scroll">
        <!-- One box, two layers. The plate layer is aria-hidden because every character in it is
             already announced by the field above it, and it is never clickable. -->
        <div class="ed-plates" #plates aria-hidden="true"></div>
        <div class="ed-surface" #surface contenteditable="true" spellcheck="false"
             role="textbox" aria-multiline="true" aria-label="Text to judge"
             [attr.data-placeholder]="placeholder()" [class.empty]="empty()"
             (input)="onEdit()" (blur)="onLeave()" (focus)="onEnter()"
             (paste)="onPaste($event)"></div>
      </div>

      <!-- The counts fold up the moment the field holds something scoreable: they are there to say
           how far the writing is from being usable, and once it is usable they are a statistic about
           text the reader can already see. -->
      @if (!settled()) {
        <p class="counter" [class.bad]="tooShort() || tooLong()">
          {{ length() | number }} chars
          · {{ words() | number }} words
          @if (tooShort()) {
            · minimum {{ min }}
          }
          @if (tooLong()) {
            · over the {{ max }} limit by {{ length() - max }}
          }
        </p>
      }
    </div>
  `,
  styles: `
    /* The whole writing surface is one framed object: strip on top, field below, one edge around
       both. It is the only field on the page a person spends time inside, so it is also the only
       one drawn as a document rather than as a control. */
    .ed {
      position: relative;
      display: flex;
      flex-direction: column;
      min-height: 0;
      flex: 1;
      background-color: var(--field-bg);
      border: 1px solid var(--edge);
      transition: border-color var(--t-fast) var(--ease-out);
    }

    .ed.focused {
      border-color: var(--touch);
      outline: 2px solid var(--touch);
      outline-offset: var(--focus-offset);
    }

    .ed-bar {
      display: flex;
      align-items: center;
      gap: var(--s-1);
      flex-wrap: wrap;
      padding: var(--s-1) var(--s-2);
      border-bottom: 1px solid var(--line);
      background-color: var(--panel-2);
    }

    .ed-tool {
      width: 30px;
      height: 26px;
      display: grid;
      place-items: center;
      padding: 0;
      border: 1px solid transparent;
      background-color: transparent;
      color: var(--ink-dim);
      font-size: var(--fs-ui);
      line-height: 1;
      cursor: pointer;
      transition: color var(--t-instant) var(--ease-out),
                  border-color var(--t-instant) var(--ease-out),
                  background-color var(--t-instant) var(--ease-out);
    }

    .ed-tool:hover {
      color: var(--ink);
      border-color: var(--touch);
      background-color: var(--touch-a12);
    }

    /* Each button is drawn in the style it applies, which is why there is no icon set here. */
    .ed-tool[data-tool='bold'] { font-weight: 700; }
    .ed-tool[data-tool='italic'] { font-style: italic; }
    .ed-tool[data-tool='underline'] { text-decoration: underline; }
    .ed-tool[data-tool='strikeThrough'] { text-decoration: line-through; }
    .ed-tool[data-tool='insertUnorderedList'],
    .ed-tool[data-tool='insertOrderedList'] { font-family: var(--font-instrument); font-size: var(--fs-meta); }
    .ed-tool[data-tool='removeFormat'] { font-size: var(--fs-meta); }

    .ed-bar-rule {
      flex: 1 1 auto;
      height: 1px;
      min-width: var(--s-2);
      background-color: var(--line);
    }

    .ed-note {
      font-family: var(--font-instrument);
      font-size: var(--fs-micro);
      letter-spacing: var(--tr-label);
      color: var(--ink-hair);
    }

    /* The scroller holds both layers at their natural height, so they cannot fall out of step: a
       mirror with its own overflow needs its scrollTop written on every frame the field moves, and
       one shared scrollport does not. */
    .ed-scroll {
      position: relative;
      flex: 1;
      min-height: 0;
      overflow: auto;
    }

    .ed-plates,
    .ed-surface {
      /* Everything that decides where a glyph lands is declared ONCE for both, in one rule, so
         neither layer can be left to a browser default the other does not share. */
      font-family: var(--font-prose);
      font-size: var(--fs-body);
      line-height: var(--lh-marked);
      font-variant-numeric: normal;
      letter-spacing: normal;
      white-space: pre-wrap;
      overflow-wrap: break-word;
      word-break: normal;
      tab-size: 4;
      margin: 0;
      padding: var(--s-4) var(--s-5);
    }

    .ed-plates {
      position: absolute;
      inset: 0;
      z-index: 0;
      overflow: hidden;
      pointer-events: none;
      user-select: none;
      /* The clone's own letters are invisible: the surface above already draws them, and a second
         copy on the plate would read as blurred text. */
      color: transparent;
    }

    .ed-surface {
      position: relative;
      z-index: 1;
      min-height: 100%;
      outline: none;
      background-color: transparent;
      color: var(--ink);
      caret-color: var(--accent);
    }

    /* The placeholder is a state of the field, not a value in it: a contenteditable has no
       ::placeholder, so an emptied surface is marked from the component and styled here. */
    .ed-surface.empty::before {
      content: attr(data-placeholder);
      color: var(--ink-dim);
      pointer-events: none;
    }
  `,
})
export class TextPanel {
  readonly state = input.required<DetectionFormState>();
  /** The run whose evidence is being painted, and the exact characters it scored. Both are signal
      inputs: this component outlives every run, and a `computed` over a plain field would be
      evaluated once and cached forever, so the second verdict would repaint nothing. */
  readonly result = input<DetectionResult | null>(null);
  readonly scoredText = input('');

  readonly min = MIN_TEXT_LENGTH;
  readonly max = MAX_TEXT_LENGTH;
  readonly tools = TOOLS;

  private readonly platesEl = viewChild<ElementRef<HTMLElement>>('plates');
  private readonly surfaceEl = viewChild<ElementRef<HTMLElement>>('surface');
  private readonly auth = inject(AuthService);

  readonly editing = signal(false);

  readonly length = computed(() => this.state().text().length);
  readonly words = computed(() => (this.state().text().match(/\S+/g) ?? []).length);
  readonly empty = computed(() => this.length() === 0);
  readonly tooShort = computed(() => {
    const n = this.length();
    return n > 0 && n < this.min;
  });
  readonly tooLong = computed(() => this.length() > this.max);

  /** There is writing in the field and it is inside the limits, so the counts below it have
      finished their job: while this holds they fold up and the panel is only the input. */
  readonly settled = computed(() => this.length() > 0 && !this.tooShort() && !this.tooLong());

  readonly placeholder = computed(() =>
    this.state().textFromDocument()
      ? 'Loaded document — edit freely, the analysis reads the characters.'
      : 'Paste or write the text to judge. 10 to 50000 characters.',
  );

  /** Where the model's phrases fall in these exact characters, or null when there is nothing to
      paint: no run yet, the field no longer holding what was scored, no quote that could be
      located, or a reader who is working rather than reading. */
  private readonly painted = computed<EvidenceSegment[] | null>(() => {
    const run = this.result();
    const text = this.state().text();
    if (!run || !text || text !== this.scoredText()) return null;
    if (this.editing()) return null;
    const out = paintEvidence(run, text);
    return out.segments.some((s) => s.direction !== null) ? out.segments : null;
  });

  /** The ramp is theme-dependent — a plate with an edge on absolute black has none against
      absolute white — and the signed-in theme is the only authority for which one to draw. Read as
      a signal so a theme flip repaints the layer instead of leaving last theme's gold on it. */
  private readonly theme = computed<EvidenceTheme>(() =>
    this.auth.theme() === 'light' ? 'light' : 'dark',
  );

  constructor() {
    // The surface is the authority while someone is typing, but a sample, a loaded document or a
    // history reopen arrive as a new value on the shared signal and have to reach the field. This is
    // registered FIRST on purpose: both effects depend on the same text signal, Angular flushes an
    // effect queue in creation order, and the plate layer is built by cloning the surface — so a
    // paint that ran before this write would clone the markup as it was, not as it now is.
    effect(() => this.writeThrough(this.state().text()));
    // The plate layer is DOM the renderer never owns, so it is written imperatively whenever the
    // paint changes — including when it changes to "nothing", which has to clear the layer.
    effect(() => this.renderPlates());
  }

  /** Rebuild the plate layer from the surface's own markup. Cloning first is the whole trick: the
      clone carries the same tags in the same order, so every box the browser computed for the
      editor is computed identically for the layer underneath it. */
  private renderPlates(): void {
    const host = this.platesEl()?.nativeElement;
    const surface = this.surfaceEl()?.nativeElement;
    if (!host || !surface) return;
    const segments = this.painted();
    if (!segments) {
      host.replaceChildren();
      return;
    }
    const clone = surface.cloneNode(true) as HTMLElement;
    wrapSegments(clone, segments, this.theme());
    host.replaceChildren(...Array.from(clone.childNodes));
  }

  /** Push a value that did not come from this field into the surface. Comparing against what the
      field already holds is what makes this safe to run on every keystroke: after an edit the two
      agree, so the caret and the markup the browser just produced are left alone. */
  private writeThrough(text: string): void {
    const surface = this.surfaceEl()?.nativeElement;
    if (!surface) return;
    if (readText(surface) === text) return;
    surface.textContent = text;
  }

  /** The characters the detector is given are read here, from the field, so the shared state and
      the surface can never disagree about what was written — and so the plate layer is locating
      quotes in exactly the string that gets sent. */
  onEdit(): void {
    const surface = this.surfaceEl()?.nativeElement;
    this.state().text.set(surface ? readText(surface) : '');
  }

  /** A paste is the one insertion whose markup is not ours to keep: dropping it to plain text means
      a Word document cannot bring its own fonts, colours and sizes into a field whose type is the
      app's, and the caret still lands where the browser put it. */
  onPaste(ev: ClipboardEvent): void {
    ev.preventDefault();
    const data = ev.clipboardData?.getData('text/plain') ?? '';
    document.execCommand('insertText', false, data);
    this.onEdit();
  }

  /** Formatting is applied by the browser to the live selection, so nothing here may touch the
      surface's markup first. `focus()` before the command covers the case where the press moved
      the caret out of the field. */
  apply(cmd: string): void {
    const surface = this.surfaceEl()?.nativeElement;
    if (!surface) return;
    surface.focus({ preventScroll: true });
    document.execCommand(cmd, false);
    this.onEdit();
  }

  /** `pointerdown` is where a toolbar press has to be caught: by `click` the browser has already
      moved the caret, and stopping the default here is what lets a bold press bold the word the
      reader had highlighted rather than the word under a fresh caret. It also lifts the paint before
      the command runs, so the strip and the field are one object being worked on. */
  engaged(ev: PointerEvent): void {
    ev.preventDefault();
    this.editing.set(true);
  }

  onEnter(): void {
    this.editing.set(true);
  }

  onLeave(): void {
    // The strip and the surface are one object: leaving either is leaving the field. The check runs
    // on the next task because `blur` fires before the new focus target exists.
    setTimeout(() => {
      const active = document.activeElement;
      const inside = active instanceof Element && Boolean(active.closest('.ed'));
      if (!inside) this.editing.set(false);
    });
  }
}

/** The characters of the surface, the way the detector should see them. */
function readText(root: Node): string {
  return readChars(root).text;
}

/** One pass over the field's markup that answers both questions the two layers have to agree on:
    what the reader wrote, and where each of those characters physically lives.
    *
    The string is not `textContent`, because a block boundary is a line the reader pressed and a
    `<br>` is one too — running two paragraphs together would shift every quote offset the model
    gets back. And it is not `innerText` either: that is a rendering-dependent API, so it reads as
    `undefined` under jsdom and the field would silently stop syncing its own text, and it would
    also make the read path differ between the browser and the test.
    *
    The slot array is what makes the plate layer honest. `text[i]` is carried by `slots[i]` — a
    (text node, offset) pair, or null when the character is a line break the markup already draws
    with its own tag. A painter that works from character counts alone would drift one position per
    paragraph the moment a document was loaded, which is the failure this layer must not have. */
function readChars(root: Node): { text: string; slots: ({ node: Text; at: number } | null)[] } {
  const chars: string[] = [];
  const slots: ({ node: Text; at: number } | null)[] = [];
  let sawLine = false;
  let lineOpen = false;
  let lineHasText = false;

  /** Start a line box, if the character about to be written needs one.
      *
      `forced` is what a block element asks for: a block always opens a line of its own, so text
      already sitting on an open line still gets broken. A plain text node is not forced — two text
      nodes meeting inside one `<b>` are one line, and treating that as a break would put a newline
      between every pair of formatted runs in the document. Either way the break is only real once
      something has been written, so the first line costs nothing. */
  const openLine = (forced: boolean): void => {
    if (!lineOpen || (forced && lineHasText)) {
      if (sawLine) {
        chars.push('\n');
        slots.push(null);
      }
      lineOpen = true;
      lineHasText = false;
      sawLine = true;
    }
  };

  const pushText = (node: Text): void => {
    const data = node.data;
    if (!data.length) return;
    openLine(false);
    for (let i = 0; i < data.length; i++) {
      // A contenteditable protects a typed leading/trailing space with U+00A0. It is one character
      // either way, so the slots stay true, but the text handed to the model is the plain one.
      chars.push(data[i] === '\u00a0' ? ' ' : data[i]);
      slots.push({ node, at: i });
    }
    lineHasText = true;
  };

  const visit = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      pushText(node as Text);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as Element;
    const tag = el.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return;
    if (tag === 'br') {
      lineOpen = false;
      lineHasText = false;
      return;
    }
    if (LINE_TAGS.has(tag)) {
      openLine(true);
      for (const c of el.childNodes) visit(c);
      lineOpen = false;
      lineHasText = false;
      return;
    }
    for (const c of el.childNodes) visit(c);
  };

  for (const child of root.childNodes) visit(child);
  return { text: chars.join(''), slots };
}

/** Wrap the runs a verdict owns in plate spans. Offsets are resolved through `readChars`, so a run
    that crosses a `<b>` boundary, a list item or a paragraph break becomes several plates drawn
    around the tags the editor is already holding — which is the only way the plate can sit on the
    word the model quoted rather than the one after it. */
function wrapSegments(clone: HTMLElement, segments: EvidenceSegment[], theme: EvidenceTheme): void {
  const { slots } = readChars(clone);
  const byNode = new Map<Text, { start: number; end: number; seg: EvidenceSegment }[]>();
  const add = (node: Text, start: number, end: number, seg: EvidenceSegment): void => {
    const list = byNode.get(node);
    if (list) list.push({ start, end, seg });
    else byNode.set(node, [{ start, end, seg }]);
  };

  let at = 0;
  for (const seg of segments) {
    const end = at + seg.text.length;
    if (seg.direction) {
      let i = at;
      while (i < end) {
        const slot = slots[i];
        if (!slot) {
          i++;
          continue;
        }
        let j = i + 1;
        while (j < end) {
          const next = slots[j];
          if (next && next.node === slot.node && next.at === slot.at + (j - i)) j++;
          else break;
        }
        add(slot.node, slot.at, slot.at + (j - i), seg);
        i = j;
      }
    }
    at = end;
  }

  if (!byNode.size) {
    clone.replaceChildren();
    return;
  }

  for (const [node, list] of byNode) {
    const parent = node.parentNode;
    if (!parent) continue;
    const data = node.data;
    const parts: Node[] = [];
    let pos = 0;
    for (const r of list.sort((a, b) => a.start - b.start)) {
      if (r.start > pos) parts.push(document.createTextNode(data.slice(pos, r.start)));
      parts.push(plate(data.slice(r.start, r.end), r.seg, theme));
      pos = r.end;
    }
    if (pos < data.length) parts.push(document.createTextNode(data.slice(pos)));
    for (const p of parts) parent.insertBefore(p, node);
    parent.removeChild(node);
  }
}

/** One quoted run, painted by the same function the history sheet and the ranking use, so a phrase
    that reads gold on the verdict is gold behind the words it was taken from. The ink is set to
    transparent rather than left alone: this layer's own colour is transparent, but a rule that
    outranks it has to be answered on the node itself, because a second copy of the words showing
    through the plate is the one thing this layer must never draw. */
function plate(text: string, seg: EvidenceSegment, theme: EvidenceTheme): HTMLElement {
  const el = document.createElement('span');
  el.className = 'ev-plate';
  el.textContent = text;
  el.title = `${seg.direction === 'HUMAN' ? 'human' : 'AI'} evidence · strength ${(seg.strength * 100).toFixed(1)}%`;
  const paint = evidencePaint(seg.direction ?? 'AI', seg.strength, theme);
  el.style.backgroundColor = paint.background;
  el.style.color = 'transparent';
  return el;
}
