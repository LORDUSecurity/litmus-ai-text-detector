# Angular Detection UI — Design Spec

**Date:** 2026-10-02
**Status:** Approved in chat, sections 1 and 2. Awaiting written-spec review.
**Scope:** A new Angular frontend for the AI-only detection webhook, plus a per-request lane-config and BYOK extension to that webhook.

---

## 1. Problem

The AI-only detection workflow (`n8n/ai-only-workflow.json`, generated from `n8n/generate-workflow.py`) currently reads every provider setting — base URL, model, API key — from n8n's own process environment. Nothing in the HTTP request can change them. Changing the gateway port means editing `.env` and restarting n8n.

This spec adds a browser interface that lets the user pick provider, host, port and model per lane, submit text, and see the full verdict. It also adds bring-your-own-key so a user can supply their own provider credential.

## 2. Decisions already made

| Decision | Choice | Why |
|---|---|---|
| How config reaches n8n | Hybrid: host allowlist | Gives per-request port flexibility without making n8n an open proxy |
| Exposure | Localhost only | No auth, no CORS config, dev-server proxy |
| Model scope | UI picks both lanes | Preserves the calibrated two-lane ensemble |
| Model list source | Curated list + free-text combobox | `/v1/models` is unreliable on the free tiers in use |
| Result display | Full evidence view | Every field the workflow returns is visible |
| BYOK key handling | Disable n8n execution persistence | n8n writes run data to SQLite in plaintext |

## 3. The finding that drove the BYOK design

n8n 2.35.7 persists the full run data of every webhook execution — the incoming body and the resolved parameters of every node, including the `Authorization` header the HTTP Request node sent. Measured against the live database at `C:/Users/adam/n8n-runtime/.n8n-data/.n8n/database.sqlite`:

```
SELECT COUNT(*) FROM execution_data;                                    -> 85
SELECT COUNT(*) FROM execution_data WHERE data LIKE '%hc3-live-test%';  -> 66
SELECT COUNT(*) FROM execution_data WHERE data LIKE '%nvapi-%';         -> 74
SELECT storedAt, COUNT(*) FROM execution_entity GROUP BY storedAt;      -> db | 85
```

The project's NVIDIA key is already stored in plaintext in 74 rows. Any BYOK key posted to the webhook would be stored the same way. No JavaScript inside the workflow can prevent this, because a Code node's own output is part of the persisted run data.

The relevant n8n logic, `n8n/dist/execution-lifecycle/to-save-settings.js`:

```js
error:   saveDataErrorExecution   === 'DEFAULT' ? DEFAULTS.ERROR   : saveDataErrorExecution   === 'all',
success: saveDataSuccessExecution === 'DEFAULT' ? DEFAULTS.SUCCESS : saveDataSuccessExecution === 'all',
```

`DEFAULTS` come from `EXECUTIONS_DATA_SAVE_ON_ERROR` / `EXECUTIONS_DATA_SAVE_ON_SUCCESS`, both defaulting to `'all'` in `@n8n/config/dist/configs/executions.config.js:116-117`. Anything other than `'all'` disables saving, and **workflow-level settings take precedence over the env default**. So the protection is set in the workflow file itself and travels with it.

Transport encryption is not the issue here: traffic to `127.0.0.1` never leaves the machine. Persistence at rest is the issue.

## 4. Architecture

```
frontend/                     new Angular 22 app, standalone components, signals
  proxy.conf.json             /api  ->  http://localhost:5678/webhook
n8n/generate-workflow.py      modified; source of truth for the JSON
n8n/ai-only-workflow.json     regenerated
.env                          new ALLOWED_LLM_HOSTS, ALLOW_BYOK, save-data settings
benchmark/                    validation harness extended
```

Requests flow browser -> Angular dev-server proxy -> n8n webhook -> LLM gateway. The proxy exists so the browser makes same-origin requests and CORS never needs configuring.

