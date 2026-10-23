import { buildSegments, type EvidenceSegment } from './evidence-segments';
import type { ModelPhrase } from '../../core/detection.types';

/** Flatten segments back to plain text — the paint must never lose or reorder a
    single character of the original. */
const rejoin = (segs: EvidenceSegment[]): string => segs.map((s) => s.text).join('');

const p = (quote: string, direction: 'AI' | 'HUMAN', strength: number): ModelPhrase => ({
  quote,
  direction,
  strength,
});

describe('buildSegments', () => {
  it('returns nothing for empty text', () => {
    const { segments, unmatched } = buildSegments('', [p('x', 'AI', 0.9)]);
    expect(segments).toEqual([]);
    expect(unmatched).toBe(0);
  });

  it('leaves text untouched when there are no phrases', () => {
    const text = 'a plain paragraph with nothing to show';
    const { segments, unmatched } = buildSegments(text, []);
    expect(segments.length).toBe(1);
    expect(segments[0].direction).toBeNull();
    expect(rejoin(segments)).toBe(text);
    expect(unmatched).toBe(0);
  });

  it('paints an exact quote and keeps the surrounding text plain', () => {
    const text = 'The model leaned on ubiquitous leverage across the ecosystem.';
    const { segments, unmatched } = buildSegments(text, [p('ubiquitous leverage', 'AI', 0.8)]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    const hit = segments.find((s) => s.direction === 'AI');
    expect(hit?.text).toBe('ubiquitous leverage');
    expect(hit?.strength).toBe(0.8);
    // plain before, hit, plain after
    expect(segments.filter((s) => s.direction === null).length).toBe(2);
  });

  it('falls back to a whitespace-insensitive match across line breaks', () => {
    const text = 'Some prose that\nwraps   awkwardly across spaces.';
    const quote = 'that wraps awkwardly across';
    const { segments, unmatched } = buildSegments(text, [p(quote, 'HUMAN', 0.6)]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    const hit = segments.find((s) => s.direction === 'HUMAN');
    // the painted span covers the real characters, newlines and extra spaces included
    expect(hit?.text).toBe('that\nwraps   awkwardly across');
  });

  it('paints a quote the model re-cased, in the text\'s own capitalisation', () => {
    const text = 'Those types of funds are particularly risky.';
    const quote = 'those types of funds are particularly'; // mid-sentence cut, lower-cased
    const { segments, unmatched } = buildSegments(text, [p(quote, 'HUMAN', 0.55)]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    // the span is sliced from the original, so the highlight shows "Those", not the model's "those"
    expect(segments.find((s) => s.direction === 'HUMAN')?.text).toBe('Those types of funds are particularly');
  });

  it('still falls back to case-folding after a whitespace collapse', () => {
    const text = 'Those types of\nfunds   are particularly risky.';
    const { segments, unmatched } = buildSegments(text, [p('those types of funds are', 'HUMAN', 0.5)]);
    expect(unmatched).toBe(0);
    expect(segments.find((s) => s.direction === 'HUMAN')?.text).toBe('Those types of\nfunds   are');
  });

  it('does not stretch the case-folded step into a paraphrase', () => {
    // same words, different *words* — a quote the text never contained must stay unpainted
    const text = 'Those types of funds are particularly risky.';
    const { segments, unmatched } = buildSegments(text, [p('those kinds of funds are particularly', 'AI', 0.9)]);
    expect(unmatched).toBe(1);
    expect(segments.every((s) => s.direction === null)).toBe(true);
    expect(rejoin(segments)).toBe(text);
  });

  it('lets the strongest phrase own an overlap', () => {
    const text = 'aaa bbb ccc';
    // weak covers "aaa bbb", strong covers "bbb ccc"; the shared "bbb" must go strong.
    const { segments, unmatched } = buildSegments(text, [
      p('aaa bbb', 'HUMAN', 0.3),
      p('bbb ccc', 'AI', 0.9),
    ]);
    expect(unmatched).toBe(0);
    expect(rejoin(segments)).toBe(text);
    const aiSeg = segments.find((s) => s.direction === 'AI');
    const humanSeg = segments.find((s) => s.direction === 'HUMAN');
    // the overlap "bbb" is owned by the stronger AI phrase, not the weaker human one
    expect(aiSeg?.text).toContain('bbb');
    expect(aiSeg?.strength).toBe(0.9);
    expect(humanSeg?.text).not.toContain('bbb');
    expect(humanSeg?.text).toBe('aaa ');
  });

  it('counts quotes it cannot locate and still paints the rest', () => {
    const text = 'Only one of these is really present.';
    const { segments, unmatched } = buildSegments(text, [
      p('really present', 'AI', 0.7),
      p('a quote that is not in the text at all', 'AI', 0.95),
    ]);
    expect(unmatched).toBe(1);
    expect(rejoin(segments)).toBe(text);
    expect(segments.some((s) => s.direction === 'AI' && s.text === 'really present')).toBe(true);
  });

  it('clamps an out-of-range strength instead of producing a wild colour weight', () => {
    const text = 'over the top strength';
    const { segments } = buildSegments(text, [p('over the top', 'AI', 1.7)]);
    const hit = segments.find((s) => s.direction === 'AI');
    expect(hit?.strength).toBe(1);
  });
});
