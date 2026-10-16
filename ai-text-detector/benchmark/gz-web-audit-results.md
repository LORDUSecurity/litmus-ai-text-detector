# GPTZero free-web audit of HC3 "HUMAN" labels (2026-10-11)

Question: *how sure are we that the texts labelled HUMAN in the benchmark corpus are
really human and not AI-generated?* GPTZero was chosen as an independent, non-LLM oracle
to break the "all our detectors are LLMs" circularity.

Method: GPTZero web Basic Scan driven through the browser. Anonymous first (5 scans, then
login wall), then after the user logged in, 9 more scans including re-runs of hu-008 and
hu-012 on the stable logged-in engine (`4.1o`). Texts are the real `corpus-holdout.json`
captures (HuggingFace HC3 via datasets-server), not authored here.

## Complete scan table (all real, recorded verbatim)

| id | HC3 label | config | chars | model | AI / Mixed / Human | verdict |
|----|-----------|--------|------:|-------|--------------------|---------|
| book-000 (Gutenberg 1895 ctrl) | HUMAN ctrl | — | 1311 | 4.1o | 0 / 1 / **99** | HUMAN ✓ |
| ai-000 (AI ctrl) | AI | finance | 542 | 4.1o | 24 / 0 / **76** | HUMAN ✗ miss |
| hu-004 | HUMAN | wiki_csai | 426 | 4.1o | 0 / 0 / **100** | HUMAN ✓ |
| hu-012 (anon run) | HUMAN | open_qa | 428 | 4.1o | **100** / 0 / 0 | AI ✗ flag |
| hu-008 (anon run) | HUMAN | reddit_eli5 | 812 | 3.3b | **100** / 0 / 0 | AI ✗ flag (wrong model) |
| ai-002 | AI | open_qa | 539 | 4.1o | 4 / 0 / **96** | HUMAN ✗ miss (under-100 warning) |
| ai-006 | AI | medicine | 1290 | 4.1o | **100** / 0 / 0 | AI ✓ |
| ai-010 | AI | finance | 1280 | 4.1o | **100** / 0 / 0 | AI ✓ |
| ai-014 | AI | wiki_csai | 1223 | 4.1o | 1 / 0 / **99** | HUMAN ✗ miss |
| ai-018 | AI | reddit_eli5 | 1299 | 4.1o | 4 / 0 / **96** | HUMAN ✗ miss |
| ai-022 | AI | open_qa | 501 | 4.1o | **100** / 0 / 0 | AI ✓ (under-100 warning) |
| hu-016 | HUMAN | medicine | 358 | 4.1o | 21 / 0 / **79** | HUMAN ✓ (under-100 warning) |
| hu-000 | HUMAN | finance | 1266 | 4.1o | 0 / 0 / **100** | HUMAN ✓ |
| hu-020 | HUMAN | finance | 944 | 4.1o | 0 / 0 / **100** | HUMAN ✓ |
| **hu-012 re-run** | HUMAN | open_qa | 428 | 4.1o | 0 / 0 / **100** | HUMAN ✓ — **flipped from AI 100% on the SAME model** |
| **hu-008 re-run** | HUMAN | reddit_eli5 | 812 | 4.1o | 0 / 0 / **100** | HUMAN ✓ |

## Confusion matrix (stable logged-in 4.1o runs only)

HUMAN rows: book-000, hu-004, hu-008 re, hu-012 re, hu-016, hu-000, hu-020 (n=7).
AI rows: ai-000, ai-002, ai-006, ai-010, ai-014, ai-018, ai-022 (n=7).
Decision: GPTZero predicts AI iff AI% ≥ 50% (equivalently, the confidence line reads
"AI generated").

| | GPTZero→HUMAN | GPTZero→AI |
|---|---:|---:|
| HC3 HUMAN (7) | 7 | **0** |
| HC3 AI (7) | **4** | 3 |

Overall acc 10/14 = 0.714. Precision on an AI flag 3/3 = 1.000. **Recall on AI only
3/7 = 0.429.** Falsely flagging a HUMAN text: 0/7. Missing a real AI text: 4/7 = 0.571.

## Answer to the user's question

Two facts, both from measured output.

1. **Under a stable engine, GPTZero did NOT flag any sampled HC3 "human" text as AI (0
   of 7).** The two 100%-AI flags seen in the first anonymous pass (hu-008, hu-012) did
   not survive: hu-012 flipped to 0/0/100 on the *same* 4.1o model just by re-running,
   and hu-008's flag was on the downgraded `3.3b` fallback the anonymous tier uses. So
   the flags were artefacts of the anonymous path, not signals about the texts.

2. **But GPTZero's silence does NOT license trusting the labels — it's not detecting AI
   often enough.** It missed 4 of 7 real ChatGPT answers (ai-000, ai-002, ai-014, ai-018),
   including ai-018 which is a textbook ChatGPT numbered list of sleep effects (~1300
   chars, well above the "under 100 words" warning threshold) that read back as 96%
   human. A detector with 43% recall cannot tell you "human" means anything; its
   non-flag is not evidence.

