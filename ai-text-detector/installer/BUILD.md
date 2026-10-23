# Building the local Windows app

Two artifacts are produced from the repo root, in this order:

1. a single-file server `AI-Text-Detector.exe` (PyInstaller), and
2. the installer `AI-Text-Detector-Setup-1.0.0.exe` (Inno Setup).

Everything below runs from the `ai-text-detector/` directory.

## 0. Prerequisite — build the web UI first

The exe bundles the compiled Angular app **and the Python backend**, so a UI or backend fix is not in
a packaged build until you re-stage and re-run step 2. Concretely: the exe built at 09:17 on
2026-10-16 shipped the BYOK form no user could submit and the History row that drew a 0 % ring for a
missing score, both of which were fixed later that day — a stale exe is not an old UI, it is a UI with
known bugs in it. If you changed `frontend/` or `backend/`, rebuild:

```bash
cd frontend && npm ci && npm run build -- --configuration production && cd ..
```

Output lands in `frontend/dist/frontend/browser/`. The container path does not have this trap:
`frontend/Dockerfile` builds the UI inside the image, so `docker compose up -d --build` always ships
the current source.

## 1. Stage a minimal data folder

`benchmark/` is ~26 MB and includes `node_modules/` and 17 MB of `book-sources/`
that the app never reads. Copy only the files the seed actually uses so the exe
stays small and ships no build junk:

```bash
rm -rf .localbuild
mkdir -p .localbuild/benchmark .localbuild/frontend/dist/frontend/browser
cp benchmark/results-*.jsonl benchmark/runs-manifest.log \
   benchmark/catalog-probe-nvidia.json benchmark/catalog-probe-openrouter.json \
   benchmark/corpus*.json .localbuild/benchmark/
cp -r frontend/dist/frontend/browser/* .localbuild/frontend/dist/frontend/browser/
```

`-r` is not optional. The UI ships two subdirectories — `research/`, the technical report the Ranking
page links (see `README.md`), and `fonts/`, the one self-hosted serif the stylesheet names — Literata,
written by `tools/fetch_fonts.py` (a CDN `<link>` is not an option for an app that
must boot with no network) — and without it `cp` prints `omitting directory` and exits non-zero, so the
stage simply has no report and no serif in it. Hit on 2026-10-17 while staging for the report. The loud
part is worth noting: the chain stops at the `&&`, and the `diff` below would also fail, because it
lists `research` on one side and not the other. What no staging check can see is a *stale* file inside
those folders — the bundle hash is the SPA's, not the asset's — which is why `verify_exe_bundle.py`
compares the served report bytes against the repo copy rather than trusting the name. A missing font,
unlike a missing report, shows up as a page that renders in the fallback serif and nothing else, so the
browser pass over the packaged app has to look at the type, not only at the HTTP status. The one face
shipped since 2026-10-19 is `Literata-var.woff2` (`tools/fetch_fonts.py` writes it and
`tools/check_font_metrics.py` measures it); the four static Spectral weights and the Cormorant display
cut it replaced are gone, so `fonts/` has exactly one file in it.

The staged `benchmark/` files carry only masked key *hints* (`api_key=nvapi-...(len 70)`),
never real keys. A strict scan for `nvapi-…{6}`, `sk-…{20}`, `AIza…{20}` returns nothing.

**Re-staging the UI for a second build: keep the `rm -rf`.** The `cp` above merges, and Angular's
bundles are content-hashed, so skipping it leaves yesterday's `main-XXXXXXXX.js` and
`styles-YYYYYYYY.css` sitting next to the new ones. `index.html` only references the new pair, so
the app still looks current — but PyInstaller embeds both, and the "did my change ship?" grep now
finds the string in a bundle nobody loads. Proved on this host: after a rebuild with the scale-note
text, staging held `main-KP47RF75.js` alongside `main-N42PPJ7G.js`. Check with

