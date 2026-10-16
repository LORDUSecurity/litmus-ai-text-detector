# Zenodo deposit — copy/paste fields + runbook

Publishing takes ~3 minutes once you have a Zenodo account (sign in via ORCID or Google).
Attach `report.pdf` (or `report.html`) as the file. Fields below are ready to paste.

---

## Title
Free-Tier LLM Gateways as AI-Text Detectors: A Dated Benchmark of Model Accuracy, Quota Regimes and Out-of-Sample Stability

## Type
Preprint

## Language
English

## Publication date
today's date in the form

## Authors
Fill the form's Author field with your name (+ affiliation if you want; "Independent Researcher" is acceptable and common).

## Abstract (plain text, matches the report)
General-purpose LLMs asked to output a structured AI-ness verdict can serve as text detectors at near-zero cost, and their availability is governed less by model quality than by the free-tier policies of the gateways that serve them. This report benchmarks 20+ models across four gateways (OpenRouter free pool, NVIDIA NIM free tier, Google AI Studio free tier, and a local multi-account router) on a 48-text in-sample corpus and a 48-text disjoint holdout drawn from HC3, all scored through the same strict-JSON prompt, a fixed decision band (AI >= 0.69, HUMAN <= 0.40) and a confidence gate (>= 0.60). Three findings stand out. First, model choice dominates tuning: identical prompts and parsers span 0.31 to 1.00 corpus accuracy, and one free model (moonshotai/kimi-k3) was the only configuration measured with zero wrong decisions on both corpora, reproduced in a same-day repeat run (AUC 1.000 both captures; McNemar vs. incumbent lanes p = 1.0000). Second, each gateway enforces a materially different regime -- OpenRouter has two 429 classes that require opposite remedies, Google's per-model daily quota (nominally 20) delivers a volume that is per model and per project (5-46 requests in one window observed) and stacks linearly with extra project keys, and NVIDIA free serving windows open and close within hours -- so a free model's measured accuracy and its usability as a live lane are separate, per-gateway questions. Third, in-sample perfection does not transfer: a lane that erred zero times on the corpus its band was fitted on produced a false negative out of sample, while the one lane that reproduced zero-wrong out of sample deferred 2-3 of 48 texts per run. A practitioner's decision table and an explicit limitations section accompany the results.

## Keywords (one per line)
AI text detection
LLM benchmark
free tier
model quotas
out-of-sample validation
NLP

## License
Creative Commons Attribution 4.0 International (CC-BY-4.0)

## Related / referenced identifiers (optional but strong)
- 10.48550/arXiv.2301.07597   (HC3 corpus — referenced)
- 10.48550/arXiv.2309.02731   (semantic-invariant HC3 — referenced)

## Notes field
Preprint / technical report; not peer reviewed. Measurement window 2026-09-28 to 2026-10-13; all availability, quota and latency claims are dated to their gateway and window. Corpus rebuild scripts and per-row JSONL captures available from the author; raw third-party text not redistributed.

---

## After publishing
1. Zenodo gives a DOI like `10.5281/zenodo.XXXXXXX` and a badge HTML snippet — drop the badge/citation into the verifier app site footer.
2. Link the DOI from the site's "How we choose models" / methodology page; cite the decision table (§6) as the source of any "best free model right now" claim shown to users, with its date caveat.
3. When the Gemini 48-row captures finish and the kimi lane gets a live webhook check, use Zenodo's "New version" on the same deposit — the DOI concept stays stable, each version gets its own DOI.
