# Zenodo deposit — copy/paste fields + runbook

Publishing takes ~3 minutes once you have a Zenodo account (sign in via ORCID or Google).
Attach `report.pdf` (or `report.html`) as the file. Fields below are ready to paste.

**This is version 2.** Version 1 covered the Part I window only (2026-09-28 → 2026-10-13) and
recommended `moonshotai/kimi-k3`, which was delisted on 2026-10-15 (HTTP 410, permanent). Publish it as
a **new version of the same deposit** rather than a new upload, so the concept DOI stays stable and the
stale recommendation is superseded rather than left beside it. Use "New version" → replace the file with
this `report.pdf` → paste the fields below.

---

## Title
Free-Tier LLM Gateways as AI-Text Detectors: A Dated Benchmark of Model Accuracy, Quota Regimes, Availability Churn and Out-of-Sample Stability

## Type
Preprint

## Language
English

## Publication date
today's date in the form

## Authors
Fill the form's Author field with your name (+ affiliation if you want; "Independent Researcher" is acceptable and common).

## Abstract (plain text, matches the report)
General-purpose LLMs asked to output a structured AI-ness verdict can serve as text detectors at near-zero cost, and their availability is governed less by model quality than by the free-tier policies of the gateways that serve them. This report benchmarks 20+ models across four gateways (OpenRouter free pool, NVIDIA NIM free tier, Google AI Studio free tier, and a local multi-account router) on a 48-text in-sample corpus and a 48-text disjoint holdout drawn from HC3, all scored through the same strict-JSON prompt, a fixed decision band (AI >= 0.69, HUMAN <= 0.40) and a confidence gate (>= 0.60). Three findings stand out. First, model choice dominates tuning: identical prompts and parsers span 0.31 to 1.00 corpus accuracy. Second, each gateway enforces a materially different regime -- OpenRouter has two 429 classes that require opposite remedies, Google's per-model daily quota (nominally 20) delivers a volume that is per model and per project (5-46 requests in one window observed) and stacks linearly with extra project keys, and NVIDIA free serving windows open and close within hours -- so a free model's measured accuracy and its usability as a live lane are separate, per-gateway questions. Third, in-sample perfection does not transfer: a lane that erred zero times on the corpus its band was fitted on produced a false negative out of sample. Part II (window 2026-10-13 to 2026-10-17) adds four findings and one retraction. (1) Accuracy does not survive listing: the lane Part I ranked first was delisted by its gateway four days after its window closed (HTTP 410, permanent), and another advertised model answers 404 on every real call while still appearing in GET /v1/models -- so availability must be probed per model, per gateway, on the day of use, and a catalogue listing is not a health check. (2) Text length is a first-order variable: on 48 texts of >=3000 characters the shipped band made zero wrong calls, while on 24 texts under 300 characters the same rule made 6 wrong calls out of 24; the band refit for that regime erases them but cuts coverage from 92% to 29%. (3) Surface formatting was inflating the headline: removing scrape noise from the human side moved the two-model ensemble from 0.938 to 0.875 accuracy per corpus and produced its first AI-passes-as-human miss, while AUC barely moved. (4) Detectors can be made to show their work: asking for verbatim evidence spans in the same strict-JSON reply yields quotes on most answered texts (575 over 144), and their strengths occupy 0.30-0.95 rather than 0-1. The retraction: the two-model tie-break that Part I replayed at 0.938 decided 3 of 48 texts on the shipped primary and got all 3 wrong, so it now ships disabled -- on the evidence measured here, a single calibrated lane beats an unvalidated second opinion. The shipped configuration is reported with its operating point (one lane, band AI >= 0.80 / HUMAN <= 0.55, confidence floors 0.50; K=5 knee refit over train+val: 156 decided, 3 wrong, 0.981 accuracy per decided) and its transport ceiling. A practitioner's decision table and explicit limitations sections accompany the results.

## Keywords (one per line)
AI text detection
LLM benchmark
free tier
model quotas
model deprecation
out-of-sample validation
evidence highlighting
NLP

## License
Creative Commons Attribution 4.0 International (CC-BY-4.0)

## Related / referenced identifiers (optional but strong)
- 10.48550/arXiv.2301.07597   (HC3 corpus — referenced)
- 10.48550/arXiv.2309.02731   (semantic-invariant HC3 — referenced)

## Notes field
Preprint / technical report; not peer reviewed. Part I measurement window 2026-09-28 to 2026-10-13; Part II window 2026-10-13 to 2026-10-17. All availability, quota and latency claims are dated to their gateway and window and are expected to expire: two models recommended or analysed in Part I were unreachable (HTTP 410 and HTTP 404) within four days of the Part I window closing. Corpus rebuild scripts and per-row JSONL captures available from the author; raw third-party text not redistributed. The same report is served as a live asset by the benchmarking app at /research/report.html.

---

## After publishing
1. Zenodo gives a DOI like `10.5281/zenodo.XXXXXXX` and a badge HTML snippet — drop the badge/citation into the verifier app site footer.
2. Put the DOI URL in the app's **Settings → paper_url** (admin-only, absolute http(s) URL required).
   The Ranking page then links the archival copy instead of the in-site one; leaving it blank is not a
   broken state, because the site always serves its own `/research/report.html`. Cite the decision table
   (§6) as the source of any "best free model right now" claim shown to users, with its date caveat.
3. The Part I follow-ups named here (the Gemini 48-row captures and a live webhook check for the kimi
   lane) did not land as hoped: the Gemini captures are still unfinished, and the kimi lane returned
   HTTP 410 `model_removed` on 2026-10-15 and was retired rather than verified. Part II documents both
   outcomes, so nothing in this runbook is still owed.