```bash
diff <(ls frontend/dist/frontend/browser) <(ls .localbuild/frontend/dist/frontend/browser) \
  && echo STAGE_MATCHES_DIST
```

and confirm the shipped file by hash, not by name: fetch `/<hash>.js` from the running exe and
compare `sha256sum` against `frontend/dist/frontend/browser/<hash>.js`.

## 2. Build the exe (PyInstaller)

`run_local.py` is the entry point; it just calls `backend.serve_local.main()`.

```bash
python -m PyInstaller --noconfirm --clean --onefile --name "AI-Text-Detector" --console \
  --paths "." \
  --icon "installer/app-icon.ico" \
  --distpath .localbuild/out --workpath build_local --specpath . \
  --add-data ".localbuild/benchmark;benchmark" \
  --add-data ".localbuild/frontend/dist/frontend/browser;frontend/dist/frontend/browser" \
  --collect-submodules backend \
  --hidden-import werkzeug.security \
  --exclude-module scipy --exclude-module numpy --exclude-module pandas \
  --exclude-module matplotlib --exclude-module sklearn --exclude-module torch \
  --exclude-module IPython --exclude-module pytest \
  run_local.py
```

Result: `.localbuild/out/AI-Text-Detector.exe` (one file, ~tens of MB).

Three gotchas that each cost a build if you hit them:

- Keep `--specpath .` (the repo root). If you point `--specpath` at `.localbuild`,
  PyInstaller resolves the `--add-data` sources relative to the spec and looks for
  `.localbuild/.localbuild/benchmark` — "Unable to find ... when adding binary and
  data files".
- The `--exclude-module` flags are **required**. The backend only imports
  flask/cryptography/pypdf, but this Python environment also has scipy/numpy/pandas
  installed, and PyInstaller walks their hooks anyway — without the excludes the
  build stalls for minutes and the exe balloons to hundreds of MB. The app never
  uses them, so cutting them is safe.
- Windows `--add-data` uses `;` to separate source from destination (`src;dest`);
  the `dest` is the folder name *inside* the frozen app that
  `serve_local.resource_dir()`/`spa_dir()` expect: `benchmark` and
  `frontend/dist/frontend/browser`.
- `--icon installer/app-icon.ico` is not decorative and it is not optional if the installer
  is to look like the product: that file is written by `python tools/make_icon.py` (which also
  writes both favicons from the same `GEOMETRY`, and `--check` fails if the three disagree), and
  PyInstaller resolves it **relative to the spec directory**, not to your shell — so build from
  the repo root, or the build falls back to the default Python snake icon without saying so. The
  same file is the `SetupIconFile` in the `.iss`, and there it resolves against `SourceDir=..`,
  so it is spelled `installer\app-icon.ico` in both places.
- **The product is Litmus; the filenames are not.** `MyAppName`, the Start-Menu entry, the
  uninstall display name and the console banner all say "Litmus". `AI-Text-Detector.exe`,
  `MySetupBase` and `%LOCALAPPDATA%\AITextDetector` deliberately keep the old spelling: changing
  the binary or the data folder would leave existing installs pointing at a name that no longer
  ships and a history directory nothing reads. The `AppId` GUID is likewise frozen — a new one
  side-installs instead of upgrading.

How to run it for a quick check (this also opens a browser tab):

```bash
.localbuild/out/AI-Text-Detector.exe
# then, in another shell:  curl http://127.0.0.1:8787/  (expect the SPA index.html)
```

Data is written to `%LOCALAPPDATA%\AITextDetector` (not the temp unpack dir), so
history persists across launches.

To verify a build without opening anyone's browser — which is what any automated check of a packaged
exe has to do — set `AITD_NO_BROWSER=1`. The server still prints its URL; only the tab is skipped:

```bash
AITD_NO_BROWSER=1 BACKEND_PORT=8822 .localbuild/out/AI-Text-Detector.exe
curl -s http://127.0.0.1:8822/ | grep -o 'main-[A-Z0-9]*\.js'   # must equal the file in frontend/dist
```

