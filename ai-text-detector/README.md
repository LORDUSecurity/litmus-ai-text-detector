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

### Your using pool: the order you set, and what health does to it

A signed-in user puts the models they can pay for into an ordered **using pool** on the Ranking
tab — by drag, or by the ↑/↓ buttons, because an order only a pointer can change is not a
control a keyboard user has. `/detect` reads it as the second authority on which model runs:

    explicit `lane_config` in the request  >  the caller's pool  >  the admin's deployed default

and the answer says which one decided (`lane_source`: `request` / `user-pool` /
`deployed-default` / `failover`), because a verdict that does not name its model is exactly the
unattributable number this app has spent rounds removing. The pool is synthesised into a plain
`lane_config` and then handled by the same branch a caller-named primary takes, so the
tie-break policy, the `base_url` fill, BYOK-first key picking and the tuning resolution stay one
code path instead of growing a parallel one that can drift from it.

**Auto-switch.** When the pool's top model returns *no verdict*, the next model in the user's
own order is tried. That happens in the backend rather than in the workflow: the engine has two
lanes and no concept of a third candidate, and teaching it an N-model walk would mean
rebuilding the shipped, measured two-lane graph. Three limits, each a real cost rather than a
taste:

* only when the answer carries no verdict — a detection the tie-break lane rescued is an answer
  the user already paid for, and replacing it with *maybe* an answer costs a second full call;
* never on an engine-level or request-level failure (`NO_FAILOVER_CODES`: `ENGINE_UNREACHABLE`,
  `ENGINE_TIMEOUT`, `INPUT_REJECTED`, `LANE_CONFIG_INVALID`, `NO_PRIMARY_MODEL`,
  `PAYLOAD_TOO_LARGE`). A different model cannot fix a dead engine, and on this platform a dead
  local port *hangs* instead of refusing, so "try twice more" is measured in minutes;
* at most `MAX_FAILOVER_ATTEMPTS = 2`, and only while `FAILOVER_RESERVE_S = 20` s remain on the
  request's own budget. The chain above this is 270 s wait → 285 s gunicorn → 330 s nginx → 360 s
  in the browser (`frontend/src/app/core/http-budget.ts`), so a
  retry started with 5 s on the clock cannot finish and would turn a clean 502 into a gateway
  timeout with no verdict at all.

That budget is `REQUEST_BUDGET_S = 250` and it belongs to the **request**, not to whichever model
happens to be on top. Getting that wrong made the switch useless in the field: on 2026-10-17 a
pool of `[nemotron-3-ultra (row ceiling 90 s), z-ai/glm-5.3-flash (240 s)]` refused in 0.7 s,
failover correctly decided to switch — and handed the replacement 109 s, because the deadline had
been computed from the *first* model's ceiling. glm needed longer than that on a congested pool,
so 120 s bought two non-answers and no verdict. The first attempt still keeps its own lane
ceiling (`max()`, not `min()` — a single lane at the shipped 240 s is what the tiers above are
derived from); only the *leftover* budget for a retry comes from the request.

The outermost tier is the browser, and it is the one no server-side file carries, so it is the one
nobody re-derives when a ceiling moves. `/detect` waited 150 s ("long because the 550B lane took 80s")
and a key probe 60 s while the chain under them grew to 270 s, so the client aborted requests the
gateway was still honouring: the user read *failed* about a detection whose verdict went on to land
in History a minute later. Both now take one constant, `HTTP_PATIENCE_MS` from
`frontend/src/app/core/http-budget.ts`, and `verify_deploy_recipe.py` fails if any server-side tier
grows past it.

A switch is reported out loud — `auto_switch: {from, to, reason, attempts[]}`, a warning line,
`lane_source: "failover"` — the result card renders it as a banner above the score, and the
history row names the model that answered rather than the one the user ranked first.

