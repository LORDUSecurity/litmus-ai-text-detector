# Zenodo deposit — copy/paste fields + runbook

Publishing takes ~5 minutes once you have a Zenodo account (sign in via ORCID or Google). Zenodo is
run by CERN, gives every record a DOI, and does not charge. ResearchGate mints no DOI of its own, so it
is the wrong place to *register* this work and a reasonable place to *mirror* it afterwards — once the
Zenodo DOI exists, post the record there and cite the DOI.

## This was a FIRST deposit; it is now a New version

Earlier drafts of this file said "this is version 2 — publish it as a new version of the same deposit".
That was wrong *then* and is corrected below: on 2026-10-18 **no Zenodo record existed for this work.**
Checked against the public search API that day:

    GET https://zenodo.org/api/records?q="Free-Tier LLM Gateways as AI-Text Detectors"   -> total 0
    GET https://zenodo.org/api/records?q="Quota Regimes, Availability Churn"             -> total 0

and no DOI was recorded in this repo (the only `zenodo.org` strings in the tree were a fabricated record
id used as a test fixture in `frontend/src/app/app.spec.ts`). So **New upload** was the right button on
2026-10-18, and "Version 2" was a property of the *text* — Part I alone, then Part I + II — not of the
record.

That premise expired when record 23460013 was published: DOI `10.5281/zenodo.23460013` now exists, so
from here on **New version** is the right button and a New upload would be a duplicate record. Anyone
following this file should read "New upload" as dated history, never as the current instruction.

## Deposit status — draft 23460013, saved 2026-10-18

The form has been filled and saved in the account owner's browser: **https://zenodo.org/uploads/23460013**
(draft id 23460013, version parent 23460012, still a draft). Read back from
`GET /api/records/23460013/draft`, not from the DOM, so this is what the server holds:

    title, publication_date 2026-10-18, version v1, language eng, license cc-by-4.0,
    resource_type publication/preprint, access_right open, keywords (the 8, no others),
    description 3160 chars, notes 628 chars (additional_descriptions type "Notes" is stored as
    metadata.notes), related_identifiers x2 (references / doi / publication-preprint),
    files 6 — each size and md5 identical to the local bytes.
    errors: {}

**Authors/Creators is now filled and saved** (revision 11, "Record successfully saved." on screen):

    "creators": [{"name": "Chafik, Mohamed Amine",
                  "affiliation": "ESI Rabat",
                  "orcid": "0009-0004-0398-7282"}]

The name came from the depositor's own ORCID registration form, not from a guess: the Zenodo profile
only ever stored a 6-character display name. Two things learned while filling that one field, both
worth knowing before touching it again:

- The dialog's *Identifiers* and *Affiliations* controls are Semantic UI multi-selects that **drop
  each other's chips**. Committing the affiliation wiped the ORCID chip and truncated "Mohamed Amine"
  to "Mohamed" in the given-name input; the first save therefore stored a half-populated author.
  Re-read all four values (family, given, identifier chips, affiliation chips) after every commit and
  only then click the modal's Save.
- Zenodo's affiliation registry has no entry for ESI Rabat — searching it returns
  *École de Gouvernance et d'Économie de Rabat (EGE Rabat)*, a different school — so the affiliation
  here is a free-text value ("Add ESI Rabat"), with no ROR behind it.

**Not the depositor's iD — Zenodo pre-filled a stranger's.** The *Identifiers* field arrived with the
chip `0009-0000-5116-6471` before anyone typed, and the *Affiliations* field arrived with two RORs
(`00qhdy563`, `036kgyt43`). That is Zenodo's author registry matching on the **name**, not the account:
the modal seeds identifier *and* affiliations from whichever stored person has that name, so the chips
on arrival are somebody else's until they are read back. Two independent sources say whose they are:

- ORCID `…/v3.0/0009-0000-5116-6471/{employments,works}`: *PhD Student* at Chouaib Doukkali University
  (El Jadida, MA) from 2022-09 and at Université Paul-Valéry Montpellier (FR) from 2023-09, self-sourced,
  plus two portfolio-optimisation journal articles (DOIs `10.23882/emss.24216`, `10.36024/1248-043-999-053`)
  added 2025-03-27.
- Zenodo's own public search `q=creators.name:"Chafik, Mohamed Amine"`: record **15208074**,
  "NSGA II MSI20.py" (2025-04-14), creator `CHAFIK, Mohamed Amine`, affiliation
  **Chouaib Doukkali University**, orcid `0009-0000-5116-6471`, owned by Zenodo user **1300829** —
  a different account from this deposit's owner, 1949713.

