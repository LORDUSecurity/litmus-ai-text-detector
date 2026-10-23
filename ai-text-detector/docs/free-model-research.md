# Free-model research for AI-text verification

Everything below was measured, not inferred. The source of truth is the backend's SQLite
(`benchmark_runs`, `benchmark_results`, `catalog_models`), which is seeded from the on-disk
artefacts in `benchmark/` by `python -m backend.seed`. The same rows render in the UI under
**Free-model research**.

Measured on 2026-10-05/06 against a 48-text labelled corpus (24 AI, 24 human, from HC3) and a
46-excerpt Gutenberg corpus of real human book text, then extended on 2026-10-09 (rounds 6–7,
retrying the non-responders) and 2026-10-10 (round 8, hunting free models outside the two
gateways already wired up — which found three that outrank the paid primary).

**Headline as of round 9:** the round-8 conclusion that OmniRoute was a dead end was **wrong, and
retracted below**. OmniRoute is installed on this host as a Node CLI (no Docker involved) and its
database already held **15 connected provider accounts**. Started and queried for real, it serves
**1608 models**, of which one — `kiro/claude-sonnet-4.5`, a free AWS Kiro OAuth login rather than an
API key — scores **AUC 0.9878, Cohen's d 5.017, accuracy 0.958 at the shipped 0.69/0.40 band with
zero deferrals**, 48/48 usable, p50 3.0 s, and is proven live end-to-end through the real webhook.
It is stored on `models` row 30 with `lane = null` — measured, not promoted. Paired against round 8's
`z-ai/glm-5.3` it is **statistically indistinguishable** (McNemar exact p = 1.0000, 1 discordant pair).
Nothing in the running configuration changed.

**Headline as of round 8:** `z-ai/glm-5.3` on the NVIDIA free tier answered 48/48 texts with zero
failures of any kind, scored AUC 1.000, and made **zero wrong calls** at the shipped 0.69/0.40 band
(3 deferrals). It is stored on `models` row 22 with `lane = null` — measured, not promoted. Nothing
in the running configuration changed.

## What the free tiers actually expose

Two catalogs were probed model-by-model with one real request each
(`benchmark/catalog-probe-*.json` → `catalog_models`, 96 rows):

| provider | answered | not_found | timeout | rate_limited | forbidden | http_400 | http_500 | listed |
|---|---|---|---|---|---|---|---|---|
| NVIDIA NIM (`integrate.api.nvidia.com/v1`) | 13 | 55 | 8 | — | — | 2 | 2 | 80 |
| OpenRouter (`openrouter.ai/api/v1`, `:free` only) | 10 | — | — | 4 | 2 | — | — | 16 |

- **NVIDIA lists 80 models and serves 13 of them for free.** The 55 `not_found` results are the
  catalog lying: the model is advertised but the free tier returns 404. The 8 `timeout` results
  (~35 s each) are the shared worker pool saturating, not the model being slow — the same model
  answers in seconds when the pool is idle.
- **OpenRouter's `:free` tier 429s in two different classes, and they need opposite responses.**
  The transient one is per model: `... is temporarily rate-limited upstream`, with the reason in
  `error.metadata.raw` (the top-level `error.message` is generic for every failure class); re-probing
  minutes later succeeds. The other is an **account-level daily quota**, and it does not clear by
  waiting: `Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model
  requests per day`. Recorded on 2026-10-06T23:00Z, **all three stored keys returned it within one
  second of each other** (`tools/probe-or-headroom.js`, which prints status and the error's first
  words and never a key), so the whole free source was closed for the rest of the day.
- **That cap is the real budget for this research.** `.env` carries `OPENROUTER_RPM=20` /
  `OPENROUTER_RPD=200`, but those are **not measured and not enforced** — nothing in the backend,
  harness or workflow reads any `*_RPD`/`*_RPM` (checked 2026-10-07), they are leftovers from the
  retired statistical detector, and the gateway's own 429 body contradicts them with
  `X-RateLimit-Limit: "50"`. The measured budget facts are: one `free-models-per-day` cap per
  account, retries spending it just as much as first attempts, and no way to read the remaining
  balance. A 48-text capture is 48 requests *plus retries*, and a 10-model 12-text screen is ~120 —
  so one day of free-tier work pays for roughly four full corpora or one screen plus two
  promotions. Two consequences for how a round is planned: probe headroom before starting a run,
  and give each candidate a `--limit` small enough that the day still covers the models that matter.
- **The cap number is visible — on the failure, never on the success.** A class-(b) 429 body
  mirrors the limiter's headers into `error.metadata.headers`, and there it does state
  `X-RateLimit-Limit: "50"` plus an `X-RateLimit-Remaining…`. Two days of "no API field exposes the
  cap" were spent reading a capture in which that text existed but was cut at 200 characters by
  `error_detail`'s truncation, so the number was there and invisible. A **200 response carries no
  `x-ratelimit-*` header at all** (verified live: all three keys served `nemotron-3-ultra-550b-a55b:free`
  and the new per-row `rate_limit` field wrote `null`). `tools/probe-or-headroom.js` and
  `run-benchmark.js` now extract those headers from both places, whitelisted to the
  `x-ratelimit-` prefix (a `Set-Cookie` planted in the same echoed object was excluded in the
  mock-429 test), so the next capped run states the window. **Still unknown: what "50" counts.**
  The captured row truncated `X-RateLimit-Remaining` and everything after it, so nobody has proven
  whether it is per day per model, per day per account, or per minute — do not quote it as a daily
  cap until the full header set is in hand.
- **The cap's scope is inferred, never read.** `GET /api/v1/key` (`tools/probe-or-accounts.js`) confirms
  the three stored keys are three distinct accounts and that none of them exposes a request counter:
  `usage` is dollars spent and read `0` on all three while every chat request returned
  free-models-per-day. So "account-level" rests on the message's own wording ("add 10 credits", per
  account) plus the fact that the harness rotates its three keys evenly — which is why all three ran
  out inside the same second rather than one at a time. Nobody has yet watched one account serve a
  request while another was capped, and that is the observation that would settle it.
- **Rotation is now the app's behaviour too, not just the harness's.** The backend used to read the
  lowest key id for every lane, so two stored accounts meant one account and a `free-models-per-day`
  429 was the end of the day for the app even while another account had headroom. It now honours a
  `<lane> lane` label and takes the stalest-used of the rest, stamping `api_keys.last_used_at` so the
  Configuration tab can show which account actually served anything.
- **A free lane's failure can arrive as a success.** OpenRouter does not relay its upstream's
  status code: `nvidia/nemotron-3-ultra-550b-a55b:free` answered 19 of its 48 requests with
  `HTTP 200` and `{"error":{"status":503},"message":"Upstream error from Nvidia: Service
  temporarily overloaded"}` in the body. The harness only retried on the *transport* status, so
  it returned that row at `attempt 1` — no backoff, no key rotation — in a run launched with
  `--tries 5 --wait_ms 10000`. The retry now classifies an in-body 429/5xx as transient like the
  transport one (`transientInBody`), and the summary counts the two failure kinds separately
  (`n_no_answer` vs `n_parse_failed`, 78 and 63 rows across all stored runs). Until this fix every
  one of those rows was published as a JSON parse failure, i.e. blamed on the model.
- **The deployed workflow was already braver than the harness, and that had to be measured rather
  than assumed.** `tools/verify-lane-retry.js` fires the real webhook with both lanes pointed at
  `tools/mock-gateway.js` on loopback (zero API budget) and counts the mock's own requests. A real
  HTTP 503 and an HTTP 200 whose body is an upstream 503 each produced **4 attempts 3 s apart** from
  the same lane, then the answer; a lane that never recovers stops at 4 per lane and returns
  `DETECTION_UNAVAILABLE` / `ALL_MODELS_FAILED` with the reason recorded as a gateway code. The
  control that makes those counts mean something is a 200 carrying `{}`: exactly **1 attempt**, so
  the re-issue is triggered by the error signal, not by the node always running `maxTries`. Read
  `neverError: true` in the workflow JSON and you would conclude the opposite — it sets
  `requestOptions.simple = false`, and in this n8n version `simple` is only consulted on the OAuth
  path, so the flag no longer suppresses the throw that `retryOnFail` needs. The harness was the
  outlier, not the workflow: same gateway, same failure, one attempt instead of four. The mechanism
  behind the 200-with-body-error throw was not located in n8n's source, so this is a behaviour the
  test pins, not one the code comments explain — an n8n upgrade is checked by re-running it.
- **Two `thinkingmachines/inkling*` models return 403 `only available on agentic hardware`.**
- **OmniRoute is self-hosted** (`http://localhost:20128/v1`) and was unreachable on 2026-10-06. It
  adds models only if it is fed upstream keys, so it is not an independent free source.

## The OpenRouter `:free` screen (12 texts, 2026-10-06)

The catalogue probe says a model answers; it does not say the model is usable as a
detector. Ten `:free` models that answered the probe were each run over
`corpus-screen12.json` — 6 AI + 6 HUMAN, evenly spaced across the HC3 configs — with the
shipped prompt and parser (`benchmark/free-openrouter-screen.sh` →
`benchmark/analyze-free-screen.js`). AUC is listed first because free models sit on their
own probability scales, so the shipped cuts are not transferable to them.

| model | usable | AUC | AI mean | HUMAN mean | sep | correct/wrong/deferred | what went wrong |
|---|---|---|---|---|---|---|---|
| inclusionai/ling-3.0-flash-sante:free | 7/12 | 1.000 | 0.760 | 0.467 | 0.293 | 3/0/4 | 5 unusable, 0 caused by the 700-token cap |
| nvidia/nemotron-3-ultra-550b-a55b:free | 11/12 | 0.767 | 0.720 | 0.466 | 0.254 | 5/1/5 | one upstream 503 |
| nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free | 10/12 | 0.700 | 0.792 | 0.648 | 0.144 | 5/3/2 | two 502 |
| nvidia/nemotron-3-super-120b-a12b:free | 12/12 | 0.611 | 0.517 | 0.452 | 0.065 | 5/2/5 | — |
| cohere/north-mini-code:free | 12/12 | 0.389 | 0.662 | 0.752 | −0.090 | 4/5/3 | inverted |
| dots-studio/dots-3-note-preview:free | 4/12 | — | 0.800 | — | — | 3/0/1 | 8 answers carried no JSON object |
| nvidia/nemotron-3.5-lightning:free | 0/12 | — | — | — | — | 0/0/0 | 12 × json_parse_failed |
| nvidia/nemotron-3.5-content-safety:free | 0/12 | — | — | — | — | 0/0/0 | 12 × no_json_object |
| google/gemma-4-26b-a4b-it:free | 0/12 | — | — | — | — | 0/0/0 | 12 × http_429 |
| google/gemma-4-31b-it:free | 0/12 | — | — | — | — | 0/0/0 | 12 × http_429 |

What this says:

1. **Half the free tier cannot be a detector at all.** Four of ten produced no usable
   score on any text. Two never returned parseable JSON, and two were blanked by rate
   limiting. At the time this screen was written the 429s were read as per-model upstream
   pool saturation and therefore transient — half of that is now wrong: the account-level
   `free-models-per-day` cap produces the same blank rows and no backoff outlasts it (the
   free-tier bullet above names both classes; the round-2 passage below shows why telling
   them apart mattered). **A 0/12 here is "retry when the quota resets", not a
   verdict on the model.**
2. **`ling-3.0-flash-sante` separated perfectly and still fails**: AUC 1.000 over the 7
   texts it answered, but 5 of 12 answers were unusable — and the first explanation given
   for that (`max_tokens` 700 was cutting them off) turned out to be wrong. The 48-text
   capture has 11 cut-off answers and **none of the 17 failures is a cut-off answer**: all
   17 finished naturally and simply did not contain a parseable `ai_probability`
   (`missing_ai_probability` 10, `no_json_object` 5, `json_parse_failed` 2). Headroom
   therefore cannot lift its yield; the failures are a format-following limit of the model.
   What the cap does cost is that 9 of the 11 cut-off scores exist only because the JSON
   salvager patched a truncated object — so the remaining question is whether a salvaged
   score equals a clean one, which round 4 measures by replaying the 12-text screen at 1400
   tokens and reading the paired drift (`benchmark/free-round4.sh`).
   Writing that audit surfaced a worse possibility, and it was real: the salvage step reads the
   number with a regex, so a cap landing *inside* the digits returns the digits that survived.
   `{"ai_probability": 0.` — a cut `0.78` — parsed as **0**, `ok: true`, i.e. a confidently
   HUMAN answer invented out of a truncated one. `n8n/test-parse.js` now pins the rule: a
   salvaged number must be followed by a JSON delimiter, or the answer fails to parse and the
   lane escalates like any other failure. Both copies (the shipped `Parse Primary` node and
   `benchmark/run-benchmark.js`) changed, so every capture made before 2026-10-07 00:00Z may
   contain salvage-read numbers that today's parser would have refused — the counts in this
   document are those older captures, and round 4 is the first one taken under the new rule.
3. **`nemotron-3-ultra-550b-a55b:free` is the usable free version of the shipped
   primary** — and its first 48-text capture (round 3, 2026-10-07) reported a **29/48 yield that
   turned out to be a harness artifact, not a measurement**. All 19 missing rows were one identical
   event: `http_status` 200, `error_code` "503", `error_detail` "Upstream error from Nvidia:
   Service temporarily overloaded", `attempt` 1, max latency 1.4 s — and no other failure code
   anywhere in the file (zero truncation, zero salvage). OpenRouter does not relay its upstream's
   status as an HTTP code; it answers 200 and puts the failure in the body, and the harness only
   retried on the transport status. So a run launched with `--tries 5 --wait_ms 10000` asked five
   times and gave up once, silently, without ever rotating to the other two keys — on exactly the
   transient the rotation was written for. Fixed the same day (`transientInBody` in
   `benchmark/run-benchmark.js`, pinned by `benchmark/test-retry.js`, 15 cases, no network), and
   round 5 (2026-10-07 01:44Z) resumed only the 19 missing rows to land the complete capture:
   **`usable=48/48 salvaged=0 truncated=0 errors=none`, `no_answer=0 bad_answer=0`**, 451 s wall
   clock, 8 of the 19 rows needing a second attempt. The model's own behaviour on all 48 texts is
   now the measurement: AUC 0.9366 (the paid endpoint scores 0.934 on the same labels), an AI mean
   of 0.741 with a 0.55 minimum against the paid 0.85 median, and separation 0.410. That is the
   shipped `0.69` primary cut landing *inside* this model's AI mass; on its own band (0.56/0.45) it
   decides 41/48 correctly with 4 reviews. So the free mirror ranks texts as well as the paid one
   and decides with the paid numbers wrongly — the free lane needs its own thresholds stored on its
   own row, which it now has (see the band section below). Whether it can *carry a lane* is a
   different question from whether it can score a corpus, and is answered there and in the
   tie-break section.
   The two failure modes are worth keeping apart, because they argue for opposite fixes: after
   re-classifying every stored run, this mirror failed 19 times with the gateway never answering
   and **0** times answering without a score, while `ling-3.0-flash-sante:free` failed 17 times
   the other way round — 0 no-answer, 17 answers carrying no score. Same headline number, one
   is retryable and one is the model's own behaviour. Round 5 is the proof of the first half: all
   19 came back on retry, and none of them came back unscored.
4. **`cohere/north-mini-code:free` is inverted** (separation −0.090, 5 wrong of 12): a
   code-tuned model reads human forum prose as more machine-like than AI prose. Same
   failure mode as `meta/llama-3.2-11b-vision-instruct` on the 48-text table below —
   the second independent confirmation that a free model cannot be trusted without a
   labelled measurement.

Round 2 (`benchmark/free-round2.sh`) had two jobs on the free tier: promote the top two 12-text
screens to the full 48-text corpus, then re-attempt the two 429-blanked gemma models at concurrency
1 with a backoff long enough to outlast an upstream pool. (The Gutenberg book control is not part of
it — it talks to the paid NVIDIA endpoint with different keys and ran separately at 22:25.)
**One of the four runs finished, and the reason is the finding.**

- Finished: `inclusionai/ling-3.0-flash-sante:free` on 48 texts (690 s wall clock, 31 usable), and
  the book control (`bookultra550`, 46/46 usable).
- Not finished: `nvidia/nemotron-3-ultra-550b-a55b:free` on 48 texts. It ran 27 minutes and wrote
  nothing, because the harness writes its results file only at the end. Stopped at 23:00Z.
- Never started: the two gemma resumes, which were queued behind it in the same script.

The stop was not impatience. At 23:00Z all three stored keys returned
`Rate limit exceeded: free-models-per-day` on a single one-word probe, so the remaining ~72
requests of round 2 (48 + 12 + 12, before retries) could not have been served at any backoff —
every retry was spending a budget that was already empty. **The plan's error was assuming the two
429 classes were the same one:** the gemma blanking was read as upstream-pool saturation (which a
slow backoff can outlast) when the account quota (which it cannot) was at least partly
responsible.

**The gemma rows are therefore not evidence about backoff at all, in either direction.** The first
round-2 attempt passed `--wait 15000` while `run-benchmark.js` reads `--wait_ms`: the harness
returned its 3000 default for the unknown name and the manifest faithfully recorded
`wait_ms: 3000`, so that retry really was 6 tries with 3/6/9/12/15s sleeps rather than the intended
15/30/45/60/75s. That flag bug is fixed (`run-benchmark.js` now exits 2 on an unknown flag, and
`free-round2.sh` uses the real names). **The corrected attempt did run, and it is still not
evidence:** round 2's `gemma-4-26b-a4b-it:free` resume spent 2764 s on 12 texts × 6 tries with the
real backoff and wrote `usable=0/12`, every row `http_429` — but that window sat entirely inside the
account cap, so it measures how long a capped day stays capped, not whether the model can score.
What it does prove is the cost of the mistake: ~70 requests of a finite budget bought a blank file.
Round 3 is `benchmark/free-round3.sh`:
it polls `node tools/probe-or-headroom.js` every 5 minutes and only starts the promotion and the two
resumes once a key has proved it can serve a request, so it can be launched against a capped account
and left to run through the reset. A capped probe returns 429 without consuming anything, so polling
costs nothing; `DEADLINE_MIN` (default 240) stops it from waiting forever.

**The reset came roughly at the date boundary, and only one round should have been running.** Round 3's
gate reported `headroom found after 50 min of waiting`, and a probe at 00:19Z returned 200 on all three
keys — so the reset falls between round 2's 23:00Z all-capped probe and 00:05Z, consistent with a
UTC-midnight window but not proven (nothing was polled inside that gap). Two facts from that moment are
worth keeping. First, **round 2 was still alive when round 3 started**, and both were resuming the same
two gemma tags into the same two files: the second one to finish would have silently overwritten the
first while both burned the fresh day's cap. Round 2's tree was stopped at 00:29Z so round 3 owns those
runs; its remaining steps are offline analysis, re-runnable at any time (`analyze-free-screen.js`,
`analyze.js`). Second, the harness's write-at-the-end behaviour is what makes this survivable — killing a
resume mid-flight leaves the previous capture intact rather than half-written, which is also why a
long-running capture shows no file progress and looks, from `ls`, exactly like a dead one.

**Round 3's gemma resume produced round 2's blank again, and this time the reason is measurable.**
It finished ~00:53Z after 2776.7 s with `usable=0/12`: every row `http_429` at `attempt: 6` (six
tries, 15 s backoff, three rotating keys), latencies 0.9–1.6 s, and the body reads `google/gemma-4-26b-a4b-it:free
is temporarily rate-limited upstream. Please retry shortly, or add your own key to accumulate your
rate limits`. That is **class (a), the per-model upstream pool — not the account cap**, and unlike
round 2 this capture was taken *after* the reset, with headroom proved twice over — a 200 on all
three keys at 00:19Z and the 29 served requests of the promotion that ran before it. So
it is evidence rather than an artifact: that model is unreachable from these accounts for at least 46
continuous minutes, which means "class (a) clears in minutes" is not a property a schedule can be
built on. 72 requests, second blank file. Decision: **stop resuming the two gemma `:free` models** —
their screen rows stay 0/12, and the paid `google/diffusiongemma-26b-a4b-it` on NVIDIA already
carries the signal they were meant to test.

**Round 5 was the retry fix's own test, and it passed.** `benchmark/free-round5.sh` resumed just the
19 mirror texts the harness had abandoned (the 29 scored rows kept untouched), at `--tries 2` and
concurrency 1 — 38 requests at most — behind both round 3 and round 4 in the queue, because all three
share one account budget whose scope is still inferred. The script prints the panel's own numbers and
regenerates the published table at the end, so the answer arrives as a row, not a claim. The result:
**all 19 came back, `usable=48/48`, `salvaged=0`, `truncated=0`, `no_answer=0`, `bad_answer=0`** — 8
of the 19 needed the second attempt, i.e. the retry the harness previously did not perform.

Three queued runs were competing for one daily budget and only one of them could change a shipping
number. Round 3's gemma resume (a declared dead end above) and round 4's 12-request `ling` replay (a
question `truncation-audit.js` had already largely answered) were stopped so round 5 could finish;
both scripts and their partial captures are left in place, and what they did not measure is listed
under "What is still unmeasured" rather than quietly dropped.

## Provenance of a measurement

`backend/seed.py` attributes a `results-<tag>.jsonl` file to a model id and provider by
reading `benchmark/runs-manifest.log`. The harness did not write that manifest, so every
new run imported as `model=<tag>, provider=nvidia, corpus=hc3-48` — a 12-text screen
claiming to be the 48-text corpus, on a tag instead of a model, under the wrong provider.
`run-benchmark.js` now appends its own params line (model, prompt, sampling, base_url,
corpus file, row/usable counts; never the key), and the seeder derives the corpus label
from the file actually read. The 2026-10-06 screen rows were backfilled from the screen
log to match.

The same class of failure has a second instance: a flag whose name was typed wrong is
dropped and its default silently used, so the run is described by parameters it never ran
with. `run-benchmark.js` now lists the flags it actually reads and exits 2 on any other
`--name`, which turns that class of error into a failed run instead of a misdescribed one.

## Every model scored on the full 48-text HC3 corpus

Scored under the shipped decision band (AI ≥ 0.69, HUMAN ≤ 0.40). A deferral — the model landing
inside the band — counts as *not correct* here, because this column scores each model standing
alone, before the tie-break lane answers. `correct + wrong + deferred = n scored`.

Two accuracy columns, because they answer different questions. `acc` = correct ÷ texts the model
actually answered; `acc / corpus` = correct ÷ texts the run was *given*. A model that answered 31
of 48 reads 0.774 / 0.500: decent on what it said, half the corpus left unsaid. Only the second
one is comparable across runs, and only the first one describes the model's judgement.

| tag | model | n | acc | acc / corpus | wrong | deferred | med AI | med human | separation |
|---|---|---|---|---|---|---|---|---|---|
| nvfull-gemma-4-31b-it | google/gemma-4-31b-it | 48 | 0.854 | 0.854 | 7 | 0 | 0.980 | 0.150 | 0.830 |
| nvfull-glm-5-3 | z-ai/glm-5.3 | 48 | 0.938 | 0.938 | 0 | 3 | 0.930 | 0.135 | 0.795 |
| nvfull-glm-5-3-flash | z-ai/glm-5.3-flash | 48 | 0.938 | 0.938 | 1 | 2 | 0.930 | 0.210 | 0.720 |
| ultra550 | nvidia/nemotron-3-ultra-550b-a55b | 48 | 0.750 | 0.750 | 2 | 10 | 0.815 | 0.250 | 0.565 |
| orfull-ling-3-1-flash | inclusionai/ling-3.1-flash | 46 | 0.783 | 0.750 | 1 | 9 | 0.850 | 0.300 | 0.550 |
| diffgemma26b | google/diffusiongemma-26b-a4b-it | 48 | 0.750 | 0.750 | 12 | 0 | 0.980 | 0.500 | 0.480 |
| orfull-inclusionai-ling-3-0-flash-sante-free | inclusionai/ling-3.0-flash-sante:free | 31 | 0.774 | 0.500 | 1 | 6 | 0.820 | 0.350 | 0.470 |
| museglimmer30b | meta/muse-glimmer-30b | 47 | 0.723 | 0.708 | 3 | 10 | 0.835 | 0.420 | 0.415 |
| orfull-nvidia-nemotron-3-ultra-550b-a55b-free | nvidia/nemotron-3-ultra-550b-a55b:free | 48 | 0.667 | 0.667 | 2 | 14 | 0.735 | 0.325 | 0.410 |
| baseline | nvidia/nemotron-3-super-120b-a12b | 48 | 0.458 | 0.458 | 10 | 16 | 0.620 | 0.365 | 0.255 |
| nano30b | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | 47 | 0.596 | 0.583 | 11 | 8 | 0.850 | 0.620 | 0.230 |
| t0 | nvidia/nemotron-3-super-120b-a12b | 47 | 0.404 | 0.396 | 10 | 18 | 0.550 | 0.325 | 0.225 |
| museglimmer02 | meta/muse-glimmer-30b | 46 | 0.717 | 0.688 | 4 | 9 | 0.845 | 0.650 | 0.195 |
| ultra550-evidence | nvidia/nemotron-3-ultra-550b-a55b | 41 | 0.463 | 0.396 | 2 | 20 | 0.670 | 0.500 | 0.170 |
| orfull-openrouter-free | openrouter/free | 33 | 0.576 | 0.396 | 7 | 7 | 0.850 | 0.680 | 0.170 |
| gptoss20b | openai/gpt-oss-20b | 47 | 0.447 | 0.438 | 6 | 20 | 0.700 | 0.600 | 0.100 |
| lagunaxs | poolside/laguna-xs-2.1 | 45 | 0.622 | 0.583 | 17 | 0 | 0.850 | 0.750 | 0.100 |
| nvfull-nemotron-3-5-lightning-30b | nvidia/nemotron-3.5-lightning-30b-a3b | 23 | 0.391 | 0.188 | 9 | 5 | 0.315 | 0.220 | 0.095 |
| repeat01 | nvidia/nemotron-3-super-120b-a12b | 45 | 0.378 | 0.354 | 9 | 19 | 0.620 | 0.550 | 0.070 |
| orfull-lfm-2-5-2-6b-free | liquid/lfm-2.5-2.6b:free | 47 | 0.319 | 0.313 | 16 | 16 | 0.665 | 0.650 | 0.015 |
| llama11v | meta/llama-3.2-11b-vision-instruct | 48 | 0.333 | 0.333 | 32 | 0 | 0.200 | 0.700 | −0.500 |

This table is generated, not hand-copied: `node benchmark/doc-table.js hc3-48` prints it from
the same `summarize_run` the panel uses.

**One row in this table changed shape twice, and both times for reasons outside the model.** The
`:free` mirror of the primary used to read `29 | 0.655 | 0.396`: it now has all 48 rows, and the
two accuracy columns moved very differently — 0.655 → 0.667 (the 19 recovered texts were not
harder or easier than the ones already captured) while acc/corpus moved 0.396 → 0.667 (the whole
gap was *absence*, not judgement). The two-step cause is in the next section but the lesson is
here: a model's acc/corpus is a property of the harness that collected it, so a yield number is
only evidence about a model once the transport failures have been separated from the model's own
non-answers (`n_no_answer` vs `n_parse_failed`, `node benchmark/analyze.js <file>`).

**Two runs used to appear above and no longer do.** `smoke` (3 texts) and `or-gemma26b-smoke4`
(4 texts, 4 × `http_429`, no manifest line and no run log) were imported before the harness
recorded `--corpus`, and the fallback credited them with the full corpus because that is what the
harness ran by default. A 4-row file cannot evidence a 48-text corpus, so `seed.corpus_label` now
labels such runs `unattributed` instead — the row is kept as capture history, it just cannot feed
the table models are ranked on. The guard sits *after* the `book`-in-tag branch, so the Gutenberg
runs keep their label at 46 rows, and the corpus is still taken from the manifest whenever the
harness recorded one (`--limit 4` against `corpus-screen12.json` is a screen, not a new corpus).
Covered by `test_run_without_provenance_is_not_credited_with_the_corpus_it_cannot_evidence`.
`doc-table.js` additionally drops any run that scored zero texts, which is why the four free models
that answered nothing on the 12-text screen (two 429-blanked, two that never returned parseable
JSON) do not appear in a ranking table for that corpus.

**Median convention, and why one row moved.** Until 2026-10-06 the backend and `analyze.js`
both took `probs[len // 2]` — for an even-length list that is the *upper* of the two middle
values, not the median. Everything here now uses the pair mean, matching the analyzers. The
change is cosmetic for most rows and not cosmetic for one: `diffgemma26b`'s median human score
moves 0.750 → 0.500 and its separation 0.230 → 0.480. Its 24 human scores are bimodal —
twelve at 0.15-0.25 and twelve at 0.75-0.98 — so there is no central value, and any median
there is decided by the tie-break convention rather than by the model. Read the 0.480 as a
warning: **separation computed from medians is not trustworthy for a bimodal scorer.** For
that model the honest statistic is the split itself (half of real human text scored ≥ 0.75),
which is exactly why it flags 12 texts wrong and is gated behind a 0.90 tie-break cut.

Two rows are not comparable and are kept only as evidence of method:
`smoke` (n=3, a connectivity check) and `or-gemma26b-smoke4` (n=0, an OpenRouter smoke test that
scored nothing). `bookdiffgemma` ran on the Gutenberg corpus, which is all-human text, so its
"accuracy 1.000" says nothing about AI detection — only that `diffusiongemma-26b-a4b-it` scored 29
real book excerpts at a median p(AI) of 0.02 and a maximum of 0.08. Note what the two accuracy
columns do with that same row: 1.000 over the texts it answered, **0.630 over the 46 texts it was
given**, because 17 replies carried nothing the parser could read. The second number is the one
that describes deploying it.

### What the ranking says

1. **Model choice dominates everything else.** The same prompt and parser span 0.313 → 0.938
   accuracy over the corpus in this table. Tuning temperature or top_p moves a model by far less
   than switching models does.
2. **Three free models on the NVIDIA lane now sit above the paid primary** — round 8, and the
   reason they were missed is a probe timeout, not a missing model (see *Round 8* below).
   `z-ai/glm-5.3` is the best detector measured here: 48/48 answered, AUC 1.000, and **zero wrong
   calls** at the shipped band, with 3 deferrals. `z-ai/glm-5.3-flash` matches its accuracy with one
   error. `google/gemma-4-31b-it` has the widest separation in the table (0.830) and is the only
   model that defers nothing — at the cost of 7 wrong calls, all of them human text read as AI.
   `nemotron-3-ultra-550b-a55b` (separation 0.565, 2 wrong, 10 deferred) is no longer the cleanest
   row in the table, but it is still the only one whose band was validated live through n8n.
   A caveat that applies to the whole ordering: the `acc / corpus` column ranks by a 48-text
   in-sample measurement, and separation is *not* accuracy — it sorts gemma above glm-5.3 even
   though glm-5.3 gets more texts right.
3. **`meta/llama-3.2-11b-vision-instruct` is inverted** (separation −0.500, 32 wrong of 48): it
   scores human text as more AI-like than AI text. It must never be used as a detector, and it is
   the clearest example of why a free model cannot be trusted without a labelled measurement.
4. **`diffusiongemma-26b-a4b-it` never defers** (0 of 48) and is extremely confident in both
   directions — its 24 human scores split into twelve at ≤ 0.25 and twelve at ≥ 0.75, with
   nothing between. That makes it a poor primary — its scale sits ~0.3 above the 550B's on the
   texts it flags AI, and half of real human text is on that side — but a useful tie-break:
   when the primary defers, this model has an opinion. Its 12 wrong calls are why it is gated
   behind `SECONDARY_DECISION_THRESHOLD` 0.90.
5. **Run-to-run jitter is real.** `baseline`, `t0` and `repeat01` are the *same model* on the same
   corpus: 0.458 / 0.404 / 0.378. Judge a model on aggregates over many texts, never on one text.
   Round 8 added a second instance of the same trap from the other direction: a 2-text probe of
   `liquid/lfm-2.5-2.6b:free` showed a clean inversion (AI 0.22, human 0.78), and the 48-text
   capture showed AUC 0.4801 — a coin flip. The probe had found nothing.

## The band a `:free` model inherits is the paid pair's, and that is not neutral

Every `:free` row in `models` that carries a band at all — ids 3, 5 and 6 — was seeded with
`ai_prob_threshold 0.69 / human_prob_threshold 0.40`, the cuts calibrated on the *paid*
`nemotron-3-ultra-550b-a55b` × `diffusiongemma` pair. A threshold is a property of the distribution
it was fitted to, so the honest question is what inheriting it costs.
Measured offline (`node benchmark/calibrate.js results-orfull-<tag>.jsonl`, zero requests) on the
full 48-text captures:

| free lane | usable | its own best band | acc | review | shipped band 0.69/0.40 | acc | review |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 48 | ≥0.56 / ≤0.45 | **0.854** | 0.083 | same pair | **0.667** | 0.292 |
| `inclusionai/ling-3.0-flash-sante:free` | 31 | ≥0.66 / ≤0.35 | 0.774 | 0.194 | same pair | 0.774 | 0.194 |

(acc counts a review as not-correct over all rows, so 0.854 = 41/48 and 0.667 = 32/48; of the
mirror's own band, the 41 decided calls are 0.932 precise, with 3 false AI, 0 false human and 4
reviews.)

So the inheritance is **not** a uniform tax. For the mirror it costs 0.187 accuracy and sends
nearly a third of texts to review, because that model's AI mass *starts* at 0.55 (mean 0.741,
median 0.735): the paid 0.69 cut sits inside the very scores it is supposed to be separating, so
real AI texts fall under it and become review or a `SINGLE_MODEL_*` guess. For ling the two bands
are the same operating point — its AI median is 0.82, above the paid cut, so nothing moves. The
discriminator is readable without a sweep: **a lane needs its own band when its minimum AI score
is below the inherited cut**, which is a one-line check over any capture.