**Auto-rearrange.** Every lane answer updates `model_health`, keyed `(provider, model_id)` —
per endpoint, because the same model id behind two gateways has two failure histories.
Transient markers (429, 503, quota, overloaded …) can never reach `down`
(`state_down_after = 10**9`): a rate limit is the provider's queue, not the model's death. A
dead failure (a 404 function-id, a 410 delisting, a parse failure) reaches `down` after three
consecutive ones. Only `down` moves a model, and it moves to the back of the *served* order
without anyone editing the user's ranking: `your_rank` is what the user set, `position` is what
is served, and a recovery restores the ranking with nothing to undo. `degraded` — one failure,
or a busy streak — is reported and badged but does not re-rank, because a single 504 on a
congested free pool must not change which model the next detection pays for.

**Auto-discovery.** Saving a key with a `model_id` probes it inline, and a key that just
answered is the only trustworthy moment to spend its budget finding out what else that endpoint
serves — before that the app cannot tell an unusable model from an unusable credential, and
screening on a broken key would mark good models dead. So verification queues one discovery job
(de-duplicated on `(user, provider)`, since re-checking a key three times must not queue three
identical screens). The job fetches the provider's catalogue **with the caller's own
credential**, keeps the free candidates (`:free`-suffixed on OpenRouter) that have never been
measured here, and screens up to `discovery_screen_limit` (default 4) of them on the two real
HC3 texts in `benchmark/corpus-probe2.json` — through the shipped engine path, tie-break off, no
`tuning` block, so an unknown model is judged at the workflow's baked defaults instead of at a
band borrowed from another model. The first of those two texts doubles as the **triage**: a candidate
that cannot answer it is recorded in `catalog_models` with the engine's own reason — `http_410`
(retired), `no_choices` (advertised but not served), `http_503` (the pool is busy) — and costs one
call rather than two, and the job moves on to the next id, trying up to `DEFAULT_TRIAGE_CAP` (24) to
get its 4 screens. Both numbers exist because a provider returns its catalogue in its own order,
which for NVIDIA is alphabetical, and the ids a new user has never measured are overwhelmingly the
ones somebody retired: measured on 2026-10-17, a four-candidate job spent eight calls on
`01-ai/yi-large`, `adept/fuyu-8b`, `ai21labs/jamba-1.5-large-instruct` and
`aisingapore/sea-lion-7b-instruct` and reported `0/4 usable` — correct arithmetic, nothing brought in
for the user. A refusal is then left alone for `DEAD_RETRY_DAYS` (7), so one bad afternoon is not
filed as a permanent delisting and the next run does not spend its whole cap re-triaging the same
legacy ids. A screen writes a `benchmark_runs` row with
`corpus='probe2'`, `tag='autoscreen'`, `n_rows=2`, which lands it in the ranking's **probe
tier** where it can never outrank a full capture (`MEASURED_MIN_TEXTS = 24`); a usable one also
gets a `models` row (`measured=0`, `lane=NULL`, thresholds NULL — a screen is evidence the model
answers, not a calibration) and is appended to the caller's pool with `source='auto'`, behind
everything they placed by hand. `POST /my/discover` runs the same job on demand and refuses with
`NO_USER_KEY` rather than spending the admin's shared key; the UI offers it only for endpoints
the caller owns a key for, and renders a refused job as refused, never as queued.

Because a verified key is the *only* thing that queues that job, the verification itself cannot be
allowed to guess. It used to give up on a bare 45 s, and on 2026-10-17 that called a working
credential broken: `z-ai/glm-5.3-flash` — the shipped default lane, chosen because it is
thorough — did not answer "reply with the single word: ok" inside 60 s on a congested pool, so the
whole "enter it, test it, save it, get your models ranked" flow stopped at step one. A probe now
gets the ceiling of the model it is probing (`probe_timeout_s`: the row's `timeout_ms`, floored at
45 s, capped at `REQUEST_BUDGET_S`; an explicit `timeout_seconds` still wins, because that is how
the verifiers ask for a fast failure). It is a ceiling, not a wait — a healthy gateway replies in
a couple of seconds and returns as fast as it always did.

