# Project Structure

Two detection stacks are in this tree. The **AI-only stack** (`n8n/`, `backend/`,
`frontend/`, `tools/`, `benchmark/`) is the one that runs today. The **legacy
multi-signal pipeline** (`detector/`, `database/`, `docker/`, `tests/`) is still
present but nothing in the running stack imports it.

```
ai-text-detector/
├── README.md                          # run commands, checks, and what the tuned parameters do
├── quickstart.sh                      # starts/stops n8n + Flask + Angular, idempotent
├── .env / .env.example                # gateway base URLs, keys, default thresholds
│
├── backend/                           # Flask + stdlib sqlite3 — the config store
│   ├── app.py                         # /detect /providers /keys /models /detections /benchmark/* /n8n/*
│   ├── db.py                          # schema + DETECTOR_DB path (backend/data/detector.sqlite3)
│   ├── seed.py                        # .env + benchmark/*.json -> SQLite rows
│   ├── purge.py                       # background thread: delete the n8n execution rows we create
│   ├── wsgi.py                        # gunicorn entry point
│   ├── data/                          # SQLite, backend-secret.key (Fernet), service logs
│   └── tests/test_backend.py          # routing, encryption, purger, tuning block (27 tests)
│
├── frontend/                          # Angular 22, standalone components + signals
│   └── src/app/
│       ├── core/                      # detection.service (HTTP + state), providers catalogue, types
│       ├── ui/                        # text-panel result-card config-panel admin-panel
│       │                              # benchmark-panel raw-json
│       └── app.spec.ts                # component-level tests over the shipped templates
│
├── n8n/                               # the workflow itself
│   ├── generate-workflow.py           # single source of truth -> ai-only-workflow.json
│   ├── ai-only-workflow.json          # 14 nodes: webhook, validate, 2 lanes, tie-break, respond
│   ├── test-normalize.js              # 59 hostile-payload cases against the shipped Code node
│   ├── test-ensemble.js               # tie-break decision table replay
│   ├── live-contract-test.js          # one real request through the live webhook
│   └── main-workflow.json             # legacy 11-stage pipeline (needs Postgres + detector/)
│
├── tools/
│   ├── start-n8n.js                   # run n8n with .env exported (N8N_BIN / N8N_USER_FOLDER override)
│   ├── tuning-echo.js                 # loopback OpenAI-compatible server, records what left the box
│   ├── verify_tuning_live.py          # stored parameters reach the workflow (real providers)
│   ├── verify_tuning_echo.py          # ...and appear verbatim in the outbound request
│   ├── verify-purge.js                # execution-row purge proof
│   └── detector-gateway.js            # superseded Node proxy, kept as a no-Python fallback
│
├── benchmark/                         # corpora, harness, and every measurement artefact
│   ├── corpus.json                    # 48 HC3 texts (24 AI, 24 human)
│   ├── corpus-books.json              # 46 real Project Gutenberg excerpts
│   ├── run-benchmark.js               # prompts + parses exactly like the workflow
│   ├── analyze.js / analyze-free-screen.js
│   └── results-*.jsonl                # one row per measured text, written by the harness
│
└── docs/
    ├── calibration.md                 # how the shipped thresholds were chosen, and their live error rate
    ├── free-model-research.md         # measured ranking of free models
    ├── legacy-multisignal-pipeline.md # the retired Docker/Postgres design, moved out of the README
    ├── api-keys.md, rate-limiting.md  # legacy-pipeline operational notes, each banner-marked
    └── production-deployment.md       # legacy Docker/Postgres deployment
```

## Legacy multi-signal pipeline (not part of the running stack)

```
detector/          # statistical feature extraction + sklearn ensemble, its own Flask app
database/schema.sql# PostgreSQL schema (the AI-only stack uses SQLite instead)
docker/            # docker-compose for detector + Postgres + Redis + n8n
tests/             # pytest suite for detector/
examples/          # sample payloads for the legacy API
```

## Conventions

- `n8n/ai-only-workflow.json` is **generated** — edit `n8n/generate-workflow.py` and re-run it,
  never hand-edit the JSON.
- Every number that decides a verdict lives in the `models` table and is sent to the workflow as a
  per-request `tuning` block; the environment values are defaults, not the effective config.
- Benchmark corpora must be real captures with a citable source. No authored sample text.
- Secrets never appear in command output; keys are stored Fernet-encrypted and forwarded as headers.