So there was never a duplicate to merge: `0009-0004-0398-7282` (registered 2026-10-18, ESI Rabat
employment + engineering-degree education) is the depositor's only iD. Three ORCID records answer to
that name (`0000-0001-8648-9473`, `0009-0000-5116-6471`, `0009-0004-0398-7282`), which is exactly why a
name-only prefill must never be trusted. The published record was re-read cookieless on 2026-10-19 and
contains **zero** occurrences of that iD, either ROR, or either institution; `metadata.creators` is the
single entry above. Had it really been a second record of the same person, ORCID's procedure is: sign
into the record to *keep*, Account Settings → Account actions → Remove a duplicate record, then enter the
duplicate's email **and password** — only e-mail addresses transfer, everything else on the duplicate is
deleted, and the deprecated iD 301-redirects to the keeper in both the UI and the API (proved against
`0000-0001-6151-2200`), so a DOI that already cites one iD keeps resolving either way.

Two traps worth recording, both caught by reading the field back instead of trusting the click:

- The DOI question is a radio pair whose values are `unmanaged` = "Yes, I already have one" and
  `managed` = "No, I need one". Picking *unmanaged* leaves `metadata.doi` empty and the form errors
  "Missing DOI for required field"; a first deposit wants **managed**, which is what the draft now
  holds (the `doi` key disappears from the saved metadata, and the error goes with it).
- The keywords typeahead is fuzzy: entering `NLP` and pressing Enter also accepted
  `(MeSH) Endopeptidase Clp`. The chip was deleted and the saved record now lists exactly the eight
  keywords above.

## Files to upload