`python tools/verify_pool_failover.py` proves the loop live against a real provider, on a
throwaway copy of the dev DB with a throwaway user whose credential is copied as ciphertext (so
no key is ever printed): the drag writes the rank; a model that genuinely refuses —
`moonshotai/kimi-k3`, retired by NVIDIA on 2026-10-15, whose gateway still answers `410 Gone`
with the end-of-life date in 0.6 s — triggers exactly one switch, named in the body; one refusal
does *not* reorder the pool; three mark it `down` and re-order the served list while `your_rank`
survives; the next detection goes straight to the live model with no failover at all; and a
verified key queues a real catalogue fetch whose screens show up in `benchmark_runs`,
`catalog_models`, `models` and the probe tier of `/ranking` — or, when the provider answers nothing,
whose *refusals* are recorded in `catalog_models` with the engine's own reason and nowhere else.
The second branch is the one that matters on a congested morning: a candidate that cannot answer the
first of the two texts costs one call, the walk moves to the next id, and nothing that refused
becomes a score, a pinnable row, or a pool entry.

The fixture is the lesson, not just the harness. It first used the advertised-but-404 NVIDIA
primary, which turned out to answer sometimes and to 503 at other times — and a 503 is
*transient*, which by design can never mark a model `down`. Asserting a re-order on it would have
been testing that provider's mood rather than the rule, so the dead model is now one the provider
has permanently retired.

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

### The published site is checked from outside, with no credentials

`https://ai-text-ranking-630tcvp9y1c.qoder.website` carries the ranking and the report, and it is a
release, not a directory — so a green repo says nothing about it. `tools/verify_public_site.py` fetches
it anonymously and runs 19 checks (current: **19/19**):

```bash
python tools/verify_public_site.py                        # the published host
python tools/verify_public_site.py --url http://127.0.0.1:4173   # a local preview of public-site/dist
```

Every check compares *bytes*, for the same reason as above: this host answers an unknown path with a
200 and HTML, which `--url http://127.0.0.1:8080` demonstrates — against the app origin the verifier
reports 9/13, failing on "served page is byte-identical", "no .model-row", "no url(/fonts/...)" and
"an unknown path is a real 404". A check that cannot fail is not a check.

The one comparison that is not byte-for-byte is the injected `data-qoder-watermark` script: the host
appends ~57 KB to every HTML document it delivers, including `report.html`. It is stripped from both
sides, because it is the host's, ours is not, and there is nothing to fix about it. Everything else
must match exactly — page, report in both formats, and each `url(/fonts/…)` name read **out of the
served stylesheet** rather than from a list written here.