That grep is the check that matters: the bundle name is content-hashed, so seeing the current hash
proves the packaged app serves the current UI rather than an older staged copy.

## 2b. What a packaged install contains when there is no `.env`

The exe deliberately ships **no** `.env` (serve_local's docstring: no lane key ever enters the
binary). Everything the app needs to answer has to therefore be either baked into `backend/seed.py`
or entered by the user at runtime:

| What | Where it comes from with no `.env` |
|---|---|
| providers, ranking, catalogue, key guides | `seed.py` constants + the staged `benchmark/` files |
| primary + secondary lane models and their bands | `seed.DEFAULT_LANES` / `seed.DEFAULT_BAND` |
| API keys | the user's own key, added in the app (BYOK) — or none, see below |
| detection engine | this process: `backend/engine.py` scores the text in Python, no workflow server |

`DEFAULT_LANES`/`DEFAULT_BAND` are copies of the workflow's own defaults in
`n8n/generate-workflow.py`, and `backend/tests/test_seed.py::test_baked_defaults_match_the_workflows_own_defaults`
parses the generator and fails if the two disagree — so if you change a default there, the build
test tells you to change it here too. Before 2026-10-16 `seed_models` only inserted rows for models
named in env, so a fresh exe had providers and **zero** models and every detection answered
`409 NO_PRIMARY_MODEL`.

**Which engine, and why the exe stopped needing n8n (2026-10-19).** `serve_local` sets
`LOCAL_SINGLE_USER=1` and binds `127.0.0.1`, so `engine.native_enabled()` is true and `/detect`
is answered inside the Python process. Before this the packaged app POSTed to
`http://localhost:5678/webhook/ai-detect-ai`, which meant the exe that "just runs the detector"
also required the user to start a workflow server — and a machine without one recorded a 502 for
every text. `n8n_url`/`webhook_path` are still stored settings (a hosted container needs them) but
they are not read on the local path, and `/gateway-health` answers `detection_engine:
"python-native"` with `n8n.on_request_path: false` so the panel cannot show a red n8n beside a
detection that just worked. `DETECTION_ENGINE=n8n` puts a local install back on the workflow path
if you want to compare the two. What guarantees the swap is not a behaviour change is
`tools/engine_parity.py`: the shipped workflow's own Code nodes are executed against 61 recorded
cases and diffed field by field against `backend/engine.py` (61/61 identical).

**The "first boot only" caveat, measured on 2026-10-16.** Settings are seeded *when absent*, so a value
already in `%LOCALAPPDATA%\AITextDetector\detector.sqlite3` outranks whatever the launcher environment
says next time. That is why re-pointing an existing install means the admin Settings panel, not an env
var — but note it only ever applied to `n8n_url`, which the local engine no longer reads.

**A keyless install used to detect, and now does not (changed 2026-10-19).** Through the workflow, a
freshly wiped exe answered two real detections in 1.8 s and 1.4 s with `api_keys` and `user_api_keys`
both empty, because the *n8n process* was started from a repo `.env` and its environment credential
paid for the lane. The in-process engine has no such borrowed credential: with no key stored it answers
400 `PROVIDER_KEY_MISSING`, whose hint names the Configuration page, and that 400 deliberately does not
start the model-failover walk (another model would answer the same way). This is the honest version of
the same fact — the exe never had a key, it was borrowing one from a server it no longer needs. A
`PRIMARY_API_KEY` in the launcher environment still works, and keyless remains legal for a self-hosted
loopback gateway, which needs no credential.

What a keyless install could *never* do is use the Ranking tab's "score with this model" pick — those
cards need a key for their provider, so on first boot the page shows zero pinnable cards. That is the
difference between "the exe can't detect" (it can, once it is paid for) and "the exe can't let a user
choose the scorer" (true, and the reason the ranking page says "store an API key to unlock scoring").

