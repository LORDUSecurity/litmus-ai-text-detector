import { Component, Input, computed } from '@angular/core';
import type {
  DetectionResult,
  LaneAttempt,
  LaneConfigUsed,
  ModelPhrase,
} from '../../core/detection.types';
import { isBenchmarkedPair } from '../../core/providers';
import { evidenceColor, STRENGTH_CEIL, STRENGTH_FLOOR } from './evidence-color';

/** The colour ramp lives in its own Angular-free module so the benchmark can measure it
    directly; it is re-exported here so the component template and the specs keep one path. */
export { evidenceColor, STRENGTH_CEIL, STRENGTH_FLOOR };

/** One run of characters in the analyzed text, either plain (direction null) or
    owned by a quoted phrase. Adjacent runs with different owners stay separate so
    the strongest evidence wins each character. */
export interface EvidenceSegment {
  text: string;
  direction: 'AI' | 'HUMAN' | null;
  strength: number;
}

const clamp01 = (n: number): number => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

/** Percent without decimals, for prose: 0.9 -> "90%". The card's `pct()` keeps the decimal
    because the measurement itself is the subject; here the cut is only context. */
const pc = (n: number): string => `${Math.round(n * 100)}%`;

/** State which cut the headline probability was judged against.
 *
    The two lanes are different models on different scales and the workflow never blends them:
    the tie-break lane puts true-human text near 0.75 and only calls AI at >= 0.90, while the
    primary decides at 0.69 / 0.40. So the same number means opposite things depending on which
    lane produced it — a live run through the packaged app showed "85.0%" sitting next to
    HUMAN_LIKELY, which reads as a broken detector when it is a correct reading of the wrong
    scale. Because the deployed primary is currently failing, that single-lane-secondary path is
    what a fresh install shows by default, so the note belongs on the headline, not in a fold.
    Returns null when no scale was reported: the baked 0.90 is a workflow default the user never
    chose, and quoting it as if the response had said so would be inventing a number. */
export function probScaleNote(result: DetectionResult): string | null {
  const lane = result.component_scores?.deciding_lane ?? null;
  if (lane === 'secondary') {
    const cut = result.component_scores?.secondary_decision_threshold;
    if (typeof cut === 'number' && Number.isFinite(cut)) {
      return `tie-break lane’s own scale — AI only at ≥ ${pc(cut)}`;
    }
    const eff = result.config_used?.thresholds?.secondary_decision;
    return typeof eff === 'number' && Number.isFinite(eff)
      ? `tie-break lane’s own scale — AI only at ≥ ${pc(eff)}`
      : null;
  }
  const t = result.config_used?.thresholds;
  if (lane === 'primary' && t && Number.isFinite(t.ai_prob) && Number.isFinite(t.human_prob)) {
    return `primary scale — AI ≥ ${pc(t.ai_prob)} · human ≤ ${pc(t.human_prob)}`;
  }
  return null;
}

/** Collapse whitespace exactly the way a quote is normalized before matching, and
    remember which original index each normalized character came from, so a match
    found in the normalized string can be mapped back onto the real text. */
function normalizeWithMap(text: string): { text: string; map: number[] } {
  let out = '';
  const map: number[] = [];
  const n = text.length;
  let i = 0;
  while (i < n && /\s/.test(text[i])) i++; // trim leading
  while (i < n) {
    if (/\s/.test(text[i])) {
      const runStart = i;
      while (i < n && /\s/.test(text[i])) i++;
      if (i < n) {
        out += ' '; // internal run -> single space
        map.push(runStart);
      }
    } else {
      out += text[i];
      map.push(i);
      i++;
    }
  }
  return { text: out, map };
}

/** Turn an index into the collapsed basis back into original-text offsets. */
function spanFrom(norm: { text: string; map: number[] }, at: number, len: number): [number, number] | null {
  const start = norm.map[at];
  const endChar = norm.map[at + len - 1];
  if (start === undefined || endChar === undefined) return null;
  return [start, endChar + 1];
}

/** Locate a quoted phrase in the text — three steps, strictest first: exact substring, then the
    whitespace-collapsed fallback, then the same collapsed basis compared case-insensitively.
    Returns [start, end) in original-text offsets, or null when the quote cannot be found at all.
    The third step exists because a model that cuts a sentence mid-flight re-cases its quote
    ("those types of funds…" for a text that reads "Those types of funds"). Measured on the 575
    real quotes in `benchmark/phrase-match-rate.js`, exact alone finds 574 and the last step
    recovers exactly one — so it is tried only after both stricter steps missed, and the painted
    span still carries the text's own capitalisation because it is sliced from the original. */
