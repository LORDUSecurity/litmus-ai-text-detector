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
cp frontend/dist/frontend/browser/* .localbuild/frontend/dist/frontend/browser/
```

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
| detection engine | `N8N_URL` in the launcher environment **on first boot only** — see the caveat below, else `http://localhost:5678` |

`DEFAULT_LANES`/`DEFAULT_BAND` are copies of the workflow's own defaults in
`n8n/generate-workflow.py`, and `backend/tests/test_seed.py::test_baked_defaults_match_the_workflows_own_defaults`
parses the generator and fails if the two disagree — so if you change a default there, the build
test tells you to change it here too. Before 2026-10-16 `seed_models` only inserted rows for models
named in env, so a fresh exe had providers and **zero** models and every detection answered
`409 NO_PRIMARY_MODEL`.

**The "first boot only" caveat, measured on 2026-10-16.** Settings are seeded *when absent*, so a value
already in `%LOCALAPPDATA%\AITextDetector\detector.sqlite3` outranks whatever the launcher environment
says next time. `N8N_URL=http://127.0.0.1:5699` therefore works on a clean install and is silently
ignored on the second launch of the same install — which is right for a user who edited the URL in
Settings, but surprising if you are re-pointing the engine with an env var. To move an existing install,
edit `n8n_url` in the admin Settings panel or delete the seeded settings row; do not expect the
environment to win twice.

**No key inside the app does not mean no detection — measured on a clean first boot, 2026-10-16.** A
freshly wiped install (`api_keys` 0 rows, `user_api_keys` 0 rows) answered two real detections in 1.8 s
and 1.4 s against a live n8n, and the engine's own `config_used` reported `key_source: "env"`: the
workflow's environment credential pays for the lane when the app holds none. What a keyless install
*cannot* do is use the Ranking tab's "score with this model" pick — those cards need a key for their
provider, so on first boot the page shows zero pinnable cards. That is the difference between "the exe
can't detect" (false) and "the exe can't let a user choose the scorer" (true, and the reason the ranking
page owes a "store an API key to unlock scoring" hint).

The engine is the remaining external dependency: `/detect` forwards to n8n, which is not inside the
exe. A user either runs n8n on their own machine or sets `N8N_URL` (admin-editable in Settings as
`n8n_url`) to the hosted stack's. Without a reachable engine the app opens, browses, ranks and
records history, but each detection ends as a recorded 502 naming the URL it tried.

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
passed 22/22 with its virgin install reading back `timeout_ms=240000 (backend waits 270 s)`. The
setup package was then recompiled at 11:45: 27.64 s, no warnings, 44,662,980 bytes around the
43,246,440-byte onefile.

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
(pass argument 2 to check a different string).

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