The row-order check is the one that matters. It re-reads `data-rank`/`data-model` from the served HTML
and compares them to `GET /ranking` answered by *this checkout's* `backend/app.py` over a copy of the
data — the same call the generator made. That is what makes the page and the app one answer rather
than two (#142). The lane check is read the same way, from the rendered `.la-model`, because
`nemotron-3-ultra-550b` legitimately appears eight times on this page as a *ranked model* — grepping
for it would have failed a correct page.

Two findings this check settled about what "private" means on a public page. Loopback *addresses* are
the leak, and `_host_label()` masks both rows that have one; the provider *name* `omniroute` stays,
because it is `/ranking`'s own label for the row and suppressing it would make the page disagree with
the app. And `report.pdf` is served as `application/octet-stream`, so the button re-types those bytes
as a Blob — the verifier asserts the content-type it actually saw (`application/octet-stream`,
471,173 B, `%PDF`).


The same rule covers the **serif faces**, and the reason is worse: a missing report is a broken link,
while a missing `fonts/` folder is a page that renders beautifully in the visitor's fallback serif and
reports nothing anywhere. Both verifiers therefore read the `url(/fonts/*.woff2)` names out of the
*served* stylesheet and compare each served file, byte for byte, against the copy
`tools/fetch_fonts.py` wrote — measured here, `GET /fonts/nope.woff2` through nginx answers **200 with
index.html**, so even a 404-shaped expectation would be wrong.

After editing the report, re-copy and rebuild the bundle:

```bash
cp research-zenodo/report.html research-zenodo/report.pdf frontend/public/research/
cd frontend && npx ng build && cd ..    # dist/frontend/browser/research/ must update too
```

### The public ranking page is generated by the app, not written by hand

The published site lives at `https://ai-text-ranking-630tcvp9y1c.qoder.website` and carries exactly two
things: the model ranking with each model's measured performance, and this report as PDF and HTML. It is
produced by `tools/public_ranking_page.py`, which opens a throwaway session on a *copy* of the dev
database and renders what `GET /ranking` answers:

```bash
python tools/public_ranking_page.py --dump    # what the route says, nothing written
python tools/public_ranking_page.py           # writes public-site/dist/ (gitignored output dir)
```

Why a generator and not a hand-written table: #142 was a page that answered a question the app had
already answered differently. The tool re-sorts nothing — it refuses to split the measured and probe
tables unless `/ranking` still puts full captures first, and after writing, it reads the row order back
out of the HTML and exits non-zero unless it equals the route's order. The lane bar renders only from
`lane_authority`; if a backend stops reporting it, the page says nothing about who answers a run rather
than inventing one client-side.

Two rendering decisions are deliberate. A ranked model whose host is loopback or private (two were
captured through a gateway on this machine) is printed as *local gateway (measuring machine)* — a public
page has no business publishing private topology. And because the host serves `report.pdf` as
`application/octet-stream` (verified: 200, 471 173 B, `%PDF` magic, no `Set-Cookie`), the PDF button
fetches those same bytes and re-types them as a Blob; an iframe on that object URL reports
`document.contentType === "application/pdf"`, so the report reads inline while the raw link still
downloads.

Regenerate and republish after any change to the ranking, the bands or the report — the site is a frozen
draft, so a stale page is a page that disagrees with the deployed app.

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
python -m pytest backend/tests -q     # 158: routing, encryption, purger, tuning block, auth gate, off-lane
                                      # sanitization, slow-engine-vs-missing-engine labelling, and the
                                      # using-pool: failover precedence, the request budget every later
                                      # attempt shares, which failures a second model cannot fix, and the
                                      # discovery walk -- a refusal costs one call, is recorded without a
                                      # score, and the next id still gets tried
cd frontend && npx ng test --watch=false   # 104: lane_config composition, key headers, admin card, quote painting,
                                      # lane-scale note, report-link fallback (blank paper_url -> /research/),
                                      # the using-pool drag/reorder panel, the auto-discovery list, and the
                                      # two request ceilings -- a detect and a key probe are waited out to
                                      # the gateway's own patience, a control-plane read is not
                                      # On a host with no commit headroom the run dies without testing anything:
                                      # esbuild "Fatal process out of memory: Zone" / "spawn UNKNOWN" in the
                                      # build phase, or a clean build followed by "Worker exited unexpectedly
                                      # with signal SIGTERM" and zero test output. Check FreeVirtualMemory
                                      # (KB) before believing either; NODE_OPTIONS cannot fix a native zone
                                      # allocation and --include subsets do not help (the build is per invocation).
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
python tools/verify_pool_failover.py  # the using pool live, on a copy of the dev DB with a real stored
                                      # key (45 checks): a rank-1 model that refuses is failed over and
                                      # the switch is reported, one refusal does NOT reorder the pool,
                                      # three consecutive ones mark it down and the served order starts
                                      # with the model that answers while your_rank is kept, the dead
                                      # model is then never called at all, the history row and the
                                      # ranking page name the model that answered, saving a verified key
                                      # queues discovery on its own, and the discovery walk turns a real
                                      # 81-id catalogue into usable models by refusing to spend two texts
                                      # on an id that will not answer -- the run measured 24 candidates
                                      # tried, 2 screened, 22 refusals recorded with their reason, and
                                      # none of the 22 reaching the ranking page or the pool
python tools/verify_deploy_recipe.py  # container preflight, no daemon needed: COPY sources vs
                                      # .dockerignore, CRLF in files a Linux container executes, the
                                      # healthcheck path is public AND routed, the timeout chain nests
                                      # at every tier -- lane -> max(lane+30, REQUEST_BUDGET_S) ->
                                      # gunicorn -> nginx -> the browser's own HTTP_PATIENCE_MS, so
                                      # no inner layer can give up before an outer one -- and DEPLOY.md
                                      # quotes the same numbers the files carry, the two shipped copies
                                      # of the report are byte-identical and tracked, and dist carries
                                      # them too (40 checks)
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