function locate(
  quote: string,
  text: string,
  norm: { text: string; map: number[] },
): [number, number] | null {
  const q = quote.trim();
  if (!q) return null;
  const exact = text.indexOf(q);
  if (exact >= 0) return [exact, exact + q.length];
  const qn = q.replace(/\s+/g, ' ');
  const at = norm.text.indexOf(qn);
  if (at >= 0) return spanFrom(norm, at, qn.length);
  const lower = norm.text.toLowerCase();
  const ql = qn.toLowerCase();
  // `toLowerCase()` is not length-preserving for every code point. If it moved the string, the
  // index map would be lying about where the match sits, so skip the step rather than guess.
  if (lower.length !== norm.text.length) return null;
  const folded = lower.indexOf(ql);
  return folded < 0 ? null : spanFrom(norm, folded, ql.length);
}

/** Paint the text: every character is owned by the strongest phrase covering it, so
    overlapping quotes never double-draw and the loudest evidence colors the overlap. */
export function buildSegments(
  text: string,
  phrases: ModelPhrase[],
): { segments: EvidenceSegment[]; unmatched: number } {
  if (!text) return { segments: [], unmatched: 0 };
  if (!phrases.length) return { segments: [{ text, direction: null, strength: 0 }], unmatched: 0 };

  const norm = normalizeWithMap(text);
  const owner = new Int32Array(text.length).fill(-1);
  const spans: { start: number; end: number; idx: number; strength: number }[] = [];
  let unmatched = 0;

  phrases.forEach((p, idx) => {
    const span = locate(p.quote, text, norm);
    if (!span) {
      unmatched++;
      return;
    }
    spans.push({ start: span[0], end: span[1], idx, strength: clamp01(p.strength) });
  });

  spans.sort((a, b) => b.strength - a.strength);
  for (const s of spans) {
    for (let i = s.start; i < s.end; i++) if (owner[i] === -1) owner[i] = s.idx;
  }

  const segments: EvidenceSegment[] = [];
  let i = 0;
  while (i < text.length) {
    const o = owner[i];
    let j = i + 1;
    while (j < text.length && owner[j] === o) j++;
    const chunk = text.slice(i, j);
    if (o === -1) {
      segments.push({ text: chunk, direction: null, strength: 0 });
    } else {
      segments.push({ text: chunk, direction: phrases[o].direction, strength: clamp01(phrases[o].strength) });
    }
    i = j;
  }
  return { segments, unmatched };
}

const REVIEW_REASONS: Record<string, string> = {
  SINGLE_MODEL_PRIMARY_UNCERTAIN:
    'The primary landed between its human and AI thresholds and the tie-break lane returned no usable verdict.',
};

@Component({
  selector: 'app-result-card',
  imports: [],
  templateUrl: './result-card.html',
})
export class ResultCard {
  @Input({ required: true }) result!: DetectionResult;
  @Input() elapsedSeconds = 0;
  /** The exact text that was scored, so quoted evidence can be painted back onto it. */
  @Input() text = '';

  /** Whose quotes are on screen: the lane that actually decided, else any usable lane
      that returned phrases. A salvaged/truncated lane returns none, so it is skipped. */
  readonly activeLane = computed<'primary' | 'secondary' | null>(() => {
    const models = this.result.evidence?.models ?? [];
    const deciding = this.result.component_scores?.deciding_lane ?? null;
    const usable = (lane: string | null) =>
      lane
        ? models.find((m) => m.lane === lane && m.ok && (m.result?.phrases?.length ?? 0) > 0)
        : undefined;
    const chosen = usable(deciding) ?? models.find((m) => m.ok && (m.result?.phrases?.length ?? 0) > 0);
    return (chosen?.lane as 'primary' | 'secondary') ?? null;
  });

  readonly activePhrases = computed<ModelPhrase[]>(() => {
    const lane = this.activeLane();
    if (!lane) return [];
    const m = (this.result.evidence?.models ?? []).find((x) => x.lane === lane && x.ok);
    return m?.result?.phrases ?? [];
  });

