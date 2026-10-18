# Zenodo deposit — copy/paste fields + runbook

Publishing takes ~5 minutes once you have a Zenodo account (sign in via ORCID or Google). Zenodo is
run by CERN, gives every record a DOI, and does not charge. ResearchGate mints no DOI of its own, so it
is the wrong place to *register* this work and a reasonable place to *mirror* it afterwards — once the
Zenodo DOI exists, post the record there and cite the DOI.

## This is a FIRST deposit, not a new version

Earlier drafts of this file said "this is version 2 — publish it as a new version of the same deposit".
That was wrong and is corrected here: **no Zenodo record has ever been published for this work.**
Checked against the public search API on 2026-10-18:

    GET https://zenodo.org/api/records?q="Free-Tier LLM Gateways as AI-Text Detectors"   -> total 0
    GET https://zenodo.org/api/records?q="Quota Regimes, Availability Churn"             -> total 0

and no DOI is recorded in this repo (the only `zenodo.org` strings in the tree are a fabricated record
id used as a test fixture in `frontend/src/app/app.spec.ts`). So use **New upload**, never "New version".
"Version 2" is a property of the *text* — Part I alone, then Part I + II — not of the record, and it
belongs in the version field (below), not in the deposit workflow.

## Files to upload

Generated, not hand-copied: `python tools/zenodo_package.py --author "Family, Given"` rebuilds
`research-zenodo/deposit/` from `GET /ranking` (the route the app and the public page both use) and a
read-only open of `benchmark_runs` / `benchmark_results`, then re-verifies what it wrote (7 checks,
last run 2026-10-18: 7/7). Its refusal to write `metadata.json` without an author is deliberate.

| File | Size (2026-10-18) | What it is |
| --- | --- | --- |
| `research-zenodo/report.pdf` | 471,173 B | the report, printable |
| `research-zenodo/report.html` | 53,601 B | the same text, readable in a browser |
| `deposit/README.md` | 4,096 B | column dictionary for the two data files |
| `deposit/model-ranking.csv` | 7,297 B | 48 model rows, in `/ranking` order, each at its own stored band |
| `deposit/per-text-scores.csv` | 367,478 B | 3,937 captures: one row per (run, text) — no corpus text |
| `deposit/runs-manifest.csv` | 21,934 B | 144 runs with the parameters of the command that produced each |
| `deposit/metadata.json` | — | the fields below, in the deposit API's own shape |

Ship the data with the report. The report's §8 promises "append-only per-row JSONL … with a run
manifest"; attaching those files turns that from a request-to-the-author into something a reader can
actually check, and the record's description now says they are there.

## Upload by API (token) — or paste the same fields in the UI

```bash
# export ZENODO_TOKEN=<your personal access token>   # never paste it into a chat or a commit
curl -s -X POST "https://zenodo.org/api/deposit/depositions" \
  -H "Authorization: Bearer $ZENODO_TOKEN" -H "Content-Type: application/json" \
  -d '{"metadata":{}}' | python -c "import json,sys; d=json.load(sys.stdin); print(d['id'], d['links']['bucket'])"
curl -s -X PUT "https://zenodo.org/api/files/<bucket>/report.pdf" --upload-file research-zenodo/report.pdf \
  -H "Authorization: Bearer $ZENODO_TOKEN"
# …once per file, then PUT the metadata below, then POST /api/deposit/depositions/<id>/actions/publish
```

`GET /api/deposit/depositions` answers **403 without a token**, which is how the endpoint was confirmed
live on 2026-10-18 without an account. `metadata.json` holds the payload for the metadata PUT.

---

## Title
Free-Tier LLM Gateways as AI-Text Detectors: A Dated Benchmark of Model Accuracy, Quota Regimes, Availability Churn and Out-of-Sample Stability

## Item type
Publication → Preprint

## Language
English

## Publication date
today's date in the form `YYYY-MM-DD`

## Version label
`v1` — the record's first version. (The report inside it is the second version of the text.)

## Authors
**Only you can fill this field, and it is the one thing standing between this package and a DOI.**
The report itself carries no byline, so nothing in the repo is authoritative for your name. Zenodo
wants `Family, Given`; affiliation is optional and "Independent Researcher" is acceptable and common.
If you have an ORCID iD, add it — it is what makes the record follow you to ResearchGate or Google
Scholar without ambiguity.

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
Creative Commons Attribution 4.0 International (CC-BY-4.0) — Zenodo's own id is `cc-by-4.0` (read from
live records on 2026-10-18; the docs' shorthand `cc-by` is a different license).

## Related / referenced identifiers
- `10.48550/arXiv.2301.07597` — HC3 corpus, relation **references** (the measurements are taken over its documents)
- `10.48550/arXiv.2309.02731` — semantic-invariant HC3, relation **references**

## Notes field
Preprint / technical report; not peer reviewed. Part I measurement window 2026-09-28 to 2026-10-13; Part II window 2026-10-13 to 2026-10-17. All availability, quota and latency claims are dated to their gateway and window and are expected to expire: two models recommended or analysed in Part I were unreachable (HTTP 410 and HTTP 404) within four days of the Part I window closing. The CSVs in this record are the tables the report argues from; raw third-party corpus text is not redistributed (see report §8). The same report is also served as a live asset by the benchmarking application at /research/report.html.

---

## After publishing

1. Put the DOI URL in the app's **Settings → paper_url** (admin-only, absolute http(s) URL required).
   The Ranking page then links the archival copy instead of the in-site one; leaving it blank is not a
   broken state, because the site always serves its own `/research/report.html`.
2. Report §8 still says the captures are "available on request to the submitting author". Once the
   record is public, that sentence should name the DOI and the three CSVs instead — a one-line edit to
   the report generator, then re-run `python tools/zenodo_package.py` and re-deposit as a **new
   version** (from then on, "New version" *is* the right button).
3. Optional: add the Zenodo badge/citation to the site footer, and post the record to ResearchGate
   citing the DOI.
4. Two Part I follow-ups named in earlier drafts of this file did not land as hoped: the Gemini 48-row
   captures are still unfinished, and the kimi lane returned HTTP 410 `model_removed` on 2026-10-15 and
   was retired rather than verified. Part II documents both outcomes, so nothing here is still owed.