The honest reading: the benchmark's HUMAN labels survive a GPTZero audit, but the audit
would have produced the same clean bill of health if the labels had been wrong half the
time, because GPTZero doesn't catch that style of ChatGPT prose on this corpus. HC3
label trust still rests on what it rested on before — **provenance** (scraped from real
finance/medicine/open_qa/reddit_eli5/wiki_csai Q&A sources, several pre-dating ChatGPT's
2022-11-30 GA), not on any detector agreeing. This run moves that one bit — 0 of 7
HC3-human flags is at least a mild positive — but not much, and it does NOT warrant
relabeling any of these texts as AI.

## Instability finding (separate, important)

hu-012 on model `4.1o` produced two opposite verdicts in one session:
- anonymous run: "highly confident this text was AI generated" (AI 100%)
- logged-in re-run: "highly confident this text is entirely human" (Human 100%)

Same text, same engine tag, same prompt path, opposite result. This is exactly the
"detector run-to-run jitter" pattern already recorded in the project memory at
[[ai-detector-run-to-run-jitter]] — but it lands harder here because hu-012's verdict
flipped across the extremes, not just across a threshold band. Judge single GPTZero
verdicts with care; they are not stable observations.

## Model drift within the free tier

Scans 1-4 anonymous ran on `4.1o`, scan 5 fell to `3.3b`, and after login the engine is
back on `4.1o`. The free tier does not hold the model fixed, so percentages across the
anonymous phase are not from one constant detector — I flagged hu-008 as "wrong model"
for that reason. Under a "logged-in + Basic Scan" configuration it appears stable at
`4.1o`.

## Length effects (secondary)

ai-002 (539 chars) and ai-022 (501 chars) both fired GPTZero's own "under 100 words,
result may be less accurate" warning, yet they went opposite ways — ai-002 missed, ai-022
caught. The two AI misses that had NO short-text warning are ai-014 (1223) and ai-018
(1299). So length is not the whole story on this corpus; the model is simply inconsistent
on this prose style. The "accuracy improves with longer inputs" caveat on GPTZero's own
homepage is a real effect but not a rescue.

## What would actually settle it

- **Same tool, paid API, more texts** — `benchmark/gptzero-verify.js` is written and
  mock-tested; a real `GPTZERO_API_KEY` on the same 7 texts, run three times each, would
  separate GPTZero's real recall from this session's noise. It costs money; the free web
  tier caps anonymous scans around 5 and the logged-in free tier is not fixed-model or
  fixed-threshold.
- **Provenance attestation** — every HC3 human row has a `source` field pointing at the
  scrape. Checking each row's original URL against 2022-11-30 is stronger evidence of
  "human" than any detector's silence.
- **De-tokenize before scanning** — hu-008's shape ("This has n't … toddlers ’ cheeks")
  shows the HC3 tokenizer noise alone can push a borderline detector over the line; any
  future oracle pass should feed the cleaned text.

## Reproduce

Manual browser flow (this audit): open `https://app.gptzero.me/`, Basic Scan (4.1o), fill
the editor with a `benchmark/gz-s-*.txt`, click Scan, wait ~5-10 s. Sample files:
`gz-book.txt`, `gz-ai.txt`, `gz-s-hu-*.txt`, `gz-s-ai-*.txt`; sample membership in
`gz-sample-index.json` (every-4th from `corpus-holdout.json`, 6 HUMAN + 6 AI + ai-000
control). Anonymous tier caps at ~5 scans then demands a free account; logged-in free
tier continues on 4.1o.

Programmatic (paid key): `printf 'GPTZERO_API_KEY=...\n' >> .env` (never print the value)
and `node benchmark/gptzero-verify.js --corpus corpus-holdout.json`.

## Round 10 addendum — fresh-AI control vs old ChatGPT (2026-10-11, logged-in 4.1o)

Motivation: the 43% AI recall above raised the question "are the chatgpt_answers really
AI?" — answered here with a control on the SAME question ("what does auld lang syne
mean"), driven through the browser-use instance (not the user's Chrome).

| control (file)                          | what it is                          | GPTZero verdict (AI/Mixed/Human) |
|-----------------------------------------|-------------------------------------|----------------------------------|
| gz-fresh-ai-002.txt                     | 2026 AI, NVIDIA 550B temp 0.7, markdown | AI 36 / Mixed 64 / Human 0 (flagged AI) |
| gz-fresh-ai-002-plain.txt               | same text, markdown stripped        | **AI 100 / Mixed 0 / Human 0** — "highly confident AI" |
| gz-orig-ai-002.txt                      | real HC3 ChatGPT answer, same question | AI 4 / Mixed 0 / Human 96 (called human) |

Findings:
1. GPTZero catches fresh 2026 AI even de-formatted — stripping markdown did NOT hide it
   (it got MORE confident, 36→100%). The "formatting artifact" hypothesis is refuted for
   this pair; GPTZero read the prose style itself.
2. GPTZero misses a text that is AI by provenance (HC3 chatgpt_answers field, live
   re-fetch verified). So its 43% recall on our AI class reflects calibration to
   contemporary-model style, not mislabelled data. A GPTZero "human" verdict on
   2022-era ChatGPT is a miss, not evidence of authorship.
3. Run caveat: the premium upsell modal recurs after scans and can occlude results;
   the 100/0/0 above was read only after dismissing "Pass on premium, continue free"
   and re-extracting the result panel text (engine tag 4.1o confirmed in the same
   innerText block).
