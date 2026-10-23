import type { DetectionResult, ModelPhrase } from '../../core/detection.types';

/** One run of characters in the analyzed text, either plain (direction null) or
    owned by a quoted phrase. Adjacent runs with different owners stay separate so
    the strongest evidence wins each character. */
export interface EvidenceSegment {
  text: string;
  direction: 'AI' | 'HUMAN' | null;
  strength: number;
}

const clamp01 = (n: number): number => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

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

/** Whose quotes are on screen: the lane that actually decided, else any usable lane
    that returned phrases. A salvaged/truncated lane returns none, so it is skipped. */
export function pickEvidence(result: DetectionResult): {
  lane: 'primary' | 'secondary' | null;
  phrases: ModelPhrase[];
} {
  const models = result.evidence?.models ?? [];
  const deciding = result.component_scores?.deciding_lane ?? null;
  const usable = (lane: string | null) =>
    lane
      ? models.find((m) => m.lane === lane && m.ok && (m.result?.phrases?.length ?? 0) > 0)
      : undefined;
  const chosen = usable(deciding) ?? models.find((m) => m.ok && (m.result?.phrases?.length ?? 0) > 0);
  const lane = (chosen?.lane as 'primary' | 'secondary') ?? null;
  return { lane, phrases: chosen?.result?.phrases ?? [] };
}

/** The one call every surface that repaints a verdict makes: pick the deciding lane's quotes,
    locate them in this exact text, and hand back both the runs to draw and the count of quotes
    that could not be found. It lives here rather than in each component because the fold, the
    history sheet and the highlight inside the input box must agree on which phrases were painted
    and which were dropped — three call sites computing that twice would be three chances to
    disagree about what the reader is looking at. */
export function paintEvidence(result: DetectionResult, text: string): {
  lane: 'primary' | 'secondary' | null;
  phrases: ModelPhrase[];
  segments: EvidenceSegment[];
  unmatched: number;
} {
  const { lane, phrases } = pickEvidence(result);
  const painted = buildSegments(text, phrases);
  return { lane, phrases, ...painted };
}