  private readonly painted = computed(() => buildSegments(this.text, this.activePhrases()));
  readonly segments = computed<EvidenceSegment[]>(() => this.painted().segments);
  readonly unmatchedCount = computed<number>(() => this.painted().unmatched);
  readonly highlightedCount = computed<number>(
    () => this.activePhrases().length - this.unmatchedCount(),
  );
  /** Only offer the panel when there is text to paint; the empty-quotes note lives inside. */
  readonly showEvidence = computed<boolean>(() => this.text.trim().length > 0);

  color(direction: 'AI' | 'HUMAN' | null, strength: number): string {
    return direction === null ? 'transparent' : evidenceColor(direction, strength);
  }

  readonly reviewReason = computed(() => {
    if (
      this.result.method === 'SINGLE_MODEL_PRIMARY_UNCERTAIN' &&
      this.result.config_used?.secondary?.enabled === false
    ) {
      return 'The primary landed between its human and AI cuts and the second-opinion lane is switched off, so this text is left for review instead of a tie-break.';
    }
    return REVIEW_REASONS[this.result.method] ?? null;
  });

  /** Single-lane run: the workflow echoes the off switch in config_used. */
  readonly secondaryDisabled = computed(
    () => this.result.config_used?.secondary?.enabled === false,
  );

  readonly verdictIcon = computed(() => {
    switch (this.result.classification) {
      case 'AI_GENERATED':
        return 'alert';
      case 'HUMAN_LIKELY':
        return 'check';
      case 'REVIEW_REQUIRED':
        return 'question';
      default:
        return 'plug';
    }
  });

  readonly pairIsBenchmarked = computed(() => {
    const used = this.result.config_used;
    if (!used) return true;
    return isBenchmarkedPair(used.primary.model, used.secondary.model);
  });

  readonly models = computed<LaneAttempt[]>(() => this.result.evidence?.models ?? []);

  readonly failedLanes = computed(() =>
    this.models().filter((m) => !m.ok && !m.skipped),
  );

  readonly usedLanes = computed(() => this.models().filter((m) => m.ok));

  readonly allLanes = computed(() => {
    const used = this.result.config_used;
    if (!used) return [];
    return [
      { lane: 'primary', config: used.primary },
      { lane: 'secondary', config: used.secondary },
    ] as { lane: string; config: LaneConfigUsed }[];
  });

  readonly warnings = computed(() => this.result.warnings ?? []);

  /** Which scale the headline probability was measured on, phrased for beside-it display. */
  readonly scaleNote = computed(() => probScaleNote(this.result));

  /** The decision cuts in force, phrased the way they are actually applied. */
  readonly cutSummary = computed(() => {
    const t = this.result.config_used?.thresholds;
    if (!t) return null;
    return `AI ≥ ${t.ai_prob} (conf ≥ ${t.ai_conf}) · HUMAN ≤ ${t.human_prob} (conf ≥ ${t.human_conf})`
      + ` · tie-break ≥ ${t.secondary_decision}`;
  });

  readonly samplingSummary = computed(() => {
    const s = this.result.config_used?.sampling;
    if (!s) return null;
    const cap = s.max_tokens === 'auto' ? 'max_tokens auto (no cap)' : `max_tokens ${s.max_tokens}`;
    return `temperature ${s.temperature} · top_p ${s.top_p} · ${cap}`;
  });

  /** Which effective values came from the saved model row rather than the workflow. */
  readonly appliedTuning = computed(() =>
    Object.entries(this.result.config_used?.tuning_applied ?? {})
      .map(([key, value]) => `${key}=${value}`),
  );

  pct(value: number | null | undefined): string {
    if (value === null || value === undefined) return '—';
    return (value * 100).toFixed(1) + '%';
  }

  barWidth(value: number | null | undefined): string {
    if (value === null || value === undefined) return '0%';
    return Math.max(0, Math.min(100, value * 100)).toFixed(2) + '%';
  }

  keySourceLabel(source: LaneConfigUsed['key_source']): string {
    // 'header' covers both routes here: the page's own BYOK field, and the backend
    // injecting the key it keeps encrypted in SQLite. The workflow cannot tell them
    // apart and neither can this view, so the label must not claim the browser sent it.
    return source === 'header' ? 'sent with the request' : source === 'env' ? 'from n8n env' : 'no key';
  }
}