Generated, not hand-copied: `python tools/zenodo_package.py --author "Family, Given"` rebuilds
`research-zenodo/deposit/` from `GET /ranking` (the route the app and the public page both use) and a
read-only open of `benchmark_runs` / `benchmark_results`, then re-verifies what it wrote (13 checks,
re-run 2026-10-20: 13/13, including "re-scoring per-text-scores.csv at each model's own exported band
reproduces the correct/wrong/decided/accuracy columns of 48/48 ranking rows" and "every capture_file
resolves on disk with its stated row count (144/144)"). Its
refusal to write `metadata.json` without an author is deliberate.

| File | Size (2026-10-20) | What it is |
| --- | --- | --- |
| `research-zenodo/report.pdf` | 476,570 B | the report, printable — 18 pages, reprinted after the §8 / header correction |
| `research-zenodo/report.html` | 54,625 B | the same text, readable in a browser |
| `deposit/README.md` | 4,724 B | column dictionary for the two data files |
| `deposit/model-ranking.csv` | 7,292 B | 48 model rows, in `/ranking` order, each at its own stored band |
| `deposit/per-text-scores.csv` | 367,478 B | 3,937 captures: one row per (run, text) — no corpus text |
| `deposit/runs-manifest.csv` | 19,042 B | 144 runs with the parameters of the command that produced each |
| `deposit/metadata.json` | 5,747 B | the fields below, in the deposit API's own shape |

**Two of these bytes changed against v1 for reasons that are not the §8 correction**, and the New
version should say so rather than let them pass as noise. `model-ranking.csv` row 3 (`z-ai/glm-5.3-flash`
on NVIDIA) moved in the one column that is *defined* to move — `health_at_export` went `unknown` → `ok`
because a live probe ran since the 2026-10-18 export; rank order and every score column are unchanged.
`runs-manifest.csv` **lost its `captured_at` column**, which was a false field: it was fed from
`benchmark_runs.imported_at`, a column the loader rewrites to `datetime('now')` for every row on each
re-import, so all 144 runs printed one shared second — `2026-10-16 23:12:48` in the published v1 and
`2026-10-19 23:42:48` in the next export of data that had not been re-captured. None of the 144 capture
files named in the manifest carries a per-request timestamp (the only `results-*.jsonl` with a `ts` key
is `results-appbench.jsonl`, the app-path latency bench, which is not a corpus run), so the honest fix
was to drop the column and say why in the README, not to rename a moving number. Two guards now fail the
build if this comes back: no manifest column may be timestamp-shaped and identical across all runs, and
every `capture_file` must exist with the row count the manifest states.

**The published v1 file carries the two superseded sizes** (471,173 B / 53,601 B, verified cookieless:
the record's `report.html` is byte-identical to the pre-correction local copy). It states "version 2"
where DataCite says `v1`, and §8 said the reproduction checklist is "available on request" in a record
that already ships the three CSVs. Both are corrected on disk and reach the DOI only through the New
version below — a published record is never edited in place.

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

1. **DONE 2026-10-20.** The DOI URL is in the app's **Settings → paper_url** on both deployments:
   `https://doi.org/10.5281/zenodo.23460013`. Written with `POST /settings` — no login needed on the
   packaged local app (single-user mode answers as its owner), and as the seeded admin through nginx
   on the container — then read back from `GET /settings` *and* `GET /config`, the handler the Ranking
   page actually renders from: both answer the DOI, and the cookieless `GET :8080/api/config` is still
   401. It survives a restart and a rebuild: after the new binary booted against the existing
   `%LOCALAPPDATA%\AITextDetector` database, `/api/config` still returned the DOI. That is what this
   setting is for — it is runtime-only, because `backend/seed.py` deliberately leaves it blank (a baked
   placeholder DOI would be a dead link in every fresh install) and `tools/verify_exe_bundle.py`
   asserts the virgin row is empty. Leaving it blank is not a broken state either: the site always
   serves its own `/research/report.html`.
2. Report §8 said the captures are "available on request to the submitting author" and the header said
   "version 2". **Both are fixed in the source** (2026-10-20): §8 now names the DOI and the three CSVs
   with their row counts, and the header states the two-part edition plus both DOIs instead of a version
   number. The corrected bytes now ship everywhere the *app* decides: `report.html` 54,625 B
   (`4fea2fd30a28f2ef`) and `report.pdf` 476,570 B (`258b47655cfcc7d8`), served byte-identical by the
   rebuilt exe (41/41), the recreated container (20/20) and the republished public page (16/16 on
   2026-10-20, release `01a15915-5417-7d11-b3a8-82873c4c8f52` — the previous release predated this build
   and its page still carried the superseded `471,173 B` figure), and
   `python tools/zenodo_package.py --check` still reproduces 48/48 ranking rows from 3,937 captures
   over 144 runs (7/7).
   **Still owed: the New version.** The published v1 keeps the old §8 wording, so it has to be superseded
   by a **New version** of record 23460013. The local side is finished: `python tools/zenodo_package.py
   --check` answers 13/13 on the current bytes (re-run 2026-10-20), and six files — not two — must
   replace their same-named predecessors, because the manifest fix touched more than the report
   (sha256 first 16, all re-hashed from disk the same day):
   `report.pdf` 476,570 B `258b47655cfcc7d8` and `report.html` 54,625 B `4fea2fd30a28f2ef` (both from
   `research-zenodo/`), `model-ranking.csv` 7,292 B `f179ab35e05d28e8` (row 3 `health_at_export` now
   `ok`), `per-text-scores.csv` 367,478 B `3cd237efeb00ec20` (unchanged from v1),
   `runs-manifest.csv` 19,042 B `4fb063d0873e91bd` (`captured_at` column dropped) and `README.md`
   4,724 B `3bd457220e8c79e4` (documents why there is no date column) — the last four from
   `research-zenodo/deposit/`.
   `deposit/metadata.json` `06668fc748b20714` is byte-identical to v1's, so the form fields need no edit.
   What blocks the upload is access, not content. The anonymous record page offers only a `/login/` link,
   no `ZENODO_TOKEN` exists in `.env`, the agent browser's Zenodo tab answers `/login/?next=/records/23460013`
   (so it holds no session), and the signed-in Chrome cannot be driven: `list_pages` works but every
   `evaluate_script`/`take_snapshot` call returns "MCP tool invocation did not complete" and `/json/*`
   404s while the browser WebSocket accepts the connection and sends zero bytes — the state where the
   user must re-approve debugging in Chrome. Once a session is reachable, the draft comes from the record
   page's New version control, the six files replace their same-named predecessors, and **Publish stays
   the author's click**.
3. Optional: add the Zenodo badge/citation to the site footer, and post the record to ResearchGate
   citing the DOI.
4. Two Part I follow-ups named in earlier drafts of this file did not land as hoped: the Gemini 48-row
   captures are still unfinished, and the kimi lane returned HTTP 410 `model_removed` on 2026-10-15 and
   was retired rather than verified. Part II documents both outcomes, so nothing here is still owed.