## 5. Wire contract

The existing webhook gains two optional additions. With neither present, behaviour is unchanged from what was verified live on 2026-10-02 across all 48 benchmark texts.

```
POST /webhook/ai-detect-ai
x-primary-api-key:   <optional, BYOK>
x-secondary-api-key: <optional, BYOK>
Content-Type: application/json

{
  "text": "...",
  "lane_config": {
    "primary":   { "base_url": "http://localhost:20128/v1",
                   "model": "nvidia/nemotron-3-ultra-550b-a55b" },
    "secondary": { "base_url": "https://integrate.api.nvidia.com/v1",
                   "model": "nvidia/nemotron-3-super-120b-a12b" }
  }
}
```

Both keys inside `lane_config`, and both lanes themselves, are optional and independent. A lane with no `base_url` keeps the env value; a lane with no `model` keeps the env value; a lane with no key header keeps the env key.

The UI presents host and port as separate fields and composes `base_url` itself. The wire carries one URL, so the server validates exactly one thing.

Two key headers rather than one because lanes may point at different providers, and different providers need different credentials.

## 6. Server-side validation

All of it in the `Normalize Input` Code node, all rejecting with the existing HTTP 400 `INPUT_REJECTED` shape so the UI needs no new error path.

**`base_url`**
- Parsed with `new URL()` inside `try/catch`; a parse failure is a rejection.
- Protocol must be `http:` or `https:`.
- Hostname, lowercased, must exactly match an entry in `ALLOWED_LLM_HOSTS`. No wildcard, no suffix matching — `evil.com` must not match via `notopenrouter.ai`.
- Any port is permitted, 1-65535. Arbitrary ports are the point of the feature.
- Userinfo rejected: `http://user:pass@localhost/` fails.
- Non-empty query string or fragment rejected.

Default `ALLOWED_LLM_HOSTS=localhost,127.0.0.1,integrate.api.nvidia.com,openrouter.ai`.

**`model`**
- Must be a string, 1-200 characters.
- Must match `^[A-Za-z0-9._:/@+-]+$`. No whitespace, quotes, braces, backslashes or control characters.

**BYOK key**
- If `ALLOW_BYOK` is not exactly `true`, the presence of either key header is a hard 400 with `error_code: 'BYOK_DISABLED'`. Default is `false`.
- If enabled: must be 1-512 characters and match `^[A-Za-z0-9._:+/-]+$`.
- A lane with no key header falls back to that lane's env key.

**Never leaked**
- The key value is read from the header in `Normalize Input` and used only to build the `Authorization` header of the outbound request.
- Node outputs carry `api_key_present: true` and `key_source: 'byok' | 'env'`, never the value.
- The response contains no key material in any field, including `warnings`.

**Provenance in the response.** Each lane reports `config_source: 'request' | 'env'` and `key_source`, surfaced as a `config_used` object so it is always possible to see what actually ran. This matters because an unbenchmarked model pair makes the verdict indicative only.

## 7. Persistence disabled

In the generated workflow JSON:

```json
"settings": {
  "saveDataSuccessExecution": "none",
  "saveDataErrorExecution": "none",
  "saveManualExecutions": false,
  "saveExecutionProgress": false
}
```

Plus the matching env vars as defense in depth for any other workflow on the instance:

```
EXECUTIONS_DATA_SAVE_ON_SUCCESS=none
EXECUTIONS_DATA_SAVE_ON_ERROR=none
EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS=false
EXECUTIONS_DATA_SAVE_ON_PROGRESS=false
```

Consequence, stated plainly: the n8n execution-history UI will no longer show run data for this workflow. Debugging moves to the Angular result panel, the response body, and the n8n process log.

## 8. Residual risks