**Ranking quality was never the mirror's problem, and the full capture makes that explicit.** On
these 48 rows its AUC is **0.9366** (Cohen's d 2.473) against the paid endpoint's **0.934** (d
2.444) on the same labels (`node benchmark/analyze.js results-orfull-…-free.jsonl
results-ultra550.jsonl`). Same ranking quality, different scale: the free model separates AI from
human text as well as the paid one does and decides worse, because it is being read on a scale
calibrated for someone else. The 29-row capture had put that the other way round (AUC 0.897), which
is what a 19-text shortfall does to a rank statistic.

Two limits remain. The 0.56/0.45 band was *chosen* on all 48 rows, so its headline accuracy is
in-sample; the honest check is the stratified halves `calibrate.js` prints, and there the own band
(0.917 / 0.792) beats the inherited one (0.708 / 0.667) in **both** folds — a consistent direction
on 24 texts each, not a validated number. And `ling`'s row is still 31 of 48 with 17 answers that
carried no score at all, so its band is a partial-capture measurement, not a finished one.

**Written to the database: `models` row 4 now stores its own band** (0.56 / 0.45, via
`PATCH /models/4`), with the provenance in its `notes` (n=48, acc 0.854, review 0.083, the
inherited-band comparison, and that the confidence cuts were left at 0.6/0.6 because confidence
measured as noise rather than signal). It stays `lane = null`, so nothing in the default pipeline reads it yet — but a stored band is not
inert: `tuning_block()` is called with the row of *whichever model the request actually names*
(the explicit-`lane_config` branch of `detect()` in `backend/app.py`), so a `lane_config` override that puts the mirror in the primary seat ships
0.56/0.45 with it. That is pinned by
`test_lane_config_naming_a_stored_model_still_carries_that_models_tuning`, not by reading the
function. Two things this does **not** fix, and one flag that misleads:

- **`secondary_decision_threshold` is the one cut taken from the *secondary* row, and on row 4 it is
  `null`.** `tuning_block()` then omits the key entirely, so the workflow's baked 0.90 stands — which
  is precisely the configuration measured above as 40 correct / 8 wrong with six real AI texts turned
  into confident `TIE_BREAK_HUMAN`. Leaving it null is deliberate: the 0.65 that would fix it is the
  knife-edge cut this document rejects (its tightest margin is 0.000). A null here means "use the
  default", *not* "this model has no opinion" — `ling`'s row stores 0.75 to avoid the same trap.
- **Rows 3, 5 and 6 still carry the paid pair's 0.69/0.40** with `measured = true`, so the flag is not
  a provenance signal: row 7 is `measured = true` with all four cuts `null`, rows 3/5/6 are
  `measured = true` with someone else's band. Read the values, never the flag.

## The Gutenberg control: do the shipped cuts over-flag real prose?

`benchmark/corpus-books.json` is 46 excerpts of published human prose captured from Project
Gutenberg (`benchmark/build-book-corpus.js`, checked by `benchmark/verify-book-corpus.js`; every
text is HUMAN by construction, so this corpus can only measure false positives, never
sensitivity). Run 2026-10-06, tags `bookultra550` / `bookdiffgemma`, imported under corpus
`gutenberg-books`; reproduce with `node benchmark/analyze-books.js`.

| model | usable | median p(AI) | worst | <= 0.40 | in band | >= 0.69 |
|---|---|---|---|---|---|---|
| nvidia/nemotron-3-ultra-550b-a55b (shipped primary) | 46/46 | 0.045 | 0.18 | 46 | 0 | 0 |
| google/diffusiongemma-26b-a4b-it (shipped tie-break) | 29/46 | 0.020 | 0.08 | 29 | 0 | 0 |

Zero false AI verdicts, and zero deferrals -- the primary did not just stay under the AI cut,
it never came within 0.22 of the HUMAN cut. Latency at concurrency 3: p50 13.3 s, p90 25.7 s,
max 37.0 s.

Read this against the live-run problem recorded in `docs/calibration.md`: there, human texts
from HC3 drifted to 0.55-0.75 on the same model at the same parameters. The drift does not
reproduce on book prose. So the false-positive exposure is **register-specific** -- informal
forum writing with typos and spacing artefacts -- not a general failure of the 0.40/0.69 cuts.
Two consequences: raising the HUMAN cut to protect literary prose would be solving a problem
that does not exist, and any future false-positive claim must say which register it was
measured in, because the answer differs by genre.

A count of 0-of-46 bounds the rate only weakly (95% upper bound near 6%). The margin between
the worst single score (0.18) and the cut (0.40) is the stronger evidence.

## Could a free model carry the tie-break lane?

The shipped primary is a paid NVIDIA endpoint. The tie-break lane is consulted on ~10% of texts,
so a free secondary is the cheapest possible way to cut cost. Measured by replaying real captures
through the shipped Code node
(`node n8n/test-ensemble.js replay benchmark/results-ultra550.jsonl <secondary> [--secondary-threshold c]`).
The answer is still no, but the reason changed on 2026-10-07 and the old reason was wrong: this
section used to reject the free mirror on *availability*, on a capture that was missing a third of
its rows because the harness had not retried an in-body 503. With the complete 48-row capture
below, the mirror answers every deferral — the shortfall is quality and stability, which is a
weaker and more honest objection than the one it replaced.

| secondary lane | cut | correct | wrong | review | TIE_BREAK_AI calls |
|---|---|---|---|---|---|
| `google/diffusiongemma-26b-a4b-it` (shipped, paid) | 0.90 | 45 | 3 | 0 | 7 |
| `inclusionai/ling-3.0-flash-sante:free` (free) | 0.90 | 38 | 7 | 3 | 0 |
| `inclusionai/ling-3.0-flash-sante:free` (free) | 0.75 | 42 | 3 | 3 | 4 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` (free mirror, 29-row capture) | 0.90 | 37 | 6 | 5 | 0 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` (free mirror, **48-row capture**) | 0.90 | 40 | 8 | 0 | 0 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` (free mirror, 48-row capture) | 0.75 | 43 | 5 | 0 | 5 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` (free mirror, 48-row capture) | 0.65 | 44 | 4 | 0 | 8 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` (free mirror, 48-row capture) | 0.55 | 43 | 5 | 0 | 9 |

The mirror's rows are the interesting ones, and the first thing they say is that its earlier
shortfall was not a scale problem at all: on the 29-row capture (last row group) the primary
deferred on 10 texts and the mirror had no usable answer for 5 of them, which is why that run could
never pass 40 and why 5 reviews appeared at every cut. On the complete capture the primary defers on
the same 10 texts (its own capture is unchanged), the mirror answers all 10, and there are **0
reviews at every cut**. Availability was the harness.

What remains is the shape of the errors. At the shipped 0.90 cut, the mirror buys the lane's 10
decisions and spends **6 real AI texts on them**: `ai-000 0.88, ai-008 0.65, ai-012 0.82, ai-014
0.82, ai-015 0.75, ai-018 0.65` all come back as confident `TIE_BREAK_HUMAN`, and the lane makes
**zero** `TIE_BREAK_AI` calls — 40 correct, 8 wrong. Lowering the cut to 0.65 recovers all six and
moves the lane's errors onto the other side: `hu-004` (0.82 — the shipped lane gets this one wrong
too) and `hu-005` (0.65). That is 44 correct / 4 wrong against the shipped lane's 45 / 3, and the
*entire* margin between the free lane and the paid one at their respective best cuts is one text —
`hu-005`, whose score equals the cut it is judged against.

Two things this measures, neither of them a threshold problem:

1. **The tie-break rule is a one-sided test, so a low-scoring lane silently becomes a human
   detector.** `secondaryVerdict(prob) = prob >= SECONDARY_DECISION_THRESHOLD ? AI : HUMAN` —
   there is no floor. A secondary at 0.89 and one at 0.05 both produce `TIE_BREAK_HUMAN`. So the
   0.90 cut is only meaningful for a model whose AI mass sits *above* 0.90; `ling` scores its AI
   median at 0.82, and at the shipped cut the rule converted five real AI texts into confident
   HUMAN verdicts (38 correct, 7 wrong) — and the mirror, whose AI mass is even lower, converts
   **six** of ten (40 correct, 8 wrong, zero `TIE_BREAK_AI` calls). **`SECONDARY_DECISION_THRESHOLD`
   is a property of the secondary's distribution, not a global constant** — the shipped value is
   calibrated to diffusiongemma and silently wrong for anything else.
2. **`ling` at its own best cut still loses 3 texts and adds 3 reviews.** Lowering to 0.75 recovers
   the 4 wrong calls but still cannot reach 45, and the sweep is not monotone (0.90 → 38, 0.75 → 42,
   0.65 → 40, 0.55 → 41): the rule reads one number against one cut, so a lane that spreads its AI
   scores across the cut flips texts back and forth as the cut moves. The 3 extra reviews are
   `SINGLE_MODEL_PRIMARY_UNCERTAIN` — the primary deferred *and* the secondary had nothing to say,
   which is the reliability cost of a lane that answered only 31 of 48 texts (17 replies carried no
   parseable probability).
3. **The mirror's 44 exists only at a single point on the number line.** Its full sweep is 0.90 → 40,
   0.80 → 42, 0.75 → 43, **0.65 → 44**, 0.70 → 43, 0.66 → 43, 0.55 → 43. Three of its ten tie-break
   scores are exactly 0.65 (`ai-008`, `ai-018` correct; `hu-005` wrong), and the rule is `>=`, so the
   peak is a knife edge: move the cut by 0.01 and the lane's best answer to the corpus's hardest
   texts changes. A "best cut" that is the modal score of the texts it is meant to separate is not a
   calibrated threshold — it is the corpus being fit, spelled out.

Treat both free totals as optimistic: each cut was chosen on the same 10 deferred texts it is then
scored against, and the noise floor on this corpus is ±1 text. The honest reading is "a free
secondary needs a per-lane cut before it is comparable at all, `ling` is worth 3 fewer correct calls
and 3 extra reviews, and the mirror gets within one text of the paid lane only by parking its cut
under three scores".

**The margins say it more sharply than the totals** (`node benchmark/band-margins.js ultra550 <sec> 0.69 0.40 <cut>`,
which lists every tie-break decision with its distance from the cut it was read against):

| lane | tie-break scores on AI text | on HUMAN text | tightest correct call |
|---|---|---|---|
| diffusiongemma @ 0.90 | 0.98 ×6 | 0.85 ×3, 0.94 ×1 (the one wrong) | 0.05 |
| `ling…:free` @ 0.75 | 0.78, 0.80, 0.82, 0.85 | 0.65 ×2, 0.60 (the one wrong) | **0.03** |
| `…550b-a55b:free` @ 0.65 | 0.88, 0.82 ×2, 0.75, 0.65 ×2 | 0.82, 0.65 (the two wrong), 0.55, 0.35 | **0.000** |

The shipped lane separates 0.85 → 0.98, so a cut anywhere in that 0.13 gap makes the same calls;
3 of its 48 decisions sit within 0.02 of their cut and none of those are wrong. `ling`'s entire AI
mass sits between 0.78 and 0.85 and its nearest human answer is 0.65, so its correct AI calls clear
the cut by 0.03-0.10. The mirror's peak is worse still: 6 of 48 decisions within 0.02, one of them
wrong, and three tie-break calls at distance exactly 0. Run-to-run jitter on this pipeline has been
measured far larger than 0.03 on borderline text, which means the recovered texts are not a stable
gain. **Do not promote a free secondary on the strength of a 44-vs-45 total, and especially not on a
cut whose margin is 0.000.**

## Does a free model work as a live lane, not just a capture?

A capture proves a model can score a corpus; it does not prove the shipped stack can judge a text
through a free endpoint. `node tools/verify-free-lane.js` does that: it sends two known-label corpus
texts to the real `POST /detect`, overriding only the primary lane to
`nvidia/nemotron-3-ultra-550b-a55b:free` on OpenRouter (BYOK header from `.env`, never printed), and
leaves the tie-break seat to the workflow's own paid default. Run twice at 00:33Z and 00:36Z on
2026-10-07, both runs returned a verdict for both texts and both classifications were right:

| run | text | verdict | p | method | deciding lane |
|---|---|---|---|---|---|
| 1 | `ai-000` (AI) | AI_GENERATED | 0.98 | TIE_BREAK_AI | secondary |
| 1 | `hu-000` (HUMAN) | HUMAN_LIKELY | 0.35 | SINGLE_MODEL_PRIMARY_HUMAN | primary |
| 2 | `ai-000` (AI) | AI_GENERATED | 0.98 | TIE_BREAK_AI | secondary |
| 2 | `hu-000` (HUMAN) | HUMAN_LIKELY | 0.85 | TIE_BREAK_HUMAN | secondary |

Two things fall out of the middle pair, and they are the interesting part. **The free mirror moved
across its own decision band between two identical requests three minutes apart** — `hu-000` scored
0.35 (decided HUMAN alone, no second call bought) then something inside 0.40–0.69 (deferred to the
tie-break). That is the run-to-run jitter measured elsewhere on this pipeline, showing up on the
first texts tried, and it means a free primary's *lane* is as unstable as its score. Second, the
tie-break answered HUMAN at **0.85 with a reported confidence of 0.92**, which is the presentation
defect recorded in the calibration notes, live: the tie-break cut has no floor, so anything under
0.90 is read as human, and the confidence shown is the lane's own claim rather than its distance
from the cut. The verdict happened to be right here.

## The pair this ships

- primary: `nvidia/nemotron-3-ultra-550b-a55b` (temperature 0.1, top_p 0.9)
- secondary, tie-break only: `google/diffusiongemma-26b-a4b-it` (same params), consulted only where
  the primary lands in 0.40–0.69, and only believed at ≥ 0.90 on its own scale.

Both lanes ran at `max_tokens 700` until 2026-10-16, when the cap was removed as a matter of policy
rather than tuned: a capped answer that stops mid-JSON cannot report its `phrases`, and phrase
evidence is what the UI now paints. `max_tokens 0` (the default in `backend/db.py`, `backend/seed.py`
and the workflow) sends **no `max_tokens` field at all**, and the response echoes
`sampling.max_tokens: "auto"`. Every `models` row was flipped to 0. The bands below were fit under the
old caps, so they are the thing to re-measure first — see the Round 15 note at the end.

Offline replay over the 48 texts (`n8n/test-ensemble.js`): 45 correct / 3 wrong / 0 deferred,
0 false negatives.

Live through the webhook, the same build scored **41/48** with 6 false positives, 0 false
negatives and 1 review (see `docs/calibration.md`). The gap is structural, not noise: the replay
was captured on a run where the primary decided some human-leaning texts by itself, so the
tie-break never saw them. A secondary calibrated on a strong primary run is untested against a
drifting primary run. Raising the tie-break cut to 0.95 was measured and rejected.

**Round 8 puts a number next to this one, and it is uncomfortable.** A single free model,
`z-ai/glm-5.3`, scores **45 correct / 0 wrong / 3 deferred** over the same 48 texts under the same
shipped band — the same 0.938 corpus accuracy as the two-model paid pair's offline replay, with no
wrong calls at all instead of three, and no secondary lane, no tie-break cut and no confidence gates.
The pair still wins on one axis: it defers nothing, so it produces a verdict for all 48 texts where
glm-5.3 produces 45. Three honest caveats before anyone acts on this. (1) The pair's number is an
offline replay and its *live* number is 41/48, so the 45-vs-45 comparison is replay-vs-replay.
(2) glm-5.3's is in-sample on the corpus every band in this document was fitted on. (3) The pair is
the only configuration that has ever been verified end-to-end through the n8n webhook. Nothing was
changed as a result; this is recorded so the next reader does not have to rediscover it.

## Retrying the non-responders, and what tuning them exposed (2026-10-09)

The models that had never produced usable answers were retried, the ones that answered were
measured on the full corpus, and the one that beat the defaults had its band stored. The part worth
reading twice is the defect the live verification caught: a stored tuned row could not actually be
reached from a model-only request.

### Round 6: cause-triage at 2 texts per model

A 48-text run is the wrong instrument for "does this model answer at all". `benchmark/free-round7.sh`'s
predecessor `free-round6.sh` gated on headroom, then probed each candidate with the 2 real texts in
`benchmark/corpus-probe2.json` (sliced from the HC3 screen corpus) at concurrency 1, and split the
outcome by cause. Log: `benchmark/free-round6.log`.

| Model | Round-6 probe | Cause |
|---|---|---|
| `inclusionai/ling-3.1-flash` | 2/2 answered, 5 s, clean | free successor to the retired `ling-3.0-flash-sante:free` |
| `dots-studio/dots-3-note-preview:free` | 2/2 answered, 17–21 s, 1 truncated, AI 0.65 vs HUMAN 0.68 | format fixed by the token cap, judgement is not |
| `nvidia/nemotron-3.5-lightning:free` | 2/2 answered, 30 s and 44 s, human row salvaged | too slow and too fragile for a live lane |
| `nvidia/nemotron-3.5-content-safety:free` | 0/2 `no_json_object` in 3 s | model-side: never emits the schema |
| `google/gemma-4-26b-a4b-it:free` | 0/2 `http_429` at 300–450 ms | per-model upstream pool refuses before a worker |
| `google/gemma-4-31b-it:free` | 0/2 `http_429` at ~300 ms | same |

The 429 timings are the whole argument: a refusal in ~300 ms on the third attempt is the router
declining, not an exhausted account, and the headroom gate that ran minutes earlier returned
`200 usable` on all three keys (`tools/probe-or-headroom.js` sends an 8-token "OK" completion, so a
cap would have shown up as `free-models-per-day` instead). All five models probed here besides `ling`
are registered with `measured = false` and `lane = null` (rows 8, 9, 11, 12, 13), and their notes say
*do not assign a lane* — a row with no numbers is the honest state, not a measurement to be filled in
later.

These probe tags (`probe-*`) are deliberately **not** imported into `/benchmark`: two texts cannot
rank a model, and importing them would put a 2/2 "yield" next to a real 48-text table.

### Round 7: the survivor on the full corpus

`bash benchmark/free-round7.sh` → tag `orfull-ling-3-1-flash`, `corpus.json` (48 texts, hc3-48),
`max_tokens 1400`, concurrency 2, `--tries 3 --wait_ms 10000`. Result: **46/48 scored** (AI 23 /
HUMAN 23), the 2 misses `http_429` refusals rather than format failures, **0 salvaged, 0 truncated**,
p50 4.1 s. Median AI 0.850 vs median HUMAN 0.300 — separation 0.550, the widest of any *free* model on
this corpus, and just under the paid 550B's own 0.565.

`benchmark/store-band.js` fits a band and scores it through the **shipped** decision logic
(`n8n/test-ensemble.js`), not a reimplementation, then refuses to store a band whose tightest margin
is under 0.03 unless `--force` is passed:

```
SHIPPED default   ai>=0.69 hu<=0.40 conf>=0.6/0.6 => acc=0.739 review=11 [tp20 tn14 fp1 fn0]
FITTED band       ai>=0.63 hu<=0.52 conf>=0/0     => acc=0.870 review=5  [tp21 tn19 fp1 fn0]
MARGIN min |score - cut| = 0.020  (1 row within 0.03)
FOLD 1 (n=24) fitted 21 vs shipped 20 | FOLD 2 (n=22) fitted 19 vs shipped 14
```

Stored on `models` id 10 with `--force`, and the 0.020 margin is written into the row's own `notes`
so the number cannot be read without its caveat.

One reconciliation, because two tools quote two numbers for the same rows: the ranking table above
scores this capture at **0.783** and `store-band.js` reports the shipped default at **0.739**. The
table applies the probability cuts only; the store-band line applies the cuts *and* the shipped 0.6
confidence gates, which demote 2 otherwise-correct rows to review. Re-running the fitter with
`--aic 0 --huc 0` reproduces 0.783/9 exactly, so the difference is the gate, not a disagreement
between tools. Row 10's notes carry both figures.

It stays `lane = null`. The 0.870 is not comparable to row 1's published 0.934 (that figure is the
two-lane ensemble measured on its own capture; this one is a single lane, in-sample), and its closest
free comparison -- row 4, 0.854 on 48 rows -- is a different subset, so promoting it would be a claim
neither number supports. It is a selectable model with a published operating point, not a default lane.

### The live check found the band was unreachable

That verification is now a kept tool, `tools/verify-stored-band.js`, because the classification alone
was what hid the defect. It names a stored model with no key of its own and asserts four things from
the workflow's own echo: the row's cuts, the row's provider endpoint, `key_source = header`, and that
the *named* lane both answered and decided. Exit 0 only if all four hold.

The first attempt was a scratch script rather than this tool, and it split cleanly in two:

- The workflow's own `config_used` echo proved the **parameters** ship: `thresholds.ai_prob = 0.63`,
  `human_prob = 0.52`, `tuning_applied.max_tokens = 1400`, `primary.key_source = "header"` (the
  stored OpenRouter key, never the browser's).
- But the **endpoint** did not follow the model. `base_url_source` said `"env"` and the URL was
  `https://integrate.api.nvidia.com/v1` — the workflow's baked default — while the model id was
  `inclusionai/ling-3.1-flash`. OpenRouter models were being POSTed at NVIDIA with an OpenRouter
  token, and the gateway's answer was the plain Go text body `404 page not found`, recorded as
  `error_code: no_choices`. The tie-break then produced a correct-looking verdict from the secondary
  lane, so the failure was invisible in the classification.
- The catalogue was checked first (`GET /api/v1/models`, free): the id is listed, free, 262k context
  — so the 404 was the wrong host, not a retired model.

The gap was in the `lane_config` branch of `/detect`: it resolved the stored model's key and tuning
but never its provider endpoint. Fixed in `backend/app.py` — a lane that names a stored model now
takes that model's provider `base_url`, an explicit caller-supplied `base_url` still wins, and a
DB-derived host passes the same `allowed_llm_hosts` gate the default path uses (three tests,
47 passing). The UI is unaffected by design: its host field is pre-filled from the provider preset,
so it always sends a URL.

Re-verified live after the fix. First with a scratch script (same shape as the tool, before I promoted
it), then with the tool itself a few minutes later:

```
scratch check, after the fix                    tools/verify-stored-band.js, minutes later
  ai-000 (AI)     0.72 -> AI_GENERATED  [pri]     ai-000 (AI)     0.98 -> AI_GENERATED  [sec]
  hu-000 (HUMAN)  0.35 -> HUMAN_LIKELY  [pri]         FAIL named lane answered: ETIMEDOUT 104.18.2.115:443
                                                  hu-000 (HUMAN)  0.22 -> HUMAN_LIKELY  [pri]  all pass
```

In the scratch run both texts then decided on the named lane, which is the part the classification
could not show. The tool's own run is the more instructive one: its `ai-000` primary lane hit
`ETIMEDOUT connect 104.18.2.115:443` -- the Cloudflare anycast address this host cannot complete TCP
to (see `docs/rate-limiting.md`) -- and the tie-break again produced a plausible `AI_GENERATED`, this
time from the paid secondary, so the tool exited 1 where a glance at the verdict would have shrugged.
Note also `hu-000` at 0.35 then 0.22, and `ai-000` at 0.72 then a lane that never answered: this is
the run-to-run drift already measured on this pipeline, not a configuration change. A `NOT OK` from
this tool can therefore mean "the row is misconfigured" or "the link dropped this call" -- read the
`error_detail` before touching anything. Both scores that did land sit in the region where the stored
band and the shipped 0.69/0.40 agree, so what these calls prove is the *plumbing*; the accuracy gain
remains the 46-row offline measurement.

### n8n took 20 minutes to accept a webhook

`POST /detect` returned 502 (`n8n unreachable`) even though the process was up and `/healthz`
eventually answered 200 in 7.7 s. Boot is serialised behind outbound calls to `api.n8n.io` that time
out at 6 s each on this host (`Failed to refresh MCP registry`, `instance-ai` relay), and
`Added webhooks for workflow "AI Text Detection (AI-Only…)"` only lands at the end of that sequence
(whole boot: `benchmark/n8n-boot-debug.log`, taken with `N8N_LOG_LEVEL=debug`).
The observable signature is the webhook returning `Cannot POST /webhook/ai-detect-ai` (404) until
registration, then 400 once the node's own validation applies. Poll for the webhook, not for
`/healthz` and not for the port.

## Round 8: looking for free models outside the two gateways already wired up (2026-10-10)

Asked to find more free models "in or outside OmniRoute". Five routes were checked; one produced
three models that now outrank the paid primary on this corpus, plus a fourth that was measured and
rejected. The other four routes are recorded because a dead end with a cause is worth as much as a
hit — it stops the next round paying for the same answer.

| route | credential needed | outcome |
|---|---|---|
| OmniRoute (self-hosted router) | none to install, but BYO provider accounts | **retracted in round 9 — it was installed all along, with 15 accounts already connected, and it yielded the best single-lane accuracy measured in this project. See [Round 9](#round-9-omniroute-was-here-the-whole-time-2026-10-11).** |
| OpenRouter `:free` catalogue | the 3 stored keys | exhausted, and shrinking: **15** free ids in a 458-model catalogue as of 2026-10-10, every one now probed |
| OpenRouter router ids (`openrouter/free`, `/auto`, `/fusion`, …) | the same keys | one measured (`openrouter/free`), negative — a router cannot be attributed; the rest not spent on, same objection |
| NVIDIA NIM catalogue | the stored NVIDIA key | **the productive one**: 80 entries, 13 answer HTTP 200 — 12 scored on probe, 1 only after the salvager — and 3 had never been benchmarked until this round |
| Cerebras / Groq / GitHub Models / HuggingFace / Together / Mistral / Google AI Studio | a new key each | not attempted: no credential exists on this host, and getting one is an identity flow, not a measurement |

### OmniRoute is a router, not a source of models — RETRACTED, see Round 9

> **This whole subsection was wrong and is kept only as a record of how it was wrong.** Both of its
> claims failed against live evidence on 2026-10-11; the correction is
> [Round 9](#round-9-omniroute-was-here-the-whole-time-2026-10-11).

The original text claimed two things:

1. **"It is not running. Not installed on this host and the Docker daemon is down."** False. It is
   installed as a **global npm package** (`omniroute@3.8.50`, `C:\nvm4w\nodejs\node_modules\omniroute`)
   with a populated data directory at `~/.omniroute` (43 MB `storage.sqlite`, 24 MB `app.log`,
   20 prior boots). Docker was never involved — the Docker check was looking for the wrong artifact,
   and "the daemon is down" was treated as evidence about a Node CLI that does not need it.
2. **"Even running, it adds no model… the binding constraint is credentials, not discovery."** False
   in the way that matters. The credentials **were already on this host**: `provider_connections`
   holds **15 connected accounts** (kiro, gemini, gemini-web, mistral, moonshot, nvidia, opencode,
   qoder, qwen-web, vertex, bluesminds, cerebras, lmarena, llm7, devin-cli). Several are **OAuth or
   browser-session logins, not API keys** — `kiro` is an AWS Kiro social login whose token OmniRoute
   refreshes itself. Those cannot be obtained by signing up for an API, so "hold the same credential
   and call the provider directly" was not available advice.

The generalisation the round drew from this — that the free surface on this host is exactly the two
gateways with a stored key — was therefore also wrong. The correct lesson is narrower and more
useful: **before declaring a source unavailable, check for an existing installation and an existing
credential store, not only for a running process.** `curl` returning `000` says the port is closed,
not that the software is absent.

### The OpenRouter free pool is not a fixed list, and it is shrinking

`GET https://openrouter.ai/api/v1/models` needs no key, so the whole catalogue is countable. On
2026-10-10 it lists **458 models, of which 15 end in `:free`**
(`benchmark/openrouter-models-2026-10-10.json`, 766 KB, kept as the evidence for the count). The
2026-10-06 pass probed 16 `:free` ids. The difference is `inclusionai/ling-3.0-flash-sante:free`,
which this document already records as having gone paid-only — so a free id can leave the pool
between two runs, and a "the free tier has N models" statement has a date attached whether or not
one is written down.

The same listing exposes six *router* ids that are not model ids at all: `openrouter/free`,
`openrouter/auto`, `openrouter/auto-beta`, `openrouter/fusion`, `openrouter/pareto-code`,
`openrouter/bodybuilder`. Only `openrouter/free` was measured (below, negative). The other five were
deliberately not spent on: they share the exact defect that made `openrouter/free` unmeasurable — the
gateway chooses the upstream, so a score cannot be attributed to a model — and `auto`/`fusion` are
documented as routing toward *paid* models, which is outside a free-model survey in the first place.

### A 30-second probe timeout was hiding three frontier-class free models

The 2026-10-06 catalogue pass (`benchmark/catalog-probe-nvidia.json`) probed all 80 NVIDIA entries
at a 30 s timeout and bucketed them: 58 `UNEXPECTED_BODY`, 9 `SCORED`, 8 `TIMEOUT`, 3 `NO_JSON`,
1 `REASONING_PREAMBLE`, 1 `PROVIDER_ERROR`. The 58 are not recoverable — they answer
`404 {"status":404,"title":"Not Found","detail":"Function '<uuid>'…"}`, which is a catalogue entry
this account is not entitled to, plus a handful of bare `404 page not found` from embedding / CLIP /
parse endpoints that are not chat completions at all.

The 8 `TIMEOUT`s were re-probed at **150 s** (`benchmark/catalog-probe-nvidia-2026-10-10.json`,
log `benchmark/nvidia-reprobe-2026-10-10.log`). Three answered:

| model | http | latency | verdict |
|---|---|---|---|
| `google/gemma-4-31b-it` | 200 | 13.98 s | SCORED, p=0.98 finish=stop |
| `z-ai/glm-5.3` | 200 | 14.39 s | SCORED, p=0.88 finish=stop |
| `z-ai/glm-5.3-flash` | 200 | 23.61 s | SCORED, p=0.92 finish=stop |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | 200 | 11.46 s | REASONING_PREAMBLE, finish=length |
| `deepseek-ai/deepseek-v4.1-flash` | — | 150.0 s | TIMEOUT (row 25) |
| `moonshotai/kimi-k3` | — | 150.0 s | TIMEOUT (row 26) |
| `meta/llama-3.2-90b-vision-instruct` | — | 150.0 s | TIMEOUT (row 27) |
| `nvidia/llama-3.1-nemoguard-8b-content-safety` | — | 150.0 s | TIMEOUT (row 28) |
| `nvidia/llama-3.1-nemotron-safety-guard-8b-v3` | — | 150.0 s | TIMEOUT (row 29) |

The lesson is in the first three latencies: 13.98 s, 14.39 s, 23.61 s. Two of the three would have
*fit* inside 30 s. They did not, because a cold NIM worker spends time before the first token that
the probe was charging to the model. **A probe timeout measures worker spin-up, not model speed** —
so a short timeout does not produce a conservative shortlist, it produces a shortlist biased against
exactly the large models worth having. All five remaining TIMEOUTs are documented on `models` rows
25–29; two of them are guard/classifier models whose job is a policy verdict, so even a clean answer
would not be an `ai_probability`.

### What the four recovered NVIDIA models scored

All four captured over the full 48-text HC3 corpus at `max_tokens 1400`, temperature 0.1, top_p 0.9,
concurrency 2, logs `benchmark/nvfree-round8.log`, captures `benchmark/results-nvfull-*.jsonl`:

| model | usable | gateway fail | model fail | trunc | salv | AUC | Cohen's d | sep | acc / corpus @ 0.69/0.40 | wrong | deferred | latency p50/p90/max |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `z-ai/glm-5.3` | **48/48** | 0 | 0 | 0 | 0 | **1.000** | 5.83 | 0.795 | **0.938** | **0** | 3 | 17.0 / 39.9 / 77.6 s |
| `z-ai/glm-5.3-flash` | **48/48** | 0 | 0 | 0 | 0 | **1.000** | 5.88 | 0.720 | 0.938 | 1 | 2 | 7.0 / 18.4 / 36.7 s |
| `google/gemma-4-31b-it` | **48/48** | 0 | 0 | 0 | 0 | 0.945 | 2.26 | **0.830** | 0.854 | 7 | **0** | 12.1 / 28.5 / 78.7 s |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | 23/48 | 0 | 25 | 8 | 8 | 0.640 | 0.55 | 0.095 | 0.188 | 9 | 5 | 31.9 / 48.0 / 66.9 s |

For scale, the paid primary `nvidia/nemotron-3-ultra-550b-a55b` (tag `ultra550`) sits at
separation 0.565, acc / corpus 0.750, 2 wrong, 10 deferred.

### `z-ai/glm-5.3` is the best detector this project has measured, and it is free

48/48 usable with **zero failures of any kind** — no gateway failure, no model-side parse failure,
no truncation, no salvaged score. AUC 1.000, Cohen's d 5.83.

The score distributions do not overlap at all:

```
AI     0.72 0.82 0.85 0.88 0.88 0.88 0.88 0.92 0.92 0.92 0.92 0.93 … 0.97 0.97
HUMAN  0.02 0.05 0.05 0.08 0.08 0.08 0.08 0.10 0.10 0.10 0.12 … 0.45 0.62 0.65
```

Highest human score 0.65, lowest AI score 0.72. Every threshold in the open interval (0.65, 0.72)
classifies all 48 texts correctly. At the shipped band 0.69/0.40 with the confidence gates off:
acc 0.938 (45/48), precision 1.000, AI recall 1.00, HUMAN specificity 1.00, **0 wrong**, 3 deferred
(the human texts at 0.45, 0.62, 0.65, which fall inside the band rather than on the wrong side of
it). Tightest deciding margin 0.030. Fold 1 better than shipped, fold 2 tie. Stored on `models`
row 22, `lane = null`.

Paired against the two models it would displace (`benchmark/paired-gap.js`, one decision rule applied
to both sides, McNemar exact two-sided on the discordant pairs only):

- vs `google/gemma-4-31b-it`, 48 shared texts: 41 both right, 0 both wrong, **4 glm right / gemma
  wrong, 0 the other way**, p = 0.125.
- vs `nvidia/nemotron-3-ultra-550b-a55b`, 48 shared texts: 35 both right, 0 both wrong, **1 glm
  right / 550B wrong, 0 the other way**, p = 1.00.

Read the direction, not the p-value. The test is underpowered *because glm never errs* — there is no
discordant pair available against it, so no 48-text corpus can make it significant. That is a
property of the model, not evidence of equivalence.

**The caveat that must travel with the number.** This is in-sample on 48 texts, and this pipeline is
measured to flip 8 of 12 borderline texts between identical runs. glm's tightest deciding margin is
0.030 (an AI text at 0.72 against a 0.69 cut), so a re-run can move that text into the band. What it
cannot plausibly do is turn it into a *wrong* call: the nearest opposite-class score is 0.65, four
times further away than the margin. The failure mode of jitter here is a deferral, not an error —
which is the graceful one, and the reason a wide band is worth more than a fitted one.

### `z-ai/glm-5.3-flash`: same AUC, narrower gap, and a rejected band worth recording

Also 48/48 with zero failures. AUC 1.000, Cohen's d 5.884, separation 0.720. AI scores span
0.80–0.97, human 0.07–0.75 — a clean but only 0.05-wide empty gap, narrower than the non-flash
model's 0.07. At the shipped band: acc 0.938 (45/48), **1 wrong** (a human text at 0.75), 2 deferred,
margin 0.050, fold 1 better and fold 2 tie. Stored on row 23, `lane = null`.

A wider band was measured and **rejected on purpose**: `--ai 0.77 --human 0.55` scores acc **0.979**,
precision 1.000, 1 deferral, and wins *both* folds in-sample. It was not stored because its margin is
0.030 sitting inside a gap only 0.05 wide, estimated from 48 points — precisely the knife-edge
`store-band.js`'s MARGIN check exists to catch, on a pipeline with measured run-to-run flips. The
0.979 is written down here so that the rejection is not re-litigated later by someone who sees the
number and not the reason.

Between the two, prefer `glm-5.3`: identical AUC, wider natural gap, zero errors at the shipped band.
The flash variant is 2.4× faster at p50 (7.0 s vs 17.0 s) and has the better tail (max 36.7 s vs
77.6 s), so it is the one to reach for if latency ever binds — but not on accuracy grounds.

### `google/gemma-4-31b-it`: an old open question answered, and a coverage/precision trade

The previous version of this document asked, under *What is still unmeasured*, whether
`google/gemma-4-31b-it:free` could be captured at all; two OpenRouter attempts said no (26 texts
reached, 2 answered, 24 of the failures at `try10`). The answer is that the question was aimed at the
wrong gateway. OpenRouter's *free pool* for that id is genuinely saturated, but the same model is
served on the **NVIDIA free tier**, where it captured 48/48 with zero failures of any kind. A
per-model upstream saturation on one gateway says nothing about the model's availability on another.

Measured: AUC 0.9453, Cohen's d 2.259, separation **0.830** — the highest on hc3-48, which is why
`doc-table.js` sorts this row first. Its output is near-quantised:

```
AI     0.92 ×4  0.98 ×19  1.00 ×1
HUMAN  0.05 0.05 0.12 0.15 ×11 0.20 0.25 0.35 | 0.85 0.92 0.95 0.95 0.95 0.98
```

That second cluster is the whole error budget: **7 human texts at 0.85–0.98, all called AI**. There
is nothing in between — no human text lands in 0.36–0.84 — so the model either sees a text as human
decisively or is confidently wrong about it. Consequence: it is the only model measured that
**defers nothing** under the shipped band (0 of 48), at acc 0.854 = 41/48. Stored on row 21 with the
shipped 0.69/0.40 unchanged (a fitted 0.60/0.45 ties it exactly, margin 0.100, both folds tie),
`lane = null`.

Paired against the paid primary over all 48 shared texts, gemma and the 550B are statistically
indistinguishable (3 discordant pairs, p = 1.00) and are nevertheless different products:

| | decides | errs | deferred | acc / corpus | acc / decided |
|---|---|---|---|---|---|
| `google/gemma-4-31b-it` | 48 | 7 | 0 | 0.854 | 0.854 |
| `nvidia/nemotron-3-ultra-550b-a55b` | 38 | 2 | 10 | 0.750 | 0.947 |

gemma maximises coverage; the 550B maximises precision on what it commits to. Which is better
depends on what a deferral costs downstream, and this document has no measurement of that cost.

**Separation is not accuracy.** `doc-table.js` sorts by separation and puts gemma (0.830) above
`glm-5.3` (0.795); by `acc / corpus` glm-5.3 wins 0.938 to 0.854. Both are true. Separation is the
distance between two medians and says nothing about how many texts land on the correct side of a cut —
gemma's median human score (0.150) is *lower* than glm's (0.135 is lower still, but gemma's is
dragged down by 17 confident texts while 7 others sit at 0.9+). When the two columns disagree, the
accuracy column is the one that describes deploying the model.

### `nvidia/nemotron-3.5-lightning-30b-a3b`: a reasoning model cannot carry this output contract

23/48 usable. **Zero gateway failures** — all 25 failures are model-side (24 `json_parse_failed`,
1 `missing_ai_probability`). It answers HTTP 200 and opens with `Here's a thinking process:`, then
spends the entire 1400-token budget on chain-of-thought so the JSON never arrives; 8 of its 23 scores
exist only because the salvager patched a cut-off reply, so its honest clean yield is 15/48. AUC
0.6402, Cohen's d 0.551, and the whole scale sits at 0.15–0.60 — **below the 0.69 AI cut**, so it can
never say AI at all without a re-fitted band. It is also the slowest model measured in this project:
p50 31.9 s, p90 48.0 s, max 66.9 s, 818 s wall clock for 48 texts. No band stored; nothing worth
fitting. The general rule: a reasoning-tuned model needs either a much larger `max_tokens` or a
prompt that forbids the preamble, and neither is free.

### `openrouter/free`: a router id cannot be benchmarked, whatever it scores

Naming `openrouter/free` means the *gateway* picks the model. Measured over 48 texts
(`orfull-openrouter-free`): 33/48 usable, and again **all 15 failures are model-side**
(6 `no_json_object`, 9 `json_parse_failed`) with zero truncation, zero salvage and zero cap-caused
failures — so 1400 tokens is plenty and the upstream simply breaks the output contract on ~31% of
texts. AUC 0.7426, Cohen's d 1.093, separation 0.170, acc / corpus 0.396. Its bias is toward calling
everything AI: median human score 0.68 sits *above* the shipped human cut of 0.40, so most real human
text is deferred or misread. All 3 deciding disagreements against `inclusionai/ling-3.1-flash` went
ling's way (McNemar p = 0.25 on 31 shared texts — indistinguishable, as usual at this corpus size).

The structural objection outranks the score. Because the gateway chooses the upstream, a number
measured today is not attributable to any model tomorrow: the same request can be served by a
different checkpoint with no config change on this side, and the capture would not record it. Stored
on row 20 as a negative result and as a warning against putting router ids in a benchmark harness.

### `liquid/lfm-2.5-2.6b:free`: a harness bug that made a live model look dead, then a model with nothing in it

It answered 400 on every request at both 1400 and 500 `max_tokens`. OpenRouter hides the real reason
in `error.metadata.raw` — which `benchmark/catalog-probe.js` captures and `run-benchmark.js`
truncates — and it reads: `The \`top_p\` parameter is not supported.` Isolated by varying one
parameter at a time (`benchmark/catalog-probe-lfm-diag.json`):

| request body | result |
|---|---|
| `temperature` only | 200, `{"ai_probability": 0.88, "confidence": 0.92}` |
| `temperature` + `top_p` | 400, `metadata.raw` = the message above |
| `temperature` + `top_p` + `max_tokens 1400` | 400, same |

`benchmark/run-benchmark.js` now accepts `--top_p null`, omits the parameter from the body, and writes
`top_p: null` into the manifest so `backend/seed.py` attribution stays truthful. Two conclusions, and
they are not the same conclusion:

1. **The workflow cannot serve it.** Both `Build-Primary-Request` and `Build-Secondary-Request` always
   emit `top_p: cfg.top_p`, and `Normalize-Input` defaults `cfg.top_p` to `num(env.LLM_TOP_P, 0.9)`.
   `tuning_block()` in the backend does omit NULL columns — but the Code node builds `cfg` from the
   environment and the request, not from that block, so **no stored row can express "send no top_p"**.
   Measurable, not servable.
2. **It would not be worth serving.** With `top_p` omitted it answered 47/48 in 2–4 s (p50 2.5 s, the
   fastest thing measured here) and scored **AUC 0.4801, Cohen's d −0.149** — a coin flip with a slight
   inversion. The 2-text probe that had shown AI 0.22 / human 0.78 was noise, not a usable inverted
   scale. Worth setting against `llama11v`'s separation of −0.500, which *is* real and reproduces over
   48 texts: an inverted small probe and an inverted 48-text measurement are different findings, and
   only the second one is a fact about the model.

Row 14 carries both conclusions. No band stored — there is no signal to fit a band to.

### Latency headroom against the workflow's own timeout

Every stored row carries `timeout_ms 90000`. The worst single text measured this round: glm-5.3 max
77.6 s, gemma max 78.7 s, glm-5.3-flash max 36.7 s, nemotron-lightning max 66.9 s. All inside 90 s —
but the two best models leave only ~12 s of headroom on their tails, and those captures ran at
concurrency 2, which inflates latency relative to the concurrency-1 path a single live request takes.
Nothing here says the timeout is wrong; it says the timeout is now a *load-bearing* number for the
first time, because the models worth using are the slow ones.

### What round 8 changes, and what it deliberately does not

Three free models now outrank the paid primary on this corpus, all on the NVIDIA lane, all at zero
cost, **none promoted**:

| models row | model | band | acc / corpus | wrong | deferred | lane |
|---|---|---|---|---|---|---|
| 22 | `z-ai/glm-5.3` | 0.69 / 0.40, conf gates off | 0.938 | 0 | 3 | null |
| 23 | `z-ai/glm-5.3-flash` | 0.69 / 0.40, conf gates off | 0.938 | 1 | 2 | null |
| 21 | `google/gemma-4-31b-it` | 0.69 / 0.40, conf gates off | 0.854 | 7 | 0 | null |

Storing a band is not inert — `tuning_block()` ships the row of whichever model a request names — so
these three are now *available* to a `lane_config` request that names them, and that is the whole of
what was changed. Promotion needs two things this round did not do:

- **a live webhook check**, and n8n has been down since the round-7 restart. Bringing it back costs
  ~20 min before the webhook registers (see *n8n took 20 minutes to accept a webhook* above), and the
  poll must be on the webhook, not on `/healthz`.
- **an out-of-sample capture.** The Gutenberg book corpus is the only second set of real captures on
  disk. Until glm-5.3's 0-wrong result is reproduced there, it is an in-sample measurement on the same
  48 texts that every other number in this document was fitted on.

One further risk specific to this route: NVIDIA entitlement is not stable. 58 of 80 catalogue entries
answer `404 Function '<uuid>' Not Found` — entitled to someone, not to this account — and nothing in
the response distinguishes "never entitled" from "withdrawn". A model that scores AUC 1.000 on a free
tier the operator does not control can stop existing between two requests, which is an argument for
keeping the paid primary configured even if a free model measures better.

## Round 9: OmniRoute was here the whole time (2026-10-11)

Round 8 wrote OmniRoute off as unavailable. The user, who owns the machine, said it was installed and
had many accounts connected. The user was right on both counts and the retraction is
[above](#omniroute-is-a-router-not-a-source-of-models--retracted-see-round-9). This section is what
the correction turned up: a router holding **15 connected accounts** and exposing **1608 models**, one
of which — reached through a free OAuth login rather than an API key — has the **highest corpus
accuracy measured anywhere in this project**.

### Finding it, and the boot mode that actually works

OmniRoute is a Node CLI installed as a **global npm package** (`omniroute` 3.8.50 → `bin/omniroute.mjs`
under the nvm-managed `node_modules`), with a populated data dir at `~/.omniroute`: a 43 MB
`storage.sqlite`, a 24 MB `logs/application/app.log` showing 20 prior boots, and a 129 KB `.env`.
Docker was never involved — `docker` being down was irrelevant, and round 8's `curl` returning
`http_code=000` said only that nothing was listening, not that nothing was installed.

Two ways to start it were tried, and they behave differently:

| command | port bound | serves | evidence |
|---|---|---|---|
| `omniroute serve --port 20128 --daemon` | yes (`0.0.0.0:20128`, plus a CLOSE_WAIT pile-up) | **no** | `/v1/models`, `/` and `/api/health` all `http=000`; `omniroute health` reports "Server not running"; `app.log` stops at *Embedded services bootstrap complete* with no `EmbedWsProxy` / `LiveWS` line, which every successful boot up to 2026-09-23 has |
| `omniroute serve --port 20128 --no-open --log --no-recovery` (foreground) | yes | **yes** | boots fully, `/v1/models` answers 401 without a token and 200 with one |

So daemon mode is the trap: a listening socket is not a serving process. Everything below was
measured against the foreground instance. It is still running that way — a foreground process, not a
service, so it dies with its shell.

Auth is OmniRoute's own: `/v1/models` returns 401 `invalid_api_key` without a Bearer token, and the
key comes from its `api_keys` table (6 rows, all 35 chars). Two project `.env` names were filled in
from it — `OMNIROUTE_BASE_URL` and `OMNIROUTE_API_KEY` — by appending with `printf` rather than
rewriting the file, and the value is never printed: the check reports `ACTIVE len=35 prefix='sk-055'`.

### The 15 accounts, and the two that cannot be read

`~/.omniroute/storage.sqlite` → `provider_connections`, re-read for this section by
`tools/omni-inventory.py` (read-only; prints key **lengths** and ciphertext flags, never values):

15 rows. 13 are `is_active=1`; `cerebras` and `llm7` are inactive. Two are **OAuth**, not API keys:
`kiro` (`auth_type=oauth`, 232-char access token, `token_expires_at=2026-10-10T23:49:03Z` — OmniRoute
refreshes it) and `devin-cli` (`oauth`, 8-char token stub). The other 13 are `auth_type=apikey`.

Two of those API-key rows are unreadable: `gemini` (151 chars) and `moonshot` (175 chars) both carry
the `enc` ciphertext prefix, while `STORAGE_ENCRYPTION_KEY` in `~/.omniroute/.env` is **present and
empty** (length 0; the CLI warns about it on every invocation). Those two accounts' credentials are
therefore undecryptable from outside OmniRoute, which is consistent with how both failed in the
screen below. This is also why round 8's advice — "hold the same credential and call the provider
directly" — was not merely wrong but not always available: `kiro` is a social login whose token the
router refreshes, and there is no key to copy out.

Cached catalogue (`key_value` namespace `syncedAvailableModels`): 12 entries, 369 model ids. The live
`/v1/models` answer is far larger — **1608 models across 36 id prefixes**, re-counted for this
section. The gap between 369 cached and 1608 live is exactly why "not available in the active live
catalog for provider X" shows up as a failure below: the synced list is stale relative to what the
account is actually entitled to.

### A per-prefix liveness screen, because 1608 is not 1608 usable models

One router multiplexes many accounts that fail independently, so a catalogue count means nothing
until each prefix is asked for a real completion. `benchmark/omniroute-providers.js` was written for
this: it copies the shipped detection prompt **verbatim** (so any score stays comparable with
`results-*.jsonl`), probes 3 models per prefix at concurrency 2 with a 90 s timeout, and grades each
answer `SCORED` / `EMPTY` / `REASONING_PREAMBLE` / `NO_JSON`. A prefix is `SERVABLE` if **any** probe
scored, otherwise it reports the first probe's verdict and detail.

Run: 103 probes, 1608 models in scope, `benchmark/omniroute-providers-all.json`, generated
2026-10-10T22:55:30Z.

**SERVABLE — 6 prefixes, 208 of 1608 models (12.9%):**

| prefix | listed | probed | scored | sample |
|---|---|---|---|---|
| `auto` | 38 | 3 | **3** | `auto/best-coding` — virtual routes, not a real account |
| `kiro` | 21 | 3 | 2 | `kiro/claude-sonnet-4.5` |
| `kr` | 21 | 3 | 2 | alias of `kiro`, same account |
| `no-think` | 77 | 3 | 1 | `no-think/kr/claude-sonnet-4.5` — a modifier wrapping another provider's id |
| `mistral` | 49 | 3 | 1 | `mistral/ministral-3b-latest` |
| `(bare)` | 2 | 2 | 1 | `Kimi Coding`, `cost-saver` |

**Blocked — 30 prefixes, 1400 models**, grouped by cause, because the causes have different remedies:

| cause | prefixes (listed) | first error, verbatim | remedy |
|---|---|---|---|
| image models on a chat endpoint | `aihorde` (185) | `400 Model 'aihorde/2DN' is an image-generation model and cannot be used on /v1/chat/completions. Use POST /v1/images/generations instead.` | none — wrong endpoint, out of scope for detection |
| missing local CLI bridge | `dv`+`devin-cli`+`dva` (375), `aug` (28), `zc` (13), `if`+`qoder` (18) | `502 spawn zcode ENOENT`; `'auggie' is not recognized as an internal or external command`; `'qodercli' is not recognized`; `500 DEVIN_AGENTIC_HOME must be an absolute path inside the bridge sandbox` | install the CLI each bridge shells out to, and set `DEVIN_AGENTIC_HOME`. Cheapest large unlock on the list |
| stale synced catalogue | `nvidia` (125), `oc`+`opencode` (187), `bm`+`bluesminds` (94), `gemini` (59), `moonshot` (4), `qwen-web` (4) | `400 Model 'z-ai/glm-5.2' is not available in the active live catalog for provider 'nvidia'.` | force a catalogue re-sync in OmniRoute. Note `nvidia` is the same account the paid primary uses successfully — so this is a sync gap, not a dead key |
| dead or expired credential | `lmarena`+`lma` (145), `vertex` (65) | `401 User not found`; `401 All 1 connection(s) authentication expired — please reconnect in the dashboard` | re-auth. `vertex` is an **identity flow** — a browser login the user must do, not something to automate |
| missing browser binary | `gemini-web`+`gweb` (7), `cfp` (20) | `503 Gemini Web requires the Playwright Chromium browser, which is not installed`; `502 browserType.launch: Executable doesn't exist` | install Playwright Chromium — one command, unlocks 27 models |
| paid or blocked upstream | `opencode` (402 `This model requires an opencode API key`), `tllm` (403 `The Old LLM is blocked by Vercel for this server egress IP`) | — | needs money or a residential egress IP |
| upstream errors / timeouts | `ddgw` (400), `felo` (400 thread creation failed), `unc` (404 model does not exist), `veo-free`+`veoaifree-web` (aborted), `pepper` (502 Amelia init failed: 404) | — | transient or dead; not worth chasing at 1–6 models each |
| free-tier rate limit | `mistral` (4 of 10 probed) | `429 Rate limit exceeded (reset after 3s)` | retry with spacing; 3 of 10 did score |

The screen is 3 probes per prefix, so **"blocked" means the probed models failed, not that every model
under the prefix fails** — a prefix with 185 entries was sampled 3 times. It is a triage instrument,
not a census, and the honest reading of it is "where to spend the next capture", not "what is
unusable".

### The `kiro` account, probed model by model

`kiro` is the interesting one: a free AWS Kiro login, OAuth, token refreshed by the router — a surface
that "go get an API key" would never have found. `benchmark/catalog-probe-omni-kiro.json` probed all
21 of its models:

- **7 scored**: `claude-haiku-4.5` (p=0.92), `claude-sonnet-4.5` (p=0.85), `deepseek-3.2` (p=0.28),
  `glm-5` (p=0.55), `minimax-m2.1` (p=0.75), `minimax-m2.5` (p=0.82), `qwen3-coder-next` (p=0.15) —
  all on `ai-000`, which is an AI text, so the low ones are already visible misses.
- **14 failed** with a single identical error: `400 Invalid model. Please select a different model to
  continue.` These are Kiro rejecting the id — every `-low`/`-medium`/`-high`/`-xhigh` effort variant,
  `claude-sonnet-5*`, `gpt-5.6-*`. Listed, not entitled.

`mistral` was probed the same way (`catalog-probe-omni-mistral.json`, 10 models): 3 scored —
`ministral-3b-latest` p=0.99, `ministral-14b-latest` p=0.00, `ministral-8b-latest` p=0.01 on the same
AI text, i.e. the 3b agrees and the two larger ones are already badly wrong — plus 4× `429 Rate limit
exceeded`, 2× stale-catalogue `400`, and 1× `403` on a Labs model. `vertex` (`catalog-probe-omni-vertex.json`,
9 models): **0 scored**, 6× `404 Unknown error (reset after 2m)`, 2× `401 API keys are not supported
by this API. Expected OAuth2 access token`, 1× `401 All 1 connection(s) authentication expired`.
Vertex needs the user to log in again.

### Four full 48-text captures

The four `kiro` models that both scored the probe and were worth a full corpus ran through the shipped
prompt at temperature 0.1, top_p 0.9, max_tokens 1400, concurrency 2 (`benchmark/omni-kiro-round.log`
carries the per-text lines):

| tag | model | usable | errors | p50 / p90 / max (ms) | wall | AUC | Cohen's d |
|---|---|---|---|---|---|---|---|
| `omfull-kiro-claude-sonnet-4-5` | `kiro/claude-sonnet-4.5` | **48/48** | none | 3009 / 3927 / 6113 | 76.1 s | **0.9878** | **5.017** |
| `omfull-kiro-claude-haiku-4-5` | `kiro/claude-haiku-4.5` | 47/48 | 1 `no_json_object` | 1552 / 2093 / 15903 | 47.0 s | 0.9475 | 1.554 |
| `omfull-kiro-minimax-m2-1` | `kiro/minimax-m2.1` | 48/48 | none (1 salvaged) | 4642 / 5409 / 5766 | 112.2 s | 0.8950 | 1.797 |
| `omfull-kiro-minimax-m2-5` | `kiro/minimax-m2.5` | 47/48 | 1 `no_json_object` | 5831 / 7484 / 10449 | 141.3 s | 0.8016 | 1.118 |

At the **shipped** 0.69 / 0.40 band, confidence gates off (`node benchmark/doc-table.js`, which reads
the numbers back from the backend's own `summarize_run` rather than re-deriving them):

| tag | model | n | acc | acc / corpus | wrong | deferred | med AI | med human | separation |
|---|---|---|---|---|---|---|---|---|---|
| `omfull-kiro-claude-sonnet-4-5` | `kiro/claude-sonnet-4.5` | 48 | 0.958 | **0.958** | 2 | **0** | 0.865 | 0.150 | 0.715 |
| `omfull-kiro-minimax-m2-1` | `kiro/minimax-m2.1` | 48 | 0.750 | 0.750 | 9 | 3 | 0.850 | 0.475 | 0.375 |
| `omfull-kiro-minimax-m2-5` | `kiro/minimax-m2.5` | 47 | 0.617 | 0.604 | 14 | 4 | 0.880 | 0.720 | 0.160 |
| `omfull-kiro-claude-haiku-4-5` | `kiro/claude-haiku-4.5` | 47 | 0.660 | 0.646 | 16 | 0 | 0.820 | 0.720 | 0.100 |

Two things in that table are worth more than the ranking.

**`kiro/claude-haiku-4.5` is a measured negative with a good AUC.** 0.9475 AUC and d 1.554 look like a
strong model, and its ranking quality genuinely is — but its HUMAN median is **0.72, inside the AI
band**. At the shipped cuts it scores 0.660 with 16 wrong calls and 0 deferrals. Its own best cut is
0.73 (acc 0.9149, 2 fp / 2 fn). AUC is threshold-free, so it cannot see this; only the band can. Row
31 records it as a negative with that reasoning, so nobody reuses the shipped band for it later.
`minimax-m2.5` fails the same way (HUMAN median 0.720) and worse.

**`kiro/claude-sonnet-4.5` wins on the corpus, and its output is coarse.** 0.958 with 0 deferrals, the
highest `acc / corpus` in the whole document, and the second-highest AUC (0.9878, behind only
`gemma-4-31b-it`'s 0.980-median profile). But look at the distribution: 20 of 24 human texts score
**exactly 0.15**, the AI minimum is 0.75, and there are only 8 distinct values in 48 answers. There is
a 0.50-wide empty gap in the middle of the scale. That is a model emitting a small set of stock
probabilities, not one estimating a calibrated risk — which is why the wide gap makes the shipped band
work, and why it should not be trusted to keep working if the model changes.

### Why the fitted band was refused, and a guard that is not a dry run

Sonnet's own best threshold is 0.73 (acc 0.9792, `tp=24 fp=1 tn=23 fn=0`), beating the shipped 0.69.
`store-band.js` was asked to fit it and refused:

```
  SHIPPED default   ai>=0.69 hu<=0.4 conf>=0.6/0.6  => acc=0.958 prec=0.958 review=0 AIrecall=1.00 HUMANspec=0.92 [tp24 tn22 fp2 fn0]
  FITTED band       ai>=0.73 hu<=0.4 conf>=0.6/0.6  => acc=0.958 prec=0.979 review=1 AIrecall=1.00 HUMANspec=0.96 [tp24 tn22 fp1 fn0]

MARGIN  min |score - cut| over deciding rows = 0.020, rows within 0.03 of a cut = 2
  NOT OK: this band reads a score that sits on top of the cut. On this pipeline that
  difference is inside the run-to-run noise, so the band would only be reproducible on
  this exact capture. Refusing to store.
```

The fitted band does not even improve the comparable number — `acc / corpus` is 0.958 either way, and
all it buys is precision 0.958 → 0.979 and HUMAN specificity 0.92 → 0.96, by converting one false
positive into a review. Under `--force` the FOLD check ran and both halves tied (fold 1: fitted 22/24
vs shipped 22/24; fold 2: 24/24 vs 24/24), which is the other way of saying the cut moves nothing
real. A 0.73 cut sits 0.01 above a human text that scored 0.72, and this pipeline flips 8 of 12
borderline texts between identical runs. The shipped 0.69 / 0.40 stands.

Recorded honestly because it cost a revert: **`--force` is not a dry run.** Re-running the command
above with `--force` to see the FOLD output passed the guard and wrote `PATCH /models/30 -> 200`,
storing the refused 0.73 band and resetting the confidence gates from 0.0/0.0 to the 0.6/0.6 default.
Report-only is `store-band.js` **without** `--row` *and* without `--force`. Row 30 was patched back to
0.69 / 0.40 with conf 0.0 / 0.0 and re-verified live (`OK 2 text(s): the row's cuts, endpoint, key and
lane all shipped`). While restoring it, `PATCH /models/30` returned `400 BAD REQUEST` for a 1010-char
notes string — the `notes ≤ 1000 chars` limit is enforced on update, not only on create.

### Is it actually better than round 8's glm-5.3? No — indistinguishable

Both scored the same 48 texts, so they can be paired directly (join on text id, both `ok`, shipped
band applied to each):

```
paired usable n=48
sonnet: correct 46  deferred 0   |   glm-5.3: correct 45  deferred 3
discordant: sonnet-right/glm-wrong=2   glm-right/sonnet-wrong=1
   hu-009  HUMAN  sonnet p=0.15 -> HUMAN  glm p=0.62 -> DEFER
   hu-014  HUMAN  sonnet p=0.72 -> AI     glm p=0.30 -> HUMAN
   hu-019  HUMAN  sonnet p=0.15 -> HUMAN  glm p=0.45 -> DEFER
McNemar exact two-sided p=1.0000
```

Three texts separate them, all of them human, and only one (`hu-014`) is a case where both models gave
a verdict and disagreed — the other two are sonnet deciding where glm deferred. McNemar's exact
two-sided p is 1.0000. On 48 texts these two are the same detector; sonnet defers less and is ~3.6×
faster than the paid primary (p50 3.0 s vs 10.9 s), which is a real operational difference, but
"more accurate" is not supported by this corpus.

### Wiring it into the pipeline, and the `localhost` trap

Provider 3 (`OmniRoute (self-hosted)`) already existed in the project database; it now has a key
(id 6, 35 chars, active) and a corrected `base_url`. The correction mattered:

`verify-stored-band.js --model kiro/claude-sonnet-4.5` failed with `ECONNREFUSED ::1:20128`. OmniRoute
binds **IPv4 only** — `TCP 0.0.0.0:20128` with no tcpv6 listener — and n8n resolves `localhost` to
`::1`. Node's own fetch resolved it to IPv4 and worked, which is why the benchmark runs (all captured
against `http://localhost:20128/v1`, per the logged params) never hit this. `PATCH /providers/3
{"base_url":"http://127.0.0.1:20128/v1"}` fixed it. **A literal IPv4 is not optional for a lane that
n8n calls.** `ALLOWED_LLM_HOSTS` already contains `127.0.0.1`, and it matches hostnames with any port,
which is what lets a non-standard router port through.

The first verification run also failed for the right reason: both verdicts were correct but
`deciding_lane` was `secondary`, and the 2026-10-09 guard treats a correct verdict from the wrong lane
as a failure. After the base_url fix:

```
row 30  kiro/claude-sonnet-4.5  lane=null  provider=omniroute  cuts ai>=0.69 hu<=0.4 conf 0/0  sec=null  mt=1400 t=0.1
  ai-000 (AI)  p=0.85 -> AI_GENERATED  [primary]  all checks pass
  hu-000 (HUMAN)  p=0.25 -> HUMAN_LIKELY  [primary]  all checks pass
OK  2 text(s): the row's cuts, endpoint, key and lane all shipped.
```

Round 8's row 22 was verified in the same pass, which it never had been: `z-ai/glm-5.3` → `OK 2
text(s)`. n8n was started for this and the webhook polled until registered rather than trusting
`/healthz`.

Stored state after the round: **row 30** `kiro/claude-sonnet-4.5` and **row 31**
`kiro/claude-haiku-4.5`, both `lane = null`, `role = either`, `measured = true`, band 0.69 / 0.40,
conf gates 0 / 0, temperature 0.1, top_p 0.9, max_tokens 1400, timeout 90 s, provider 3. The shipped
pair is unchanged — primary `nvidia/nemotron-3-ultra-550b-a55b`, secondary
`google/diffusiongemma-26b-a4b-it`. Nothing was promoted.

### What this round does not prove

- **0.958 is in-sample**, on the same 48 texts every other number in this document was fitted on.
  *(Resolved in round 10 below: sonnet has now been run against both the Gutenberg control and a
  disjoint two-sided HC3 holdout, and improved to 0.979 out of sample. The caveat that remains is that
  both corpora are still HC3 — GPT-3.5-era AI on both sides.)*
- **The coarse output distribution is a stability risk, not a strength.** 8 distinct values with 20/24
  human texts pinned at exactly 0.15 means the band sits in a gap that this model happens to leave
  empty. There is no evidence the gap survives a model revision.
- **The kiro token expires.** `token_expires_at` was 2026-10-10T23:49:03Z at the time of writing and
  OmniRoute refreshes it, but a refresh failure turns row 30 into a dead lane with no warning beyond a
  401. A lane whose credential a third-party router renews on its own schedule is a different
  reliability class from an API key.
- **`auto`, `no-think` and `(bare)` are unbenched.** `auto/*` are 38 virtual routes that scored 3/3 on
  the screen — `auto/best-free`, `auto/coding:cheap`, `auto/glm`, `auto/minimax` — but a virtual route
  resolves to whatever account is healthy at call time, so its behaviour is not fixed by its id. That
  makes it unusable as a stored lane even if it measures well.
- **The 1400 blocked models were sampled, not surveyed.** Installing Playwright Chromium (27 models),
  `zcode` (13), `auggie` (28), `qodercli` (18) and the Devin CLI plus `DEVIN_AGENTIC_HOME` (375) are all
  untried. Vertex (65) needs the user to log in again — an identity flow, handed off rather than
  automated.

### The corrected lesson

Round 8 concluded a source was unavailable because a port did not answer and a Docker daemon was down.
Both were true and both were beside the point. The check that was missing: **before declaring a source
unavailable, look for an existing installation and an existing credential store, not only for a
running process.** `~/.omniroute` had 43 MB of state, 20 logged boots and 15 connected accounts the
whole time. On this machine the single most productive free-model surface turned out to be one that
was already paid for, already logged in, and already on disk.

## Round 10: the out-of-sample check that #56 actually needed (2026-10-11)

Round 8 promoted nothing and round 9 promoted nothing, both for the same stated reason: the headline
numbers — glm-5.3's **0 wrong calls** and sonnet's **0.958** — are *in-sample*, measured on the same
48 HC3 texts every threshold in this document was fitted on. Task #56 was "validate glm-5.3's 0-wrong
result out of sample," and the honest first finding is that **the corpus on disk could not answer it.**

### The Gutenberg control is saturated and cannot discriminate

`corpus-books.json` is 46 real Project Gutenberg excerpts, **all HUMAN**. It tests one half of one
confusion matrix: false positives on real prose. Run at the shipped 0.69/0.40 band, four different
models all score **specificity 1.000 — 0 false positives, 0 deferrals**:

| capture | model | hu | spec | fp | defer | p min/med/max |
|---|---|---|---|---|---|---|
| `book-omkiro-sonnet-4-5` | `kiro/claude-sonnet-4.5` | 46 | 1.000 | 0 | 0 | 0.02 / 0.15 / 0.15 |
| `book-nv-glm-5-3` | `z-ai/glm-5.3` | 46 | 1.000 | 0 | 0 | 0.01 / 0.02 / 0.08 |
| `bookultra550` | `nvidia/nemotron-3-ultra-550b-a55b` | 46 | 1.000 | 0 | 0 | 0.01 / 0.045 / 0.18 |
| `bookdiffgemma` | `google/diffusiongemma-26b-a4b-it` | 46 | 1.000 | 0 | 0 | 0.01 / 0.02 / 0.08 |

When every model scores 1.000 the test has no headroom left: it cannot separate a strong detector from
a weak one, and it says nothing at all about **AI recall**, which is the half glm's "0 wrong" claim
lives in. A saturated control is not a failed experiment — it is a control that stopped being able to
fail. So #56 needed a *two-sided* out-of-sample corpus, not this one.

### A disjoint two-sided HC3 holdout, with provenance and an independent verifier

`benchmark/build-holdout-corpus.js` builds `corpus-holdout.json`: 48 texts, **24 AI / 24 HUMAN**, drawn
from the same HuggingFace `Hello-SimpleAI/HC3` rows endpoint as the in-sample corpus but from a
different offset window (`--start_offset 600`), the same 300–1600 char bounds and the same
CONFIGS/class balance. It never touches `corpus.json` (md5 `97efa1d396bc05a7183e52ea771033aa`,
identical before and after). Disjointness is enforced two ways and both are printed:

- **by normalised text** — exclusion matches on `s.replace(/\s+/g,' ').trim().toLowerCase()`, so a
  whitespace-only difference cannot fake disjointness. Builder reported `OVERLAP with corpus.json: 0
  OK - disjoint`, 6 candidates rejected as already-seen.
- **by question** — HC3 pairs a human and a ChatGPT answer to the *same* question, so disjoint text can
  still leak topic. Checked: **0 of 48 shared questions** with the in-sample set.

`benchmark/verify-holdout-corpus.js` is an independent check that does not reuse the builder: per row it
re-fetches `offset=row_idx&length=1` from the datasets-server and asserts the whitespace-collapsed text
is an exact match inside the answer list its label claims, is **not** in the opposite list, and the
question matches. It was **negative-controlled first** on a hand-tampered copy (word-order swap, label
flip, invented tail clause) — all 3 FAILed with correct diagnoses, then the tampered file was deleted.
Real run: **48/48 verified, by label {"AI":24,"HUMAN":24}**. Every row carries
`{dataset, config, split, row_idx, question, source, fetched_at}`.

The backend attributes it as its own corpus: `POST /benchmark/import` derived **`holdout`** from the
manifest's `corpus: corpus-holdout.json`, distinct from `hc3-48`, so the published comparison table is
not contaminated. The four captures are runs **61–64** (`book-`/`oos-` × sonnet/glm), 61 runs total.

### The result: raw accuracy separates, decisive accuracy does not

`benchmark/oos-compare.js <in.jsonl> <oos.jsonl>` reports each class at the shipped band, a deferral
counting as not-correct. Both captures ran 48/48 usable, 0 salvage, 0 truncation, `errors=none`.

| model | in-sample AI | in-sample HUMAN | in-sample overall | OOS AI | OOS HUMAN | OOS overall |
|---|---|---|---|---|---|---|
| `kiro/claude-sonnet-4.5` | 24/24 right, 0 wrong, 0 defer | 22 right, **2 wrong** (hu-004 0.85, hu-014 0.72), 0 defer | **0.958 (46/48)** | 24/24 right, 0 wrong, 0 defer | 23 right, **1 wrong** (hu-007 0.72), 0 defer | **0.979 (47/48)** |
| `z-ai/glm-5.3` | 24/24 right, 0 wrong, 0 defer | 21 right, 0 wrong, **3 defer** | **0.9375 (45/48)** | 23 right, **1 wrong** (ai-012 p=0.35), 0 defer | 22 right, 0 wrong, **2 defer** | **0.9375 (45/48)** |

Two things move, in opposite directions:

- **sonnet improved** out of sample, 0.958 → 0.979. Its two in-sample human false-positives did not
  recur as a class; the single OOS miss (hu-007 at 0.72) is a different text. Its coarse output
  distribution is stable across both corpora — 4–5 distinct values, AI pinned high, HUMAN at 0.15.
- **glm's celebrated "0 wrong calls" did not survive.** Its overall accuracy is *identical* in and out
  of sample (0.9375 both), but the composition changed: in sample all 3 misses were HUMAN deferrals;
  out of sample it traded one for a **false negative** — `ai-012` scored **0.35**, below the 0.40 human
  cut, so a real AI text was confidently read as human. A 0-wrong in-sample result was a property of
  those 48 texts, not of the model. This is the exact failure mode #56 existed to catch.

Because both models ran the *same* 48 holdout texts, a paired McNemar is legitimate here (unlike any
cross-corpus comparison). The shipped `benchmark/paired-gap.js` counts **decisive-vs-decisive
disagreements only** — a deferral is neither right nor wrong, so it is reported separately as "who the
band costs," not as a discordant pair:

```
PAIRED GAP  oos-omkiro-sonnet-4-5  vs  oos-nv-glm-5-3
side                       correct  wrong  deferred  acc/all  acc/decided
oos-omkiro-sonnet-4-5      47        1       0          0.979     0.979
oos-nv-glm-5-3             45        1       2          0.938     0.978

both right 44   both wrong 0   A right/B wrong 1   B right/A wrong 1   both deferred 0
McNemar exact two-sided p = 1.0000 on 2 discordant pair(s)  -> indistinguishable on this corpus

who the band costs (one side deferred, the other decided):
  only oos-nv-glm-5-3 deferred: 2  hu-014 hu-016
the deciding disagreements (pA vs pB):
  hu-007   B right / A wrong  0.72 vs 0.15
  ai-012   A right / B wrong  0.75 vs 0.35
```

**p = 1.0000.** The two models disagree decisively on exactly 2 texts and split them 1–1: sonnet misses
`hu-007` (0.72), glm misses `ai-012` (0.35). On decided calls they are **equal at 0.978–0.979**. The
whole raw-accuracy gap (47/48 vs 45/48) is glm's **2 deferrals** (`hu-014`, `hu-016`), not glm being
wrong — and a deferral is the pipeline correctly declining, not an error. **The honest headline is not
"sonnet beats glm out of sample" — it is "both hold up out of sample, they are statistically
indistinguishable, and glm loses its in-sample 0-wrong property"** (ai-012 became a false negative).

*(An earlier ad-hoc count that treated a deferral as incorrect reported p=0.6250 on 4 pairs; that is
the wrong convention for McNemar and is superseded by the shipped tool's p=1.0000 on the 2
decisive-vs-decisive pairs. Recorded so the discrepancy is not rediscovered.)*

### Latency: the NVIDIA free tier is ~2× slower today than round 8 measured

| capture | model | p50 | p90 | max |
|---|---|---|---|---|
| `book-omkiro-sonnet-4-5` | sonnet (kiro/OmniRoute) | 3104 ms | 3606 ms | 4952 ms |
| `oos-omkiro-sonnet-4-5` | sonnet (kiro/OmniRoute) | 3195 ms | 4033 ms | 33557 ms |
| `book-nv-glm-5-3` | glm-5.3 (NVIDIA free) | 27806 ms | 45579 ms | 87582 ms |
| `oos-nv-glm-5-3` | glm-5.3 (NVIDIA free) | 22283 ms | 48113 ms | 74908 ms |

glm-5.3's round-8 capture ran p50 **14.4 s**; the same model on the same NVIDIA lane today runs p50
**22–28 s** with a 87 s tail, and a 48-text capture costs ~730 s wall clock. sonnet through the local
OmniRoute/kiro path is ~7–9× faster at p50 ~3.1 s. Free-tier latency is not a model property — it is
shared-pool load, and it roughly doubled in a day. Any "fast enough for a live lane" judgement on a
free NVIDIA model is time-of-day-bound.

### What round 10 does not prove

- **Both corpora are still HC3.** The holdout is disjoint *within* HC3, so its AI class is still
  GPT-3.5-era ChatGPT output. Neither capture tests detection of modern-model prose, which is the
  transfer caveat that has stood since calibration. A true out-of-distribution check needs a
  current-model AI corpus, which does not exist on disk.
- **n=48 cannot rank two models that differ by 2 texts.** paired-gap McNemar p=1.0000; on decided
  calls they are equal (0.978 vs 0.979). Do not read the table as a promotion of sonnet over glm.
- **Nothing was promoted.** Both remain `lane = null`: sonnet on `models` row 30, glm-5.3 on row 22.
  The shipped pair is unchanged — row 1 `nvidia/nemotron-3-ultra-550b-a55b` primary, row 2
  `google/diffusiongemma-26b-a4b-it` secondary. Promotion still needs a **live webhook check** through
  the deployed n8n, which this round did not run.
- **sonnet's credential is a router-refreshed OAuth token**, not an API key (see round 9). Its
  out-of-sample accuracy is real but its availability as a lane depends on a third party's refresh.

## Round 11: clean human text vs clean AI text — separating style from scrape noise (2026-10-11)

Every number above is measured on `corpus.json` / `corpus-holdout.json`, whose **human** class was
scraped out of varied tokenizers and its **AI** class was not. That asymmetry leaks into the score: a
model can raise `ai_probability` on a human answer just because the human answer *looks* machine-clean,
or lower it because the scrape left messy whitespace no chat model emits. GPTZero's own independent audit
of our labels (round 9) had already shown the effect — `hu-008` and `hu-012` flipped its verdict purely on
whitespace. So this round rebuilds **both classes through one symmetric cleaning pass** and re-scores the
lane set, so the remaining delta is authorship *style*, not formatting.

### The transform is whitespace-and-whitelist only, and it is proven content-preserving

`benchmark/build-clean-corpus.js` writes `corpus-clean.json` / `corpus-holdout-clean.json` (sources
untouched; cleaned text in `text`, raw kept in `text_original`, every id/label/`config`/provenance field
carried over). Two steps, both content-safe: a **whitelist token strip** (`[ source ]` case-insensitive,
`URL_\d+`, emptied brackets `[([{]\s*[)\]}]`, and the literal backslash-`n`/`r`/`t` serialization artefacts
→ a space), then a **whitespace-only normalise** (fold every unicode space via `/\s/g`, rejoin split
contractions `you 're`→`you're`, drop space-before-punctuation, collapse runs, trim). Deliberately *not*
applied: adding space-after-period (found class-symmetric, and it corrupts `Ph.D.`/`U.S.A.`) and stripping
`*` (E\*Trade is content). `clean(raw)` never invents characters — correctness is asserted, not trusted:

- **fold invariance**: `fold(cleaned) === fold(content-stripped)` (fold = drop all whitespace + lowercase)
  for **all 96 rows, 0 failures**; an abort-on-mismatch and an 11-case transform self-test ship with it.
- **marker forensics** (real re-count, `text` field, main corpus): the artifacts sit almost entirely on the
  human side and the clean pass drives them to zero in *both* classes —

| marker | dirty HUMAN | dirty AI | clean HUMAN | clean AI |
|---|---|---|---|---|
| space-before-punctuation | 55 occ / 8 rows | 0 | 0 | 0 |
| split contractions (`you 're`) | 13 / 4 | 0 | 0 | 0 |
| double/triple spaces | 14 / 6 | 0 | 0 | 0 |
| wiki tokens (`[ source ]`, `URL_0`) | 1 / 1 | 0 | 0 | 0 |
| literal `\n`/`\r`/`\t` | 0 | 10 / 2 | 0 | 0 |

  Holdout is the same shape (space-before-punct 68/8 human rows, split-contractions 8/4, double-spaces 4/2,
  1 `[ source ]`, 2 `URL_\d` — all human-only). Total scored-text length drops only **−0.4%** (main) /
  **−0.5%** (holdout).

### Harness change this round: a `--timeout` flag, because slow ≠ dead

`run-benchmark.js` aborted every request at a hard-coded 90s. That silently reported NVIDIA's free
`z-ai/glm-5.3` lane as dead (all 4 tries of every row at 90s). Adding `--timeout` (seconds, default 90,
recorded in the manifest) and re-probing showed it is the **pool, not the harness**: on the *same* key and
endpoint the paid `nemotron-3-ultra-550b` answered in **12 s** while `glm-5.3` stayed silent past **150 s**.
That is the round-8 lesson restated — a probe timeout measures queue wait, not model speed.

Partial captures are now resumable without a second full burn: `node benchmark/resume-lanes.js` reads
`runs-manifest.log` (last params line per tag), lists every lane with `usable < rows`, and `--go` re-runs
each with `run-benchmark.js --resume`, which keeps already-ok rows and calls only the missing ones — so a
lane that died at 5/48 on the daily free cap continues when the quota resets. Gate/probe tags are skipped
unless named explicitly with `--only`, and a results file older than the 2026-10-07 parser fix carries a
salvage warning. **Verified live (2026-10-11, round 12):** 8 OpenRouter lanes were resumed through the
6-account rotation (`benchmark/log-resume-or-6keys.txt`, every params line records `keys=6` — rotation
itself pinned by `benchmark/test-retry.js`, 15/15), recovering 32 rows across two daily-quota windows with
**zero harness-side failures**: every row still unscored afterwards carries an attributable gateway/model
error (class-(a) pool `http_429`, a `402` paid flip, a catalogue-removal `404`, or model-side malformed
output), never an abort or a lost already-recorded row. The resumed final numbers are in the round-12
section below.

### Single-model result: AUC is robust; effect size and the human cut are what the dirt was buying

Same per-model params as each dirty baseline (temp 0.1, prompt `direct`), dirty vs clean at the deployed
single-model cut (`AI≥0.69 &conf≥0.6`, `HUMAN≤0.40`). `d` = Cohen's d, `gap` = mean AI − mean human.

| lane (main unless noted) | usable | AUC d→c | d d→c | gap d→c | humanAsAI d→c | aiAsHuman d→c |
|---|---|---|---|---|---|---|
| `nemotron-3-ultra-550b` | 48/48 | 0.934→0.935 | 2.444→2.246 | 0.415→0.374 | 2→3 | 0→0 |
| `diffusiongemma-26b` | 48/47 | 0.954→0.935 | 1.729→1.843 | 0.443→0.473 | 12→10 | 0→0 |
| `kiro/claude-sonnet-4.5` | 48/48 | 0.988→0.987 | **5.017→4.265** | 0.665→0.637 | 2→3 | 0→0 |
| `kiro/claude-haiku-4.5` | 47/48 | 0.948→0.948 | 1.568→1.531 | 0.284→0.273 | 16→15 | 0→0 |
| `kiro/minimax-m2.5` | 47/48 | **0.802→0.882** | 1.129→1.515 | 0.235→0.316 | **14→10** | 0→0 |
| `kiro/minimax-m2.1` | 48/48 | 0.895→0.874 | 1.797→1.809 | 0.346→0.385 | 9→8 | 0→0 |
| `kiro/claude-sonnet-4.5` (HOLDOUT) | 48/48 | 0.999→1.000 | 6.964→7.258 | 0.657→0.664 | 1→1 | 0→0 |

Four honest findings, and they are not all one direction:

- **Ranking barely moves.** AUC shifts between −0.019 and +0.080 — small and both-signed. The confound
  does *not* live in the ordering of texts; a model that ranked AI above human on dirty text still does on
  clean. AUC was therefore never the right lens for this question (it is insensitive to a band problem, as
  calibration already noted).
- **The elite models' *effect size* was partly the dirt.** sonnet's Cohen's d collapses 5.02→4.27 and the
  550B's 2.44→2.25; both gaps narrow. On dirty text they were being paid for spotting formatting noise, so
  their headline separation was flattered. Their *ranking* stays elite; their *margin* is smaller than advertised.
- **The sign is model-specific.** `minimax-m2.5` gets *better* clean (AUC 0.802→0.882, humanAsAI 14→10) —
  for it the scrape noise was actively misleading, so cleaning helps. `diffusiongemma` is mixed (AUC dips,
  gap widens, humanAsAI 12→10). Haiku over-flags humans (16→15) regardless: **that is a real weakness, not
  the confound** — cleaning cannot rescue a model that reads short human prose as machine text.
- **`aiAsHuman` is 0→0 across every single-model lane.** Cleaning never turns an AI text into a confidently
  HUMAN verdict at the single-model cut. The false-negative risk lives in the *ensemble*, next.

### The free lanes finished the next evening — same direction, plus two lanes cleaning *helped*

The NVIDIA and OpenRouter free captures above were written while their pools were still throttling; they
completed on 2026-10-11 (NVIDIA sweep marker 20:02). Same params per lane as its dirty baseline, same cut.
`usable` is dirty/clean answered rows — on the free lanes that is the pool's cap, not the model's.

| lane (NVIDIA free unless noted) | usable d/c | AUC d→c | d d→c | gap d→c | humanAsAI d→c | aiAsHuman d→c |
|---|---|---|---|---|---|---|
| `z-ai/glm-5.3` | — | **GATE_DEAD** — silent past 150s at the gate, again | — | — | — | — |
| `z-ai/glm-5.3-flash` | 48/48 | 1.000→0.997 | 5.884→3.967 | 0.684→0.615 | 1→2 | 0→0 |
| `google/gemma-4-31b-it` | 48/**5** | 0.945→1.000 (n=5, see caveat) | 2.259→1.075 | 0.587→0.430 | 7→1 | 0→0 |
| `nvidia/nemotron-3.5-lightning-30b` | 23/20 | 0.640→0.692 | 0.554→0.628 | 0.061→0.115 | 0→0 | 9→7 |
| OpenRouter `nemotron-3-ultra-550b:free` | 48/48 | 0.937→0.906 | 2.473→1.985 | 0.388→0.368 | 2→2 | 0→**1** |
| OpenRouter `inclusionai/ling-3.1-flash` | 46/38 | 0.985→0.997 | 3.347→3.442 | 0.452→0.482 | 1→0 | 0→0 |
| OpenRouter `liquid/lfm-2.5-2.6b:free` | 47/47 | 0.480→0.651 | −0.150→0.419 | −0.035→0.096 | 9→5 | 7→4 |
| OpenRouter `openrouter/free` (mirror) | 33/22 | 0.743→0.804 | 1.080→1.195 | 0.228→0.238 | 7→2 | 0→0 |

- **The elite-free lanes match the elite-paid pattern**: glm-5.3-flash's `d` collapses 5.88→3.97 exactly as
  sonnet's did — its dirty headline was partly formatting noise. Its clean AUC (0.997 on 48/48 answered) is
  the best clean free number measured, tied with `ling-3.1-flash`'s 0.997 (on 38 answered).
- **Two lanes get *better* on clean text.** `ling-3.1-flash` (AUC +0.012, humanAsAI 1→0) and the
  `openrouter/free` mirror (+0.061, humanAsAI 7→2) — like `minimax-m2.5` above, for them the scrape noise was
  actively misleading. `lfm-2.5-2.6b` improves most (+0.171) but was near-chance dirty (0.480, negative `d`);
  even cleaned it is 0.651 — still not a lane.
- **The free 550B mirror crosses to an AI→HUMAN miss on clean text** (`aiAsHuman` 0→1, `ai-018` at p=0.25) —
  the same failure the ensemble showed: with the human class no longer carrying messy signal, a marginal AI
  text can fall under 0.40. It is the only lane that breaks this way while scoring its full corpus on both
  sides; `lightning` already missed AI texts on both dirty and clean (next bullet). Sign of the band effect,
  not of this lane's quality — it is otherwise the free mirror of the paid 550B lane.
- **`nemotron-3.5-lightning`'s 9→7 is not an improvement.** Dirty missed 9 (incl. ai-015, ai-019) and clean
  puts 7 of its 11 answered AI texts into a HUMAN verdict at the cut (ai-011 was demoted to REVIEW only
  because its conf=0.50 fell under the gate; an 8th scored p=0.35 low). The dirty and clean sets differ —
  ai-004/005/010/011 were answered clean but never dirty — so the delta itself is coverage luck. The stable
  fact: the majority of the AI texts it does answer score under the human cut, consistent with round 8's
  verdict that a reasoning model cannot carry this output contract.
- **`gemma-4-31b-it`'s clean row is 5 texts (3 AI / 2 human).** Its AUC 1.000 is *not* comparable to the
  48-text dirty 0.945; the pool gave 5 of 48 — the gate passed at usable=1, then 43 of the remaining rows
  died in `exception` aborts at the 180s timeout. Coverage, not capability. All it honestly shows: the 3 AI
  rows scored 0.98 and ranked above both human rows, but one human (hu-020, p=0.95) still crossed the cut
  (that is the 7→1 in humanAsAI — one flagged out of two answered).
- **`glm-5.3` stays GATE_DEAD on NVIDIA free**, second sweep, same shape: gate of 2 texts at 150s never
  answered. Per the standing rule this names the gateway — the model is served fine on OpenRouter's mirror;
  the NVIDIA free pool for `z-ai/glm-5.3` is what will not serve.

The `ling-3.1-flash` and `openrouter/free` rows above were still partial-coverage when written; the resume
round below completed them the same night and supersedes their usable/AUC columns.

### Round 12 — the OpenRouter resume across six accounts: final free numbers

`resume-lanes.js --go` re-ran the 8 incomplete OpenRouter lanes on the 6-key rotation
(`benchmark/log-resume-or-6keys.txt`). Net +32 recovered rows across two quota windows; every remaining
miss now has an attributed reason.

| lane | usable before | usable after | still missing (reason) |
|---|---|---|---|
| `orfull-ling-3.1-flash` (dirty) | 46/48 | 48/48 | none |
| `clean-orfull-ling-3.1-flash` | 38/48 | 47/48 | 1 — `402` (inclusionai flips one text to paid) |
| `clean-orfull-openrouter-free` | 22/48 | 43/48 | 5 — answered but unparseable (4 `no_json_object`, 1 `json_parse_failed`) |
| `orfull-openrouter-free` (dirty) | 33/48 | 33/48 | 15 — class-(a) `http_429` upstream pool |
| `orfull-lfm-2.5-2.6b:free` (dirty) | 47/48 | 47/48 | 1 — `http_429` |
| `clean-orfull-lfm-2.5` | 47/48 | 47/48 | 1 — `400` |
| `orfull-inclusionai-ling-3.0-flash-sante:free` | 31/48 | 31/48 | 17 — `404` catalogue removal, lane permanently frozen |
| `or-google-gemma-4-26b-a4b-it:free` (12-text screen) | 0/12 | 0/12 | 12 — class-(a) `http_429`, all six accounts |

All seven scored lanes at the deployed band (AI≥0.69 / HUMAN≤0.40 / conf≥0.60; AUC Mann-Whitney over
answered rows; `acc/corpus` divides by 48):

| lane | usable | AUC | wrong decided | humanAsAI | aiAsHuman | deferrals | acc/corpus | med AI | med HUMAN |
|---|---|---|---|---|---|---|---|---|---|
| `ling-3.1-flash` dirty | 48/48 | 0.986 | 1 | 1 | 0 | 12 | 0.729 | 0.850 | 0.325 |
| `ling-3.1-flash` clean | 47/48 | 0.996 | 0 | 0 | 0 | 11 | 0.750 | 0.870 | 0.300 |
| `openrouter/free` dirty | 33/48 | 0.743 | 7 | 7 | 0 | 7 | 0.396 | 0.850 | 0.680 |
| `openrouter/free` clean | 43/48 | 0.768 | 7 | 5 | 2 | 13 | 0.479 | 0.810 | 0.600 |
| `lfm-2.5-2.6b:free` dirty | 47/48 | 0.480 | 16 | 9 | 7 | 16 | 0.313 | 0.665 | 0.650 |
| `lfm-2.5-2.6b:free` clean | 47/48 | 0.651 | 9 | 5 | 4 | 17 | 0.438 | 0.720 | 0.600 |
| `ling-3.0-flash-sante:free` (frozen at 31) | 31/48 | 0.911 | 1 | 1 | 0 | 6 | 0.500 | 0.820 | 0.350 |

- **`inclusionai/ling-3.1-flash` is now the best OpenRouter-free detector on record**: full coverage on both
  sides, clean AUC 0.996 with **zero wrong decided calls**, med HUMAN 0.300 against the 0.40 cut —
  defer-heavy (11) but never wrong, the glm-5.3 behaviour pattern without the pool that will not serve.
- **The mirror's clean "improvement" was a 22-row artifact.** At 43 rows clean rises only 0.743→0.768
  (not 0.804), and cleaning *introduced* 2 `aiAsHuman` misses — the same band failure mode the paid 550B
  mirror shows. med HUMAN 0.600 keeps it biased toward reading humans as AI regardless of coverage.
- **A `404` cannot be resumed.** `ling-3.0-flash-sante:free` asked 17 missing texts and got 17 catalogue
  removals; its 0.911 AUC is permanently a 31-row measurement.
- **gemma-4-26b-a4b-it:free is closed on its third inquest**: 0/12 across all six accounts. This is the
  distinguishing case the two-429-classes note predicts — per-account caps stack across keys and six
  accounts buy six resets, but class-(a) pool saturation is shared and six accounts buy nothing.

### The deployed ensemble: cleaning cost accuracy and produced its first AI-passes-as-human miss

`node n8n/test-ensemble.js replay <primary> <secondary>` runs the shipped "Ensemble & Build Result" node
with the deployed config (0.69/0.40, conf 0.6, secondary 0.90, disagreement 0.30), not a reimplementation.

| corpus | capture pair | correct / wrong / review | acc/all | acc/answered | misjudged |
|---|---|---|---|---|---|
| dirty `corpus.json` | `ultra550` + `diffgemma26b` | 45 / 3 / 0 | **0.938** | 0.938 | hu-004, hu-009, hu-011 (all HUMAN→AI) |
| clean `corpus-clean.json` | `clean550-hc3` + `cleandiff-hc3` | 42 / 5 / 1 | **0.875** | 0.894 | hu-004, hu-009, hu-010, hu-014 (HUMAN→AI) **+ ai-021 (AI→HUMAN)** |
| clean HOLDOUT (OOS) | `clean550-holdout` + `cleandiff-holdout` | 43 / 1 / 4 | 0.896 | **0.977** | ai-012 (AI→HUMAN p=0.38) |

On clean text the deployed detector gets **worse (0.938→0.875)** and, for the first time, leaks a **real AI
text into a HUMAN verdict** (`ai-021` via `TIE_BREAK_HUMAN`). The mechanism is the tie-break: diffusiongemma
already defers aggressively, and once the human class stops carrying the messy-signal that was pushing the
primary confidently to AI, the secondary's human-leaning pull can win on a marginal AI text. The dirt was
partly *propping up the AI side* of the shipped pair. Net human-as-AI also rose 3→4 (hu-010 and hu-014
newly cross the cut; hu-011 stopped). This is the single most decision-relevant number in the round: the
deployed calibration is measured to be **more optimistic on dirty text than the same thresholds deserve on
clean text.**

The holdout replay says the pair is strong on unseen clean text (**0.977 over answered, 1 miss**) — and its
only miss is `ai-012`, the *same* adversarial AI text that broke glm-5.3's "0-wrong" in round 10 (there 0.35,
here 0.38). `ai-012` is a recurring false-negative class, not noise. The `over all` figure (0.896) is depressed
only because 12 diffusiongemma secondary rows returned `http_429`, forcing 4 deferrals — a free-pool rate cap,
not a wrong call.

### Length is a second confound, and it is the one GPTZero warns about

`corpus.json`/`corpus-holdout.json` are not just dirty-vs-clean unbalanced — they are **length-unbalanced by
class**, which is a shortcut independent of formatting:

| corpus | AI median words | HUMAN median words | humans in 50–99 words | any text ≥ 500 words |
|---|---|---|---|---|
| `corpus.json` | 190 | **85** | 13 / 24 | **0 / 48** |
| `corpus-holdout.json` | 170 | **75** | 14 / 24 | **0 / 48** |

GPTZero publishes no hard 500-word minimum (its one enforced number is a 10,000-character *upper* bound), but
its stated reason is the real one: it scores **perplexity** ("is the writing obviously predictable?") and
**burstiness** ("how much variation in sentence length and style?") — both are sample statistics, so a
few-sentence input gives a high-variance estimate and *"short texts or highly edited passages can make
detection harder,"* with *"accuracy improv[ing] with longer inputs."* An external audit backs the mechanism
empirically: bucketing essays short (40–100 w) / medium (100–350) / long (350–800), AI detection held at
91–100% but *"human generated essays fluctuated"* with false positives concentrated in the short bucket —
"reliability in distinguishing human-authored texts is limited." ([arXiv 2506.23517](https://arxiv.org/abs/2506.23517))

This is not hypothetical for us — it is the shape of every problem in this document:

- **Human over-flagging** (haiku 16/24, diffusiongemma 12) *is* the short-essay false-positive pattern. Our
  human median is 85 words, squarely in the unreliable bucket.
- **Run-to-run jitter** (8/12 borderline texts flip on identical reruns) *is* the thin-sample variance a
  length floor exists to damp.
- **A length shortcut is baked in**: AI answers average ~2× the words of human answers, so a model can key on
  "long = AI" without reading style. This is a confound *on top of* the formatting one just removed, and
  neither this round nor the cleaning pass touches it.

The sharpest consequence is for deployment, not the benchmark: **the 0.69/0.40 band was fit on ~85-word human
text.** Long real-world prose scores differently (usually more confidently human), so this calibration is
validated only for short inputs; its transfer to 500-word documents is untested and is now the top open risk.

### What round 11 does not prove

- **The clean free lanes are now scored, with coverage caveats** (section above, captured 2026-10-11 when
  the pools served): 48/48 for glm-5.3-flash and the OR 550B mirror, but only 5/48 (`gemma-4-31b-it`) and
  20/48 (`nemotron-3.5-lightning`) answered, so those two rows are partial. `z-ai/glm-5.3` main remains
  GATE_DEAD on the NVIDIA pool — second sweep, gate of 2 texts at 150s never answered; that names the
  gateway, and its OpenRouter mirror is a different measurement.
- **Length-matched re-measure not run.** Isolating style from word-count would mean restricting both classes to
  a common band (e.g. 120–200 words); that is a separate capture and is explicitly deferred, not assumed done.
- **Still HC3-only.** Both corpora draw from the same source; the AI class is GPT-3.5-era. Cleaning fixes a
  formatting leak, not the modern-model transfer caveat that has stood since calibration.
- **n=48, and single-model deltas are not paired-tested here.** This round reports dirty→clean movement per
  model; it does not claim a new promotion. Nothing is promoted — the shipped pair is still row 1
  `nvidia/nemotron-3-ultra-550b-a55b` + row 2 `google/diffusiongemma-26b-a4b-it`, both un-changed by round 11.
- **The direction that matters for a product is the ensemble, and it moved the wrong way.** Treat 0.938 as an
  upper bound inflated by scrape noise; ~0.875 is what the same thresholds earned on clean in-sample text.

## Round 12: length bands — do the fits hold on bigger and smaller texts? (2026-10-12→15)

The HC3 corpus texts run ~500–2500 characters. Two questions this round had no answer for: does a
model's score (and therefore its fitted band) survive texts 2–4× longer, and does it survive texts
under 300 characters? Corpora were cut from *real* HC3 rows only (`build-length-bands.js` →
`corpus-long.json`, 48 texts ≥3000 chars, and `corpus-short.json`, 24 texts <300 chars) — no
authored text, same provenance discipline as everything else in this file. The AI half of
`corpus-long` is the tail of HC3's own answers; HC3's AI class thins out past 3000 chars, which is
exactly why the separate stress corpus (real pre-AI Gutenberg prose 3000–6000 chars on the human
side, gateway-generated long articles on the AI side, tasks #94/#95) exists.

**Capture yield.** `z-ai/glm-5.3-flash` answered **48/48 on long and 24/24 on short, zero
truncated, zero salvaged** — output format does not degrade with input length. `moonshotai/kimi-k3`
captured the long band at p50 246 s/row and then, mid-resume on 2026-10-15, hit **HTTP 410 Gone**:
NVIDIA retired the model at a scheduled end-of-life (2026-10-15T08:00:00Z). Its long-band capture is
frozen forever at **39 usable/48**, the nine missing rows unrecoverable by any retry — and with it,
the kimi promotion candidacy of round 13 is **void on this gateway**: a lane cannot ship on a model
whose EOL has passed. 410 is a fourth NVIDIA failure class beyond 429/503/400, and unlike the other
three it is permanent. (A same-gateway `top_p=0.95` validation-400 era and two timeout eras preceded
it; see the gateway-findings notes.)

**Accuracy held — and the honest twist is that long text was *easier*, not harder.** Scoring the
captured rows offline (`compare-longbands.js`, zero quota):

| corpus | band | decided | wrong | review |
|---|---|---|---|---|
| long 48 — glm-5.3-flash | old 0.69/0.40/gate-0.6 | 48/48 | 0 | 0 |
| long 48 — glm-5.3-flash | saved v2 0.90/0.35/gate-0.65 | 44/48 | 0 | 4 |
| long 39 — kimi-k3 | old 0.69/0.40/gate-0.6 | 39/39 | 0 | 0 |
| long 39 — kimi-k3 | saved v2 0.70/0.60/gate-0 | 39/39 | 0 | 0 |
| short 24 — glm-5.3-flash | old 0.69/0.40/gate-0 | 22/24 | 6 (2FP/4FN) | 2 |
| short 24 — glm-5.3-flash | old 0.69/0.40/gate-0.6 | 14/24 | 2 | 10 |
| short 24 — glm-5.3-flash | saved v2 0.90/0.35/gate-0.65 | 7/24 | 0 | 17 |

Three readings, in order of usefulness:

1. **On long texts every band tested makes zero wrong calls.** Length inflates confidence and pushes
   probabilities to the extremes, so a band that is merely adequate at 800 chars is comfortable at
   3500. The v2 fits neither break long-band accuracy nor buy any — at worst they convert a handful
   of decisions into reviews (flash 48→44 decided). That is the conservative direction, so the fits
   are safe to keep; but "we hard-tested at 3–6k chars" must not be claimed as evidence *for* the v2
   cuts over the old ones — on this axis they are equivalent, and the stress corpus (task #95) is
   where a real difference can still show up, because it changes the *text distribution*, not just length.
2. **Short text is where bands actually bleed.** The old band put out 6 wrong calls on 24 short
   texts; the saved v2 band erases them (0 wrong) at the price of coverage 92%→29% — the confidence
   gate 0.65 plus the wider review zone deflects almost everything under 300 chars. On fragments
   there is little style to judge, so deferring is arguably the *correct* verdict, not a failure —
   consistent with the length-shortcut confound noted in round 11.
3. **kimi's 0-wrong property survived a third corpus family** (39/39 decided, 0 wrong, 0 review on
   long texts) right up to the moment the gateway deleted it. The record stands as measurement; it
   changes nothing deployable.

Latency at this length: glm-5.3-flash p50 moved from ~6 s on the hc3-48 texts to **168 s** on the
long band, and kimi p50 246 s — long texts sit in the bucket where NVIDIA's ~300 s server-side
ceiling (measured 2026-10-14, `integrate.api.nvidia.com/v1` returns HTTP 504 at 302 s regardless of
client timeout) is survivable for scoring but was fatal for the 8192-token *generation*
requests. Long-text scoring on the community NVIDIA pool is a minutes-per-request affair; anything
user-facing on that lane needs the timeout math done per band, not per model.

## Round 13: the three new gateways — and the first free lane that sweeps both corpora (2026-10-12)

The re-enumeration pass across OmniRoute, Gemini (new provider #4) and the NVIDIA newcomers. Screens are
12 texts; captures are the full 48; every band number here uses the deployed rule (AI ≥0.69, HUMAN ≤0.40,
conf ≥0.60) through the same scorer, never a reimplementation.

### OmniRoute: seven screens, two captures, and the most reliable channel measured

| screen lane | usable/12 | AUC | wrong | defer | acc |
|---|---|---|---|---|---|
| `auto/best-free` | 12 | 1.000 | 4 | 0 | 0.667 |
| `kiro/deepseek-3.2` | 12 | 0.694 | 3 | 1 | 0.667 |
| `kiro/glm-5` | 12 | 0.972 | 1 | 1 | 0.833 |
| `kiro/qwen3-coder-next` | 12 | 0.444 | 6 | 2 | 0.333 |
| `mistral/codestral-latest` | 12 | 0.292 | 7 | 0 | 0.417 |
| `mistral/ministral-14b-latest` | 12 | 0.444 | 5 | 0 | 0.583 |
| `no-think/kiro/claude-sonnet-4.5` | 12 | 0.944 | 1 | 0 | 0.917 |

- **`auto/best-free` is the third documented case that AUC cannot see a band problem.** Perfect ranking
  (1.000) and still 4 HUMAN→AI misses on 12 texts, because the whole human class sits at medHU 0.720 —
  above the 0.40 cut — with no overlap for the AUC to punish. It was eliminated at screen on band, not AUC.
- Four lanes died at screen (`deepseek-3.2`, `qwen3-coder-next`, both `mistral/*`); two earned full captures:

| capture lane | usable/48 | AUC | wrong | defer | acc/corpus | misses |
|---|---|---|---|---|---|---|
| `kiro/glm-5` | 48 | 0.905 | 5 | 1 | 0.875 | hu-003, hu-005, hu-008, hu-014, ai-020 |
| `no-think/kiro/claude-sonnet-4.5` | 48 | 0.986 | 2 | 0 | 0.958 | hu-004, hu-014 |

- **`kiro/glm-5` screens well and fails the capture** — the opposite of what a 0.972 screen promises. It
  over-flags humans *confidently* (three texts at the identical p=0.92, conf 0.85) and still leaks `ai-020`
  at 0.15. Eliminated.
- **`no-think` sonnet is an alternate route, not an improvement**: 48/48 answered, zero salvage, zero
  errors, AUC 0.986, 2 wrong, 0 defer — the same profile class as the deployed kiro sonnet route measured
  in rounds 7–9. Stripping the thinking tokens did not damage the detector.
- **`hu-014` is now a confirmed recurring false-positive class**: flagged by `kiro/glm-5` (0.85) *and*
  `no-think` sonnet (0.75), deferred by kimi — no lane measured in this document decides it correctly as
  human.
- Reliability: the local OmniRoute gateway produced both 48/48 captures this round with **zero salvage,
  zero truncation, zero transport errors** — the cleanest capture channel this project has measured, paid
  tiers included.
- Harness trap found the same night: `catalog-probe.js` writes `catalog-probe-<tag>.json` with default tag
  `primary`, so the Gemini 62-model probe overwrote the NVIDIA census file. No data loss (the census is
  intact in `log-nv-probe-20261011.txt`, 80 per-model lines), but concurrent probes must pass `--tag`.

### Gemini: a drip-fed free tier, not a 20/day tier

62 models probed, 9 screened, 4 capture lanes opened (2 preview-family still dripping; 3.5-flash captured
only 4/48 under class-429 flood, 3.1-lite full-captured at 46/48 and band-dead). The quota shape is unlike
the other three gateways:

- The metric is `generate_content_free_tier_requests`
  (quotaId `GenerateRequestsPerDayPerProjectPerModel-FreeTier`), stated limit **20 per model per project
  per day** — but empirically only **~5–12 requests per model per day are actually delivered**. The
  `retryDelay` in RetryInfo (e.g. 2670 s) is *not* a refill signal: a probe at retryDelay+44 min bought
  exactly one more row.
- Preview names bill into the base-model bucket (`gemini-3-flash-preview` → error body says
  `model: gemini-3-flash`); `*-latest` aliases bill into yet another bucket. Two independent limiters to
  plan around.
- Quota errors arrive both as plain 429/503 **and as HTTP-200-with-ARRAY-body**; the harness treats both
  as retryable.
- **Live-lane implication: the preview-family lanes cap at roughly 10–20 verdicts/day** (measured on
  `gemini-3-flash-preview` and `gemini-3.8-flash` drips). Fine for a benchmark dripped over nights;
  disqualifying as a sole production lane. *Corrected 2026-10-13: this does not generalize across the
  family — `gemini-3.1-flash-lite` delivered 46 requests in one window. See the two paragraphs after the
  screen table.*
- The `gemma` family is contract-dead on this third gateway too (`json_parse_failed` = the model echoes the
  schema template verbatim, `"ai_probability": number`; `missing_ai_probability` = one inner signals object
  and no verdict) — screens returned 4/12 and 3/12. Model-side; unresumable.
- The `lite` family is band-bad on the shipped cuts: AUC 0.81–0.92 but medHU 0.515–0.865, i.e. 3–4 of 12
  humans cross the AI cut. All three lites eliminated at screen.

| gemini lane | screen usable/12 | AUC | wrong | status |
|---|---|---|---|---|
| `gemini-3.8-flash` | 12 | 1.000 | 0 | capture 21/48 (project-2 window, AUC 1.000 0 wrong 0 defer so far) |
| `gemini-3-flash-preview` | (round 9) | 1.000 | 0 | capture 31/48 (project-2 window, AUC 1.000 but 2 wrong: hu-010@0.82, hu-014@0.88) |
| `gemini-3.1-flash-lite` | 12 | 0.917 | 3 | eliminated (band) — later captured 46/48 in one window, band confirmed dead (13 wrong, all HUMAN→AI) |
| `gemini-3.5-flash-lite` | 12 | 0.806 | 4 | eliminated (band) |
| `gemini-flash-lite-latest` | 12 | 0.917 | 4 | eliminated (band) |
| `gemini-3.5-flash` | 6 | n/a | — | quota-throttled screen; capture 4/48 (43×429 + 1×503) — uncapturable at this pool |
| `gemini-3.6-flash` | 0 | n/a | — | not served |
| `gemini-3.7-flash` | 1 | n/a | — | not served |

`gemini-3.8-flash`'s screen is the strongest free number on this gateway: AUC 1.000 with **medHU 0.060** —
humans sit far below the cut, not just correctly-ranked. Captures run as multi-day windowed resumes
(`tries=1`, ~48–75 s per answered row). **Hop-3 landed 2026-10-12/13 (~07:00 MST window): both lanes at
15/48** (preview +6, 3.8-flash +5 — squarely in the measured 5–12/day drip), and *at current coverage both
carry AUC 1.000, 0 wrong, 0 defer over their 15 answered rows* (n-bounded, partial — hop-4 is scheduled for
the next window; ~3 more hops to completion).

**The drip rule has now been refuted for one lane, and it matters for how you read every number above
(2026-10-13).** `gemini-3.1-flash-lite` was launched as a hop-fed capture on the same assumption as the
preview lanes — ~5–12 delivered per model per day — and instead delivered **46 of 48 texts in a single
window** (`usable=46/48`, the 2 failures both `http_503`, p50 ~5 s). So the delivered volume is *per lane*,
measured, not a gateway-wide constant: the preview-family drip is real for preview-family lanes, and a
46-request burst is real for a lite lane. Treat any "Gemini caps at N/day" sentence as applying to the
model it was measured on until proven otherwise.

**And the screen's verdict survived its own capture — the fourth gateway confirming the bimodal rule.**
Scored on those 46 rows (22 AI / 24 HUMAN): AUC 0.955, but at the shipped 0.69/0.40 band the lane is
**13 wrong, 0 deferred, every one of them a HUMAN text read as AI** (hu-003 … hu-023, clustered at
p 0.82–0.92), with the human class sitting at Q1 0.12 / median 0.835 / Q3 0.88 — a bimodal scorer whose
upper mode fully overlaps the AI class. This is the same failure class as `auto/best-free`, haiku and
diffusiongemma: a ranker, not a detector, and no per-model band rescues it (there is no cut that puts the
0.82–0.92 human mode below the AI texts at 0.88+). Elimination at screen cost nothing here — the capture
only confirmed what the 12-text band score already said, while *adding throughput that tempts you to
reconsider a lane you correctly closed.*

**A second Google project doubles the drip, per-model (2026-10-13, `GEMINI_API_KEY_2`).** The quota is
per project per model, so a second key is a second bucket for every lane — no calendar wait. One window on
project 2: `gemini-3.8-flash` 15→**21/48** (its bucket served 8 rows incl. the gate, then 429'd — the same
~5–12 delivered shape as project 1), `gemini-3-flash-preview` 15→**31/48** (**+16 rows in one window — the
5–12 drip was project-1's number, not a family law**, exactly the per-lane lesson from lite), and
`gemini-3.5-flash` 4→9/48 (38 fresh 429s — its project-2 bucket arrived near-empty, consistent with the
gate-probe-spends-bucket effect). Scored at current coverage: 3.8-flash stays the clean profile (AUC 1.000,
0 wrong, 0 defer, medHU 0.070 on 21 rows); **3-flash-preview took its first two band misses — hu-010 at
0.82 and hu-014 at 0.88, both clean rows, not salvage** — and `hu-014` is the known recurring false-positive
class (also flagged by glm-5 and no-think sonnet). AUC is still 1.000 on both lanes: the human upper tail
overlapping the AI class is precisely the band problem AUC cannot see, now demonstrated on this gateway's
flagship screen at n=31.

### NVIDIA newcomers: one contract gotcha, one ended era, one full sweep

| lane (screen, 12 texts) | usable | AUC | wrong | note |
|---|---|---|---|---|
| `moonshotai/kimi-k3` (top_p 0.95) | 12 | 1.000 | 0 | first try 0/12 — see gotcha |
| `deepseek-ai/deepseek-v4.1-flash` | 12 | 1.000 | 0 | medHU 0.075; era ended overnight |
| `z-ai/glm-5.3-flash` | 12 | 1.000 | 0 | alive, p50 7.8 s — round 11 confirmed |
| `google/gemma-4-31b-it` | 2 | 0.500 | 1 | pool death mid-run (84 s→203 s+) |

- **kimi-k3 hard-rejects `top_p` ≠ 0.95** (`400 Validation: top_p is fixed at 0.95 for Kimi K3`). The
  initial 0/12 was *our* parameter, not the gateway. Re-screened at 0.95: 12/12.
- **deepseek-v4.1-flash was an availability era, not a time-of-day effect**: 12/12 on the evening of
  10-11, 2/48 overnight, 0/2 on a morning probe. The conditional-resume design (probe before resuming)
  is what avoided a six-hour dead run.
- `gemma-4-31b-it`'s NVIDIA pool degraded mid-window the same shape as its earlier OpenRouter saturation:
  slow first, then `exception` aborts. Lane closed on both gateways.

### kimi-k3: the first free lane with zero wrong on in-sample *and* out-of-sample

| corpus | answered/48 | AUC | wrong | defer | acc/corpus | medAI | medHU |
|---|---|---|---|---|---|---|---|
| `corpus.json` (in-sample) | 48 | 1.000 | 0 | 3 | 0.938 | 0.860 | 0.220 |
| `corpus-holdout.json` (OOS) | 48 | 1.000 | 0 | 2 | 0.958 | 0.860 | 0.200 |

- Both corpora answered **48/48 with zero salvage, zero truncation, zero transport errors** on the free
  NVIDIA pool (`log-nvfull-kimi*.txt`, `log-nvholdout-kimi*.txt`), p50 latency 13.6 s in-sample.
- In-sample deferrals: `hu-011`, `hu-014`, `ai-020` — all low-confidence under the 0.60 gate, none a
  wrong decision. Holdout deferrals: `ai-007` (p=0.62, conf=0.55), `hu-007` (p=0.32, conf=0.58).
- **`ai-012` — the adversarial AI text that broke glm-5.3's out-of-sample run (0.35) and the ensemble's
  clean replay — kimi decides correctly as AI at p=0.78, conf=0.66.** The recurring false-negative class
  is handled by this lane on the exact texts that killed the others.
- McNemar (`paired-gap.js`, p-band-only rule 0.69/0.40 applied to both sides, same 48 holdout texts):

| pair | kimi side | incumbent side | discordant | exact p |
|---|---|---|---|---|
| vs `oos-omkiro-sonnet-4-5` | 47 right / 0 wrong / 1 defer | 47 right / 1 wrong / 0 defer | 1 | 1.0000 |
| vs `oos-nv-glm-5-3` | 47 right / 0 wrong / 1 defer | 45 right / 1 wrong / 2 defer | 1 | 1.0000 |

  The single disagreements both favor kimi (`hu-007`: 0.32 correct vs sonnet's 0.72 human-as-AI;
  `ai-012`: 0.78 correct vs glm's 0.35 miss). With one discordant pair the exact test says
  **indistinguishable** — the honest reading is structural, not statistical: at n=48 a one-error
  difference cannot be separated (and jitter flips ~8 of 12 borderline texts between identical runs),
  but kimi is the only lane ever measured here with **0 wrong on both corpora**, while the paid-adjacent
  sonnet route and the free glm lane each take one on the same holdout.
- **Re-run the same night (`nvholdout2-kimi-k3`, 2026-10-12, identical params): the 0-wrong profile
  reproduced.** 48/48 answered, zero salvage/truncation/transport errors, AUC 1.000 again, **0 wrong
  again**, 3 defers. The pool, however, was ~7× slower that window (p50 96 s vs ~13 s earlier; wall clock
  5182 s) — serving and slow, the known NVIDIA regime, and a reminder that a live-lane latency verdict on
  this pool is time-bound.
- Per-text movement between the two holdout captures quantifies the jitter for this lane: 33 of 48 scores
  changed (typical drift ±0.04–0.10), and exactly **3 verdicts moved — none of them into a wrong call**:
  `ai-007` deferred→correct AI (0.62→0.72, conf 0.55→0.62), `hu-016` and `hu-022` correct HUMAN→deferred
  (0.25→0.62, 0.15→0.42). Worst-case jitter demotes a decision to REVIEW; it never created a false
  positive, and `ai-012` reproduced its correct AI call (0.78→0.74, conf 0.66 both runs).
- **Promotion status: candidate, not promoted.** Outstanding gates: a live n8n webhook run through the
  real lane plumbing, the `top_p=0.95` lane config, and multi-day pool-stability observation — the
  deepseek era collapse and NVIDIA's silent 404 entitlement withdrawals are the cautionary precedents.
  Decision presented to the user; nothing deployed without their sign-off.

## Round 14: the stress test — long human articles vs long self-generated AI articles (2026-10-15)

The standing question from the user: *"for the perfect model hard test them for article from human
before ai books and ai generated text long."* Round 12 answered the length part for HC3 text; this
round builds a corpus that is *not* HC3 at all on either side.

**Corpus.** `benchmark/corpus-stress.json`, 42 rows, every text 3000–6000 chars, verified zero
sha256 overlap against all six prior corpora (`merge-stress-corpus.js` refuses on any collision):
33 HUMAN long passages from pre-AI published books (Gutenberg, provenance-verified) and 9 AI
self-generated long articles (gemma-4-31b-it ×5, nemotron-3.5-lightning-30b-a3b ×4 after band
filtering; temperature 0.7, top_p 0.9, max_tokens 3000 — config recorded per row). The generation
pass made 24 attempts and honestly yielded 10 usable articles: deepseek-v4.1-flash returned **zero**
(every attempt died on NVIDIA's ~302 s server-side 504 while generating) and one 7773-char gemma
article was dropped for exceeding the band rather than loosening the contract.

**Capture yields on scoring.** sonnet 42/42 (p50 3.3 s, OmniRoute), glm-5.3-flash 42/42 (p50 143 s),
ultra 41/42 (one permanent 503), glm-5.3 37/42 (five rows 504'd at the ~300 s ceiling even with a
450 s client timeout — and the five missing rows are all HUMAN, so the AI class is fully covered).
salvaged=0, truncated=0 everywhere: the output format survives 5k-char inputs.

**Verdict at the saved v3 bands** (decided/wrong over the stress capture alone):

| model | band | decided | wrong | AI p range | HUMAN p range |
|---|---|---|---|---|---|
| kiro/claude-sonnet-4.5 | 0.75/0.30/g0 | 42/42 | 0 | 0.85–0.92 | 0.02–0.15 |
| z-ai/glm-5.3-flash | 0.80/0.55/g0.50 | 42/42 | 0 | 0.88–0.95 | 0.01–0.05 |
| z-ai/glm-5.3 | 0.65/0.30/g0.55 | 37/37 | 0 | 0.86–0.93 | 0.01–0.03 |
| nvidia/nemotron-3-ultra-550b | 0.70/0.45/g0.65 | 39/41 | **5** | 0.15–0.85 | 0.01–0.05 |

Three models transfer perfectly, with margins far from both cuts. The paid flagship does not: five of
the nine AI articles scored 0.15–0.30 with confidence 0.75–0.88 — confidently wrong HUMAN calls on
the same texts every other model reads at ≥0.85.

**No band rescues ultra.** `band-sweep.js` grid-searches every candidate over old fit data + stress
jointly: the only bands with zero stress wrongs (e.g. 0.80/0.05) decide just 24/96 old rows and 34/41
stress rows — they avoid the error by abstaining on most of everything. The failure is a capability
gap on this AI class, not a threshold misfit, and re-fitting on train+val+stress would only convert
coverage into false restraint. Combined score over ALL corpora at the saved bands (`fit-bands.js --eval`
with the stress tags now registered as val data): sonnet 272 decided/2 wrong, glm-5.3-flash 195/3,
glm-5.3 172/0, ultra 176/11.

**Consequence for the lane choice.** The deployed single lane is nemotron-3-ultra (row 1). On the
evidence of rounds 10–14 — holdout FN (ai-012), short-band bleed, and now systematic misreading of
fresh long self-generated articles — the free trio glm-5.3 / glm-5.3-flash / sonnet is strictly more
trustworthy on the class of text the verifier site actually receives. Switching the primary lane is
a deployment decision and was presented to the user with these numbers; nothing changed without
sign-off.

Ops notes: glm-5.3 long-text scoring p50 of 263 s means a client timeout below ~300 s converts a
slow pool into fake rows (8 aborts on pass 1, recovered 37/42 at 450 s minus the 504 ceiling);
ultra's 503 storm cleared in minutes; and the harness print assumed `config` is a string, which
crashed the first sonnet resume on object-shaped self-generated rows (`String(item.config)` fix).

## Round 15: the token cap is deleted, not raised (2026-10-16)

The phrase-evidence work made the cap a correctness problem instead of a latency problem. A verdict
that arrives is useless to the UI if it stops before the JSON closes: no `phrases`, so nothing to
paint green or red. The deployed primary at 700 tokens was doing exactly that, while the same model
at 1400 and other lanes finished with quotes. Raising the number again would only move the ceiling,
and every free lane answers a differently-sized question, so the cap was removed as policy:

- `max_tokens` **0 is now the contract for "uncapped"**, and 0 is what the code defaults to. The
  workflow builds the gateway body with `...(cfg.max_tokens > 0 ? { max_tokens: cfg.max_tokens } : {})`,
  so an uncapped request carries no `max_tokens` key at all — the gateway's own default applies.
- The tuning validator floor went from 16 to 0 (`TUNING_FIELDS`), which is what lets a stored 0 be
  *sent* rather than rejected. This ordering matters for a redeploy: the old version 400s on
  `tuning.max_tokens = 0`, so the new workflow must be live **before** any DB row is flipped.
- The answer-provenance block echoes `"max_tokens": "auto"` instead of a number, and the result card
  reads `max_tokens auto (no cap)`. A number is still echoed when a cap is deliberately set.
- The prompt's trailing "Keep the whole response under 400 tokens." is gone from both the workflow and
  the harness; they are asserted byte-identical (785 characters each) so a capture stays comparable
  to what production sends.
- Defaults followed: `backend/db.py` column default 700 → 0, `backend/seed.py` `LLM_MAX_TOKENS`
  default 700 → 0, harness `--max_tokens` default 700 → 0. All 31 `models` rows in the dev database
  were set to 0. The key-verification probe keeps its `max_tokens: 8` — it asks for one word and a
  cap there is not a truncation risk, it is the point.

**What this costs, and it is not yet paid.** Every saved band in `models` was fitted on answers that
came back under a 700- or 1400-token cap with the 400-token instruction in the prompt. Removing both
changes what the model can say, so the bands are now measurements of a configuration that no longer
exists. The direction of the change is favourable (a longer answer can only contain more evidence, and
salvage-of-a-truncated-object is the failure mode being eliminated), but the *probability* a model
reports is not known to be cap-invariant: round 11 already showed the sign of a style effect flipping
between models. Re-validation therefore has to be a fresh full 48-text capture per deployed lane,
scored against the disjoint holdout, plus a live webhook check, before any band is quoted as current.

**Verified live the same day.** Through the real app path (`POST /api/detect` on the dev database,
single lane, secondary off), a 1 462-character AI text and a 1 343-character human text both returned
`sampling.max_tokens: "auto"` with `tuning_applied.max_tokens: 0`, and **4 verbatim phrases each** with
direction and strength — three graded HUMAN and one AI on the human text, three AI and one HUMAN on the
AI text. Verdicts were right in both directions (AI 0.85 / HUMAN 0.15) in 4.6 s and 4.4 s. That is the
behaviour the cap was blocking: the answer closes its JSON, so the quotes exist.

The *deployed* NVIDIA primary could not be included in that proof, and the reason is the pool, not the
code: an uncapped gateway-level probe answered 0/3 (every leg hitting the 90 s client timeout across
4 tries, 756 s wall clock, `error_code: exception`, `benchmark/results-nocap-probe3.jsonl`) and the
app's own detect 502'd at its 120 s wall clock. Ultra's uncapped phrase yield therefore stays unmeasured
until the pool answers, and it is the first thing #109 has to capture.

One ops finding came out of the same verify, and it applies to any self-hosted gateway: the
`omniroute` provider was stored as `http://localhost:20128/v1`, and the workflow's fetch resolved
`localhost` to `::1`, where nothing listens — `ECONNREFUSED ::1:20128` in 9 s while the very same
gateway answers on `127.0.0.1:20128`. A local gateway base_url has to name the IPv4 loopback
explicitly; `localhost` is not portable across stacks that bind v4 only.

## Round 16: the bands are re-fit off the probability lattice, not off the cap (2026-10-16)

Round 15 left a debt: every saved band measured a configuration that no longer exists. This round pays
it for the one lane that can answer today, and the answer to the debt question is not the one the
round-15 note guessed at.

**Was the cap the variable? No — and the honest control says so.** The Oct 11 capture could not serve
as the control because it predates the phrase prompt, so comparing it to an uncapped run moves two
things at once. The control is a second full capture *with the current prompt* at 700 tokens
(`capclean-omkiro-sonnet-4-5`), so cap-vs-no-cap differs in exactly one field. `benchmark/paired-gap.js`
on the two same-corpus captures: **McNemar exact two-sided p = 1.0000, 0 discordant pairs** at both the
old and the new cut, mean |Δp| **0.0115** over 48 paired texts. Removing the cap changed no decision
this model had already made.

**What it did change is the lattice.** The capped answers emit `{0.15, 0.25, 0.35, 0.75, 0.85, 0.88,
0.92, 0.95}`; the uncapped ones emit `{0.15, 0.25, 0.72, 0.75, 0.85, 0.88, 0.92}` — `0.72` is new and
`0.35`/`0.95` are gone. That matters because the shipped AI cut was **0.75**, a value the model actually
prints. A human text that rounds to 0.75 gets a confident AI verdict; the same text at 0.72 defers. The
two regimes disagree exactly there: `hu-005` answers 0.75 capped (a false positive) and 0.72 uncapped
(a deferral). This is not a cap finding, it is a **cut-placed-on-a-populated-probability** finding, and
the run-to-run jitter noted in round 9 is what makes it a live risk rather than a curiosity.

**Pricing the trade in operating points** (hum 0.30, gate 0, two disjoint 48-text uncapped captures —
`nocap-clean-omkiro-sonnet-4-5` fit and `nocap-holdout-omkiro-sonnet-4-5` holdout):

| AI cut | train decided | train wrong | holdout decided | holdout wrong |
| --- | --- | --- | --- | --- |
| 0.75 | 47 | 2 | 47 | 0 |
| 0.80 | 44 | 0 | 42 | 0 |

Any cut in (0.75, 0.85) is the same point because nothing lives between those two lattice values, so
the interval was collapsed to its midpoint with the largest distance from any emitted probability. The
exchange rate is explicit: **4 deferrals bought per false positive removed** (`flipK` = (94−86)/(2−0) =
4.0). The user chose the 0.80 point — 86/96 decided, 0 errors — over 94/96 with 2 boundary false
positives. That is a product decision, not a measurement, and it is now recorded as one.

**The fitter encodes the reasoning** so the next refit cannot re-commit the same error.
`benchmark/fit-bands.js --knee` takes a new last tie-break: on equal utility and equal decided count it
prefers the candidate whose distance to the nearest value the model actually emits is largest (`margin`
in `knee-picks.json`, printed per pick and stamped into the stored note). `--knee` also now accepts
`--train=` / `--val=` capture overrides, which is how a single regime gets fitted without dragging the
capped-era tags back in. `MAP[30]` and `band-sweep.js` `ROWS[30]` point at the two uncapped captures.
Regression check: refitting **all** rows on their existing data produced the identical band for every
model except row 30, and re-running row 30 after the change reports `changed: false` — the fit is
idempotent. `--eval` on the stored band: `30 kiro/claude-sonnet-4.5 @ 0.8/0.3/0 : c86 w0(0FP/0FN) r10
cov=90%`.

**Proven live through the app path.** `models` row 30 was written and then read back through a real
authenticated `POST /api/detect` on single-lane `kiro/claude-sonnet-4.5` with the secondary off. The
response echoes `config_used.thresholds.ai_prob: 0.8` with `sampling.max_tokens: "auto"`:

| text | label | p | verdict | review | phrases |
| --- | --- | --- | --- | --- | --- |
| hu-014 | HUMAN | 0.75 | REVIEW_REQUIRED | true | 4 |
| hu-005 | HUMAN | 0.72 | REVIEW_REQUIRED | true | 4 |
| hu-004 | HUMAN | 0.85 | AI_GENERATED | false | 4 |
| ai-000 | AI | 0.85 | AI_GENERATED | false | 4 |
| hu-000 | HUMAN | 0.25 | HUMAN_LIKELY | false | 4 |

hu-014 and hu-005 are the two texts the old band would have called AI; both now defer. **4 verbatim
phrases on every one of the five**, uncapped, which closes the round-15 open question for this lane.
And `hu-004` is the honest limit of the whole exercise: it answered 0.75 in the fit capture and **0.85
live**, and a cut at 0.80 does not stop a human text that lands on 0.85. Raising the cut moves FP risk
up one lattice step, it does not remove it. Only a second holdout that holds `hu-004` at 0.85 would
justify 0.88 — and the holdout capture answers that text at 0.15, which says the variance is in the
model, not in the text.

Two tooling gaps surfaced and were not papered over. `benchmark/patch-bands-v3.js` did an
unauthenticated `fetch`, and `/models` has required a session since #100, so the patch tool could not
write to the auth'd backend — row 30 was written directly. **Closed later the same day:** the tool now
logs in with `ADMIN_USERNAME`/`ADMIN_PASSWORD` from the environment (the password is never an argument
and the log prints only a `sha1:` fingerprint), carries the signed-cookie session, and re-reads the row
after every `PATCH` to prove what landed. Verified against a throwaway instance on its own DB rather
than against the dev store: the happy path rewrote row 1 (`0.69/0.4/0.6/0.6 -> 0.61/0.42/0.55/0.55`,
`VERIFIED from re-read`), `changed=false` rows are named instead of silently dropped, and each failure
mode has its own exit code — 2 no password in the environment, 3 login refused, 4 logged in but role is
`user` (confirmed with a registered non-admin: `PATCH /models is admin-only`), 5 `GET /models` failed.

The second gap was worse than it looked, because it produced *confident wrong answers*. `BACKEND_API`
takes an origin with no path: `backend.app` serves the API at `/`, and `backend/auth.py` compares
against prefix-less `PUBLIC_PATHS`, so `POST /api/auth/login` answers `401 AUTH_REQUIRED` **even with a
correct password**. `/api` is a browser-side convention (`frontend/src/app/core/backend.service.ts`)
that the SPA proxy strips before Flask sees it, which is why the UI and this tool need different bases.
A wrong base used to print `login FAILED 200 ...: null`, which reads like a credential problem. The
messages now separate the three shapes seen in practice — `BAD_CREDENTIALS` (right origin, wrong
password), `AUTH_REQUIRED` (a `/api` prefix on a prefix-less guard), and a non-JSON body (a page server
answered: wrong port, or a second process already bound to it). The last case is not hypothetical: an
orphaned `nohup python -u .tmp_spa_serve2.py` from earlier work was still listening on 8795 beside the
throwaway backend, so requests were split between two servers on two different databases and a probe
`POST /auth/register` wrote its user into neither of the ones being inspected. Werkzeug rebinds a busy
port instead of failing, so `netstat -ano | grep :<port>` showing two PIDs is the check.

The redeploy check also wasted a probe on `POST /webhook/ai-detect-ai-only`: the workflow's real path is
`ai-detect-ai` (`n8n/ai-only-workflow.json`), confirmed by the workflow's own 400 `INPUT_REJECTED` on an
empty body.

**Still unvalidated, and no accuracy number for these may be quoted as current:** the deployed NVIDIA
primary and secondary bands, and `kiro/claude-haiku-4.5` (0.75/0.35), are all still capped-era fits. The
pool refused today's attempts — the 2-text reachability probe returned **usable 0/2**, both legs dying as
`error_code: exception` at exactly 45 s (`benchmark/results-nvstatus-probe.jsonl`) — so re-validation of
those rows is blocked on pool availability, not on method.

## Round 17: the strength ramp is scaled on the evidence the lanes emit, not on 0..1 (2026-10-16)

Round 16 put verbatim quotes on screen, and the colour that paints them mapped `strength` linearly
onto 0..1. That is a design guess about a measurable quantity, so it was measured:
`benchmark/phrase-strength-range.js` reads the phrase-bearing capture rows and prints the
distribution, then evaluates the **shipped** ramp against it (the script transpiles
`frontend/src/app/ui/result-card/evidence-color.ts` with esbuild and runs that module, so the numbers
below are the ones the browser computes, not a reimplementation). Corpus: the deployed primary
`kiro/claude-sonnet-4.5`, three captures — uncapped fit, uncapped disjoint holdout, capped control —
**575 quotes over 144 answered texts** (192 + 192 + 191), **355 AI / 220 HUMAN**, and **0**
salvaged-or-truncated rows carrying quotes, which the script asserts rather than assumes because the
parser strips evidence from those.

| set | min | p10 | p25 | p50 | p75 | p90 | p95 | max | distinct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ALL | 0.30 | 0.50 | 0.65 | 0.76 | 0.83 | 0.88 | 0.90 | 0.95 | 44 |
| AI | 0.30 | 0.50 | **0.71** | **0.80** | 0.85 | **0.88** | 0.91 | 0.95 | 40 |
| HUMAN | 0.30 | 0.50 | 0.60 | 0.70 | 0.80 | 0.85 | 0.86 | 0.92 | 29 |

Two properties drive the redesign. Nothing is ever emitted below **0.30**, so a 0..1 ramp spends its
bottom third — 30% of its perceptual range — on values that do not exist. And the AI evidence is
crowded at the top: three quarters of the 355 AI quotes lie in **0.71–0.95**, which under 0..1 scaling
is the last 24% of the ramp. Over that interval the old ramp moved **17 green units and +0.072 alpha**
— the exact region where a reader has to tell strong evidence from merely typical. Unlike the
`ai_probability` lattice of round 16, strengths are near-continuous at two decimals (40 distinct AI
values in 355 quotes), so colour genuinely can carry an ordering worth rendering.

The ramp is therefore anchored to the observed domain — `STRENGTH_FLOOR = 0.30`, `STRENGTH_CEIL = 0.95`
— with six hue stops placed **on the measured quantiles** (t 0.63 = p25, 0.77 = p50, 0.89 = p90) so hue
advances fastest where the evidence sits. Values below the floor or above the ceiling clamp to the
endpoints, and the mapping is monotonic, so ordering never inverts.

| measure over the observed AI strengths | shipped (round 17) | previous (0..1) |
| --- | --- | --- |
| distinct observed AI strength values | 40 | 40 |
| green-channel spread across the range, units | 148 | 70 |
| green units spent from AI p25 (0.71) to p90 (0.88) | 58 | 17 |
| alpha gained over that same interval | 0.089 | 0.072 |
| alpha spread across the whole range | 0.340 | 0.273 |
| alpha ceiling | 0.600 | 0.699 |
| HUMAN alpha, weakest quote (0.30) to strongest (0.92) | 0.140 → 0.541 | 0.298 → 0.598 |

Hue separation roughly doubles where it matters (17 → 58 green units across the crowded band) while
the alpha ceiling **drops** 0.699 → 0.600, because the strength signal is now carried by hue as well as
by depth. That drop is the legibility half of the change: the palest quote renders at 0.26 instead of
the old 0.426 — under the previous ramp even the *weakest* evidence any lane ever reported (0.30) was
already painted deep orange at alpha 0.43, which is how a design lie looks on screen.

Sample of the shipped function (values it prints; the browser serialises the same numbers ±0.001):

| strength | t | shipped AI colour | alpha |
| --- | --- | --- | --- |
| 0.30 | 0.000 | rgba(236, 200, 106) | 0.260 |
| 0.45 | 0.231 | rgba(234, 183, 88) | 0.338 |
| 0.71 (p25) | 0.631 | rgba(231, 133, 55) | 0.474 |
| 0.80 (p50) | 0.769 | rgba(235, 104, 47) | 0.522 |
| 0.88 (p90) | 0.892 | rgba(243, 75, 52) | 0.563 |
| 0.95 | 1.000 | rgba(248, 52, 46) | 0.600 |

**Verified, including what was not.** `npx ng test --watch=false` → 49 passed (result-card now 14
specs; 4 of them new: floor/ceil clamping, the crowded-top separation, the alpha legibility band
0.1..0.62 across the whole domain, and "never transparent"). `npm run build` → `main-MA4W2S7R.js`, and
that rebuilt bundle was then driven in the browser — the real `result-card` component, a captured
4-phrase answer fed through a `window.fetch` stub, colours read back with `getComputedStyle`:

| rendered mark | direction | strength | computed background | ink (light / dark) |
| --- | --- | --- | --- | --- |
| quote 1 | HUMAN | 0.90 | rgba(71, 201, 138, 0.53) | rgb(22, 32, 58) / rgb(232, 238, 252) |
| quote 2 | HUMAN | 0.70 | rgba(71, 201, 138, 0.396) | same |
| quote 3 | HUMAN | 0.60 | rgba(71, 201, 138, 0.333) | same |
| quote 4 | AI | 0.45 | rgba(234, 183, 88, 0.337) | same |

The legend now shows four swatches instead of a gradient bar — human 0.70, AI weak 0.30, AI typical
0.71, AI strong 0.90 — each labelled with the strength it renders, so the key is drawn from the same
function as the highlights and cannot drift from them.

**No contrast ratio is reported, and this round can say why rather than decline.** The measurement
says the panel behind the text computes to `rgba(0, 0, 0, 0)`: the glass panels are translucent, so
there is no opaque backdrop to composite against and any WCAG figure would be arithmetic performed on
a fiction. What can be stated is the bound that actually matters: alpha never exceeds **0.600**, and
the ink stays the theme's own foreground colour — the tint is a highlight, not a text colour.

Two structural notes came out of the same pass. The ramp moved into its own Angular-free module
(`evidence-color.ts`, re-exported by `result-card.ts`) precisely so the benchmark could import the
shipped function instead of paraphrasing it — the measurement and the UI now share one implementation,
and the tests and browser proof above were re-run *after* the move. And the recurring
`ECONNREFUSED ::1:20128` was root-caused to a **seed reverting a fix**: `providers.omniroute` was
stored as `http://localhost:20128/v1`, n8n's fetch resolves `localhost` to `::1`, and every boot of
`serve_local` re-seeded that row from `.env` and `seed.py`, undoing any direct database edit. Both
defaults are now `http://127.0.0.1:20128/v1` with the reason in the seed string, so the fix cannot
silently revert; `python -m pytest backend/tests -q` → 89 passed.

**Owed by this measurement:** the floor, ceiling and stops describe the *deployed primary's* output
habits. The NVIDIA pair and `kiro/claude-haiku-4.5` (#111) have never answered uncapped with phrases,
so if they emit 0.2 or 0.99 the re-run of the script will show it and the stops move — the ramp is
data, not a constant to inherit. The stops were also placed on two-decimal rounded values; a lane
reporting three decimals would land between them (the ramp interpolates, so it degrades smoothly).

## Round 18: the shared page is verified from the outside, and it was broken in one place (2026-10-16)

Everything this round needed was a real non-admin account on a real server, not another capture. A
throwaway launcher brought up `backend.serve_local` on 127.0.0.1:8820 (one listener, verified with
`netstat -ano | grep :8820` after the two-owners-of-one-port incident in round 16), and the agent
browser registered `uiverify1` through the actual Angular form. What the signed-out and signed-in
DOMs report:

Nav for `role: "user"` is `Detect / Ranking / History` with the account chip reading
`uiverify1 USER`; the Admin tab is absent from the DOM, not merely hidden, and the admin route
refuses to open for a non-admin (`app.ts` guard plus the spec at `app.spec.ts`). The Admin panel
itself was read only while a stale admin cookie was still in the jar — band fields
(`0.69/0.40/0.65` on the primary row, `temp/top_p/max_tok (0 = auto)/ai ≥/human ≤/tie-break ≥`) and
the copy "…become the defaults for every user — they never see or change these parameters." On the
user views the only inputs present anywhere in the DOM are `provider`, `key` (type=password) and
`model`: no threshold, no sampling field. That is the "users can't touch advanced parameters"
bullet, confirmed by inspecting what a non-admin can actually focus.

Ranking replaced the research page as required: `Model ranking` heading, one circular metric per row
(`.ring-metric > svg.ring-svg > circle.ring-track + circle.ring-value` with `.ring-num`/`.ring-unit`
rendering `100 %` `accuracy`), the tier sentence "Full captures first — fewest wrong, then widest
coverage — then probes on a short sample", per-row `#1 z-ai/glm-5.3-flash 100% 47 right 0 wrong
1 review coverage 98% on 48 texts`, the credential chips `free tier / shared key / add a key /
deployed · primary / deployed · secondary`, then `Your API keys` and `How to get an API key` with the
three redirects (`build.nvidia.com`, `openrouter.ai/keys`, `aistudio.google.com/app/apikey`). No paper
link renders — `body` contains no `paper|preprint|doi` string — because `paper_url` is still blank
until the Zenodo deposit exists, which is the "redirect to the paper" bullet wired and dormant.

Design bullet, measured rather than eyeballed: `.panel` computes `backdrop-filter: blur(18px)
saturate(1.4)` with `rgba(24,34,58,.55)` over a blue radial-gradient body in dark, and the same blur
with `rgba(255,255,255,.62)` and text `rgb(22,32,58)` in light. The toggle is a real control
(`aria-label="Switch to light mode"`), it flips `data-theme` on `<html>` both ways, and it restored
dark afterwards. History for this user reads "Nothing saved yet. Run a detection on the Detect tab
and it will appear here, private to your account." — private being the operative word: `detections`
is keyed by `user_id` and this account had no rows.

**The defect the browser found.** The add-a-key form never let anyone submit. A field filled by the
*agent browser's trusted `fill`* (18 chars in the DOM, confirmed in the accessibility tree) still
left `button[type=submit]` disabled, and a synthetic `input` event behaved identically — so this was
not a test artifact. Cause: `canSave` was a `computed()` reading **plain class fields** bound with
`[(ngModel)]`. The app is zoneless (no `zone.js` in `package.json`, no `polyfills` in `angular.json`),
so a computed with no signal producers is cached after its first evaluation and nothing schedules the
re-check that would notice the typed key. It had survived because no spec had ever typed into that
field.

Fix (`frontend/src/app/ui/ranking-view/ranking-view.ts`): `providerId`/`newKey`/`verifyModel` became
writable signals, the form dropped `FormsModule` for native `[value]` + template-bound `(input)` /
`(change)` listeners (which is what schedules change detection in a zoneless app), and `canSave` stayed
a computed — now with real producers. New `ranking-view.spec.ts` drives it the way a user does (set
`.value`, dispatch `input`, flush the scheduler — no component-property pokes) across six cases: empty
form stays disabled, typing enables it, the trimmed key plus `provider_id` reach `POST /my/keys`,
naming a model triggers verification, a 400 renders the backend's message and leaves the form usable,
whitespace never submits. `npx ng test --watch=false` → **Tests 55 passed** (was 49); `npm run build`
clean. Live re-check against the rebuilt bundle on 8820:

```
step1_empty_disabled            true
step2_dom_value                 "bad key !!!"
step2_disabled_after_typing     false          <-- was true before the fix
step3_message                   "api_key must be 1-512 chars of [A-Za-z0-9._:+/-]"
step3_is_bad_style              true   (.doc-status.bad, color rgb(255, 95, 109))
step4_btn_usable_again          false
keys_rows_now                   "(no key rows)"   <-- the 400 was rejected before storage
```

The rejection text is the server's own — `fetch POST /api/my/keys` under this session returned
`400 {"error":"api_key must be 1-512 chars of [A-Za-z0-9._:+/-]"}`, and the UI now surfaces it instead
of failing silently. Lesson worth keeping: in a zoneless Angular app, **a form field that gates a
binding must be a signal**; `[(ngModel)]` on a plain field is fine when a click handler reads it, and
silently broken when a `computed` or an enable/disable binding depends on it.

**kiro/claude-haiku-4.5 re-validated, and it did not move.** #111's OmniRoute half is closed. The lane
answered `48/48` usable on `corpus-clean` in 42 s and `48/48` on the disjoint `corpus-holdout-clean`
in 42 s, uncapped with the phrase prompt (`results-nocap-clean-omkiro-haiku-4-5.jsonl`,
`results-nocap-holdout-omkiro-haiku-4-5.jsonl`), and `fit-bands.js` row 31 now points at those two
instead of the capped-era `omfull-*` lists:

```
node benchmark/fit-bands.js --knee --model 31
  saved   0.75/0.35/g0: train c27 w0(0FP/0FN) r21 | val c22 w0(0FP/0FN) r26
  K=0   pick 0.55/0.50/g0    decided=96 wrong=31   (train w15 15FP | val w16 16FP)
  K=1   pick 0.55/0.50/g0.7  decided=95 wrong=30
  K=2/5/40 pick 0.75/0.35/g0 decided=49 wrong=0    margin=0
  FINAL K=5 floor=0.95: 0.75/0.35/g0 -> decided=49 wrong=0 acc/dec=100.0%
```

The knee picks the band that is already saved: at every K ≥ 2 the 95% floor rejects the greedy
0.55/0.50 cut (which buys 96 decided but 31 wrong — 15 FP and 16 FN, i.e. it would call human text AI
roughly as often as AI text), and the conservative cut holds 0 wrong on both corpora. `changed:false`,
so **no band write was owed and none was made** — `knee-picks.json` records row 31 as confirmation.
`--eval` agrees: `c49 w0(0FP/0FN) r47 cov=51%` across train+holdout. Honest limit: margin 0 means the
cut sits exactly on a value the model emits (the lattice shows `0.72 0.75 0.78`), so a small jitter in
that model's output moves a text across the line — the same run-to-run variance measured on sonnet.
Haiku's cost is coverage: it decides half the corpus and defers the rest, with no errors in either
corpus. That is the operating point the criterion selects, not a bug.

**The two deployed NVIDIA lanes could not be re-validated, and the secondary's number got worse.**
Same gate-then-capture discipline as #111 (`/c/tmp/nv111.sh`, 2 texts / 150 s / 1 try before any 48).

Primary, row 1 `nvidia/nemotron-3-ultra-550b-a55b` on `PRIMARY_BASE_URL`: gate answered **0/2** again
(16:13:13 → 16:15:43, i.e. two 150 s timeouts). The capture was skipped rather than run — a 0/2 gate
means the pool is refusing, and a 48-text run against a refusing pool produces a file of timeouts, not
a measurement. The lane's saved band (0.70/0.40 with the 0.65 tie-break gate) therefore stays a
capped-era fit, and the primary lane's live behaviour under `max_tokens 0` remains unmeasured. The app
itself is fine: n8n answered the webhook path with its own 400 on a malformed body, and the UI's Detect
request went out and queued — the refusal is the gateway, not the workflow.

Secondary, row 2 `google/diffusiongemma-26b-a4b-it` on `SECONDARY_BASE_URL`: gate answered **2/2 in 2 s**
(944 ms p50 over the train sweep), so the captures were authorized. Train came back **37/48 usable in
36 s**; the disjoint holdout **0/48 in 52 s** — the pool collapsed inside the same minute, and the
result file is a wall of timeouts. Fitting the train capture alone with the explicit-tag form:

```
node benchmark/fit-bands.js --knee --model 2 --train nocap-clean-diffgemma --val nocap-holdout-diffgemma
  saved   0.9/0.3/g0: train c26 w3(3FP/0FN) r7 | val c0 w0
  K=0  pick 0.50/0.45/g0   decided=36 wrong=9  (9 FP)
  K=1  pick 0.80/0.35/g0   decided=35 wrong=8  (8 FP)
  K=2+ pick 0.90/0.30/g0   decided=29 wrong=3  (3 FP)   margin=0.02
  FINAL K=5 floor=0.95: NO band meets the floor — leave saved band alone
```

Two things follow, and the second is the reason #111 stays open. First: on uncapped answers the
secondary lane now makes **3 human-as-AI false positives on 29 decided — acc/decided 89.7%, under its
own 95% floor** — and no cut in the grid repairs it, because every candidate fails at least as badly
(all the errors are FP, so they are the AI-leaning side of the human corpus; raising the human cut does
not remove them). Second: with the holdout at 0/48 there is no disjoint evidence, so any band chosen
here would be in-sample. `fit-bands.js` writes `changed:false` with a `no band meets floor` note, and
`knee-picks.json` carries both row 31 (confirmation) and row 2 (refusal) as `changed:false`, so
`patch-bands-v3.js` — which skips unchanged rows and names them — writes nothing. **No band was written
to the database in this round at all.** What #111 still owes: a clean 48+48 pair on each NVIDIA lane on
a day the pool answers, and a decision from the user on the secondary — 89.7% acc/decided with FP on
human text is an operating point to be chosen (or the tie-break switched off), not one to be hidden
behind a deferral.

## Round 19: what the failed detect taught about History, and two bugs it exposed (2026-10-16)

Round 18's detect leg ended in a 502 that the app recorded, and the recorded row was then read back
through the UI. The row was not just an unhelpful blank — it was **wrong in three ways that a user
would act on**, and none of the 55 passing frontend specs could see any of them.

**1. `latency_ms` stored seconds on the failure path.** `forward()` multiplies by 1000 on the success
paths and returned `time.time() - t0` raw in the `URLError/TimeoutError` branch, so a 120 023 ms
timeout was written as `120` and the History row read **"120 ms"** for a two-minute hang. Fixed at
the source (`backend/app.py`, the branch now returns `(time.time() - t0) * 1000` with a comment naming
the symptom and date).

**2. `text_length` was NULL whenever the workflow never ran.** The length is taken from the workflow's
`_internal` block, which does not exist on a transport failure, so every failed row rendered **"? chars"**
— a 42-character paragraph indistinguishable from a 4-character word. `record_detection()` now takes an
optional `text_length` and `/detect` passes `len(payload["text"])`, so the caller's own input is
recorded even when nothing upstream answered.

**3. An un-scored run rendered a 0 % ring.** `ai_probability` is NULL when no lane answered, and the
circular metric coerces that to 0 — so the row showed a confident **"0% AI prob."** next to an empty
verdict pill. 0 % is not "unknown", it is the strongest HUMAN answer the scale can give, which is the
one reading that is definitely false. The row now draws an empty dashed circle labelled **"not scored"**
in the same slot (the layout does not shift), states the HTTP status, and says **"length not recorded"**
only when the backend genuinely could not know it. Clicking such a row no longer hands its error body to
the Detect tab: `open()` requires a `classification` string in the stored payload before it emits.

Also worth recording because it looks like a bug and is not: the app's copy on this path is **"n8n
unreachable at http://localhost:5678/webhook/ai-detect-ai"**, and that is misleading — n8n was up and
answering (HTTP 400 on an empty body); the *deployed NVIDIA primary* hung until the socket timed out.
The message does at least name the target, per the standing rule, but it attributes a pool failure to
the orchestration layer. A caller-supplied `tuning` block is ignored unless the request also carries an
explicit `lane_config` (`app.py`: `caller_tuning if isinstance(caller_tuning, dict) else tuning_block(...)`,
and the stored primary row's `timeout_ms` of 90 000 sets the 120 s wall clock), which is why the probe
below could not shorten the timeout.

**Tests added for the fixes, not for the code.** `backend/tests/test_backend.py` gained
`test_a_transport_failure_records_elapsed_time_in_milliseconds` (a scripted clock, so the assertion is
exact rather than a race: `latency_ms == 120023.0`, and the error string must still name the URL) and
`test_a_recorded_failure_keeps_the_text_length_the_user_sent`. `frontend/.../history-view.spec.ts` is
new and covers both shapes plus the reopen guard.

```
python -m pytest backend/tests -q            → 91 passed in 55.21s        (was 89)
npx ng test --watch=false                   → Test Files 6 passed, Tests 61 passed (was 55)
npm run build                               → main 300.70 kB, clean
```

**Live proof, through the real app path, as a throwaway non-admin.** `POST /api/detect` with a 191-char
text returned 502 after the 120 s wall clock; the stored row read `len 191 | lat_ms 120106 | http 502`,
and the same row in the built UI on 127.0.0.1:8820 rendered:

```
histprobe1  (new, fixed backend): "10/16/2026, 5:32:50 PM · 191 chars · 2 min 0 s · HTTP 502"
                                       app-circular-metric: absent   .no-score: true  "not scored"
uiverify1   (row 125, legacy data written before the fix): "… · length not recorded · 120 ms · HTTP 502"
                                       app-circular-metric: absent   .ns-ring: "dashed 74px"
```

The second line is the honest limit: the frontend cannot repair a row the old backend already stored
wrong, so `120 ms` stays for pre-fix rows — the discriminator is that a *new* failure now reads "2 min 0
s". The two accounts each saw exactly one row, their own, which is the per-user scoping re-proved as a
side effect. Both throwaway users and their detection rows were deleted after the check.

## Round 20: the packaged local app was dead on arrival, twice (2026-10-16)

Round 19 left one leg unproven: does the PyInstaller exe actually *detect*? It did not, and the
answer was two separate shipping defects, both invisible in dev because dev has a `.env`.

**Defect 1 — an install with no `.env` has no lane at all.** `seed_models()` built its rows from
`env.get("PRIMARY_MODEL")` / `env.get("SECONDARY_MODEL")`, and a frozen exe deliberately ships no
`.env` (no lane key may enter the binary). So the installed app seeded four providers, the ranking,
the catalogue — and zero models, and every `/detect` answered:

```
detect: 409 wall 0.0s {"classification":"DETECTION_UNAVAILABLE","error_code":"NO_PRIMARY_MODEL",
                        "error":"No primary model is configured in the database."}
packaged DB: providers [nvidia, openrouter, omniroute, gemini]  models count: 0  keys: []
```

The fix is `seed.DEFAULT_LANES` / `seed.DEFAULT_BAND`, which are **copies of the workflow's own
defaults** — the `env.X || '...'` and `num(env.X, default)` literals in `n8n/generate-workflow.py`,
i.e. exactly what a lane runs on when env omits them. Nothing was promoted: nemotron-ultra primary,
diffusiongemma tie-break, band 0.69/0.40 with conf gates 0.60/0.60 and the tie-break cut at 0.90.
Two honesty points follow from that. First, the seeded note now says the band is capped-era and
awaits #111, because a fresh install must not look better validated than it is. Second, copies drift,
so `test_baked_defaults_match_the_workflows_own_defaults` parses the generator and compares each
number and both model ids — change one file without the other and the suite fails.

**Defect 2 — the launch environment was ignored, in the container too.** Chasing the 409 surfaced a
worse one: `read_env()` parsed the `.env` *file* and never looked at `os.environ`. So after fixing
the lanes, the rebuilt exe still hung 120 s and answered 502, because `N8N_URL=http://127.0.0.1:5699`
in its launch environment had been thrown away and the seeded `n8n_url` stayed the default
`http://localhost:5678` (which this host answers, slowly). The same applies to the compose stack:
`docker-compose.yml` passes `N8N_URL: "http://n8n:5678"` as a container env var and the repo `.env`
has **no** `N8N_URL` line, so the backend was seeded `http://localhost:5678` — unreachable from
inside its own container — and every detection in the hosted deploy would have 502'd. `read_env()`
now overlays the process environment for the names seed understands (plus numbered
`OPENROUTER_API_KEY_n` / `GEMINI_API_KEY_n`), with the live environment winning; `docker/DEPLOY.md`
records it, and that a setting already stored is not rewritten by a redeploy.

**Verification of the packaged app, as a first boot.** Deleted `%LOCALAPPDATA%\AITextDetector`, built
the exe (43,204,927 bytes, `main-G3T5SIXI.js` — the current content hash), launched it with
`AITD_NO_BROWSER=1 BACKEND_PORT=8822 N8N_URL=…`, and drove it over HTTP with a throwaway account:

```
packaged DB after boot: primary nvidia/nemotron-3-ultra-550b-a55b 0.69/0.40 cap 0
                        secondary google/diffusiongemma-26b-a4b-it  same band
                        n8n_url=http://127.0.0.1:5699  webhook_path=/webhook/ai-detect-ai
register 201 · login 200 · detect 200 {"classification":"AI_GENERATED","ai_probability":0.91,
                                       "confidence":0.83,"method":"PRIMARY_DECIDED_AI","id":1}
engine received lane_config {primary: nemotron-ultra @integrate.api.nvidia.com,
                             secondary: diffusiongemma} and tuning {ai 0.69/0.6, human 0.4/0.6,
                             secondary 0.9, temperature 0.1, top_p 0.9, max_tokens 0, timeout 90000}
history row: ai 0.91 · text_length 149 · latency_ms 52 · http_status 200 · primary_model recorded
```

The limits, stated plainly: the detection engine in that run was a loopback stand-in, so the seeded
lane, the band, the payload the app sends, the recorded row and the history/detail reads are all real
app behaviour — **the verdict itself is not a model answer**. No provider was called and no key was
used. Phrase painting was not re-proven through the exe (the stand-in returns the top-level
`phrases` shape, not the workflow's `evidence.models[].result.phrases`); it was proven in the built
UI in an earlier round, and the packaged bundle is the same content hash. The local app still needs
a reachable n8n — it is UI + backend + data, not the pipeline — and that is now written in
`installer/BUILD.md` §2b together with what a no-`.env` install does and does not ship.
`backend/tests` went 91 → **101 passed** (ten new in `test_seed.py`: six on the seeded lane and its
drift guard, four on the env overlay); `ng test` stays at 61 passed, `npm run build` clean. The installer (step 3) was recorded
as unbuildable here, and that was **wrong**: `ISCC.exe` does exist, at
`C:\Users\adam\AppData\Local\Programs\Inno Setup 6\ISCC.exe`. A winget `JRSoftware.InnoSetup` install
goes per-user under `%LOCALAPPDATA%\Programs`, so a sweep of `Program Files` / `Program Files (x86)`
— the only two places round 20 looked — finds nothing. See round 24 for the compile that followed
from correcting that.

## Round 21: the Ranking tab became a control, and that exposed three bugs (2026-10-16)

Round 20 shipped a ranking page that *told* the user which free model is best. The objective asked for
more: "pick them auto if key verified working is there". This round makes the ranking actionable — each
card with a working key behind it gained a **Score with this model** button — and finding out what that
path actually did produced two live defects.

**What the pick sends.** Only the model id. `DetectionService.pickedModel` is a signal, and `detect()`
merges it into the body as `lane_config.primary = {model: id}` — deliberately *no* `base_url`, no
thresholds, no sampling. The backend then fills the endpoint from the model's stored row, applies that
row's calibrated band and resolves the credential. So a signed-in user can score with any ranked model
without ever acquiring an advanced-parameter control, which is the property the design insisted on. The
shell records the pick, moves to the Detect tab and shows a banner naming the model that is deciding;
`use the deployed lane` clears it.

**Defect 1 — the pick bypassed the user's own key.** `stored_key_for()` is the credential resolver for
requests that arrive with an explicit lane, and it only ever returned `active_key()` — the admin's shared
key. The deployed-lane path next to it already preferred the signed-in user's stored key
(`db.user_key_for_provider`, verified-first). So a user who was shown "your key is ready" on the ranking
was billed to the shared account, and on an install with **no** shared key the request left with no
credential at all. Reading the two branches side by side found it; a test proved it:

```
test_a_model_picked_from_the_ranking_is_paid_for_with_the_pickers_own_key
  admin seeds provider + model + shared key nvapi-shared-111222333
  user nadia stores nvapi-nadia-99887766
  POST /detect {"lane_config": {"primary": {"model": "test/model"}}}
  AssertionError: 'nvapi-shared-111222333' == 'nvapi-nadia-99887766'   (before the fix)
```

After it, `stored_key_for(lane, base_url, model_id, user_id=…)` tries the user's own verified key for the
host first and falls back to the shared one. A second test was written, then **deleted**: it asserted a
409 when neither exists, which contradicts the intentional env-key fallback. `backend/tests` 102 → 103.

**Defect 2 — a model could tie-break with itself.** The deployed pair is nemotron-ultra primary +
diffusiongemma secondary, and diffusiongemma is also ranked (19th). Picking it as the scorer set it as
primary while the panel left the secondary lane on — same input, same stored tuning, so the second call
was a coin-flip copy of the first and any "agreement" it reported was an artefact. `detect()` now forces
`secondary_off` when the resolved primary and secondary ids match, and
`record_detection(…, secondary_off=True)` keeps `secondary_model` null instead of echoing the workflow's
env default:

```
test_a_lane_cannot_tie_break_with_itself
  AssertionError: {'secondary': {'base_url': …, 'model': 'google/diffusiongemma-26b-a4b-it'}}
                   != {'enabled': False}                                (before the fix)
```

The UI mirrors the invariant: picking a tie-break model switches the second opinion off for the run and
the banner says why ("a model cannot corroborate itself"). Clearing the pick puts it back **only** if the
pick is what removed it — a user who turned the lane off in Configuration stays off. 103 → 104 backend
tests, and `ng test` 61 → **67 passed** (six on the service's body construction, one end-to-end spec that
clicks the card button and reads the request body).

**Live legs, through the built app, as a non-admin.** `backend.serve_local` on a clean port serving
`main-CKDOSAMD.js`, throwaway account `verifyr20` (role `user`: nav is Detect / Ranking / History, no
Admin), n8n at `http://localhost:5678/webhook/ai-detect-ai`:

```
ranking: 48 cards, 23 with a key behind them -> 23 Score-with-this-model buttons
pick rank-19 google/diffusiongemma-26b-a4b-it (the deployed tie-break)
  -> Detect tab, banner + tie-break note, second opinion off
  ai-012 (real HC3 AI text, 1519 chars): AI_GENERATED 0.94 / conf 0.98
     method SINGLE_MODEL_PRIMARY_AI, latency 1107 ms
     3 phrases returned: 2 painted, 1 reported "could not be found verbatim"
  hu-016 (real HC3 human text, 862 chars): HUMAN_LIKELY 0.18 / conf 0.85, latency 679 ms
     2 phrases, both painted green
painted background colours read with getComputedStyle:
  human  strength 0.95 -> rgba(71, 201, 138, 0.56)     human 0.90 -> rgba(71, 201, 138, 0.53)
  AI     strength 0.80 -> rgba(235, 104,  47, 0.52)     AI  0.70 -> rgba(231, 136,  56, 0.47)
stored rows 125/126: user_id scoped, secondary_model = null  (the attribution fix, observed live)
```

The green alpha rises with strength and the AI ramp moves orange → red across the same span, which is
the "yellow color degradation selon strength" the objective asked for, measured rather than asserted.

**The non-lane pick did not answer, and the reason is not the feature.** Picking rank-1
`z-ai/glm-5.3-flash` (0 wrong on the 48-text corpus, `lane = null`) ran the full 120 s wall clock
(stored `timeout_ms` 90000 + 30 s) and the UI reported *"Cannot reach the detection path — n8n
unreachable at http://localhost:5678/webhook/ai-detect-ai"*. Probing the webhook directly with the same
text settles where the fault lies:

```
google/diffusiongemma-26b-a4b-it : HTTP 200 in 1.1 s -> AI_GENERATED 0.94
z-ai/glm-5.3-flash               : no response in 100 s (client timeout)
```

The engine is alive; the model stalls. Two findings follow. First, a stalled model is currently reported
as an unreachable *engine*, which is technically the standing contract ("unreachable must always name the
gateway") but points the user at the wrong thing — the workflow's HTTP leg needs to distinguish "no
answer from the model" from "no answer from n8n". Second, the ranking's promise that a top-ranked model
can be used for scoring is only as good as its pool: `glm-5.3-flash` answers on the batch harness and
does not answer here, on the same gateway, minutes apart. That is the run-to-run instability already
recorded for free pools, now visible in a product surface.

**Also measured on the way:** a second direct run of the same text returned 4 phrases where the first
returned 3, and confidence moved 0.98 → 0.96 — the jitter again, not a parser change. One rejected quote
differed from the source only in letter case (`It is important…` where the text reads `However, it is
important…`); case-insensitive matching would recover those, but matching policy decides what counts as
verbatim evidence, so it is left as an open choice rather than changed silently.

**Defect 3 — naming only a primary did not mean "no tie-break".** The browser leg above passed
`lane_config.secondary` explicitly (the banner's "second opinion off" switch sends `{"enabled": false}`),
so it never exercised the shape the Ranking pick actually sends: a `primary` model id and *nothing* about
a second lane. An absent `secondary` is not an off switch — the workflow answers it with its own deployed
default, so the call really is a two-lane call, while `secondary_model` was taken only from the caller's
lane and the stored row said NULL for a lane that ran. And when the picked model *was* the deployed
tie-break, the same silence described a model corroborating itself. `detect()` now resolves the implied
lane from the DB before the self-tie-break guard: the row records the deployed tie-break unless it
collides with the pick, in which case the lane is switched off on the wire rather than merely omitted.
Test-first, red as `AssertionError: None == 'google/diffusiongemma-26b-a4b-it'`:
`test_an_omitted_secondary_lane_still_cannot_tie_break_with_the_picked_model` (asserts the engine is sent
`secondary == {"enabled": False}`, not an absent key, no `secondary_decision_threshold` in the tuning, and
a NULL row) and `test_a_picked_model_that_is_not_the_tie_break_keeps_the_deployed_one`.

**The rebuilt exe, first boot, both pick shapes, real engine.** `.localbuild/out/AI-Text-Detector.exe`
was rebuilt from this tree (43,208,340 bytes, 18:14) after deleting `%LOCALAPPDATA%\AITextDetector`,
launched with `AITD_NO_BROWSER=1 BACKEND_PORT=8823` and no environment for the engine at all — the seeded
default `http://localhost:5678` *is* this host's live n8n:

```
served bundle: main-CKDOSAMD.js / styles-4IXTWISY.css  (equals frontend/dist)
packaged DB after boot: lanes nemotron-ultra(primary) + diffusiongemma(secondary),
                        api_keys 0 rows, user_api_keys 0 rows
pick the deployed tie-break, secondary omitted -> 200 in 1.8 s
   AI_GENERATED 0.94 / conf 0.98  method SINGLE_MODEL_PRIMARY_AI  3 phrases
   row 1: primary_model diffusiongemma  secondary_model NULL  deciding_lane primary
   engine warnings: "Second-opinion lane is disabled: gray-zone texts answer as REVIEW_REQUIRED;
                     no tie-break call was bought."        config_used key_source "env"
pick the deployed primary, secondary omitted -> 200 in 1.4 s
   AI_GENERATED 0.94 / conf 0.98  method SINGLE_MODEL_SECONDARY_AI  3 phrases (all diffusiongemma)
   row 2: primary_model nemotron-ultra  secondary_model diffusiongemma  deciding_lane secondary
   engine warnings: "The primary model failed; this verdict comes from the fallback lane, read
                     against SECONDARY_DECISION_THRESHOLD."
dev stack (8792), same legs: row 113 identical to exe row 1; row 114 (pick google/gemma-4-31b-it,
   secondary omitted) stored secondary_model = diffusiongemma on a 120 s HTTP 502 — the attribution
   is written on the failure path too, not only when a verdict arrives.
```

Three things that Round 20's loopback leg could not prove: the `{enabled: false}` the fix sends is what the
engine acts on (its own warning says no tie-break call was bought); a fresh install with **zero stored
keys still detects**, because the credential comes from the workflow's own environment
(`key_source: "env"`) — so a no-`.env` install is un-*pinnable*, not broken; and a failed primary is
reported as a fallback verdict instead of silently averaged into one.

Two new measurements came out of the same minutes. `google/gemma-4-31b-it` on the **NVIDIA** gateway now
stalls past the wall clock (no answer in 100 s, 502 at 120 s) where round 8 captured it 48/48 with zero
failures on that same gateway — a second id for the entitlement/pool-stability question, and a reminder
that "unreachable" must name the gateway *and the day*. And the jitter reached the deployed primary itself:
nemotron-ultra produced no usable answer inside a 1.4 s two-lane call while diffusiongemma answered 0.94,
so the identical 0.94/0.98 in exe rows 1 and 2 is the *same deciding model*, not two lanes agreeing.

**The gap the packaged leg exposed was UI, not code — and it is now closed.** On a fresh install nothing
is seeded with a key by design, so every ranking card lacked the pick button and the page said nothing
about why. The Ranking view now explains the state and offers the way out: a blue-glass note when
`pinnable() === 0` ("Detection still works: the deployed lane scores your text without any setting of
yours. What a key unlocks is choosing *which* model scores it."), a per-card line naming the provider with
no key, an "Add a key ↓" button that focuses the BYOK field, and a different sentence for a signed-out
visitor ("Sign in first: keys belong to your account, which is also what saves your history").

Driven in the rebuilt packaged app (`main-CPJ6HOWO.js`, wiped data folder, throwaway account), the DOM
shows `cards: 48, pick buttons: 0, per-card locks: 48, note: present` and the button moves focus
`BODY → INPUT/key`; the note's own computed style reads the theme tokens in both modes — dark
`background rgba(77,141,255,0.1)`, `border rgba(120,160,255,0.32)`, `blur(18px)`, `radius 14px`, text
`rgb(232,238,252)`; light `background rgba(47,106,224,0.1)`, `border rgba(40,90,190,0.3)`, text
`rgb(22,32,58)`. It disappears the moment one card has a key behind it, so the note is tied to the
pickable count and never to the key list.

**A reproduction trap worth recording:** the same code on the *dev* stack (which loads the repo `.env`)
answers `pinnable: 2` for the identical zero-key database, because `stored_key_for` falls back to the
environment credential. The packaged app, which ships no `.env`, answers `pinnable: 0`. So the empty-state
can only be reproduced in the exe, not in dev — the check above had to run against the packaged build.

Tests: `backend/tests` **106 passed** (the two above plus the credential pair and the ten in
`test_seed.py`), `ng test` **72 passed** (five new specs for the lock state, DOM-driven), production
build clean. The exe was rebuilt from this tree at 18:27 (43,211,358 bytes, staging folder cleaned to the
four current files so no stale bundle ships). Throwaway accounts (`imp21-*`, `lock118`) and their
detection rows are deleted from both databases and the packaged data folder was removed again, so the next
launch is a genuine first boot.

## Round 22: the uncapped re-validation of the deployed pair — one lane confirmed, one lane is gone (2026-10-16)

Round 21 removed the token cap from the product (`max_tokens 0` sends no field at all) and re-fit the two
kiro rows on uncapped captures (#109). What was left for #111 is the pair that is actually *deployed*:
`models` row 1 `nvidia/nemotron-3-ultra-550b-a55b` (primary) and row 2
`google/diffusiongemma-26b-a4b-it` (tie-break), both still carrying bands fitted under the cap. This
round measured both. One came back confirmed; the other cannot be measured at all, because the model no
longer exists on the wire.

**Leg 1 died on its first row, and the label lied about why.** The 48-text uncapped capture
(`--tag r111-clean-nv550 --model nvidia/nemotron-3-ultra-550b-a55b --corpus corpus-clean.json`) wrote all
48 rows inside 25 s at 65–483 ms each, every one `ERR:no_choices`. That label is a *parse* complaint — no
`choices` array — and it is what the harness produces when a provider error body does not match the shape
it expects. Reading `error_detail` off the file gave the real answer:

```
{"status":404,"title":"Not Found","detail":"Function id '948fe171-…' version 'null':
 Specified function in account '2R2Q7s3i…' is not found"}     http_status 404, attempt 1
```

Three follow-ups turned "the id is wrong" into "the deployment is gone":

- `GET https://integrate.api.nvidia.com/v1/models` answers **200 with 81 ids**, and
  `nvidia/nemotron-3-ultra-550b-a55b` is *exactly* one of them (17 nemotron-family entries listed). The
  catalogue is not evidence of servability — this round is the second time that distinction mattered, the
  first being the 58-of-80 `404 Function … Not Found` sweep recorded under *What is still unmeasured*.
- Both stored NVIDIA keys return the same 404 with the same function id, so it is not one account's
  entitlement and not a credential problem.
- OpenRouter's mirror (`nvidia/nemotron-3-ultra-550b-a55b:free`, `models` row 4) answers 404 too, and its
  body relays the identical upstream text: `{"error":{"message":"Provider returned error","code":404,
  "metadata":{"raw":"…Function id '948fe171-…'…"}}}`. Two gateways, one cause — the NVIDIA serving
  function behind this model is not deployed.

So this is the same failure class that retired `kimi-k3` with a 410 on 2026-10-15, one step worse to
detect: a 410 says "gone", a 404 that arrives while the model is still advertised says nothing.
**The consequence is already visible in the product:** the packaged app's own warning measured today reads
*"The primary model failed; this verdict comes from the fallback lane, read against
SECONDARY_DECISION_THRESHOLD"* — every default-lane detection right now is being decided by the tie-break
model, not by the primary. **Row 1's band was therefore left exactly as it is and is explicitly NOT
claimed valid in the uncapped regime**; `fit-bands.js` carries that sentence in the registry next to its
capture lists, so a future `--knee` run cannot quietly treat the capped-era numbers as current.

**The harness now reports the HTTP code it was given.** A non-2xx response is classified before the body
reaches `extract()`, because providers do not agree on an error shape and `no_choices` was swallowing the
one piece of information that mattered. Transient handling is unchanged — only 429/500/502/503/504 still
re-request, so a permanently dead model is not hammered:

```
node benchmark/test-retry.js
  FAIL a transport 404 is reported as http_404, not no_choices   got: {"error_code":"no_choices"}
  FAIL a 410 EOL is reported as http_410 (kimi-k3 class), not a parse failure
  FAIL a 400 from a bad request shape is http_400 and not retried
  18 passed, 3 failed                          ← red, written before the fix
  …
  ok   a 400 from a bad request shape is http_400 and not retried
  21 passed, 0 failed                          ← green
```

and against the live dead id, one row now costs 0.4 s instead of a leg:

```
node run-benchmark.js --tag probe-nv550-404 --model nvidia/nemotron-3-ultra-550b-a55b \
  --corpus corpus-clean.json --limit 1 --concurrency 1 --tries 2 --timeout 60
  [1/1] ai-000 AI open_qa -> ERR:http_404 (366ms)     usable=0/1 errors={"http_404":1}
```

**The same one-request probe across every stored NVIDIA row** (`benchmark/probe-servable-nvidia.mjs`,
16-token "Reply with exactly: OK", concurrency 2, status only — a reachability check, not a benchmark, and
`content=""` is expected because a reasoning model spends 16 tokens before it speaks):

| model id (row) | status | wall | reading |
|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b` (1, deployed primary) | 404 | 385 ms | function not deployed — dead |
| `google/diffusiongemma-26b-a4b-it` (2, deployed tie-break) | 200 | 1.4 s | healthy |
| `z-ai/glm-5.3` (22) | 200 | 269 s | answers, but 4.5 min for 16 tokens |
| `google/gemma-4-31b-it` (21) | 504 | 302 s | gateway ceiling, no verdict obtainable |
| `z-ai/glm-5.3-flash` (23) | 200 | 80 s | slow, usable |
| `nvidia/nemotron-3.5-lightning-30b-a3b` (24) | 200 | 134 s | slow, usable |
| `moonshotai/kimi-k3` (26) | 410 | 91 ms | "reached its end of life on 2026-10-15T08:00:00Z" |
| `deepseek-ai/deepseek-v4.1-flash` (25) | 200 | 301 s | answers, at the gateway's own ~300 s ceiling |

Two things this table buys. First, `kimi-k3`'s 410 arrives in the *same flat shape* as the ultra's 404
(`{"type":"about:blank","title":"Gone","status":410,"detail":…}`) — under the old classifier both were
`no_choices`, which is precisely why an EOL kept being re-diagnosed from scratch. Second, the NVIDIA free
pool today is not a single regime: the same account serves diffgemma in 1.4 s and glm-5.3 in 269 s, so a
candidate's **latency bucket** has to be measured before it can be considered for a lane, not assumed from
its AUC.

**Leg 2: the tie-break lane was re-validated uncapped, on both corpora.** The clean leg had to be resumed
— its first pass lost 11 of 48 rows to `http_429` — and the holdout leg had to be re-run from a file that
was 0/48 usable (48/48 `http_429`, the pool state round 21 recorded). Both are complete now, same params
(temp 0.1, top_p 0.9, uncapped, current phrase prompt), disjoint corpora:

| capture | tag | answered | usable | salvaged | truncated | errors |
|---|---|---|---|---|---|---|
| fit corpus | `nocap-clean-diffgemma` | 48/48 | 48 | 1 | 0 | none |
| holdout corpus | `nocap-holdout-diffgemma` | 47/48 | 47 | 2 | 0 | 1 × `http_429` |

`fit-bands.js` drops salvaged and truncated rows, so the fit sees **47 train + 45 val = 92 rows**. Scored
at the band the DB actually carries (0.90/0.30, no confidence gate):

| corpus | correct | wrong | false positives | false negatives | deferred |
|---|---|---|---|---|---|
| `nocap-clean-diffgemma` | 34 | 5 | 5 | 0 | 8 |
| `nocap-holdout-diffgemma` | 35 | 2 | 2 | 0 | 8 |
| both | 69 | 7 | 7 | 0 | 16 |

Coverage 76/92 = 83%, accuracy over decided 69/76 = **90.8%**, and every one of the 7 errors is the same
direction: a human text scored ≥ 0.90 and called AI. That is the failure this lane is allowed to have in
the deployed shape (it only speaks inside the gray zone, above `secondary_decision_threshold`), but it is
the lane's real false-positive rate on clean text and it did not change when the cap went away.

The knee re-fit confirms the saved band rather than moving it. Sensitivity across the coverage price K:

| K | picked band | decided | wrong | accuracy/decided |
|---|---|---|---|---|
| 0 | 0.55/0.50/g0 | 92 | 20 | 78.3% |
| 1 | 0.85/0.40/g0 | 89 | 17 | 80.9% |
| 2 | 0.90/0.40/g0 | 77 | 7 | 90.9% |
| 5 (shipping) | 0.90/0.40/g0 | 77 | 7 | 90.9% |
| 40 | 0.90/0.40/g0 | 77 | 7 | 90.9% |
| saved | 0.90/0.30/g0 | 76 | 7 | 90.8% |

and the run's own conclusion line is the finding: `FINAL K=5 floor=0.95: NO band meets the floor — leave
saved band alone`. The tool's floor test asks for **accuracy over decided ≥ 95% while deciding at least 12
rows**; nothing in the grid satisfies it for this model in the uncapped regime — its ceiling is ~91%,
because 7 human texts sit at or above 0.90 probability and the highest AI cut the grid can place is 0.90.
That is not a tuning artefact: the saved-band line printed neither the `saved band is the pick for K < x`
note nor the `decide MORE and err LESS` warning, and both of those are suppressed only when **no candidate
band on the whole grid makes fewer than 7 errors**. Seven mislabelled texts out of 92 clean ones is this
lane's floor, cap or no cap. The mechanism is in the rows themselves: every one of the seven is a text the
model is *confident* about — clean `hu-004 0.98/0.95`, `hu-005 0.92/0.95`, `hu-009 0.95/0.98`,
`hu-019 0.95/0.92`, `hu-020 0.98/0.95`; holdout `hu-002 0.98/0.95`, `hu-009 0.98/0.95` (p/confidence; the
repeated `hu-009` is a slot name in two disjoint corpora, not the same text twice). Probabilities run
0.92–0.98 with confidence 0.92–0.98, while the grid's highest AI cut is 0.90 and its highest gate is 0.75 —
no band can exclude a mistake the model is sure of. The best the grid offers over the saved band is one extra decision (77 vs 76)
for the identical 7 errors, by moving `human_cut` 0.30 → 0.40. That is a real option, it is not a
floor-satisfying improvement, and it has **not** been written to the database: row 2 keeps 0.90/0.30/0.0.

**The two kiro rows were re-checked under the same criterion and hold.** Same command, same regime, both
uncapped 48+48:

| row | model | band | decided | wrong | accuracy/decided | coverage |
|---|---|---|---|---|---|---|
| 30 | `kiro/claude-sonnet-4.5` | 0.80/0.30/g0 | 86/92 | 0 | 100% | 93% |
| 31 | `kiro/claude-haiku-4.5` | 0.75/0.35/g0 | 49/92 | 0 | 100% | 53% |

`knee-picks.json` records `changed: false` for both, so nothing was patched and nothing awaits sign-off.
Haiku's shape is worth stating plainly: it is exact on everything it decides and defers half of everything
it is asked — 21 of 47 train rows and 26 of 45 holdout rows come back `REVIEW_REQUIRED`.

**Where that leaves #111 and the deployed default.** The tie-break lane is re-validated and unchanged; the
kiro rows are re-validated and unchanged; the primary lane cannot be re-validated because the model is
unreachable at the provider, and that is an admin decision, not a measurement: the default primary has to
be re-pointed at an id that serves — or the outage has to be outlasted. Nothing was promoted: the standing
gate (48-text capture + disjoint holdout + live webhook check + user sign-off) is nowhere near met for any
candidate, and a 16-token reachability probe is not a benchmark.

**The decision taken at the end of this round: outlast it.** No model row, lane config, band or workflow was
changed; rows 1 and 2 keep their ids and bands, the app keeps answering from the fallback lane with the
engine's own warning, and `tools/reachability-probe.js` is how the restoration gets noticed — hourly, via
Qoder automation `e847ef77-9610-4fd1-8ec3-aab178078131`, three 16-token requests per run appended to
`tools/reachability-log.jsonl`, exit 1 while anything is unreachable, printing a `RESTORED` line the first
time a previously-failing target answers 2xx. Its first two runs already taught something: the primary's
failure **changed shape within fifteen minutes**, from `404` at 385 ms to a stall past 60 s, which is why
the probe logs status and latency instead of a pass/fail boolean. A restored id still owes the uncapped
48 + 48 before its band means anything.



Questions that are genuinely still open, plus one that got answered by a different model than the
one it was written for. The budget, not the method, is what limits these: each needs a fresh 48-text
capture against a free pool that refuses requests at random.

- **Does the 1400-token replay change `ling`'s 17 unscored answers?** Answered for the successor,
  not the retired id: `ling-3.0-flash-sante:free` went paid-only, and `orfull-ling-3-1-flash` was
  captured at 1400 tokens with **0 salvaged and 0 truncated** over 46 answered texts, so on this
  model the token cap is not what produced the salvage. The retired id's own 17 rows can never be
  re-measured. (`benchmark/free-round4.sh` was still written for the original question; the
  successor's round-7 run superseded it.)
- **Can `google/gemma-4-31b-it:free` be captured at all?** *Answered in round 8, and the answer
  reframes the question.* Not through OpenRouter's free pool: its 48-text run
  (`benchmark/run-orgemma431b.log`) reached 26 of 48 and answered **2** — 11 `http_429` and 13
  `exception`, 24 of them at `try10`, the backoff already at its ceiling — and the concurrency-1
  resume killed on 2026-10-07 showed the same shape at `try6` on every text it reached. That 429 is
  *per-model* upstream saturation, not the account cap: while the resume was failing, round 5's
  headroom probe returned `200 usable` on all three keys within the same six minutes
  (`benchmark/free-round5.log`). But the same weights are served on the **NVIDIA free tier**, where
  round 8 captured them **48/48 with zero failures** (`nvfull-gemma-4-31b-it`, AUC 0.945, separation
  0.830). So the dead end was a property of one gateway's free pool, not of the model — and the
  general lesson is that "unreachable" findings must always name the gateway they were measured on.
- **Does `z-ai/glm-5.3`'s 0-wrong result survive a second corpus?** *Answered in round 10: no.* On a
  disjoint two-sided HC3 holdout its overall accuracy held at **0.9375 (45/48)** but the composition
  changed — in sample all 3 misses were HUMAN deferrals, out of sample it traded one for a **false
  negative** (`ai-012` scored **0.35**, below the 0.40 human cut). The 0-wrong property was a fact
  about those 48 texts, not about the model. Its structural saving grace (highest human 0.65, lowest AI
  0.72 in sample) did **not** transfer: on the holdout the AI floor dropped to 0.35. Still `lane = null`.
  The highest-value unmeasured thing on this list is now closed; what remains open is whether the
  *finding* generalises beyond HC3 at all (see the modern-model-corpus gap in round 10).
- **Is the NVIDIA entitlement to `glm-5.3` / `gemma-4-31b-it` stable?** *Partly answered in round 22, and
  the answer is worse than the question assumed.* 58 of 80 catalogue entries answer
  `404 Function '<uuid>' Not Found`, and nothing in that response distinguishes "never entitled" from
  "entitlement withdrawn". Round 22 caught the withdrawal happening to a model that is *still listed*:
  `nvidia/nemotron-3-ultra-550b-a55b` returns 200 in `GET /v1/models` (81 ids, exact match) while every
  chat completion answers 404 with the same function id — on both stored NVIDIA keys, and relayed
  identically through OpenRouter's `:free` mirror. A free model that measures AUC 1.000 can vanish between
  two requests; worse, the deployed primary vanished while still being advertised, so the listing cannot be
  used as a health check. What remains open is periodic reachability probing over days, not a measurement.
- **What does a deferral actually cost?** Round 8 produced two models with the same 0.938 acc/corpus
  and opposite profiles — `gemma-4-31b-it` decides all 48 and errs 7 times; the 550B decides 38 and
  errs twice. Choosing between them needs a cost for `REVIEW_REQUIRED` in the product, and no
  measurement in this document supplies one. `benchmark/tiebreak-floor.js` prices a related question
  (a human-side floor on the tie-break), not this one.
- **Would the mirror hold up as a live *primary*?** `tools/verify-free-lane.js` puts it in the primary
  seat for two texts and both were right, but two texts is a connectivity check. Its band is now
  stored on its own row (`models` id 4, `lane = null`), which is the precondition for that test to
  mean anything; the measurement itself would cost 48 requests against the same pool. *Round 22 narrowed
  this:* the mirror is currently unmeasurable too, because OpenRouter relays the identical NVIDIA function
  id and answers 404 — the two routes are not independent.
- **Does the deployed primary come back?** *Opened 2026-10-16 by the operator's decision.* Waiting was
  chosen over re-pointing the lane, so `tools/reachability-probe.js` now runs hourly (Qoder automation
  `e847ef77-9610-4fd1-8ec3-aab178078131`; three 16-token requests per run — primary, tie-break, and the
  OpenRouter mirror — appended to `tools/reachability-log.jsonl`, exit 1 while anything is unreachable,
  and it prints a `RESTORED` line the first time a previously-failing target answers 2xx). The first two
  runs already say something: the primary's failure changed shape inside 15 minutes, from `404` at 385 ms
  to a stall past 60 s, so a re-check has to log the *code and latency*, not a yes/no. What it does not do
  is decide anything — a restored id still owes the uncapped 48 + 48 before its band means anything, and
  the app keeps answering from the fallback lane until it does.
- **Is `ling-3.1-flash`'s 0.870 real out of sample?** It is in-sample on 46 rows, its tightest margin
  is 0.020, and this pipeline is measured to flip 8 of 12 borderline texts between identical runs.
  The band moved both folds, which is the most a 48-text corpus can say. Settling it needs a second
  corpus of real captures. *(The Gutenberg book set is no longer the candidate — round 10 showed it is
  saturated at specificity 1.000 for every model and one-sided. `corpus-holdout.json`, the disjoint
  two-sided HC3 set, is the right second corpus and is already built and verified.)*
- **Is any of the separation style, or is it just word count?** Round 11 removed the *formatting* confound
  but not the *length* one: the AI class averages ~2× the human class (main: 190 vs 85 words) and not one of
  the 96 texts reaches 500, so a model can read "long = AI" without touching style, and every human-side
  false positive sits in GPTZero's documented short-text unreliability zone. Answering it needs a **length-
  matched** capture — both classes restricted to a shared word band (e.g. 120–200) — rerun dirty *and* clean.
  Related and still open: **does the 0.69/0.40 band, fit on ~85-word text, transfer to long documents?** It
  has never been measured above a few hundred words. *(The clean free-NVIDIA / OpenRouter lanes from round 11
  are also still unscored — the free pools were saturated or in flight at write time and will be appended
  when they serve.)*

## Round 23: the packaged app learns where its engine lives (2026-10-16)

Round 20 rebuilt the `.exe` on a fixed bundle and proved it serves it, but a verdict was still out of reach on
any machine without n8n: the engine URL was a build-time constant, so the packaged app either shipped pointing
somewhere that answered or it shipped broken, and nobody on that machine could change it. The objective allows
only one person to change anything like that — "only login admin can change the for all users" — so the target
became an admin setting, and the answer to "how does the local app get a verdict" became three sentences instead
of a rebuild.

**The three ways it reaches an engine.** (1) It ships pointing at `http://localhost:5678/webhook/ai-detect-ai`,
which is right on any machine running `quickstart.sh` or the compose stack. (2) An admin opens
Settings → *Detection engine & site links* and types a reachable n8n — a hosted deployment, or a colleague's
box — and that applies to every user of that install, because `forward()` reads `setting("n8n_url", …)` per
request rather than once at boot. (3) If neither is available, the response says so in the vocabulary the UI
already understands: a labelled failure, never a score. `setting(key, default)` is `db.get_setting(key) or
default`, so a blank stored field falls back to the built-in default instead of posting to an empty string —
which is what lets the card show "leave blank to use `http://localhost:5678/webhook/ai-detect-ai`" honestly.

**What the gate actually does.** `/settings` sits in `auth._ADMIN_PREFIXES_ANY_METHOD`, so a signed-in plain user
gets 403 on the *read* as well as the write — the list of writable keys (`paper_url`, `paper_title`,
`api_key_guides`, `allowed_llm_hosts`, `n8n_url`, `webhook_path` and the three decision cuts) is not something a
user can probe by guessing names. The frontend renders the card in an explicit "admin-only" state rather than as
five blank fields, which is the difference between "you are not the admin" and "the settings are missing". The
user-facing surface for the same values is public-by-role: `GET /config` gives every signed-in user the paper
link and the key guides — `api_key_guides` is parsed out of its stored JSON text into a list on both `/config`
and `/settings`, so the shape the admin's form sent is the shape it reads back.

**Validation happens before the write, and the write is atomic.** `setting_problem()` rejects a target the
forwarder could not use — no scheme, no host, a bare `http://`, a webhook path without a leading slash — each
with a 400 naming the field, and `put_settings()` validates every key in two passes so a bad guide row cannot
land next to a good engine URL and leave the pair half-written:

```
PASS  POST /settings refuses a target the forwarder cannot use  [400 + field=n8n_url for 3 forms]
PASS  a path without a leading slash is refused  [400 {'detail': "webhook_path must start with '/', e.g. /webhook/…"}]
PASS  a rejected write left the stored engine URL alone  ['http://localhost:5678']
```

**The unreachable engine is labelled, not scored.** On a transport failure `/detect` returns 502 with

```json
{"error_code": "ENGINE_UNREACHABLE", "classification": "DETECTION_UNAVAILABLE",
 "engine_url": "http://127.0.0.1:1/webhook/ai-detect-ai",
 "hint": "The detection engine is the n8n workflow at that URL. The packaged local app defaults to
          http://localhost:5678, so it needs n8n on this machine, or an admin has to point
          Settings -> detection engine at a reachable n8n."}
```

`DETECTION_UNAVAILABLE` is the workflow's own fourth verdict, and the History panel already renders it as
"no verdict" rather than a percentage — so a machine with no engine produces a page that says what to fix,
instead of a number someone might quote.

**Verification.** `tools/verify_admin_settings.py` starts a real `backend.serve_local` process against a
*throwaway copy* of the dev database (admin password replaced on the copy only, with a random value that is
never printed) and drives the whole surface over HTTP: 16/16 checks — plain-user 403 on GET and POST, admin
login, the field-level refusals above, the round-trip of engine target and guides, guide visibility from a
signed-in plain user's `/config`, `/n8n/status` reporting the *stored* target with `healthz=200 published=True
webhook=True`, the labelled 502 from a deliberately dead one, and then one real detection through the just-saved
target:

```
PASS  a real detection runs through the saved engine target
      [200 HUMAN_LIKELY p=0.25 conf=0.85 lane=secondary method=SINGLE_MODEL_SECONDARY_HUMAN]
      evidence phrases: 1 across 2 lane(s),
      first='[HUMAN 0.6] The switch converged on the ingress ring and the protection path stayed clear …'
```

The second line is the round-17 strength ramp's input arriving through the *admin-saved* target: a
verbatim span with a direction and a strength, which is what the UI paints green or degrades toward red.

Single fallback lane, as expected while the deployed primary stays dead (round 22 / open above). The real
`backend/data/detector.sqlite3` was not touched. That run also re-demonstrated the jitter this project already
measures — the *first* attempt answered `200 DETECTION_UNAVAILABLE` ("All configured models failed to return a
usable verdict") and the second, 20 s later, produced the verdict above. The tool retries precisely because one
attempt is not evidence about the settings; it is evidence about the pool.

**One Windows finding worth keeping.** The first run of that tool reached `http://127.0.0.1:5678/healthz` from
the *same* child process that then got `WinError 10061` posting to the workflow on the same port. `netsh int
ipv4 show dynamicport tcp` on this host reports the ephemeral range starting at **1024**, so 5678 is a port
Windows will hand to an outbound socket; a client whose local port equals the server's port is refused on
loopback, and because the failed connect leaves that port free, the retry picks it again and the refusal sticks.
The stored value now uses `localhost` — the spelling the packaged app itself ships — and the case is documented
in the tool, because a real install under gateway-pool load can hit the same thing. That is exactly the failure
`ENGINE_UNREACHABLE` exists to make visible instead of silently scoring.

**Three Angular findings from the last card.** `NgModel` claims `name` as a directive `@Input`, so
`[name]="…"` is swallowed by the directive and never reaches the DOM — a dynamically-named input needs
`[attr.name]`, or the row is unaddressable from a test (and from a11y tooling). The guide rows live inside a
signal, so in-place mutation never re-invalidates it under zoneless; they bind through `editGuide(index, field,
raw)` with an immutable map. And `NgModel` writes its first value in a microtask, so a spec that reads an input
on the same tick the settings signal lands sees an empty string — `await fixture.whenStable()` is load-bearing,
not decoration. Suite: `Tests 81 passed (81)` over 7 files, `ng build` clean.

**Then the evidence panel got measured, not assumed.** The outstanding product question on this line of work
was whether the matcher should loosen: models quote across line breaks, re-case a mid-sentence fragment, and
swap quote marks. `benchmark/phrase-match-rate.js` joins every real capture row to its corpus text **by
verified length** (the same `id` exists in dirty, clean and holdout variants, so a plain `Map<id, text>` let the
last file loaded win and silently mismatched 106 rows before that was fixed) and asks, for all 575 quotes from
the round-17 set, whether `locate()` can find them:

```
quotes: 575
  (a) exact substring              574  99.8%
  (b) whitespace fallback (shipped)  0  0.0%   -> shipped total 574 99.8%
  (c) + case-insensitive             1  0.2%   -> shipped total (a+b+c) 575 100.0%
  (d) + punctuation-insensitive      0  0.0%   (NOT shipped)
  (e) still not found                0  0.0%
```

So the answer was "almost nothing is missing". Exact matching alone found 99.8% — the panel was never silently
losing evidence. One quote needed a case fold (`hu-017`: the model cut a sentence and lower-cased the first
word), and punctuation folding bought literally zero. `locate()` therefore gained a **third** step, tried only
after exact and whitespace-collapsed both miss, applied on the already-collapsed basis with the offset map kept
intact — and skipped entirely if `toLowerCase()` changed the string length, because then the map would be
pointing at the wrong characters. The painted span is sliced from the original text, so the highlight still
shows the text's own capitalisation. Step (d) was **rejected on measurement**: it would highlight spans the
model did not quote verbatim, which is the one thing the evidence panel's claim rests on. Three specs pin it:
case-only quote painted in original casing, case *and* whitespace together, and a same-shape paraphrase still
counted as unmatched.

**Still not proven by this round:** the container remains `docker compose config --quiet` (exit 0) and nothing
more — the build is blocked on `Wsl/WSL_E_WSL_OPTIONAL_COMPONENT_REQUIRED`, which needs an elevated install plus
a reboot, i.e. operator action, not a missing Docker. And `paper_url` is still blank until the preprint has a
Zenodo record; the card removes the Ranking-page button when it is, so the blank state is correct behaviour, not
a gap in the UI. The coverage numbers above come from a script that *mirrors* `locate()`; the shipped matcher
itself is covered by the three new specs, and the mirror is the thing to re-run if either side changes.

## Round 24: an 85 % next to "human" is not a contradiction, it is two scales (2026-10-16)

Round 23's verifier ended with a live verdict that read, on screen, `AI probability 85 %` under the
word **HUMAN_LIKELY**. That is the kind of thing a user is entitled to reject the whole product over,
so it got chased to the root rather than smoothed over.

**Why it happens.** There are two probability scales in this system and they are never blended. The
primary lane is read against the calibrated band (AI ≥ 0.69, human ≤ 0.40, confidence ≥ 0.60). The
tie-break lane is read against *its own* single cut — `prob >= secondary_decision_threshold ? AI :
HUMAN` — because diffusiongemma puts genuinely human text around 0.75, a number the primary's band
would call AI. When the deployed primary cannot answer, `Assemble Verdict` falls to
`SINGLE_MODEL_SECONDARY_*` and the surviving score is on the second scale while the UI printed it as
if it were on the first. That path is now the *only* default path, because round 22 measured the seeded
primary (`nvidia/nemotron-3-ultra-550b-a55b`, row 1) answering **404 on every chat completion** while the
catalogue still lists it — 48/48 rows failed in 65–483 ms with `Specified function in account … is not
found`, on both stored keys and on OpenRouter's mirror too. Every default-lane detection therefore comes
from the tie-break model, and what replaces it is task #119.

**What was fixed, and what deliberately was not.** The honest options were to renormalise the number
into a common scale or to state the scale. Renormalising would have invented a statistic nobody fitted
— mapping 0.85 onto the band's coordinates is a claim about diffusiongemma's distribution that the
captures do not support. So the readout now names which ruler it is on. `probScaleNote()` in
`result-card.ts` returns, for a secondary decision, `tie-break lane's own scale — AI only at ≥ 90 %`,
preferring the per-decision `component_scores.secondary_decision_threshold` (which is what actually
governed *that* verdict, and can be a row's stored 0.95) over the environment echo in
`config_used.thresholds.secondary_decision`, and returns `null` when neither is reported rather than
reciting the workflow's baked 0.90 — a number the backend does not know. A primary decision gets
`primary scale — AI ≥ 69 % · human ≤ 40 %` instead, so the two branches are distinguishable without
reading the code. The History list, which has no room for a sentence, tags the same rows
`· tie-break scale`, and nothing is shown where there is no probability to explain. Five specs pin
the precedence (per-decision cut wins over the config echo) and the null; `ng test` went 81 → **86
passed**, `ng build` clean.

**Proving the package, not the timestamp.** Rebuilding the exe is where this project has repeatedly
shipped a stale UI, because the staged bundle is a copy and content-hashed names make `cp` merge
rather than replace: after re-staging without the `rm -rf`, `main-KP47RF75.js` was still sitting next
to `main-N42PPJ7G.js`, and `index.html` loaded only the new one, so everything *looked* right while the
package carried dead code. The answer is a check that reads the artifact the way a browser does.
`tools/verify_exe_bundle.py` boots `.localbuild/out/AI-Text-Detector.exe` on its own port against a
throwaway copy of the dev DB, then asserts the served `index.html` names dist's bundle, that the bytes
it downloads hash to dist's digest, and that a string from the change under test is in the file:
**6/6 passed** — `main-N42PPJ7G.js`, 315,526 bytes, `sha256 4a334736f7b3472b…`, four occurrences of
"tie-break lane", `styles-DPLNOMS6.css` matching too. The same proof now sits in `installer/BUILD.md`
§1–3 with the merge trap written down.

**A documented absence that was simply the wrong search.** `installer/BUILD.md` step 3 and this
document both asserted that the installer could not be built here because `ISCC.exe` was "genuinely
absent" — evidence: a sweep of `Program Files` and `Program Files (x86)`. It is installed at
`C:\Users\adam\AppData\Local\Programs\Inno Setup 6\ISCC.exe`, because winget's `JRSoftware.InnoSetup`
packaging puts Inno under `%LOCALAPPDATA%\Programs` for a per-user install. Correcting the two files,
the compile worked and immediately produced three real bugs in the `.iss`, all of them path-base
confusion: Inno resolves a relative `[Files] Source` against the *script's* directory, so the first
attempt aborted on line 67 looking for `installer\.localbuild\out\…` — fixed with `SourceDir=..`,
which `OutputDir` also turns out to respect (with `OutputDir=Output` the installer appeared in the
repo root). `OutputBaseFilename={#MyAppName}-Setup-…` emitted a filename with spaces, and
`ArchitecturesInstallIn64BitMode=x64` warns on every compile since Inno 6.3 substitutes `x64os` for the
deprecated identifier. All three corrected; final compile **18.2 s, no warnings**, emitting
`installer\Output\AI-Text-Detector-Setup-1.0.0.exe`, 44,641,939 bytes (`sha256 71db6ac31cb87d45…`)
around the 43,226,239-byte exe — the size alone says the payload was read from `.localbuild/out`,
since the earlier path bug made the compiler refuse to proceed.

**Not proven by this round.** The installer was *compiled*, not *run*: installing writes to
`%LOCALAPPDATA%\Programs\AI Text Detector` plus Start-Menu entries, so it stays an operator step (and
no 7-Zip/innounp is present on this host to list the payload without installing). `python
tools/verify_admin_settings.py` still passes **16/16**, this time with the live detection succeeding on
the first attempt (`200 HUMAN_LIKELY p=0.25 conf=0.85 lane=secondary
method=SINGLE_MODEL_SECONDARY_HUMAN`, one evidence phrase across two lanes) — the earlier failures were
the script's own vocabulary check, not the app's. Which portable model should *replace* the dead
primary is round 25's question; `tools/probe_primary_candidates.py` separates "answers right now" from
"measured on 48 texts with a fitted band", and the two sets are not the same set.

## Round 25: "unreachable" was a lie half the time — and what the candidates actually do (2026-10-16)

Round 24 left #119 (replace the dead deployed primary) to this round's probe, and the probe's first
full table said ten of the portable candidates were dead. **That table was wrong, and the way it was
wrong is the finding.**

**The label defect.** `forward()` caught `(URLError, TimeoutError, OSError)` in one branch and answered
`502 ENGINE_UNREACHABLE — check Settings → engine URL`. A *read timeout* lands in the same branch, so
a lane that was alive and merely slow was reported as a missing engine, to the one person allowed to
act, pointing them at the field that was correct. The ceiling is `timeout_ms/1000 + 30 s` and the
shipped rows sit at 90 s, i.e. 120 s — below the ~269 s `z-ai/glm-5.3` takes on long text and below the
~302 s at which the gateway 504s `gemma-4-31b-it` (both measured in round 22).

**Why the message text cannot decide it.** On this host a connect to `127.0.0.1:1` — nothing listening
— came back as `<urlopen error timed out>`, not a refusal. Windows hung instead of refusing, so the
identical two words arrive from a dead address *and* from a thinking model. The fix discriminates with
a second, cheaper observation: after the failure, open one bare TCP handshake to the same host:port
(`_engine_listening()`, 2 s, no request, no provider budget). Handshake succeeds → `ENGINE_TIMEOUT`
("accepted the connection but did not answer within N s", hint names the lane's `timeout_ms` and says
Settings → engine URL is *working*); handshake fails → `ENGINE_UNREACHABLE` as before. Both keep the
same 502 / `classification: DETECTION_UNAVAILABLE` / `review_required: false` contract, so the UI still
draws "no verdict" — only the words and the named knob change, and `app.ts` already renders
`error_code`/`hint` generically.

Tested three ways (`backend/tests/test_auth.py`, pytest **111** passed, 70.5 s): a `/detect` through the
route with `urlopen` monkeypatched to `TimeoutError("The read operation timed out")` → ENGINE_TIMEOUT,
seconds in the message, `timeout_ms` in the hint, "unreachable" absent from it; the same route with a
refused `URLError` against the dead port → ENGINE_UNREACHABLE; and `forward()` called directly against
a thread that `accept()`s and never replies → ENGINE_TIMEOUT with `timeout_s: 1`, then the same call
after the listener closes → ENGINE_UNREACHABLE. The `listen()`-without-`accept()` fixture was tried and
**rejected**: on this host the first handshake succeeds and the second hangs, so that shape tests the
OS backlog rather than the code. One pre-existing test had to pin `_engine_listening` — it fires a
transport error at the *shipped default* `127.0.0.1:5678`, which is a live port on any machine running
n8n; a unit test that asks the network whether the engine exists fails for reasons outside the code.

**The candidates, re-measured at the widest ceiling the app allows.**
`python tools/probe_primary_candidates.py --texts z-ai/glm-5.3-flash,z-ai/glm-5.3,inclusionai/ling-3.1-flash,nvidia/nemotron-3-super-120b-a12b:free`,
each leg one real detection with the tie-break lane off, against a throwaway DB copy whose
`timeout_ms` was raised to the validator's maximum (300000 → a 330 s ceiling; a 330000 first attempt
spent **zero** provider requests, because every leg died as `LANE_CONFIG_INVALID` before the forward —
the tuning range is 1000…300000 and a probe that ignores that measures its own rejection):

| model (portable, measured) | answers | slowest leg | legs | stored band | measured accuracy in the DB |
| --- | --- | --- | --- | --- | --- |
| `z-ai/glm-5.3` | yes | 113 s | REVIEW_REQUIRED / REVIEW_REQUIRED | 0.65/0.30, conf 0.55 | 37/37 correct, 0 wrong, 0 deferred (stress) |
| `z-ai/glm-5.3-flash` | yes | 86 s | REVIEW_REQUIRED / REVIEW_REQUIRED | 0.80/0.55, conf 0.50 | 42/42 correct, 0 wrong, 0 deferred (stress) |
| `nvidia/nemotron-3-super-120b-a12b:free` | yes | 21 s | HUMAN / HUMAN | 0.69/0.40, conf 0.60 | 5/12 correct, 2 wrong, 5 deferred (hc3-screen12) |
| `inclusionai/ling-3.1-flash` | NO | 17 s | DETECTION_UNAVAILABLE / HUMAN | 0.65/0.60, conf 0.00 | 36/47 correct, 0 wrong, 11 deferred (clean) |

Read it against round 22 and the picture changes twice over. `glm-5.3` is not dead and never was: 269 s
on long text was a *timeout*, and on this 60-word pair it answers in 113 s. Both `glm` rows return
`REVIEW_REQUIRED` on such a short text — that is their own bands behaving (a two-text probe cannot
establish accuracy, which is why the table carries the stored corpus numbers beside it), so "answers"
here means transport, not quality. `ling-3.1-flash`'s NO is *not* a transport label at all: the body
came back from the workflow with `classification: DETECTION_UNAVAILABLE` and no `error_code`, i.e. the
lane ran and produced no parseable score — exactly the second failure class
[[n-no-answer-vs-parse-failed]] keeps separate. The only row that both answers quickly *and* emits
verdicts on this pair is `nemotron-3-super-120b-a12b:free`, and it is also the weakest measured row
(2 wrong on 12 texts).

**The decision, and why it is not shipped in this round.** Best measured portable candidate is
`z-ai/glm-5.3-flash`: 0 wrong on both the 48-text hc3 capture and the stress corpus, and alive today.
Two things must be solved *together* when it ships, or the promotion is worse than the status quo:

1. `seed_models()` upserts the two lane rows with `DEFAULT_BAND` in its `ON CONFLICT … DO UPDATE`
   clause, so the moment `glm-5.3-flash` becomes a lane row, every boot rewrites its fitted
   0.80/0.55/0.50 back to the incumbent's 0.69/0.40/0.60 — a re-seed that silently un-calibrates the
   default lane. The seed needs a per-lane-model band (env override still first, then the model's own
   fitted band, then `DEFAULT_BAND`), and `test_seed.py`'s `DEFAULT_BAND` equality assertions move with
   it.
2. The demotion is not free either: `DEFAULT_LANES` only *adds* lanes, so flipping the primary string
   leaves `nvidia/nemotron-3-ultra-550b-a55b` with `lane='primary'` in an existing DB and
   `default_models()` then chooses between two primaries. The change must clear the old lane (the
   `clear_other_lane()` helper exists for exactly this) rather than assume a fresh install.

That is a bounded, testable change and it is the next step, not this round's: shipping a default whose
band the boot process overwrites would trade "decided by the tie-break lane" for "decided by the wrong
cuts", which is the one outcome the calibration work exists to prevent.

**Also fixed this round, while proving the package.** `tools/verify_exe_bundle.py` grew two checks and
one real defect fell out of them. `proc.terminate()` on a PyInstaller **onefile** kills the bootloader
and leaves the unpacked child serving — after an earlier 6/6 run, PID 12328 was still listening on
8793, and a verifier that boots a server on a fixed port without checking the port is free can read
all its proofs from the *previous* binary (identical digests, because the frontend hadn't changed).
It now refuses to start on a busy port (naming the owner via `Get-NetTCPConnection`), tears down with
`taskkill /PID x /T /F`, and asserts the port is free again. The new behaviour check — point the
packaged app at a listener that accepts and goes quiet, then `POST /detect` and expect
`ENGINE_TIMEOUT` — is what proves the *backend* half of the exe, which no hash of `main-*.js` can
show; it also caught that `/detect` is behind the auth gate (401 `AUTH_REQUIRED` for an anonymous
call) and that boot's `seed.run_seed()` rewrites `n8n_url` from `N8N_URL` and `timeout_ms` back to
90000, so steering a packaged instance needs env, not the DB.


```bash
python -m backend.seed                      # re-read benchmark/*.json and run logs into SQLite
curl -s localhost:8787/benchmark/models     # the ranked table above, as JSON
curl -s localhost:8787/benchmark/catalog    # the probe buckets
node benchmark/doc-table.js hc3-48          # regenerate the full-corpus table, don't retype it
node tools/probe-or-headroom.js             # is the free tier servable right now? (exit 0/1/2)
node tools/probe-or-accounts.js             # are the stored keys distinct accounts, and can a cap be read?
node benchmark/tiebreak-floor.js            # what a human-side floor on the tie-break would cost
node benchmark/nonresponders.js benchmark/results-<tag>.jsonl   # N SCORED NO-ANS BAD YIELD MAXTRY SALV TRUNC
                                            # for one capture; --retry splits gateway-retryable
                                            # failures from model-side ones
node benchmark/store-band.js --tag orfull-ling-3-1-flash --ai 0.63 --human 0.52 --aic 0 --huc 0 --dry-run
                                            # fit a band, score it through the SHIPPED decision node,
                                            # compare with the defaults on the same rows, and refuse it
                                            # if any cut sits <0.03 from a measured score; replace
                                            # --dry-run with --row <models.id> to store it with notes
node benchmark/truncation-audit.js benchmark/results-orfull-inclusionai-ling-3-0-flash-sante-free.jsonl \
  # split a run's failures into "the token cap caused it" vs "the model just did not answer in JSON"
node benchmark/paired-gap.js benchmark/results-nvfull-glm-5-3.jsonl benchmark/results-ultra550.jsonl \
  --ai 0.69 --human 0.40
  # two captures of the SAME corpus, one decision rule applied to both, McNemar exact two-sided on the
  # discordant pairs only. This is the only honest way to ask whether an accuracy difference is real
  # on 48 texts: agreement carries no information, +/-1 text is noise, and jitter flips 8 of 12
  # borderline texts between identical runs. Flags take a value, so a bare filename list is positional.
node benchmark/run-benchmark.js --model <id> --tag <tag> --base_url https://integrate.api.nvidia.com/v1 \
  --max_tokens 1400 --top_p null
  # --top_p null omits the parameter entirely instead of sending a number. Some free upstreams reject
  # it outright (liquid/lfm-2.5-2.6b:free answers 200 with temperature alone and 400 with top_p), so a
  # harness that always sends top_p reports such a model as dead when it is only incompatible. Note the
  # shipped WORKFLOW cannot omit it -- see round 8.
node benchmark/catalog-probe.js --timeout 150 --concurrency 2 --tag nvidia-2026-10-10
  # --timeout is in SECONDS, not ms. A probe timeout measures worker spin-up, not model speed: at the
  # 30s default three frontier-class free models looked dead, and at 150s they answered in 14-24s.
  # Reads PRIMARY_BASE_URL / PRIMARY_API_KEY from .env by default (NVIDIA here); --base_env / --key_env
  # retarget it, --filter <regex> narrows the catalogue. Always pass --tag: without it the run writes
  # catalog-probe.json and overwrites the earlier snapshot the comparison depends on.
node benchmark/build-holdout-corpus.js --out corpus-holdout.json --total 48 --seed 20261011 \
  --exclude corpus.json --start_offset 600 --per_fetch 100
  # round 10: build a disjoint two-sided HC3 holdout. Exclusion is on normalised text, so a
  # whitespace-only difference cannot fake disjointness; prints OVERLAP and exits 2 if non-zero.
  # Never touches corpus.json. Ids stay ai-NNN/hu-NNN for tool compatibility.
node benchmark/verify-holdout-corpus.js --file corpus-holdout.json --concurrency 3
  # independent provenance check (does NOT reuse the builder): re-fetches offset=row_idx&length=1
  # per row and asserts the text is in its claimed answer list, not the opposite one, question matches.
  # Negative-control it first on a hand-tampered copy; exits 2 on any failure.
node benchmark/oos-compare.js results-nvfull-glm-5-3.jsonl results-oos-nv-glm-5-3.jsonl --ai 0.69 --human 0.40
  # in-sample vs out-of-sample for ONE model at the shipped band. compare.js joins by text id and so
  # cannot compare two different corpora; this reports each class separately and prints
  # "NOT PRESENT in this corpus -- untested, not passed" for a one-sided corpus, so an all-human
  # book set cannot fake a pass. A deferral counts as not-correct.
bash benchmark/free-round3.sh               # waits for headroom, then runs the uncaptured rows
bash benchmark/free-round6.sh               # cause-triage: 2 real texts per non-responder, concurrency 1
bash benchmark/free-round7.sh               # full 48-text capture of the survivor, then import + rank
                                            # (MODEL=/TAG= env vars promote a different model)
bash benchmark/free-round4.sh               # same gate: the 1400-token replay that tests salvaged scores
                                            # (not run: killed 2026-10-07 so round 5 could measure the
                                            # question that gates a shipped decision; see "What is still unmeasured")
bash benchmark/free-round5.sh               # same gate, behind 3 and 4: resume only the mirror's 19 abandoned texts
node tools/verify-free-lane.js              # live /detect through a free OpenRouter lane (2 requests, needs the backend + n8n up)
node tools/verify-stored-band.js --model inclusionai/ling-3.1-flash
  # does this row ship -- its cuts, its provider endpoint, its stored key, and its own lane deciding?
  # 2 requests, needs backend + n8n up, exit 1 if any of the four fails (this is what caught the
  # wrong-host bug: the verdict looked right while the named model never ran)
node benchmark/panel-row.js <captureTag>    # one attributed /benchmark/models row, on one line
node benchmark/analyze-books.js             # the all-human Gutenberg false-positive control
node benchmark/analyze-free-screen.js       # the 12-text free screen, as scored
node benchmark/test-retry.js                # the retry classifier, offline: no request is spent
node tools/verify-lane-retry.js             # the deployed workflow's retry, live webhook against a loopback mock
# the tie-break-lane comparison: replays the shipped Code node, not a reimplementation
node n8n/test-ensemble.js replay benchmark/results-ultra550.jsonl benchmark/results-diffgemma26b.jsonl
node n8n/test-ensemble.js replay benchmark/results-ultra550.jsonl \
  benchmark/results-orfull-inclusionai-ling-3-0-flash-sante-free.jsonl --secondary-threshold 0.75
node n8n/test-ensemble.js replay benchmark/results-ultra550.jsonl \
  benchmark/results-orfull-nvidia-nemotron-3-ultra-550b-a55b-free.jsonl --secondary-threshold 0.65
node benchmark/band-margins.js ultra550 orfull-nvidia-nemotron-3-ultra-550b-a55b-free 0.69 0.40 0.65 \
  # how far each tie-break call sits from its cut (the 0.000-margin finding is here, not in the totals)
node benchmark/phrase-strength-range.js      # round 17: the observed phrase-strength domain + the shipped
  # colour ramp evaluated over it. Reads the three claude-sonnet-4.5 phrase captures by default, or pass
  # capture files. It imports frontend/src/app/ui/result-card/evidence-color.ts through esbuild, so it
  # reports the ramp the browser runs; rerun it whenever a new lane is captured, and move the stops if a
  # lane emits outside 0.30-0.95. Offline: spends no request.
BACKEND_API=http://127.0.0.1:8787 ADMIN_USERNAME=admin ADMIN_PASSWORD=... \
  node benchmark/patch-bands-v3.js           # write knee-picks.json into models through the real PATCH
  # It logs in first (every mutating endpoint is admin-only since #100) and re-reads each row after
  # writing, printing VERIFIED from re-read only when the stored band equals the pick. BACKEND_API is an
  # ORIGIN WITH NO PATH -- /api is the SPA proxy's convention and the guard answers AUTH_REQUIRED.
  # Password from the environment only; the log prints a sha1: fingerprint. --dry prints the bodies
  # without logging in, --all rewrites changed=false rows, --row N limits to one. Exit 2 no password,
  # 3 login refused, 4 not admin, 5 GET /models failed, 1 a PATCH or verify failed. Rows it will not
  # write are named (row 26 kimi-k3 is EOL: its band is a record, not a lane).
node benchmark/calibrate.js results-orfull-nvidia-nemotron-3-ultra-550b-a55b-free.jsonl \
  # the free mirror's own band and the inherited one, on the full 48 rows, with stratified halves
node benchmark/analyze.js results-orfull-nvidia-nemotron-3-ultra-550b-a55b-free.jsonl results-ultra550.jsonl \
  # AUC per file: the free/paid ranking-quality comparison
# ---- round 11: clean-vs-dirty confound ----
node benchmark/build-clean-corpus.js   # writes corpus-clean.json + corpus-holdout-clean.json from the
  # dirty corpora; whitespace + a fixed whitelist only, never edits the sources, and ABORTS unless
  # fold(cleaned)==fold(content-stripped) for every row and every formatting marker is driven to 0.
node benchmark/confound-table.js "label,dirty.jsonl,clean.jsonl" \
  # single-model dirty-vs-clean: AUC (Mann-Whitney, tie-corrected), Cohen's d, mean gap, and the
  # human-as-AI / ai-as-HUMAN counts at the deployed cut. Formulas mirror analyze.js; pass one pair
  # per model captured on both corpora. Offline -- reads the jsonl, spends no request.
node benchmark/run-benchmark.js --tag clean550-hc3 --model nvidia/nemotron-3-ultra-550b-a55b \
  --corpus corpus-clean.json --max_tokens 700 --timeout 90
  # --timeout is SECONDS (added this round; default 90). It gates whether a score is *obtained*, never
  # the value, so raise it for a cold free pool: NVIDIA's free tier timed out every row at 90s while the
  # paid lane on the same key answered in 12s. Record of what was used lands in the params: manifest line.
node n8n/test-ensemble.js replay benchmark/results-clean550-hc3.jsonl benchmark/results-cleandiff-hc3.jsonl
  # the deployed ensemble re-scored on CLEAN text; diff against the same replay on the dirty captures to
  # see the confound at the decision layer (0.938 -> 0.875, and the first AI->HUMAN tie-break miss).
bash benchmark/sweep-openrouter.sh   # clean-text re-score of the OR free lanes (3-key rotation, conc 1)
bash benchmark/sweep-nvidia.sh       # paid holdout pair first, then NVIDIA free lanes GATED 2-text/150s
                                     # before a full run, so a saturated pool fails fast instead of grinding
```

New measurements go through `benchmark/` so they carry provenance; the database and the UI only
ever display what those artefacts recorded. Corpora must be real captures with a citable source —
never authored sample text.

### Round 25b — promoting the default primary out of the dead lane (#119, 2026-10-16)

The probe in Round 25 re-read the four "dead" candidates and they were never dead: they were
timing out against a 90 s lane ceiling. `z-ai/glm-5.3-flash` answers a real document in ~86 s,
and it is the only candidate that both answers and carries a fitted band -- 0.80/0.55,
measured by the K=5 knee refit on train+val: **156 decided, 3 wrong, acc/decided 0.981**.
The deployed primary (`nvidia/nemotron-3-ultra-550b-a55b`) is listed by `GET /v1/models` and
404s on every completion, so a fresh install has been reaching a working model only through
its fallback. It is therefore no longer the default.

Promoting a lane turns out to be a three-file change, and each file is a separate trap:

1. **`backend/seed.py` `DEFAULT_LANES`** picks the model, but `seed_models()` re-runs on every
   boot and wrote `DEFAULT_BAND` (0.69/0.40 -- nemotron's cuts) into the promoted row. A
   default lane re-banded to another model's ruler is a verdict-changing edit nobody made, so
   the baked band became per-model: `LANE_BANDS` / `LANE_TIMEOUT_MS`, consulted between the
   operator's env and the workflow fallback. `DEFAULT_BAND` stays what it always was -- the
   workflow's own neutral default, still asserted against the JS by the drift test.
2. **the seed only writes the rows it names**, so on an existing install the *previous*
   default kept `lane='primary'` beside the new one: two primary rows, and which model answers
   becomes a question about row order while the UI shows a lane nobody chose. `seed_models()`
   now un-claims lanes it did not name (`lane = NULL`, `role = 'either'` -- `role` is
   CHECK-constrained and cannot go NULL) and leaves the demoted row's band, notes and history
   exactly as they were.
3. **`.env` is operator config and outranks every baked default.** Leaving
   `AI_PROBABILITY_THRESHOLD=0.69` there while naming glm would have re-created through the
   front door the exact bug closed in step 1, so the threshold block now carries glm's own
   measured cuts and says why it must move with `PRIMARY_MODEL`.

Verified: `python -m pytest backend/tests -q` green, including two new tests -- one that a
promoted lane is seeded on its own measured band (and that env still beats the overlay), one
that changing the default does not leave the old model claiming a lane. The upgrade path was
then proved on a live copy of the dev DB: one `lane='primary'` row, glm at
0.80/0.50/0.55/0.50 with `timeout_ms=120000`, nemotron demoted with its 0.69 band intact.
`tools/verify_exe_bundle.py` gained two checks so the packaged app is proven by the database
its own boot writes, not by the developer's tree.

Not done: the promoted band was fitted 2026-10-15, i.e. under the token cap that came off on
2026-10-16, so the seeded note still says uncapped re-validation is pending (#109/#111) rather
than claiming a validation that has not happened. The deployed n8n workflow JSON still carries
nemotron as its *fallback* model id; the backend names the model explicitly on every request,
so nothing routes through that constant -- but a standalone import of the JSON would.

### Round 25c — re-auditing the tie-break on the new primary, and the ruler bug found doing it

The promotion changes which texts the tie-break is ever asked about, so its authority has to be
re-measured on the new lane rather than inherited. Two defects stood in the way, and both are
now fixed in `benchmark/tiebreak-authority.js`:

1. **The band was a constant.** The tool hardcoded `0.40`/`0.69`/`MID = 0.545` -- nemotron-3-ultra's
   ruler. Pointed at another model's captures it judged those scores against the previous model's
   cuts, so "deferrals" and "verdicts" were artifacts of the wrong ruler. It produced the numbers
   this round first published (44/4 shipped, a 43/2/3 candidate), which are **retracted**.
2. **The rule was a copy, not the node.** After parameterising the band, the tool still
   reimplemented the arithmetic in its own file. The shipped node does not defer on probability
   alone: `primaryVerdict` needs `p >= ai_prob_threshold AND conf >= ai_conf_threshold` (and the
   mirror of that on the human side), so a low-confidence reading defers even when its probability
   sits outside the band. My hand-rolled version counted 1 deferral of 48; the real rule defers 3.

It now *executes the shipped Ensemble node* (`n8n/test-ensemble.js -> evaluate()`), reads the
band from the same config a live request would use (so `.env`, the workflow fallback and the model
row agree by construction), prints each capture's provenance out of `benchmark/runs-manifest.log`,
and exits 2 when the primary capture's manifest model is not the model whose ruler is in play --
the exact mistake it was rewritten out of. `test-ensemble.js` gained an optional overrides
argument (last positional, so existing calls are unaffected) and prints the band and lane models
on every replay, because a tally without its ruler is unreadable.

Faithful measurement, deployed default (`z-ai/glm-5.3-flash` primary at 0.80/0.55 with the 0.50
confidence floors, `google/diffusiongemma-26b-a4b-it` tie-break at 0.90, hc3-clean 48):

| rule | correct | wrong | review | acc/answered | answered |
| --- | --- | --- | --- | --- | --- |
| shipped: secondary decides every deferral | 44 | 4 | 0 | 0.917 | 48 |
| secondary may not create an AI verdict over a human-leaning primary | 44 | 2 | 2 | 0.957 | 46 |
| secondary lane off | 44 | 1 | 3 | 0.978 | 45 |

The tie-break decided 3 of 48 texts and got all 3 wrong: `hu-004` (primary 0.78), `hu-009`
(0.50, conf 0.40) and `hu-019` (0.45, conf 0.35) -- three human texts, each answered AI by a
secondary reading 0.95-0.98. A strong primary whose band is tight leaves the lane nothing useful
to do: no AI text deferred, so every text it was asked about was human.

That is a property of *this* primary, not a verdict on the lane, and the counter-evidence is in
the same tool. Replayed on the previous primary with its own ruler (nemotron-3-ultra-550b at 0.69/0.40, conf floors 0.60):
the tie-break decided 15 of 48 texts, 13 correctly; with the lane off the ensemble answers only
38 of 48 -- 36 correct and 2 wrong, i.e. 8 fewer correct answers to buy 2 fewer errors. So the
lane's AI authority is worth a great deal when AI texts do defer, and is pure cost when they
don't.

One arithmetic trap to record before anyone reads a cut sweep as symmetric: `secondaryVerdict`
is `p >= secondary_decision_threshold ? AI : HUMAN`. Raising the cut above every score does not
silence the lane, it makes the secondary answer HUMAN on everything. On this pair that degenerates
to 47/1/0 (all three deferrals happen to be human) and on the ultra550 pair it collapses to
36/12/0. No candidate rule is applied on the strength of a degenerate cut.

Sample: 48 texts, one corpus, and the primary capture was taken at `max_tokens=1400` while the
secondary capture is uncapped -- so this is capped-era evidence on the primary side (#109/#111).
No matched glm-flash x diffgemma pair exists on a second corpus, so the tie-break's numbers here
are not cross-validated; the holdout has a diffgemma capture but no glm one.

**Decision, priced for the operator.** The three rows above are the whole exchange: shipping the
lean-floor restriction (needs a new config key in the node, then a redeploy) halves the false
accusations at 2 extra reviews; turning the lane off (already supported end to end, one default
flip) removes 3 of the 4 errors at 3 extra reviews and gives up the backup-lane insurance; the
shipped rule answers every document and carries 4 false AI accusations against human writing. The
choice is the operator's, because the band was fitted on the primary alone and never licensed the
secondary to overturn it.

### Round 25d — the timeout defect found by running the default for real

Latency, measured the hard way: the first live run of `tools/verify_default_lane_live.py`
answered 502 `DETECTION_UNAVAILABLE` / "timed out". That was not pool bad luck alone -- when
`.env` names a lane, the seed took the operator's timeout path and never consulted the model's
measured ceiling, so the promoted 86 s model ran against the baked 90 s one. Timeouts are a
property of the model, not a policy, so `LANE_TIMEOUT_MS` now applies whenever
`LLM_TIMEOUT_MS` is unset, whichever way the lane was chosen; an explicit operator timeout
still wins. The suite covers both.

The rerun after that fix still timed out, at the model's own 120 s ceiling, so the proof stands
at 7/10 checks: seeding, the promoted lane, its band and `/api/models` are all verified on a live
boot of the packaged backend, while one real document through the free NVIDIA pool did not answer
in time. That is an open operating question, not a broken build: glm-5.3-flash's measured median
is ~86 s and the pool's tail is longer than any client ceiling that stays usable in a browser.

### Round 25e — the choice made, shipped, and made un-overridable from a browser

Two decisions, both the operator's, both now the shipped default:

1. **The tie-break lane ships switched off.** On the corrected round-25c numbers the lane was
   asked 3 of 48 texts and answered all 3 wrongly (three human documents accused of being AI),
   while the lane-off ensemble answered 45 of 48 at 44 correct / 1 wrong / 3 review.
2. **The glm lane ceiling goes to 180 s**, the number the band's own capture was run under
   (superseded 2026-10-17 by Round 26: the ceiling is a property of the *pool*, not the capture).
   120 s was already tried and timed out on a live document; a ceiling that is lower than the
   measurement it is supposed to protect is not a margin, it is a coin flip.

Implementation, and the part that only showed up while implementing it:

* `LANE_DEFAULT_ENABLED = {"secondary": False}` writes `models.enabled` on the **first insert
  only** -- absent from the upsert's `DO UPDATE`, because a boot that re-imposes a default is
  the same machine that silently reverts an admin's deliberate change (the trap documented for
  direct DB edits). Existing installs cannot be reached by a first-install default, so
  `apply_lane_defaults()` says it once, records `lane_defaults_version`, and stays out of the way.
* `SECONDARY_ENABLED` in `.env` outranks the baked default, on a new install and on the upgrade
  path alike -- the project rule that env beats a baked value, the same way a threshold in env
  beats a measured band. `.env.example` now says `false` with the round-25c numbers next to it,
  so the file and the code cannot drift apart silently.
* `detect()` sends `lane_config.secondary = {"enabled": false}` whenever there is no **enabled**
  secondary row, in both branches: no `lane_config` at all, and a caller that names only a
  primary. The second branch is the one that mattered. An absent `lane_config.secondary` does not
  mean "off", it means "the engine decides", and the engine's env default names a second model --
  so clicking a ranking card to pick a scorer used to buy back the tie-break the operator had
  just switched off, and let a model nobody had enabled overturn the primary. Now a request body
  can switch a lane **off** and never **on**; enabling it is an admin write to the row.
* The admin's Configuration switch was a browser preference all along: `toggleSecondary()` only
  ever wrote `localStorage`. With the backend now authoritative, that card would have shown an
  admin a setting that changed nothing for anybody else, so it reads `models.enabled` on load and
  PATCHes the row on toggle, and says so out loud if the write fails. The per-user, per-request
  opt-out still works the way it always did -- off stays something a caller may ask for.
* `tools/verify_default_lane_live.py` gained an eleventh check, because "the default answered"
  was not the claim under test any more: the proof now requires that the engine reports
  `config_used.secondary.enabled === false` and returns **one** evidence leg. A DB row with
  `enabled = 0` is a statement about configuration; a one-leg answer is the behaviour.

Provenance of the run, stated as it happened: the first live boot after the timeout fix passed
10/10 -- the promoted lane answered a real document in 85.8 s (`22:11:35 → 22:13:00`), with
phrase evidence, on its own band. Minutes later, on the same build, a rerun answered 502
`DETECTION_UNAVAILABLE` / "timed out". That is the pool regime this document has repeatedly
recorded (p50 ~86 s with a tail past 200 s), not a regression in the ceiling: 180 s plus the
backend's 30 s headroom is what the measurement supports, and no client ceiling outruns a server
that is queueing. The gap that was open -- the eleventh check never having been seen on a *passing*
run -- closed on the final code at 22:25 UTC: `11/11 checks passed`, with
`PASS the default lane answered a real document [200 AI_GENERATED, received_at 22:25:13.703Z,
completed_at 22:26:24.251Z]`, `PASS phrase evidence ... 3 quote(s) over 1 leg(s)` and
`PASS ...and the tie-break lane stayed off (no second call bought) [secondary.enabled=False legs=1
disable_warning=True]`. A timed-out run still fails that check loudly rather than reporting an off
lane it never observed.

**The same default, proven inside the shipped artifact (22:32-22:39 UTC).** A hosted page's default
is a database row; a local app's default is a binary, and the two are proved differently. The exe
was re-staged from the fresh bundle (`main-CJD6OAB6.js`, staged with the `rm -rf` that BUILD.md
insists on, so no superseded hash was embedded), rebuilt, and `python tools/verify_exe_bundle.py`
now runs **18 checks in two boots**:

* the upgrade boot (a throwaway copy of the dev DB, whose tie-break row still said `enabled = 1` and
  which carries no `lane_defaults_version` key) proves the packaged seed switches it off exactly once
  (`secondary_enabled=0 lane_defaults_version=2`) -- and, new, proves what the packaged backend
  *sends*: the stand-in engine now keeps the request body it was given, and it received
  `lane_config = {"primary": {...}, "secondary": {"enabled": false}}`, i.e. one lane named and the
  tie-break explicitly off. Headers are not read, so no key enters the report.
* the virgin boot (a database file that does not exist, which is the state a user who just ran the
  installer is in -- the copy above cannot prove the first-install insert path at all) proves the
  binary seeds one primary `z-ai/glm-5.3-flash` on its measured `0.8/0.55` band with
  `timeout_ms=180000` (since Round 26: 240000), a configured secondary row with **`enabled = 0`**,
  and the version key.

Because the re-seed rewrites `timeout_ms` from `LANE_TIMEOUT_MS`, the timeout ceiling under test can
only be set through the environment: the upgrade boot pins `LLM_TIMEOUT_MS=1000` so its wait is 31 s
(`timeout_s=31` in the ENGINE_TIMEOUT line) instead of 210 s (Round 26: 270 s), and the virgin boot
deliberately leaves it unset so the shipped ceiling -- 180 s then, 240 s since Round 26 -- is what
gets read back. `installer/README.txt` gained a "Which model
answers" section for the same reason: a user reading the folder should learn the lane ships off and
that switching it on is an admin action for the whole install, not a personal preference.

## Round 26: the ceiling is a property of the pool, not of the capture (2026-10-17)

The container deploy was proven live (#106) with one clause still unsatisfied: on the **shipped
default lane** the verifier's two texts both came back `502 … ENGINE_TIMEOUT`. The tempting reading
was "the container is broken". It was not. A throwaway probe that lifted only
`tuning.request_timeout_ms` made the same pipeline answer `hu-000` correctly -- `HUMAN_LIKELY`,
p 0.15, three phrase quotes -- in **214.2 s**, while the hourly reachability probe recorded the same
model at 127-162 s in that hour and the row's ceiling was 180 s (+30 s of backend grace = 210 s).
The ceiling was cutting a working detection, and the engine tier follows the row automatically: the
workflow's HTTP nodes read `options.timeout` from `config.request_timeout_ms`, which `app.py` derives
from `models.timeout_ms`.

The operator priced the trade (task #127): **wait for glm rather than move the default lane.** So
`seed.LANE_TIMEOUT_MS` moved 180000 -> 240000 and the chain moved with it as one unit:

```
lane timeout_ms 240 s  ->  backend wait 240 + 30 = 270 s
                         ->  gunicorn --timeout 285 s
                         ->  nginx proxy_read_timeout 330 s
```

The band (0.80/0.55) did not move, so no accuracy claim in this document changed: a raise in the
ceiling buys *coverage*, not a new ruler. Texts that used to 502 now answer, and the first one that
did answered in its usual place.

Two mechanics worth recording, because both were assumed rather than checked:

* `seed_models`' upsert carries `timeout_ms = excluded.timeout_ms`, so the baked change reaches an
  **existing** install at its next boot -- no migration, no one-shot floor, no admin action. Proved
  on the running container: row 1 read `timeout_ms=240000` with `updated_at` at the recreate, band
  and lane untouched. This is the *opposite* of the `enabled` column, which is deliberately absent
  from the upsert so an admin's lane switch survives a boot (#124).
* Every tier above the lane has to be re-derived from it, and nothing in the repo checked that. The
  ceiling has now moved 120 -> 180 -> 240 while the outer tiers were each hand-edited once.

So `tools/verify_deploy_recipe.py` grew four checks: compose and the container entrypoint must agree
on `--timeout`; gunicorn must clear `lane + 30`; nginx must clear gunicorn; and `docker/DEPLOY.md`
must quote the same three numbers the files carry (the doc is the thing an operator edits). The
check reads the ceiling out of `seed`, not out of a number typed into the tool, and it was
negative-tested by setting nginx to 280 s: `FAIL nginx proxy_read_timeout clears gunicorn
[nginx 280s > gunicorn 285s]`, exit 1, then restored. 33/33 after the raise. The live verifier's own
client timeout went 300 -> 360 s for the same reason: a probe that gives up before the server does
reports a working deploy as broken.

After the rebuild (`docker compose build backend web && docker compose up -d`), the same 11 checks
were re-run against the published nginx origin with **no `DETECT_MODEL` override** -- the shipped
operating point, not a stand-in:

```
PASS  a labeled human text is scored through nginx (hu-000)
      [200 209.4s verdict=HUMAN_LIKELY p=0.18 key_source=header
       lane=z-ai/glm-5.3-flash secondary=False evidence_legs=1 phrases=3]
PASS  a labeled AI text is scored through nginx (ai-000)
      [200 199.5s verdict=AI_GENERATED  p=0.80 ...      phrases=3]
PASS  the deployed lane separates the two classes it was calibrated on
      [human=HUMAN_LIKELY ai=AI_GENERATED]

12/12 checks passed
```

209.4 s and 199.5 s are both above the 180 s ceiling the row used to carry, and both answer. The
count went 11 -> 12 because the separation check only runs when no `DETECT_MODEL` override is set:
it refuses to claim class separation for a lane it did not pick. `ai-000` landing exactly on the
0.80 cut is the probability lattice doing what Round 16 said it does -- the cut sits *between*
populated values for that reason, and the row's own band made the call, not a rounding tie.

One honest limit: this proves the shipped operating point **answers** at 240 s, on two texts, in a
pool regime where 200 s is normal. It does not re-fit anything and it is not a coverage claim --
a pool slower than 240 s still returns `502 ENGINE_TIMEOUT`, and that is now the operator's chosen
failure mode rather than a defect inherited from the capture.

The packaged local app carries the same decision, because it bakes the same seed: after rebuilding
the onefile (`installer/BUILD.md` -- backend-only change, so no UI re-staging, `STAGE_MATCHES_DIST`
confirmed first), `tools/verify_exe_bundle.py` passed 22/22 including its virgin-install phase, which
reads the ceiling out of `seed.LANE_TIMEOUT_MS` and compares it to what the binary actually wrote:

```
PASS  with the 240 s ceiling the capture actually supports
      [timeout_ms=240000 (backend waits 270 s)]
```

## Round 27: the report is a page the product serves, not a folder in the repo (2026-10-17)

Asked whether the research was "in" the deliverable, the answer had been no: it lived in
`research-zenodo/`, waiting on a DOI only the author can mint. That is the wrong shape for a site whose
whole claim is "here is the evidence, dated to its window" — a user on the deployed page cannot see a
directory. So the report became **an asset of the build**, and this round is the measurement of that
move plus the two things it exposed.

**One placement covers three servers.** `frontend/angular.json` globs `public/**/*` into the bundle, so
`frontend/public/research/report.html` arrives in `dist/frontend/browser/research/`, which nginx serves
through `try_files $uri` and the packaged exe serves through the same `SpaSplit` tree it uses for the
SPA. The dev server serves it too. Nothing backend-side was added, and the deposit copy under
`research-zenodo/` stays the authority — the preflight now fails if the two drift and prints the exact
`cp` that repairs it.

**`paper_url` could not be the mechanism, and that is a validation fact, not a preference.** The
settings validator accepts only an absolute http(s) URL with a host, so `/research/report.html` can
never be stored in it. The link therefore resolves in the view: admin URL set → the archival copy (a
DOI); blank → the report this deployment serves. The earlier behaviour ("link renders only when a URL
is configured") is gone, because it left a fresh install with no citation at all — the same
user-visible dead end the Ranking page had with zero pinnable cards.

**The trap that would have shipped silently: `cp` without `-r`.** Staging step 1 of `installer/BUILD.md`
is `cp frontend/dist/frontend/browser/* …`; `cp` prints `omitting directory` and exits non-zero for
`research/`, so the exe would carry the current hashed bundle and no report. What makes this worth
writing down is that no existing check would have caught it *by name*: the bundle hash is the SPA's,
and both servers answer an unknown path with `index.html` and **status 200**. A status code can never
show a missing static asset here. The checks added compare served **bytes** with the repo copy:

```
PASS  the packaged exe serves the technical report the Ranking page links  [200 53601 bytes identical_to_deposit=True]
PASS  …and the printable copy as a PDF  [200 471173 bytes magic=b'%PDF-']
```

`backend/serve_local.py` also pins `.pdf: application/pdf` in its mimetype map. Without it the frozen
exe falls through to the Windows registry for an extension it has never seen, and the printable copy
arrives as whatever the host decides — which is why the container check asserts the Content-Type, not
just the bytes.

**Live proof, both artifacts, same build.** The two shipped images were rebuilt (`docker compose up -d
--build backend web`, n8n left running so its webhook registration survives) and the stack re-driven
through nginx:

```
PASS  …and the technical report is served by the site, byte for byte  [53601 B ctype='text/html' part_ii=True]
PASS  …and the printable copy downloads as a PDF, not as HTML  [471173 B ctype='application/pdf']
PASS  a labeled human text is scored through nginx (hu-000)
      [200 203.4s verdict=HUMAN_LIKELY p=0.18 key_source=header lane=z-ai/glm-5.3-flash
       secondary=False evidence_legs=1 phrases=3]
PASS  a labeled AI text is scored through nginx (ai-000)
      [200 223.6s verdict=AI_GENERATED p=0.88 key_source=header lane=z-ai/glm-5.3-flash
       secondary=False evidence_legs=1 phrases=4]
PASS  the deployed lane separates the two classes it was calibrated on
14/14 checks passed
```

`ai-000` read `p=0.80` in Round 26's run and `0.88` here — same verdict, same side of the 0.80 cut by
a wider margin, and exactly the run-to-run movement Round 16 measured. Noted rather than smoothed
over, because a report that quotes one number cannot claim the other.

The preflight that needs no daemon went 33 → 38 checks, the container proof 12 → 14, the exe proof
22 → 24; the setup package recompiled in 41.656 s with no warnings (44,941,122 bytes).

**What could not be re-run, and why it matters.** The Angular suite did not execute for this round.
The failure is the host, not the code: `ng test` builds through esbuild, and every invocation — a
whole-`ui` chunk and a single file — died in the build phase. Three different error strings appeared
inside twenty minutes of retries: `# Fatal process out of memory: Zone`, then
`Error: spawn UNKNOWN` while starting the esbuild service, then `Error: EINVAL` at
`new Worker (node:internal/worker:293)` from piscina's thread pool. `Win32_OperatingSystem` reported
`FreePhysicalMemory 1,596,324 KB` but `FreeVirtualMemory 24,080 KB`: the commit charge, not the working
set, was exhausted (vmmem holding ~5.8 GB with the stack up, which this round must not stop).
`bash` itself failed to fork once in the same minute (`0xC0000142`). `NODE_OPTIONS=--max-old-space-size`
cannot reach a native zone allocation, and shrinking `--include` to one file does not help because the
build is per invocation, not per file — so there was no knob left to turn. What was done instead, and its exact limit: the three new specs
(blank → `/research/report.html`, DOI → DOI href, DOI + blank label → "Read the paper") ran green
earlier in this same session against the sources that are now in the build — `ranking-view.ts` and
`ranking-view.spec.ts` have not been touched since — and the shipped bundle was then proved by
grepping **the served bytes** for the fallback path (`verify_exe_bundle.py "/research/report.html"`,
2 occurrences in `main-3WW256QJ.js`). That is evidence the behaviour is in the artifact; it is not
evidence the specs still pass, and the suite owed a rerun on a host with commit headroom.

**The owed rerun happened, ~40 minutes later, and it passes.** Commit headroom came back on its own
(`FreePhysicalMemory 1,244,212 KB`, `FreeVirtualMemory 2,788,380 KB` — an order of magnitude more than
during the failures), which let the first retry get *further* than any earlier attempt and produce a
**fourth** signature: the bundle built cleanly (`Application bundle generation complete. [39.476 seconds]`),
Vitest printed its `RUN` banner, and then the log ended at `Error: Worker exited unexpectedly with
signal SIGTERM during stopping state` with **zero** test output and exit code 1. Nothing in that log
distinguishes a dead host from a broken spec, so it is recorded here as the shape to check for. The
second attempt (`npx ng test --watch=false --reporters=dot`) ran end to end:

```
Application bundle generation complete. [33.604 seconds]
 Test Files  7 passed (7)
      Tests  89 passed (89)
   Duration  33.01s (environment 81%, setup 8%, tests 7%, transform 2%, import 2%)
```

86 → 89 is exactly the three `describe('RankingView report link')` specs, all seven spec files were
discovered (`--include` left at its default), and the gap this round opened is closed. The behavioural
proof did not depend on it: the link was also driven in the deployed container as a signed-in non-admin,
where `a.paper-link` read `href="/research/report.html"`, `target="_blank"` and `rel="noopener noreferrer"`
beside 48 model cards — and the URL itself answers `200` with no session (`Set-Cookie` absent), so the
document is public even though the UI path to it is not.

**The report itself grew a Part II** (window 2026-10-13 → 2026-10-17, §9): kimi-k3's zero-wrong
recommendation is retracted as an operating choice — the model returns HTTP 410 `model_removed` with
`effectiveDate: 2026-10-15T08:00:00Z`, so its capture is frozen at 39/48 and its promotion was already
void on holdout grounds; the shipped primary's replacement lane is documented with its measured band,
its 156-decided/3-wrong score and the tie-break-off comparison table; the transport ceiling (504 at
~302 s server-side, no client timeout outruns it), the length-band collapse (coverage 92 % → 29 % on
<300-char texts), the stress corpus, the clean-vs-dirty effect sizes, the phrase-evidence domain
(575 quotes, 0.30–0.95) and the uncapped control (McNemar p = 1.0000, 0 discordant) all appear with
their corpora named. The §6 table now carries the two correction marks instead of a prose caveat, and
the old "no free lane verified through a live webhook" limitation is closed by the 12-of-12 run above.


## Round 28: the shortest fuse in the timeout chain was inside the browser, and the catalogue that screened 0/4 (2026-10-17)

Three of the brief's promises were driven against their own artifacts this round — auto-discovery,
auto-switching to the second-best model, and "enter your key, test it, get your models ranked". Each
broke on the same class of mistake: a limit written at one tier without reading the tier outside it.
All three are now measured, and one live run proves all three at once
(`tools/verify_pool_failover.py`, **45/45 checks**, a real NVIDIA key, a throwaway account, a copy of
the dev database).

**The chain, as it stood before this round.** Every number below was in a shipped file:

```
one lane call       240 s   seed.LANE_TIMEOUT_MS (the shipped primary's ceiling)
the backend waits   270 s   lane + 30 s of grace (app.py)
a whole request     250 s   REQUEST_BUDGET_S; app.py takes max(lane + 30, this)
gunicorn            285 s   docker/backend-entrypoint.sh
nginx               330 s   docker/nginx.conf proxy_read_timeout
the browser    150 s for /detect, 60 s for a key probe   <-- the smallest number in the chain
```

The outermost tier was the one with no patience, which produced two user-visible lies. A detection
that the server was still honouring was reported as failed at 150 s — and then carried on behind the
abort and landed in History, so the row and the panel disagreed about whether the text had been judged.
Worse, the key probe's 60 s ceiling reproduced exactly the false negative `probe_timeout_s` was written
to remove: on a congested pool `z-ai/glm-5.3-flash` does not answer a one-word probe inside 60 s, so
the app told a user their **working key was broken**. Because a verified key is the only thing that
queues auto-discovery, the whole flow the brief asks for — enter it, test it, save it, get his models
ranked — stopped at step one, silently, with a red label the user had no way to doubt.

The fix is one constant, `HTTP_PATIENCE_MS = 360_000`, in a file that exists to make the chain
readable: `frontend/src/app/core/http-budget.ts` carries the table above in its doc-comment and is
imported by the detection request and by every model-bearing call in `backend.service.ts`. 360 s
clears nginx's 330 s, so a timeout now means what a timeout should mean — the browser itself is stuck
— and the answer the user gets is the one the *server* decided: a verdict, a named `ENGINE_TIMEOUT`,
or a gateway 504. It is not a blanket 360 s: the control-plane reads keep their own short waits
(pool GET/POST and discovery status 8 s, catalogue 15 s), and a spec asserts the difference, because
"make everything patient" would also have made a stuck settings panel look alive for six minutes.

```
PASS  the stored key verifies live against the provider  [nvidia/z-ai/glm-5.3-flash latency=68413ms]
```

68,413 ms — eight seconds past the ceiling that would have called that key dead. The worst probe
measured anywhere in this session, 81,812 ms, is now a named constant in `backend.service.spec.ts`
(`MEASURED_SLOW_PROBE_MS`), so the ceiling is anchored to an observed reply rather than to a guess,
and `verify_deploy_recipe.py` fails if any server-side tier grows past the browser's number. The
preflight went 38 → 40 checks and both new guards were proven red by temporary edits before being
proven green by revert.

**A budget belongs to the request, not to whichever model is on top of the pool.** The failover
worked; its arithmetic did not. A pool of `[nemotron-3-ultra (row timeout 90 s), z-ai/glm-5.3-flash
(240 s)]` refused in 0.7 s, failover correctly decided to switch — and handed the replacement 109 s,
because the deadline had been computed from the *first* model's ceiling. glm needed longer than that
on a congested pool, so 120 s bought two non-answers and no verdict. `REQUEST_BUDGET_S = 250 s` is now
the request's own ceiling and `deadline = time.time() + max(timeout, REQUEST_BUDGET_S)`; the first
attempt still keeps its lane's ceiling (`max()`, not `min()` — a single lane at 240 s is what the
tiers above are derived from), and only the *leftover* for a retry comes from the request, with
`FAILOVER_RESERVE_S = 20 s` required before a second call may start. Below that reserve the retry
cannot finish, and starting one converts a clean 502 into a gateway timeout with no verdict at all.

The live sequence, with a model that returns HTTP 410 (`moonshotai/kimi-k3`, delisted 2026-10-15)
dragged to rank 1 on purpose:

```
PASS  detection 1 still returns a real verdict  [REVIEW_REQUIRED in 75.1s]
PASS  the verdict names the switch, from the model that refused to the one that answered
      [{"from": "moonshotai/kimi-k3", "to": "z-ai/glm-5.3-flash",
        "reason": "the first model returned no usable verdict"}]
PASS  ONE refusal does not reorder the pool (a blip is not a delisting)
      [health=degraded failures=1 position=1 your_rank=1]
PASS  three consecutive refusals mark the model down  [failures=3 last=no_choices]
PASS  the auto-rearrange moves the served order and keeps the user's rank
      [your_rank=1 served=2 demoted=True]
PASS  detection 4 answers  [REVIEW_REQUIRED in 85.8s]
PASS  no failover was needed: the pool's own reordering picked the live model first
      [lane_source=user-pool auto_switch=False]
PASS  the history row names the model that answered, not the one the user ranked first
PASS  the ranking page carries the same health the pool does
      [{"health": "down", "your_rank": 1, "position": 2}]
```

The two orders on that last pair of lines are deliberate and are the design answer to "auto rerange
of the ones brought into the using pool": the **ranking page** orders by *measurement* — kimi-k3 stays
#1 on its 48-text capture even though it currently 410s, because that capture is a dated fact — while
the **pool** orders by *health*, and the health marker is shown on the ranking card too, so a user can
see why his #1 is not being called. `your_rank` is stored and never rewritten by the machine; only
`position` moves.

**The screening walk, which is what "bring his models" actually has to mean.** The 2026-10-17 failure
being repaired is quoted in `backend/tests/test_discovery.py`: the live job for nvidia listed 81 ids,
screened the first four in catalogue order (`01-ai/yi-large`, `adept/fuyu-8b`, `ai21labs/jamba-1.5-large-instruct`,
`aisingapore/sea-lion-7b-instruct`) and wrote `0/4 usable`. A provider's catalogue is **alphabetical**,
and the delisted legacy models sort first, so "screen the first four" means "spend the user's budget on
the four oldest ids". The walk now separates *trying* from *buying*: the first screen text doubles as
triage — a candidate that refuses text 1 has already told us it is no use, so giving it a second text
spends money on a 404 — and the walk continues past refusals up to `DEFAULT_TRIAGE_CAP = 24`, while
`discovery_screen_limit` (admin setting, 0-24, default 4) still bounds how many full 2-text **screens**
get bought. A refusal writes only a `catalog_models` row; it never becomes a `benchmark_runs` row, a
`models` row, a rank, or a pool entry, and `needs_screen` skips a `dead` id for `DEAD_RETRY_DAYS = 7` so
a delisting that was really an outage is still noticed.

The same live run, start to finish, on the real 81-id catalogue:

```
PASS  discovery is refused without the caller's own key  [http 409 NO_USER_KEY]
PASS  saving a verified key queues discovery on its own  [job 1 (key verified)]
   [   0.0s] running: fetching catalogue
   [   4.0s] running: triage 8/24, screen 0/4: deepseek-ai/deepseek-v4.1-flash
   [ 185.0s] running: triage 11/24, screen 0/4: google/deplot
   [ 189.0s] running: triage 17/24, screen 0/4: ibm/granite-3.0-8b-instruct
   [ 193.0s] running: triage 21/24, screen 0/4: meta/llama-3.2-11b-vision-instruct
   [ 365.4s] running: triage 22/24, screen 1/4: meta/llama-3.2-90b-vision-instruct
   [ 562.8s] running: triage 24/24, screen 2/4: meta/llama2-70b
   [ 566.8s] done: 2/2 usable of 24 candidates tried
PASS  a real catalogue fetch came back with models to choose from  [n_listed=81]
PASS  each candidate that refused the triage text was recorded as a refusal
      ['triage …' notes for nvidia: 0 -> 22, refused=22 (tried 24, screened 2)]
PASS  a refusal names why it refused, so a delisting is not mistaken for a bad afternoon
      [meta/llama2-70b -> triage 0/1 texts answered: no_choices -- {"data":"{\"sta"]
PASS  the screens left real benchmark_runs rows, attributed to the probe corpus  [144 -> 146]
PASS  screened models got a row so they can be ranked  [31 -> 32]
PASS  a 2-text screen lands in the probe tier and cannot outrank a measured capture
      [best measured #1 (moonshotai/kimi-k3, n_rows=48) vs screened #4, #16, #19]
PASS  a discovered model is appended behind the healthy ones the user placed by hand
      [placed=[('z-ai/glm-5.3-flash', 1), ('moonshotai/kimi-k3', 4)]
       auto=[('meta/llama-3.2-11b-vision-instruct', 2),
             ('meta/llama-3.2-90b-vision-instruct', 3)]]
PASS  a model that refused triage was not added to the using pool
```

**24 candidates tried, 22 refusals recorded, 2 screened, 2 usable** — where the old walk produced 0.
The cost is honest and is now printed in the job's own final stage ("`{n_usable}/{n_screened}` usable
of `{n_tried}` candidates tried"), because the row count cannot show it: every id NVIDIA serves is
already in `catalog_models`, so a run that touched nine brand-new ids and a run that touched none both
read `96 -> 96`. That is why the verifier's before-snapshot was rewritten — the first version asserted
on a number that provably cannot move, and the assertion it replaced had been passing on luck.

Two timings from that log worth keeping. The gap between `triage 8/24` at 4.0 s and `triage 11/24` at
185.0 s is three candidates, one of which ran to the full 180 s screen ceiling on a congested pool: the
walk is bounded by `TRIAGE_CAP`, not by the provider's mood, and its worst case is 24 candidate calls
of which at most 4 cost two texts. And `n_tried` is deliberately *not* a column on `discovery_jobs` —
only `n_listed`, `n_screened`, `n_usable` and `stage` are exposed on `/my/discover`, so the verifier
has to parse the count out of the stage text, which is exactly what a user sees.

**The dead end that made a suite look cleaner than it was.** The first version of the new detection
spec used Angular's `fakeAsync`/`tick`, and the file failed to *collect*:

```
Error: zone-testing.js is needed for the fakeAsync() test helper but could not be found.
Test Files  1 failed | 6 passed (7)
     Tests  86 passed (86)
```

The tally said 86 passed — the truth is that sixteen tests in the failed file never ran, and the line
that would have shown it was the summary of a run whose headline looked almost clean. `frontend/package.json`
carries no `zone.js` at all (Angular 22.2, zoneless), so `fakeAsync` is simply not available in this
project; `vi.useFakeTimers()` + `vi.advanceTimersByTime()` do drive the rxjs `timeout()` operator and
the spec was rewritten on them. A second finding in the same file: a request aborted by `timeout()` is
already **cancelled**, so the "clean up the pending request" step — `req.flush()` — throws `Cannot
flush a cancelled request`. The test's own teardown was the bug; `http.verify()` counts the cancelled
request as handled, and nothing needs draining.

**Tallies.**

```
python -m pytest backend/tests -q           158 passed in 96.50s   (adds the offline discovery walk: 6)
cd frontend && npx ng test --watch=false    Test Files 8 passed (8) / Tests 104 passed (104)
python tools/verify_deploy_recipe.py        40 checks passed
python tools/verify_pool_failover.py        45/45 checks passed, live against NVIDIA
```

The Angular run needed two attempts: the first died with `Worker exited unexpectedly with signal
SIGTERM during stopping state` and zero test output — the host's commit-charge exhaustion recorded in
Round 27, not a spec failure.

**What this round did not ship.** The verifier runs `python -m backend.serve_local` on a copy of the
dev database, so it proves the **sources**. The deployed artifacts are older than these fixes: nginx is
serving `main-3WW256QJ.js` while local `dist` holds `main-Y72S4VCX.js`, the images are about six hours
old, the exe is from 15:51 and the installer from 15:53, and the sources under test were edited at 21:24
(`backend/discovery.py`) and 21:28 (`http-budget.ts`, `detection.service.ts`). The 360 s browser tier,
the probe ceiling, the failover budget and the triage walk are therefore in the repo, the two unit
suites, the preflight and the live verifier — **not yet in the container or the packaged app**, which
is the rebuild that follows. Still true from earlier rounds, and unchanged by this one: the shipped n8n
workflow reports gateway statuses as `no_choices`, so a History row reads `no_choices` where "HTTP 410
end of life" would be honest, and closing that is a workflow redeploy rather than a backend edit.

## Round 29: the ranking page went public, and the design brief became a measured rule (2026-10-18)

Two asks drove this round. The first: put the technical report on a **public** site whose page carries
only the ranking table and each model's measured performance, ordered the way the application's own
auto-order ranks them. The second was a design brief, not a feature request:

> no avoid rounded corner make them sharp with the top right and down right cutted, make the font the
> why cleaner and interactive and make dark absolute dark and light absolute white, leave blue for
> interaction light hovering and such

**The public page.** `tools/public_ranking_page.py` reads the same `models` rows `/api/ranking` serves,
sorts them by the same key the app sorts by (measured rows first, then by the operating point the band
gives them), and writes one static HTML file — no app framework, no backend, nothing that can be
broken by a bad deploy. `tools/verify_public_site.py` then checks the **published artifact from
outside**: 20/20, twice, including "the served page is byte-identical to what the repo built"
(`0f2a9c2fdf5b3db6`, then the 59,476 B rebuild) and the report PDF served as `application/pdf`.

**The brief became a script.** `tools/check_visual_language.py` reads the surfaces that carry the theme
and fails on what the brief forbids: a non-zero `border-radius` (softness is not in this language —
the only allowed radius is `50%` on a round object, while the signature is a `clip-path: polygon()`
that cuts the **top-right and bottom-right** corners of presenting surfaces via `--cut/--cut-sm`;
list rows, tickets and anything that scrolls stay square),
a dark ground that is not `#000000` or a light ground that is not `#ffffff`, a radius in a chamfered
box, and anything frosted (`backdrop-filter`, `blur(`, `drop-shadow(`, `--glass-sheen`). Blue is
carved out by role rather than by eye: `--grad-cta` fills stay on the surfaces that *are* the selection
— tabs, the pinned card, the auth submit, the CTA — and the decorative wash behind the Detect banner is
gone. Three surfaces are checked: `frontend/src/styles.css` (2,541 lines), `ranking-view.ts`'s
component styles (112), and `PAGE_CSS` inside the site generator (145). It reports
`PASS: 0 problem(s)`.

**Two failure modes that only a parser can see.** Writing the guard was worth more than the polish,
because both of these passed every grep we had:

- A stray top-level `#` line — a comment written without `/* */` — is swallowed by CSS error recovery
  as the *prelude of the next block*. The next block was `:root{…}`, so the whole token file stayed in
  the stylesheet, every `var()` resolved from a fallback or not at all, and the page still looked
  plausible. `style_guards()` counts parsed blocks and fails when the token block is not applied.
- A backtick inside a CSS comment inside an Angular `styles: \`…\`` **closes the template literal**.
  The build dies with NG1002/NG2012 and the app stops compiling entirely. CSS comments in component
  styles are therefore backtick-free by rule, and the site generator now runs the same guard on the
  CSS it is about to write — it refuses to publish a stylesheet a parser would not apply.

The guard is not decorative either way: `verify_public_site.py` check 1b runs it against the **served
bytes**, and `verify_exe_bundle.py` runs it against the stylesheet the packaged app answers with, so a
broken sheet fails a deploy check rather than shipping.

**A JS-bundle grep cannot prove a CSS-only change.** While verifying the packaged exe, `--chamfer`
occurred **0×** in `main-DXYSVFUN.js` and **8×** in `styles-GZL5AEYN.css`: the Angular application
builder folds every component's `styles:` into the global sheet, so a whole design pass moves zero
bytes in the JS. The bundle check's needle grep would have failed a correctly shipped change. It now
searches both halves and reports which one carries it — the 31/31 run prints
`main 0 occurrence(s), stylesheet 8` — and `verify_container_deploy.py` gained the same pair of checks
(3e: the served sheet equals dist's, byte for byte; 3f: a parser reads it as written).

**The rebuild, and what it cost.** `ng build` exit 0 in 16.052 s; `ng test` **122 passed** across 8
files; `pytest` **164 passed**; `verify_deploy_recipe.py` **40/40**; `.localbuild` staging reported
`STAGE_MATCHES_DIST` (a `cp` merge is how a stale hashed bundle gets served); PyInstaller exit 0, and
`verify_exe_bundle.py --chamfer` **31/31**; the installer compiled in 23.781 s (45,125,975 B).

Two self-inflicted incidents belong in the record. First: `docker build --target build .` failed with
`open Dockerfile: no such file or directory` because the Dockerfile lives at `frontend/Dockerfile` and
I omitted `-f`; the chained `echo` in the same command made the completion notification read
`exit=0`, which is `tail`'s exit, not the build's. Second: chaining that build with a second
containerised `ng test` **crashed the WSL VM** (`rpc error: code = Unavailable desc = error reading
from server: EOF`); the three containers auto-restarted healthy inside a minute, but n8n re-registers
its webhook roughly 20 minutes after its own boot, so live detects through `:8080` degrade in that
window. The lesson is not "avoid Docker" — it is one containerised build per invocation, and read
`FreeVirtualMemory` before believing a build died for logic reasons.

Then host→`127.0.0.1:8080` returned `RemoteDisconnected` on four attempts, including `curl`, while
`docker ps` reported the container healthy and its own healthcheck passing — the Windows
ephemeral-port/loopback trap from earlier rounds, where a client dialing a port can be *assigned* that
port and get a refusal that sticks. `python tools/verify_container_deploy.py` from a fresh process
answered every route fine. The running container was meanwhile read from inside itself
(`docker exec … wget -qO- http://127.0.0.1/`), which is how the stale pair
`main-NPBOP6NA.js` / `styles-LH6W3N7R.css` (built 00:39) was caught serving a page 75 minutes older
than `dist` (01:54) — a container that says "Up 9 hours (healthy)" is not evidence of what it serves.

After the rebuild, the web container serves `main-DXYSVFUN.js` / `styles-GZL5AEYN.css` and
`verify_container_deploy.py` passes on the shipped lane, with the two new stylesheet checks green.

**What the design pass did not do.** It did not touch the calibrated bands, the prompt, the parser, or
anything a verdict depends on — the pages changed are CSS, one component's inline styles, and the site
generator. It also did not rewrite the dated Round 25–27 entries that record the old glass measurements
in `check_palette_contrast.py` output and prose: those are history, and the guard now prevents the
glass returning rather than pretending it never shipped.

**What remains.** Nothing on the artifact side — repo, unit suites, preflight, public site, packaged
exe, installer and the running container all carry the change. Open from before this round and
unchanged by it: the shipped n8n workflow still labels gateway failures `no_choices`, and
`SINGLE_MODEL_SECONDARY_AI` / the stalled NVIDIA pool still mean the tie-break lane cannot be captured
right now.

## Round 30: "public" meant a research repository, so the tables became a deposit (2026-10-18)

Round 29 published the ranking as a web page. The brief was met literally and missed the intent: *"when
i said public i meant in a research site like reseach gate or maybe zenedo"*. A host that serves bytes
makes the report **reachable**; a repository that mints a DOI makes it **citable**, and only the second
one survives the site. So this round moved the artifact into `research-zenodo/` and made it a deposit —
and in doing so found a false statement in the repo's own runbook.

**The runbook was wrong about its own history.** `research-zenodo/zenodo-deposit.md` opened with
"This is version 2 … publish it as a **new version of the same deposit**". No such deposit exists. It
was checked against Zenodo's public search rather than against memory:

    GET /api/records?q="Free-Tier LLM Gateways as AI-Text Detectors"   -> {"hits": {"total": 0}}
    GET /api/records?q="Quota Regimes, Availability Churn"             -> {"hits": {"total": 0}}

and the only `zenodo.org` strings anywhere in the tree are a made-up record id in a front-end unit
spec. "Version 2" was the *text's* version (Part I, then Part I + II) leaking into the *record's*
version field. The file now says **first upload**, keeps `v1` as the version label, and explains when
"New version" does become correct (after the DOI exists, when §8 is rewritten to name it).

**The vocabulary came from live records, not from the docs.** Zenodo's documentation page renders
`license: "cc-by"` and `publication_type: "preprint"`; reading actual 2026 preprints through
`/api/records?type=publication&subtype=preprint` gives `license: {"id": "cc-by-4.0"}` and
`resource_type: {"type": "publication", "subtype": "preprint"}` — `cc-by` is a *different* licence, so
the shorthand in the runbook would have mislicensed the deposit. `GET /api/deposit/depositions` answers
**403 without a token**, which is how the deposit endpoint was confirmed live without an account.

**A report with numbers and no data is a request to the author.** §8 promises "append-only per-row
JSONL … with a run manifest" and a "reproduction checklist available on request". Attached to a DOI
those stop being a favour. `tools/zenodo_package.py` writes `research-zenodo/deposit/`:

| File | Rows | Source |
| --- | --- | --- |
| `model-ranking.csv` | 48 | `GET /ranking`, in route order — the same call the app and the public page use |
| `per-text-scores.csv` | 3,937 | `benchmark_results` joined to `benchmark_runs`, opened `mode=ro` |
| `runs-manifest.csv` | 144 | every run's generation parameters (`temperature`, `top_p`, `max_tokens`, `concurrency`, endpoint) |
| `README.md` | — | column dictionary, plus the four-step recipe to re-derive any figure |

No corpus text is exported: `text_id`, `label`, `answered`, `error_class`, `ai_probability`,
`confidence` only. The corpus is third-party HC3 data and the report's non-redistribution claim holds.

**The check that makes it citable is the re-score, not the row count.** `--check` filters the per-text
file to a run, applies *that model's own exported band and confidence gate*, and requires the ranking
file's `correct` / `wrong` / `decided` / `accuracy_on_decided` to fall out of it. **48/48 rows
reproduce exactly** — so a reader who follows the README lands on the numbers the report prints, and
the two files cannot silently describe different measurements. Row counts against the live tables
(3,937 / 144) and route-order equality are separate checks. 7/7 pass, twice, and the generator opens
the database read-only so an accidental write is impossible rather than merely unplanned.

**Two refusals, both earned.** Loopback masking: two models were captured through a gateway on this
machine, so `endpoint` prints *local gateway (measuring machine)*, and a byte-level scan over every
written file fails the run if any secret-shaped (`sk-`, `Bearer`, `api_key=`), loopback-shaped
(`127.0.0.1`, `localhost`) or local-path-shaped string survives — the scan runs against what was
written, not against a field list, so a new column cannot smuggle one through. And `metadata.json` is
refused without `--author`: a DOI mints a citation, the report carries no byline, and nothing in the
repo is authoritative for the name, so it is the one field that has to come from the user. The builder
was proved against a throwaway directory (`upload_type`/`publication_type`, `cc-by-4.0`, `language:
eng`, 8 keywords, both HC3 DOIs as `references`, a description that now names the three CSVs),
together with its two refusal paths — empty author, and a malformed ORCID.

**What this round did not do.** It published nothing: the deposit is a directory of files and a
runbook, and the account, the name and the publish click are the user's. The Qoder Sites page from
Round 29 is still live and untouched (taking it down is also the user's call). pytest, `ng test` and
`ng build` were not re-run, because nothing in the backend, the workflow or the bundle changed — the
diff is one new tool, three generated CSVs, a column dictionary, a corrected runbook and this entry.

