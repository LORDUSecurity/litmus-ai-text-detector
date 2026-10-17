# AI Text Detection — free-tier LLM verifier

Judges whether a piece of text was written by an AI. One calibrated model lane scores
every text; an **optional** second-opinion lane resolves only the gray zone. No Python
detector service, no PostgreSQL, no Docker required: Angular → Flask + SQLite → one n8n
workflow → any OpenAI-compatible gateway (NVIDIA NIM, OpenRouter, self-hosted OmniRoute).

## Get your first verdict

```bash
# 1. one API key from any provider in the ranking below — a free NVIDIA NIM key works
cp .env.example .env          # then set PRIMARY_API_KEY (and SECONDARY_API_KEY if you want the pair)

# 2. seed providers, keys, lane models and their measured bands into SQLite
python -m backend.seed

# 3. start whatever is down (n8n, backend, ng serve) and it prints the three URLs
./quickstart.sh

# 4. open http://localhost:4200, pick "AI-flavoured paragraph", press Detect
```

**One model scores every text.** A short paragraph answers in ~7 s (the deployed lane's
measured p50); the live proof's full document took 86 s on the free NVIDIA pool, so the lane
ceiling is 240 s per attempt — wait for it before you call it dead. The second-opinion lane
ships configured but switched **off**: on the 48-text corpus, with the deployed primary on its
own measured band, it was asked about 3 texts and answered all 3 wrongly, while the same
ensemble with the lane off answers 45 of 48 at 44 correct / 1 wrong (round 25c). A gray-zone
text therefore answers `REVIEW_REQUIRED` instead of buying a tie-break call. An **admin** can
turn the lane back on for everyone with the switch on the Configuration tab (it writes the
model row's `enabled` column); a request body can only ever switch a lane off, never on. The
same default is in `.env` as `SECONDARY_ENABLED`, which a new install and a one-shot upgrade
both honour.

Instead of pasting, you can **Load document** to pull text out of a `.txt`/`.md`/
`.docx`/`.pdf` and drop it straight into the box. The file is read locally by the Flask
backend (`POST /extract`), never stored, and follows the same path as pasted text; `.docx`
is unpacked with the stdlib, `.pdf` needs `pypdf` (or PyMuPDF as fallback).

## The model ranking (all numbers measured, 48-text HC3 corpus @ 0.69/0.40 band)

Sorted by what you should actually reach for. AUC is the ranking-quality metric;
"wrong" is decisive misclassifications; "defer" is gray-zone deferrals. Full
provenance and per-text lists in [docs/free-model-research.md](docs/free-model-research.md).

| rank | model | gateway | AUC | wrong (in-sample) | wrong (holdout) | defer | latency p50 | status |
|---|---|---|---|---|---|---|---|---|
| 1 | `moonshotai/kimi-k3` | NVIDIA free | 1.000 | 0 | 0 | 3 | 13.6 s | best measured; **candidate, not promoted**; pool latency regime-swings (see caveats) |
| 2 | `z-ai/glm-5.3` | NVIDIA free | 1.000 | 0 | 1 | 3 | 17.0 s | broke OOS on `ai-012` (p=0.35); `lane = null` |
| 3 | `z-ai/glm-5.3-flash` | NVIDIA free | 1.000 | 1 | — | 2 | 7.0 s | fastest strong lane; the 1 wrong was a human text at 0.75 |
| 4 | `google/gemma-4-31b-it` | NVIDIA free | 0.945 | 7 | — | 0 | 12.1 s | never defers, confidently wrong 7× — coverage/precision trade |
| 5 | `inclusionai/ling-3.1-flash` | OpenRouter free | 0.985 | 1 | — | 9 | 4.2 s | fitted own band 0.63/0.52 raises acc to 0.870; one text flipped to paid mid-run |
| 6 | `nvidia/nemotron-3-ultra-550b-a55b` + `google/diffusiongemma-26b-a4b-it` | NVIDIA (pair) | 0.934 | 0 | — | 0 | 10.9 s + 0.7 s | pair replay 45/48 with zero deferrals — **no longer the shipped lane**: it 404s on every completion while still being listed (task #119), so the default primary is now `z-ai/glm-5.3-flash` |

Honest reading of the table:

- **kimi-k3 is the only lane with zero wrong on both the in-sample corpus and a
  disjoint HC3 holdout**, reproduced on a second holdout night. The McNemar test
  can't separate it from sonnet-4.5 or glm (one discordant pair at n=48), so the
  claim is structural, not statistical.
- **The tie-break pair's remaining value is uptime, not accuracy — and it is now off by
  default.** Several free single lanes match the pair's 45/48 with one model, and on the
  deployed lane the tie-break was measured answering 3 of 3 questions wrong (round 25c). It
  stays configured because a *drifting* primary is a different case: there the same authority
  was worth 13 correct calls out of 15. So it is a switch, priced as failure independence
  rather than accuracy, and the shipped setting is off.
- **NVIDIA's free pool is time-bound.** kimi's capture window swung from p50 13 s
  to ~7× slower on an identical re-run; a live request may sit past 200 s when the
  pool congests (the shipped per-attempt timeout plus the backend's +30 s wait
  window are what you'll feel first).
- **`top_p` is contractual, not cosmetic**: kimi-k3 hard-rejects anything but 0.95
  (`400 Validation: top_p is fixed at 0.95 for Kimi K3`). Stored model rows carry
  each model's required sampling shape precisely because of this.
- Free entitlements evaporate silently: 58 of 80 NVIDIA catalogue entries answer
  `404 Function '<uuid>' Not Found`, and that response doesn't distinguish
  "never entitled" from "withdrawn". "Unreachable" findings always name the gateway.

### Switching your first lane to one of these

The Configuration tab's lane cards list the measured ids with their scores. Pick a
model, leave host/port on its provider preset, and press Detect — the backend resolves
the provider endpoint, injects the stored key for that host, and ships that row's
stored band and sampling to the workflow. To try one without touching the defaults,
name it in the primary card only (that per-request `lane_config` override is passed
through untouched; defaults keep serving every other caller).

```bash
node tools/verify-stored-band.js --model moonshotai/kimi-k3
# 2 live requests: does this row ship — its cuts, endpoint, key, and its own lane deciding?
```

## Stack

```
Angular 22 (localhost:4200, IPv6 only)
    |  /api -> proxy
    v
Flask backend (127.0.0.1:8787)            backend/app.py
    |  SQLite: providers, endpoints, encrypted keys,
    |  models + tuned parameters, detections, benchmark runs, catalogue probes
    |  also runs the n8n execution-row purger in a background thread
    v
n8n 2.35.7 webhook (localhost:5678)       n8n/ai-only-workflow.json
    |  primary lane + optional tie-break lane
    v
OpenAI-compatible gateway (NVIDIA NIM / OpenRouter / self-hosted OmniRoute)
```

The old multi-signal pipeline (PostgreSQL, Redis, the Python `detector/` service,
Docker Compose, provenance and watermark stages) is still in the tree and documented
in [docs/legacy-multisignal-pipeline.md](docs/legacy-multisignal-pipeline.md); nothing
here needs it. The compose file in `docker/` belongs to that legacy design — the
current stack deliberately runs container-free via `quickstart.sh` (n8n on SQLite,
Flask on SQLite, ng serve).

### Deploying a changed workflow (n8n 2.x)

```bash
node tools/start-n8n.js import:workflow --input=n8n/ai-only-workflow.json
node tools/start-n8n.js publish:workflow --id=ai-detection-ai-only
node tools/start-n8n.js                    # restart: the webhook registry is built at boot
```

`update:workflow --active=true` is not enough — the registry keys off
`activeVersionId`, which only `publish:workflow` sets (and there is no
`activate:workflow` command; `publish` re-activates). Prove registration by polling the
webhook itself, not `/healthz`: an empty POST must answer the workflow's own 400.

### Keys

`backend.seed` encrypts every key it finds in `.env` into `api_keys` — the two lane
keys plus any number of `OPENROUTER_API_KEY`, `_2`, `_3`, … At request time a key
labelled `<lane> lane` serves that lane; any other key of the provider is an
interchangeable account picked stalest-use-first, so accounts take turns — OpenRouter's
`free-models-per-day` cap is account-scoped. A key never travels to another provider's
host: explicit `lane_config` requests choose the credential by the host being called.
The result panel's provenance fold says which source served each lane.

### Who changes what — and how the packaged `.exe` finds an engine

Users never see an advanced parameter. A signed-in user picks a model from the Ranking
tab (which costs one `lane_config.primary.model` and nothing else — the endpoint, band
and credential are resolved server-side from the stored row) and stores their own key.
Everything that affects *every* user lives behind `/settings`, which `backend/auth.py`
gates to the admin role on GET as well as POST, so a plain user cannot even enumerate
the writable keys: `n8n_url`, `webhook_path`, `paper_url`, `paper_title`,
`api_key_guides`, `allowed_llm_hosts` and the three decision cuts. Unknown keys are
skipped, and `put_settings()` validates every field in a first pass before writing any
of them, so a rejected value leaves the stored one intact.

That indirection is the whole answer to "how does a local app get a verdict":

1. It ships pointing at `http://localhost:5678/webhook/ai-detect-ai` — right for any
   machine running `./quickstart.sh`.
2. An admin types a reachable n8n into **Settings → Detection engine & site links** and
   it takes effect on the next request (a blank field falls back to the default above).
3. If nothing answers, `/detect` returns a *labelled* failure — 502 with
   `classification: DETECTION_UNAVAILABLE`, the URL, and a hint naming the field to
   fix. History renders it as "no verdict", never as a score. There are two labels,
   because the two causes have opposite cures: `ENGINE_UNREACHABLE` (nothing accepted
   the connection — Settings → engine URL is wrong or n8n is down) and
   `ENGINE_TIMEOUT` (the engine accepted it and its lane did not answer within
   `timeout_ms + 30 s` — the URL is fine, the lane or the pool is slow). `forward()`
   tells them apart by opening one bare TCP handshake to the same host:port after the
   failure, because "timed out" is what both a dead address and a thinking model
   produce on this platform.

`python tools/verify_admin_settings.py` proves all of it against a real
`backend.serve_local` process on a **throwaway copy** of the dev DB (16 checks: the 403
gate both ways, the 400 refusals, the guide round-trip into a plain user's `/config`,
`/n8n/status` following the stored target, the labelled 502, and one real detection
through the just-saved target).

### The technical report is an asset of the site, not a folder in the repo

Every deployment serves its own evidence: `frontend/public/research/report.html` (+ `report.pdf`) is
copied from the deposit package and lands in the built bundle, so nginx and the packaged `.exe` both
serve it at `/research/report.html` — no object storage, no second origin, and nothing for a user to
configure. The Ranking page links it unconditionally.

`paper_url` stays the *override*: set it (admin-only, and the validator requires an absolute http(s)
URL, which is why a site-relative path cannot be stored there) and the link becomes the archival copy,
e.g. a DOI. Leave it blank and the link is the report this build ships. Neither state leaves the page
without a citation — which is why the earlier "link renders only when a URL is configured" behaviour
was dropped.

Two copies means one drift risk, and all three verifiers cover it: `verify_deploy_recipe.py` fails if
`frontend/public/research/report.html` and `research-zenodo/report.html` are not byte-identical
(it prints the exact `cp` to repair), fails if either copy is untracked, and warns if
`frontend/dist` carries an older report than the source. `verify_container_deploy.py` and
`verify_exe_bundle.py` then prove it from the *served* bytes, because both servers answer an unknown
path with `index.html` and **200** — a status code can never show a missing asset here.

After editing the report, re-copy and rebuild the bundle:

```bash
cp research-zenodo/report.html research-zenodo/report.pdf frontend/public/research/
cd frontend && npx ng build && cd ..    # dist/frontend/browser/research/ must update too
```

### Tuned parameters are not decorative

Every parameter on a `models` row is applied to the detection, not just stored: the
backend looks up the row for the model actually being called and ships sampling shape,
the four decision cuts, the per-lane timeout (and the tie-break cut from the secondary's
own row) as a `tuning` block. `Normalize Input` range-checks each field and merges it
over the environment defaults; the response reports what was in force
(`config_used.thresholds`, `config_used.sampling`) and which values came from the row
(`config_used.tuning_applied`). A `NULL` `secondary_decision_threshold` is *omitted* —
the workflow's baked 0.90 governs, so an unconfigured row silently inherits a cut
fitted for a different distribution. See [docs/calibration.md](docs/calibration.md).

### Verification suite

```bash
node n8n/test-normalize.js            # 68 hostile-payload cases against the shipped Code node
node n8n/test-parse.js                # 14: truncated answers salvage a whole number, never a cut one
node n8n/test-ensemble.js             # tie-break decision table + the optional-lane contract
python -m pytest backend/tests -q     # 127: routing, encryption, purger, tuning block, auth gate, off-lane
                                      # sanitization, and slow-engine-vs-missing-engine labelling
cd frontend && npx ng test --watch=false   # 89: lane_config composition, key headers, admin card, quote painting,
                                      # lane-scale note, report-link fallback (blank paper_url -> /research/)
                                      # On a host with no commit headroom esbuild dies in the build phase
                                      # ("Fatal process out of memory: Zone"); run --include subsets —
                                      # NODE_OPTIONS cannot fix a native zone allocation.
node tools/verify-lane-retry.js       # the workflow retries a failing lane (loopback mock, no budget spent)
node tools/verify-free-lane.js        # a real text judged end-to-end through a free OpenRouter lane
node benchmark/phrase-match-rate.js   # how many of the model's own quotes the evidence panel can locate (575/575)
python tools/verify_admin_settings.py # admin surface live: 403 gate, 400 refusals, guide round-trip,
                                      # /n8n/status, labelled ENGINE_UNREACHABLE, one real detect (16 checks)
                                      # runs on a copy of the dev DB; the real one is not written
python tools/verify_tuning_live.py    # stored parameters reach the workflow (real providers)
python tools/verify_default_lane_live.py  # the promoted default lane, live: boot re-seeds a copy of the
                                      # dev DB from .env, the old primary loses its lane, one real
                                      # document is judged by the new model on its own measured cut,
                                      # and phrase evidence comes back on the deployed default
python tools/verify_deploy_recipe.py  # container preflight, no daemon needed: COPY sources vs
                                      # .dockerignore, CRLF in files a Linux container executes, the
                                      # healthcheck path is public AND routed, the timeout chain nests
                                      # (lane + 30 -> gunicorn -> nginx), DEPLOY.md quotes the same
                                      # numbers the files carry, the two shipped copies of the report
                                      # are byte-identical and tracked, and dist carries them too
                                      # (38 checks)
python tools/verify_container_deploy.py # the RUNNING stack through nginx: liveness public, gateway
                                      # status behind login, served bundle == dist, /research/report.html
                                      # byte-equal to the repo copy + the PDF served as application/pdf,
                                      # admin sign-in, seeded promoted primary, purger on n8n's own
                                      # SQLite, webhook registered, two real labelled corpus texts scored
                                      # with phrase evidence, and the two verdicts on opposite sides
                                      # (14 checks on the shipped lane; 13 with DETECT_MODEL, which
                                      # skips separation)
python tools/verify_exe_bundle.py     # the packaged .exe is proved in two boots (24 checks). Upgrade
                                      # boot: a throwaway copy of the dev DB, where the boot must switch
                                      # a previously-enabled tie-break row off exactly once, the served
                                      # bundle must hash equal to dist, /research/report.html must equal
                                      # the repo copy and the PDF must start with %PDF, and the stand-in
                                      # engine reports the body the binary sent -- one lane named,
                                      # secondary {"enabled": false}. Virgin boot: an empty database,
                                      # i.e. what the installer actually delivers, seeded with one
                                      # primary on its measured band, a 240 s ceiling, and the tie-break
                                      # row present but disabled.
node benchmark/tiebreak-authority.js  # is the tie-break lane's authority earning its keep
                                      # (runs the shipped node, judged on the band the deployed
                                      # config gives that model; refuses to score a capture whose
                                      # manifest model is not the ruler in play)
node benchmark/doc-table.js hc3-48    # regenerate the published comparison table from the DB
./quickstart.sh                       # start whatever is down; --stop takes the three ports down
```

## Documentation map

| File | What it answers |
| --- | --- |
| [research-zenodo/report.html](research-zenodo/report.html) | The citable technical report — also served by every deployment at `/research/report.html`, which is what the Ranking page links by default. |
| [docs/free-model-research.md](docs/free-model-research.md) | Every measured lane, the corpora, the McNemar tests, and why the shipped lanes are the ones they are. |
| [docs/calibration.md](docs/calibration.md) | How the decision cuts were fitted, and the replay commands that reproduce them. |
| [docs/api-keys.md](docs/api-keys.md) | Where keys live, how they are encrypted, and how to add or rotate one. |
| [docs/production-deployment.md](docs/production-deployment.md) | Running this outside a laptop. |
| [docs/legacy-multisignal-pipeline.md](docs/legacy-multisignal-pipeline.md) | The Docker/PostgreSQL/Python-detector design this app replaced. |