1. **Allowing `localhost` on any port** means anything that can reach the webhook can make n8n dial other local ports — `localhost:3306`, `localhost:5678` itself. With no auth and loopback-only binding, an attacker must already be on the machine. Accepted. An optional `ALLOWED_LLM_PORTS` can tighten this later; it is deliberately out of scope now.
2. **Client-side allowlist checking in Angular is cosmetic.** The server is authoritative. The UI warns but never blocks, so the two lists cannot drift into a false sense of safety.
3. **The key is in n8n process memory** for the duration of one execution. Disabling persistence stops it reaching disk; it does not make n8n a secrets manager.
4. **`ALLOW_BYOK=true` on a non-loopback n8n is unsafe.** The `.env` comment must say so, and the setting must default to `false`.
5. **Existing leak.** The 74 rows already containing the NVIDIA key are not fixed by any of the above. See section 13.

## 9. Angular structure

```
frontend/
  angular.json  package.json  tsconfig.json  proxy.conf.json
  src/
    main.ts  index.html  styles.css
    app/
      app.ts                      page shell; owns all form state as signals
      app.config.ts               provideHttpClient
      core/
        detection.types.ts        LaneConfig, DetectionResult, LaneEvidence
        detection.service.ts      detect(); the only place HttpClient is used
        providers.ts              curated provider and model catalogue
      ui/
        config-panel/             provider, host, port, model per lane, BYOK inputs
        text-panel/               textarea, char counter, sample loaders
        result-card/              verdict, probabilities, lanes, warnings
        raw-json/                 collapsible formatted response
```

`app.ts` holds the form state and the two BYOK keys as signals. The keys live nowhere else: not in `localStorage`, not in `sessionStorage`, not in a service, not in the URL. Non-secret UI preferences (last provider, last models, host, port) persist to `localStorage` from `config-panel`.

### Provider catalogue

`providers.ts` exports presets:

| id | label | host | port | scheme | path |
|---|---|---|---|---|---|
| `omniroute` | OmniRoute (local gateway) | `localhost` | `20128` | http | `/v1` |
| `nvidia` | NVIDIA NIM | `integrate.api.nvidia.com` | `443` | https | `/v1` |
| `openrouter` | OpenRouter | `openrouter.ai` | `443` | https | `/api/v1` |

Model presets carry the benchmark measurement where one exists, so the UI can label a choice with its evidence:

| model | note |
|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b` | benchmarked, AUC 0.934 |
| `nvidia/nemotron-3-super-120b-a12b` | benchmarked, AUC 0.654 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | unverified |

Selecting a provider fills host, port, scheme and path and swaps the model list. Every field stays editable, and the model input is a free-text combobox over a `<datalist>`, so any port and any model string the gateway accepts will work. A preset is a convenience, not a constraint.

When the selected pair is not the benchmarked 550B/120B combination, `result-card` shows a standing notice that the thresholds were calibrated for that pair and the verdict is indicative.

### Request path

`DetectionService.detect(text, laneConfig, keys)` posts to `/api/ai-detect-ai`, sets the two key headers only when a key is present, and returns a typed `DetectionResult`. Timeout 150s, because `hu-015` took 80s in the live benchmark run and the 550B lane is slow.

## 10. Error handling

Seven distinct states. They are kept separate because the failure modes have different causes and different fixes, and collapsing them is what makes a tool like this unusable to debug.

| State | Trigger | Presentation |
|---|---|---|
| Pre-flight | text empty, <10 or >50000 chars, model charset invalid | inline field error; no request sent |
| `INPUT_REJECTED` | HTTP 400 from the workflow — allowlist, charset, `BYOK_DISABLED` | red banner showing the server's `error` and `error_hint` verbatim |
| n8n-level | HTTP 422 malformed body | red banner: rejected by n8n before reaching the workflow |
| `ALL_MODELS_FAILED` | HTTP 200 with both lanes failed | amber, listing each lane's `error_code` and `error_detail` |
| `REVIEW_REQUIRED` | HTTP 200 | neutral grey, with the reason: gray zone, high disagreement, or low confidence |
| Unreachable | network error or status 0 | "cannot reach n8n at /api — is it running on :5678 and is the proxy configured?" |
| In flight | request outstanding | elapsed-seconds timer |

`ALL_MODELS_FAILED` is where a 401 from a bad BYOK key or a 404 from a wrong model id surfaces, with the provider's own message passed through. That is the most common real failure and it must be readable.

Client-side allowlist checking warns but never blocks. The server decides. The warning is computed against the host list in `providers.ts`, not against `ALLOWED_LLM_HOSTS` — the UI has no way to read n8n's environment, and adding an endpoint to expose it would only create a second list that can disagree with the real one. A host that is not a known preset therefore warns "not a recognised provider host; the server may reject this", and the server's 400 remains the only authoritative answer.

## 11. Testing

In descending order of value.

**1. Hostile-input table against the `Normalize Input` JavaScript.** The validation code is extracted from the generator and run under Node against a table of inputs, each with an expected accept/reject. This is the security boundary and gets the most coverage:

- `http://169.254.169.254/latest/meta-data/` — cloud metadata
- `http://localhost:5678/rest/settings` — n8n itself
- `http://localhost:3306/` — the database port
- `http://openrouter.ai.evil.com/v1` — suffix-match attempt
- `http://evil.com/v1`
- `http://user:pass@localhost/v1` — userinfo
- `javascript:alert(1)` and `file:///etc/passwd` — scheme
- `http://localhost:20128/v1?x=1` — query string
- `http://localhost:99999/v1` — port out of range
- model `x"} , {"role":"system` — JSON injection attempt
- model containing a newline, a backslash, a quote, a brace
- a 10 KB key, a key containing a space
- any BYOK header with `ALLOW_BYOK=false`
- the accept cases, which must still pass: the two URLs from section 5; `https://openrouter.ai/api/v1`; `http://127.0.0.1:20128/v1`; and a request with `lane_config` omitted entirely, which must fall back to the env values exactly as it does today

**2. Angular unit tests** with `HttpTestingController`: asserts the request URL, that both key headers are set when keys are present and omitted when absent, the `lane_config` shape, and — as its own explicit assertion — that no key value appears anywhere in the serialized request body.

**3. Live end-to-end.** `benchmark/live-webhook-test.js` is extended to send `lane_config`, then re-run over all 48 corpus texts to confirm the verdicts still match the 2026-10-02 baseline (accuracy 0.646, precision on decided calls 0.969, review rate 0.333, 1 false positive, 0 errors). Then a real BYOK call to OpenRouter with a key in the header, followed by re-querying the SQLite file to prove the key count did not increase. That query is the acceptance test for the BYOK requirement.

**4. Browser.** `ng serve`, then drive the real UI: golden path, each of the seven error states, and a check that the BYOK field is `type="password"` with `autocomplete="off"`.

## 12. Out of scope

- Authentication, multi-user support, TLS termination.
- Run history or persistence in the UI.
- Live `/v1/models` discovery.
- `ALLOWED_LLM_PORTS`.
- Any change to the detection prompt, thresholds, weights or gray zone. They stay at the values calibrated on 2026-09-29.
- Updates to `README.md`, `PROJECT_STRUCTURE.md` and `docs/api-keys.md`, which are already stale with respect to the AI-only workflow. Worth doing, separately.

## 13. Open item requiring explicit user permission

The 74 existing rows containing the NVIDIA key. This is a destructive write to the user's n8n database and will not be performed without a direct instruction. The choices:

1. `DELETE FROM execution_data WHERE data LIKE '%nvapi-%'` — removes the 74 rows, keeps other history.
2. `DELETE FROM execution_data` — removes all 85 rows.
3. Leave the database alone and **rotate the NVIDIA key** instead.

Rotation is the only option that is safe if the database file was ever copied, backed up or shared. Deleting rows does not zero the freed SQLite pages, so the key can remain recoverable from the raw file afterwards; a `VACUUM` is needed to rewrite it.