The remaining external dependency is the gateway itself: a local install needs an API key and network
reach to `integrate.api.nvidia.com` (or whichever provider the row names), and nothing else. Without
them the app opens, browses, ranks and records history; each detection ends as a recorded 400 naming
the missing credential — never as a score.

## 3. Build the installer (Inno Setup)

> **Correction (2026-10-16):** this section used to say step 3 could not run here because
> `ISCC.exe` was "absent — searched under Program Files and Program Files (x86)". That search was
> the whole problem: the Inno Setup on this host was installed by winget (`JRSoftware.InnoSetup`)
> **per-user**, which puts it under `%LOCALAPPDATA%\Programs`, outside both of those trees. It is
> present and it compiles.

```bat
"C:\Users\adam\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer\AI-Text-Detector.iss
```

Run it from the repo root. Two traps, both hit in the round that found the compiler:

* **From Git Bash, do not pass slash switches.** `ISCC.exe /Q installer\…` does not quiet the
  build — the MSYS path layer turns `/Q` into a second filename and Inno answers
  "You may not specify more than one script filename". Use no switches and forward-slash paths.
* **Relative `[Files] Source` paths are relative to the `.iss`, not to your working directory.**
  The first compile of the day aborted on line 67 looking for
  `installer\.localbuild\out\AI-Text-Detector.exe`, because the script was written against the repo
  root. `[Setup] SourceDir=..` re-bases them. `OutputDir` turns out to be relative to `SourceDir`
  as well — with `OutputDir=Output` the installer appeared in the *repo root*, not in `installer\` —
  so it is spelled `installer\Output` and is therefore repo-root-relative like everything else.
* **`OutputBaseFilename` must not be derived from `MyAppName`.** `{#MyAppName}` is "AI Text
  Detector" with spaces, and the first build emitted `AI Text Detector-Setup-1.0.0.exe`. The script
  now has a separate `MySetupBase` define so the friendly name keeps its spaces and the file does
  not.
* `ArchitecturesInstallIn64BitMode=x64` compiles but warns ("identifier x64 is deprecated,
  substituting x64os") on every run; both architecture directives now say `x64compatible`.

Compiled clean on 2026-10-16 in 18.2 s with no warnings: `installer\Output\AI-Text-Detector-Setup-1.0.0.exe`,
44,641,939 bytes carrying the 43,226,239-byte onefile exe. Rebuilt the same evening after the tie-break
lane started shipping off: 27.06 s, no warnings, 44,655,417 bytes around the 43,238,512-byte onefile.
And again at 23:41 after `DEFAULT_API_KEY_GUIDES` was rewritten with the sign-in-first wording: 11.4 s,
no warnings, 44,659,459 bytes around the 43,242,453-byte onefile.

Round 26's ceiling raise is the same kind of change: `LANE_TIMEOUT_MS` is backend-seeded text, so the
exe frozen inside the setup package is the only copy a local-app install ever boots. The onefile was
rebuilt (backend-only, so no UI re-staging — `STAGE_MATCHES_DIST` confirmed first) and `verify_exe_bundle.py`
passed 22/22 including its virgin-install phase, which reads the ceiling out of `seed.LANE_TIMEOUT_MS`
and compares it to what the binary actually wrote:

```
PASS  with the 240 s ceiling the capture actually supports
      [timeout_ms=240000 (backend waits 270 s)]
```

The report asset is the other kind of change: it is *staged*, so it only reaches a user if step 0 and
step 1 both ran. Re-staged and rebuilt on 2026-10-17 at 15:51 (UI change in `ranking-view.ts`, so a
full re-stage, done from `rm -rf` with `cp -r`), verified 24/24, then compiled at 15:53: **41.656 s,
no warnings, 44,941,122 bytes around the 43,522,151-byte onefile**. The two new checks in that run:

```
PASS  the packaged exe serves the technical report the Ranking page links
      [200 53601 bytes identical_to_deposit=True]
PASS  …and the printable copy as a PDF  [200 471173 bytes magic=b'%PDF-']
```

The 2026-10-19 build is the one where the local app stopped needing a workflow server at all:
`backend/engine.py` scores the text inside the Flask process whenever the install is a local
single-user one, so the `.exe` on a laptop detects with n8n absent. That is not a claim a timestamp
can carry, so the runbook records what proved it. Step 1 was re-staged from `rm -rf` anyway
(`STAGE_MATCHES_DIST`, 6 entries, one hashed JS/CSS pair), step 2 wrote a 43,759,494-byte onefile,
and `verify_exe_bundle.py` — rewritten for this — passed **43/43**:

```
PASS  packaged backend answers /detect from its own process, and says so
      [200 detection_engine='python-native' n8n.on_request_path=False]
PASS  …and a real detect through the packaged binary returns a verdict it built itself
      [200 AI_GENERATED method=SINGLE_MODEL_PRIMARY_AI engine='python-native' p=0.93]
PASS  …and nothing at all was ever sent to a workflow server
      [connections the n8n_url listener accepted=0 bodies=0]
PASS  …and DETECTION_ENGINE=n8n puts this same binary back on the workflow
      [200 detection_engine='n8n' on_request_path=True]
```

The last two are a pair, and reading only one of them mis-reads the other. "The listener saw nothing"
is indistinguishable from "the listener was broken", so the same binary is booted a second time with
`DETECTION_ENGINE=n8n` and that boot *does* record the call — which turns the silent listener into
evidence instead of an outage. Nothing in this run reaches a provider, because the side that answers
is a loopback stand-in gateway the verifier owns; a stalled pool therefore cannot make the check red,
and no free-tier budget is spent to prove a dispatch rule.

`method` is `SINGLE_MODEL_PRIMARY_AI`, **not** `PRIMARY_DECIDED_AI`, and a first pass that asserts the
latter goes red on a correct build. `PRIMARY_DECIDED_*` is only reachable when both lanes return a
usable answer (`backend/engine.py:1056`); the shipped install runs one lane, so the one-lane branch at
`backend/engine.py:1079` decides and `backend/engine.py:1091` appends the verdict suffix. Same tree,
backend suite: **224 passed**.

Compiled at 16:53 the same day: **24.297 s, no warnings, 45,175,683 bytes** around the
43,759,494-byte onefile.

The 2026-10-20 run is the second report-asset rebuild, and the same trap in reverse: the report's §8 and
header had two claims the minted DOI made false ("available on request", "version 2"), so the fix could
not reach a user until steps 0→3 ran again. Step 0 took 10.5 s; step 1 started from `rm -rf` and reported
`STAGE_MATCHES_DIST` over 7 top-level entries with `research/` (2 files) and `fonts/` (2 files) inside;
step 2 wrote a **43,632,696-byte** onefile and `verify_exe_bundle.py` passed **41/41**, including

```
PASS  the packaged exe serves the technical report the Ranking page links  [200 54625 bytes identical_to_deposit=True]
PASS  …and the printable copy as a PDF  [200 476570 bytes magic=b'%PDF-']
PASS  …and ships no fake paper link  [paper_url=None (seed.py:621 leaves it to the author)]
```

The third line is the point of doing this after setting `paper_url`: the DOI belongs to the *running*
install's settings table, never to the frozen seed, so a build that baked it would put a citation
somebody else's account can retire into every fresh copy. Step 3 compiled in **23.813 s, no warnings,
45,014,386 bytes**. On the hosted side the same bytes went out as `docker-compose build web` +
`up -d --no-deps --force-recreate web` (backend and n8n left alone at 21 h and 26 h) and
`verify_container_deploy.py` passed **20/20** — report byte-equal through nginx, plus two real texts
scored on the shipped lane (128.1 s `HUMAN_LIKELY` p=0.2, 122.1 s `AI_GENERATED` p=0.8, 3 phrases each).

The 2026-10-20 evening run is the first rebuild whose subject is a **data migration** (#206: a
`probe_status` written by an older build as `timeout` left the model with no `models` row at all, so
no picker could list it). A migration is not greppable in a bundle, which is why the runbook records
what was measured instead of the timestamp:

- Step 0: `ng build` rc=0 in 12.398 s → `main-6J66ALWS.js` (349,374 B); `ng test` **163 passed / 10 files**.
  Step 1 from `rm -rf`: `STAGE_MATCHES_DIST`, 11 entries, secret scan clean.
- Step 2: PyInstaller rc=0 in 119 s → **43,657,322-byte** onefile.
- `verify_exe_bundle.py` was extended first and run against the **stale** exe: **39/47**, the eight reds
  being exactly the new checks (no `/my/pool/options` JSON at all — the route did not exist in that build).
  Rebuilt, the same run went **48/48**: `probe_vocab_version=2`, the planted legacy `timeout` row re-read
  as `slow` with a `models` row at `timeout_ms=240000`, the planted 200-with-no-score row re-read as `dead`
  with **no** row, and the picker answering 200 / 38 entries with the asleep id present, `measured=False`.
- Step 3: **21.812 s, no warnings, 45,036,724 bytes** (`sha256 e17c44ded976414e…`). Proven as a payload
  without installing anything: `/PORTABLE=1 /SILENT /NOICONS /SUPPRESSMSGBOXES /DIR=<temp>` extracted a
  43,657,322-byte exe whose sha256 equals the verified onefile's (`ba46cbd229a87b51…`).
- Hosted: one `docker-compose build` per invocation (backend, then web — chaining them has crashed the
  WSL VM on this host), then `up -d --no-deps --force-recreate` for each service only, so n8n stayed at
  34 h and the `backend_data` volume was never touched. `verify_container_deploy.py` **19/19**.

The container's own boot log is the evidence the migration reached the operator's real database, because
**`serve_local` does not print the seed summary — only `docker/backend-entrypoint.sh` does.** Hosted it
said `probe_vocab_reclassified: 96, probe_vocab_rows_added: 26` with `models` at 28 and users/keys/
detections/runs unchanged (5/11/47/144). For the local install, read it back over the API instead:
`/api/gateway-health` showed `db_path` = `%LOCALAPPDATA%\AITextDetector\detector.sqlite3` and
`db_counts.models` 4 → **28**, and `/api/my/pool/options` answered 27 entries of which **11** are
unmeasured `slow`/`busy` rows at the 240 s ceiling — the models that used to be missing — with zero
legacy words left. (`/api/` is the prefix on the packaged app too; a bare `/my/pool/options` answers the
SPA `index.html` with 200.)

**Read the tool's arguments before writing them down.** This section used to say "pass the report path
as argument 1 … pass argument 2 to check a different string". `verify_exe_bundle.py:747` has exactly one
argument — the string to grep the served bundle for, defaulting to `tie-break lane` — and the report
path is a fixed part of the check set now. `MSYS_NO_PATHCONV=1` is still required when that one argument
starts with `/`, because Git Bash rewrites it into `C:/Program Files/Git/…` before Python sees it.

**Relaunch the exe with its inherited environment, not a hand-made one.** PyInstaller's onefile parent
unpacks and then re-execs itself, and the app resolves its data folder from `LOCALAPPDATA` — a launch
that passes only the variables you thought of (`AITD_NO_BROWSER`) can boot against a different, empty
database than the one the user's history and settings live in, which reads as "the rebuild lost my
paper link". The safe form on this host is a PowerShell wrapper that sets the one variable and calls
`Start-Process`, so the child inherits everything else:
`$env:AITD_NO_BROWSER='1'; Start-Process -FilePath '.localbuild/out/AI-Text-Detector.exe' -WindowStyle Hidden`.
Confirm by reading `GET /api/config` back: the DOI still there means the right database was opened.

Pass the string the change under test introduced as argument 1 to make it the needle the *served
bundle* is grepped for, which proves the exe ships that change rather than an older staged copy:

```bash
MSYS_NO_PATHCONV=1 python tools/verify_exe_bundle.py "accept-offer"
```

The `MSYS_NO_PATHCONV=1` is required from Git Bash, not cosmetic: when the argument looks like a path
the shell rewrites it (a leading `/` becomes `C:/Program Files/Git/…`) before Python ever sees it, and
the run then reports `FAIL … contains '<rewritten string>' [0 occurrence(s)]` — a broken check, not a
broken build. Measured on this exact command on 2026-10-17; the other 23 checks, including both report
byte-equality ones, passed in the same failing run. A CSS-only change needs the needle checked in
`styles-*.css` as well, because Angular folds component `styles:` out of `main-*.js` entirely.

**Why the guide wording had to reach a build at all:** the cards are backend-seeded text, so the only
copy a local-app user ever reads is the one frozen inside the exe. Editing `backend/seed.py` changes
the dev tree and nothing else until steps 1-3 run again — which is the same trap as step 0 for the UI.

Output: `installer\Output\AI-Text-Detector-Setup-1.0.0.exe`.

The installer reads `.localbuild\out\AI-Text-Detector.exe`, so build the exe first — and rebuild it
after any UI change, because it packages the staged bundle, not `frontend/dist`. `installer/` is a
snapshot, so a stale exe ships a stale UI with no error anywhere. Prove the package instead of
trusting the timestamp:

```bash
python tools/verify_exe_bundle.py            # serves /, hashes the bundle, greps the change
```

It boots the packaged exe on its own port against a throwaway copy of the dev database and asserts
that the served `index.html` names the same `main-*.js` as `frontend/dist`, that the bytes hash to
the same digest, and that a string from the change under test is actually in the downloaded file
(argument 1 sets that string; it also compares the served `styles-*.css`, the report, the font and the
favicons byte for byte, and proves the migration above re-read the legacy probe rows).

**Read backend-seeded text from the database file, not from HTTP.** The clean-install phase asserts the
shipped API-key guides and the absent `paper_url` by opening the virgin `detector.sqlite3`, because a
GET `/config` on the packaged app returns the SPA `index.html` with status 200 (the API lives under
`/api/`, and `/api/config` answers 401 `AUTH_REQUIRED` until you log in). Measured 2026-10-16: the
first version of that check parsed the served HTML as JSON and so reported 0 guides and a `None` paper
link — a green-looking run that had checked nothing, caught only because the counts refused to match.
Reading the seeded row is what proves the frozen `seed.py` carries the sign-in-first wording.

Then it asserts the two things no hash can show, because a copy of a developer's database is not a
new install. The stand-in engine now keeps the JSON body the packaged backend sent, so the run proves
the request itself names one lane and says `secondary: {"enabled": false}`; and a second boot with a
database file that does not yet exist reads back what the binary seeds on a user's very first launch
(one primary on the promoted model and its measured band, `timeout_ms` 240000, the tie-break row
present with `enabled = 0`). The upgrade copy proves `apply_lane_defaults()` switches an
already-on row off exactly once and writes `lane_defaults_version`; the virgin boot proves the
first-insert path, which the upsert never touches.

Two consequences of the boot re-seed worth knowing before debugging this tool: `models.timeout_ms`
is rewritten from `LANE_TIMEOUT_MS` on every start, so the only way to shorten the wait inside the
upgrade boot is to launch it with `LLM_TIMEOUT_MS=1000` (which is what keeps that phase at ~31 s
rather than 270 s), and the clean-install phase must *not* set it, or it would override the shipped
240 s ceiling it exists to prove. `SECONDARY_ENABLED` is left unset for the same reason.

It installs per-user by default (no UAC), creates Start-Menu + optional Desktop
shortcuts, and leaves `%LOCALAPPDATA%\AITextDetector` on uninstall.