## 14. Verification bar

This spec is not satisfied by code that compiles. Done means:

- the hostile-input table passes, with the reject cases actually rejecting;
- `ng build` and the Angular unit tests pass;
- all 48 benchmark texts return the same verdicts through the UI contract as through the baseline;
- a real BYOK call succeeds against a real provider;
- the SQLite key count is unchanged after that call, shown as actual query output;
- the UI has been driven in a real browser, golden path and failure banners.

## 15. As built (2026-10-06) — two deviations from this spec, and one item that cannot be met

**§9 changed: the UI does not talk to n8n directly.** `proxy.conf.json` maps `/api` to
`http://127.0.0.1:8787`, the new `tools/detector-gateway.js`, which forwards to the webhook and
adds CORS. The gateway is load-bearing, not a convenience — see the next point. The config panel
reads `/api/gateway-health` and says out loud whether purging is active.

**§7 and §3 were wrong, and the user chose a different fix.** Disabling save-data settings does
not keep a BYOK key off disk. n8n writes the inbound webhook request, headers included, as part
of starting the execution, and `DISCARDABLE_DATA_MODES` excludes the `webhook` mode, so the
settings in §7 only suppress the node run data. Measured, not inferred. The chosen remedy is a
session-scoped purger: after each call the gateway deletes this workflow's execution rows, scoped
by `id > max(id) at gateway start` so pre-existing history is never touched, with
`PRAGMA foreign_keys=ON` so the `execution_data` cascade actually fires.

Evidence, re-queried from `database.sqlite` after roughly 72 live calls made through the gateway:

```
workflow rows now      = 85      (85 before the session started)
execution_data rows    = 85
rows containing nvapi- = 74      (the pre-existing rows, deliberately left alone)
```

and from `tools/verify-purge.js`, with a canary sent as both key headers: rows containing the
canary went 1 immediately after the call to 0 nine seconds later, while the legacy count stayed
74 and the total stayed 85.

**§14's "all 48 texts return the same verdicts" is not achievable as written.** The detector is
not reproducible run to run. The full 48-text run through the new contract gave accuracy 0.646
(identical to the baseline) with 0 errors, but precision 0.886 against 0.969, review rate 0.271
against 0.333, and 4 false positives against 1 — 11 texts changed category.

To find out whether `lane_config` caused that, the 12 texts that moved were run twice more, same
code path, once with no `lane_config` (env fallback) and once with it:

```
                 no lane_config      with lane_config
correct           3/12                4/12
wrong             hu-004 hu-013       hu-004 hu-010 hu-011
deferred          7                   5
```

The two runs disagree with each other on 8 of the 12, in both directions — `hu-004` is wrong
without the override and correct with it, `hu-015` the other way round. A behavioural change from
the request shape would not move texts back and forth. This is gray-zone jitter at temperature 0.1
on the 550B lane; `hu-004` is wrong in three of the four observations and is a hard case, not a
regression. Aggregate accuracy held.

**Not demonstrated:** a human typing a real key into the BYOK field and reading a verdict from
the page. The header path with a real key is proven at the contract level
(`n8n/live-contract-test.js` scenario 6: verdict produced, `key_source: header`, key absent from
the response), and the page's header behaviour is proven by unit tests, but the two were not
joined by a hand.

Anything not demonstrated is reported as not demonstrated.

### Result-card fields changed on 2026-10-06 (tie-break policy)

§8 described the payload as `component_scores.model_disagreement` and `weights_used`.
Those two are gone with the weighted blend. The shipped ensemble node now returns
`lane_gap` (informational), `decision_mode: "tie_break"`, `deciding_lane` and
`secondary_decision_threshold`, and the card reads the headline probability as the
deciding lane's own number rather than an average. `npm test` (28 tests, 3 files) and
`ng build` pass against the new types; the page was not re-driven in a browser
for this change.
