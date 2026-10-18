# Deploying the AI text detector (Docker Compose)

A single-host, three-container stack: **web** (nginx + the built Angular SPA),
**backend** (Flask/gunicorn + SQLite), and **n8n** (the AI-only detection workflow).
The browser talks only to nginx; nginx reverse-proxies `/api/` to the backend, so the
API is same-origin and the session cookie flows without any CORS exception.

```
browser ──http──> web (nginx :80)
                    ├── /            → Angular static files (SPA)
                    └── /api/        → backend:8787/   (prefix stripped)
backend (gunicorn :8787) ──http──> n8n:5678/webhook/ai-detect-ai
        │                              │
        ├── SQLite + Fernet key  ◄──── backend_data volume
        └── purger deletes n8n execution rows ◄──── n8n_data volume (shared with n8n)
```

## Prerequisites

- Docker Engine + Compose v2 (`docker compose version`).
- A populated `.env` in the **repo root** (copy `.env.example` and fill in the lane API
  keys). Compose reads this file for `${VAR}` interpolation, and the backend bind-mounts
  it so `seed.py` can import the providers, encrypted keys, tuned models and thresholds.

## Quickstart

```bash
# from the repo root
docker compose up -d --build          # build web+backend, pull n8n, start all three
bash docker/n8n-import.sh             # one-time: import, publish, activate, restart n8n
# wait for n8n to finish booting and register the webhook (see Troubleshooting)
```

`n8n-import.sh` exports `MSYS_NO_PATHCONV=1` and `MSYS2_ARG_CONV_EXCL='*'` because Git Bash rewrites
`--input=/n8n-workflows/ai-only-workflow.json` into a `C:/Program Files/Git/...` path before docker
sees it, and the CLI then reports ENOENT for a file that is plainly mounted.

Open `http://localhost:8080` and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`
(default `admin` / `changeme-admin` — **change these first**).

Verify the stack:

```bash
docker compose ps                                   # all three "running"/"healthy"
curl -s http://localhost:8080/api/healthz            # {"ok":true} — anonymous liveness
curl -s http://localhost:8080/api/gateway-health     # 401 until you send a session cookie
docker compose logs -f backend                       # seed summary + gunicorn boot
```

`/api/healthz` is the only route that answers with no session. That is deliberate: the backend's own
`HEALTHCHECK` needs a target that cannot 401 (it used to poll `/gateway-health`, the container never
turned healthy, and `web` never started), and `/gateway-health` answers the DB path, row counts, the
engine URL and the seeded lane defaults — an operator command, not a public endpoint. For it:

```bash
curl -s -c /tmp/j.txt -X POST http://localhost:8080/api/auth/login \
     -H 'Content-Type: application/json' \
     -d "{\"username\":\"$ADMIN_USERNAME\",\"password\":\"$ADMIN_PASSWORD\"}" > /dev/null
curl -s -b /tmp/j.txt http://localhost:8080/api/gateway-health
```

`/api/gateway-health` reports `purge_enabled: true` once the purger has opened n8n's
SQLite. If it shows `purge_enabled: false`, the `n8n_data` volume is not yet shared or
n8n has not created its database — the purger now waits for n8n to create that file instead of
opening it first, so give n8n a minute before restarting anything.

## Configuration

All of these are read from the repo-root `.env` (Compose interpolation) with the
defaults shown in `docker-compose.yml`. The lane keys/models/thresholds come from the
existing `.env`; the deploy-specific vars are:

| Variable            | Default            | Purpose                                                        |
|---------------------|--------------------|----------------------------------------------------------------|
| `ADMIN_USERNAME`    | `admin`            | First-boot administrator username.                            |
| `ADMIN_PASSWORD`    | `changeme-admin`   | First-boot administrator password. **Change before exposing.**  |
| `SECRET_KEY`        | *(empty)*          | Cookie-signing secret. Empty = use the persisted Fernet key file in `backend_data` (stable). Set a long random string for multiple backend replicas. |
| `WEB_PORT`          | `8080`             | Host port the SPA is published on. Use `80` for a bare host.    |
| `N8N_PORT`          | `5678`             | Host port the n8n editor is published on.                       |
| `USE_SECURE_COOKIES`| `false`            | Set `true` behind TLS so the session cookie is HTTPS-only.      |
| `ALLOW_BYOK`        | `true`             | Let users attach their own key to an admin-allowed endpoint.    |
| `ALLOWED_LLM_HOSTS` | nvidia+openrouter  | Exact hosts a request may target. Add `generativelanguage.googleapis.com` to allow Google keys. |
| `GUNICORN_WORKERS`  | `2`                | Backend worker processes (each runs its own purger).            |
| `GUNICORN_THREADS`  | `8`                | Threads per worker; raises concurrency on slow-pool detections. |

The admin can change the deployed model lanes, thresholds, the paper link and the
API-key guides from the **Admin** tab at runtime; those edits live in the `backend_data`
volume and survive redeploys. Ordinary users see the **Ranking**, **History** and
**Detect** tabs only, and never the advanced parameters.

Two things about how configuration reaches the database, both learned on 2026-10-16:

- `backend/seed.py` reads the mounted `.env` **and** the container environment, with the
  live environment winning for the names it understands (`N8N_URL`, `WEBHOOK_PATH`, the lane
  models/keys, the thresholds). Before that, the `N8N_URL: http://n8n:5678` entry in
  `docker-compose.yml` was ignored because it was not in the file either, so the backend was
  seeded `http://localhost:5678` — unreachable from inside its own container — and every
  detection 502'd. Compose sets that var for you; nothing needs to be added to `.env`.
- Settings are seeded **only when absent**. If a stack was first booted with the wrong
  `n8n_url`, redeploying with the fix does not rewrite it; correct it in the Admin tab
  (Settings → `n8n_url`) or start from a clean `backend_data` volume.

## Production / TLS notes

- Terminate TLS in front of nginx (reverse proxy or cloud LB) and set
  `USE_SECURE_COOKIES=true`. The container itself listens on plain HTTP :80.
- Persist the two named volumes (`backend_data`, `n8n_data`). Losing `backend_data`
  loses the accounts, history, BYOK keys, ranking edits and the Fernet master key that
  encrypts the stored lane keys — without it the encrypted keys are unrecoverable.
- Pin image tags for reproducibility: replace `n8nio/n8n:latest` with the version you
  validated, and `nginx:1.27-alpine` / `node:22-alpine` / `python:3.12-slim` likewise.
- The backend forwards each detection to n8n and waits; with the shared NVIDIA pool a
  verdict can take ~2 minutes. The timeout chain is `lane timeout_ms` (240 s on the shipped primary)
  → backend wait (+30 s = 270 s) → gunicorn `--timeout` 285 s → nginx `proxy_read_timeout` 330 s →
  the browser's own 360 s (`frontend/src/app/core/http-budget.ts`), and
  each tier must stay above the one inside it. Do not lower them below the slowest lane's timeout.
  The chain moved as one unit on 2026-10-17 because the shipped lane answered a 320-char text in
  214.2 s -- inside the 240 s ceiling, outside the 180 s one it replaced. The last tier is the one
  that was missing: the UI used to give up at 150 s, *inside* every server tier above it, so a
  detection the backend was still honouring was reported to the user as a failure while it carried on
  and landed in History. With a browser tier above nginx, a timeout on screen means the browser
  itself is stuck -- which is what an operator can act on.

## How BYOK keys are kept off disk

n8n writes the inbound webhook record — including any `x-primary-api-key` header — into
its SQLite database when an execution starts, and turning run-data saving off does NOT
remove it (measured 2026-10-06). The backend therefore shares the `n8n_data` volume and
runs a purger that deletes those execution rows a second after each detection. Keep the
shared volume mounted; without it the guarantee is lost.

The delete is of the `execution_entity` parent row, and n8n's schema declares
`execution_data.executionId … ON DELETE CASCADE`, so the blob that carries the header goes with it
-- which is why `PRAGMA foreign_keys = ON` is not optional in `purge.py`. Rows that predate the
backend process are purged at startup too: the steady-state sweep only deletes `id > startup_max_id`,
so before that startup pass every execution that was mid-flight when the deploy restarted kept its
key for the life of the volume. `/api/gateway-health` reports the count as
`startup_rows_purged` next to `rows_purged`.

What this does not claim is that a key is never on disk: for the seconds an execution is running,
n8n has written the inbound request. The guarantee is "deleted within a few seconds of the call,
and never left behind by a restart", not "never written".

## Troubleshooting

- **n8n restarts in a loop, `docker compose ps` shows no `healthy` flag for it, log says
  `SQLITE_READONLY: attempt to write a readonly database`** — n8n's `database.sqlite` in the
  `n8n_data` volume is owned by `root` while n8n runs as uid `node`. It gets created by the backend:
  `sqlite3.connect()` creates the file it is handed, and the backend container is root, so a backend
  that boots before n8n finishes migrating leaves a file n8n cannot write. Fixed in `backend/purge.py`
  (the purger refuses to create the path and waits for n8n instead). To repair an already-poisoned
  volume: `docker compose down && docker volume rm <project>_n8n_data && docker compose up -d`.
  Check ownership with
  `docker compose exec -T n8n ls -la /home/node/.n8n` — the `.sqlite` must be `node node`.
- **Detection 404s / "Cannot reach the detection path"** — the n8n webhook is not
  registered yet. n8n can take a while to register webhooks after boot. Run
  `docker compose restart n8n`, wait, then retry. Confirm with
  `curl -s -b /tmp/j.txt http://localhost:8080/api/n8n/status` (`webhook_registered: true`;
  `/tmp/j.txt` is the cookie jar from the signed-in `curl` above — `/api/n8n/status` is not public).
- **`POST /api/detect` answers `502 DETECTION_UNAVAILABLE` with `error_code: ENGINE_TIMEOUT`** — the
  stack is intact: n8n accepted the webhook call and the model lane simply did not finish inside its
  own ceiling. The message names the URL and the wait, and the engine-vs-transport discrimination is
  deliberate (`ENGINE_UNREACHABLE` is the other 502, and means nothing answered). Measured on the
  live deploy: the shared NVIDIA pool pushed `z-ai/glm-5.3-flash` from its usual ~86 s to 127–162 s in
  a single hour, and one text exceeded the 180 s lane ceiling and came back at 214 s. The operator
  priced that wait as worth having and the shipped ceiling moved to 240 s (see the chain above), so a
  slow-but-legal detection now succeeds rather than 502s. Retry an ENGINE_TIMEOUT anyway -- it means
  the lane ran past 240 s -- or raise that model row's `timeout_ms` from the Admin tab, but keep the
  whole chain above it: do not lower nginx `proxy_read_timeout` or gunicorn `--timeout` below
  `timeout_ms + 30 s`.
- **Ranking tab is empty** — `seed.py` found no benchmark data. The backend image copies
  `benchmark/` (results/manifest/catalog/corpus); rebuild with `--no-cache` if you added
  new capture files, then `docker compose exec backend python -m backend.seed`.
- **Login fails right after first boot** — the admin is seeded from `ADMIN_USERNAME` /
  `ADMIN_PASSWORD` on the backend's first start only. If you change them later, either
  recreate the `backend_data` volume (destroys all data) or change the password from the
  Admin tab while signed in.
- **`docker compose config` warns about an unset var** — that variable is missing from
  the repo-root `.env`; the compose defaults still apply, but add it to silence the
  warning and make the deploy explicit.

## What changed from the old stack

The previous `docker/docker-compose.yml` ran Postgres + Redis + a `detector/` service
that no longer exists. This stack is SQLite-only (no Postgres/Redis), drops `detector/`,
adds the Flask `backend` and the nginx `web` tier, and shares n8n's volume so the purger
can enforce the BYOK no-persist guarantee.

## Live deploy proof

`python tools/verify_container_deploy.py` drives the *running* stack and is the only check that can
see the failures this recipe actually had. On the shipped lane it asserts fourteen things: nginx
proxies `/api/healthz` without a session and keeps `/api/gateway-health` behind one, the SPA it
serves is the bundle the repo just built, the two copies of the technical report under `/research/`
are served byte-for-byte as the repo holds them (the PDF with a `application/pdf` Content-Type, not as
HTML), the deploy's admin can sign in, the containerised backend seeded the promoted primary, the
purger attached to n8n's own SQLite without creating it, the workflow is published with its webhook
registered, two real labeled `benchmark/corpus.json` texts are scored through the whole path (nginx →
gunicorn → n8n → provider lane → parse → verdict) and come back with phrase evidence attached, and the
two verdicts are on opposite sides. Nothing it prints contains a key.

The `/research/` pair is the one check that cannot be satisfied by a config edit. `frontend/Dockerfile`
builds the UI inside the image, so the report reaches an image only when it is in
`frontend/public/research/` — see the copy step in `README.md`. Both servers answer an unknown path
with `index.html` and **200**, so a status code proves nothing about a static asset: the check fetches
`/research/report.html` and compares bytes with `research-zenodo/report.html`, and asserts the PDF
starts with `%PDF`.

Set `DETECT_MODEL` to score those two texts on another calibrated row instead of the shipped default
(`DETECT_MODEL=google/diffusiongemma-26b-a4b-it python tools/verify_container_deploy.py`). That is
what proves the plumbing when a free pool is saturated; the tool drops to thirteen checks, says out
loud that the shipped operating point was not exercised, and does not assert class separation for a
lane it did not pick.

Run it after every `docker compose up -d --build`. Measured here on 2026-10-17:

* **12/12 on the shipped `z-ai/glm-5.3-flash` lane** — `hu-000` HUMAN_LIKELY p 0.18 in 209.4 s and
  `ai-000` AI_GENERATED p 0.80 in 199.5 s, 3 phrase quotes each, through nginx.
* **11/11 on an override lane** (`google/diffusiongemma-26b-a4b-it`: p 0.15 in 8.3 s, p 0.98 in
  1.9 s) — the run that proved the containers were fine while the default lane was 502ing.

Earlier the same morning, before the lane ceiling was raised, the shipped default returned two
`502 ENGINE_TIMEOUT` between 10:40 and 11:00 UTC while the hourly reachability probe recorded the
same model at 127–162 s on a short prompt: the answer was arriving at ~214 s against a 180 s
ceiling. Both timings above land in the same pool regime, and both now fit under the ceiling — which
is why the chain is 240 s lane → 270 s backend wait → 285 s gunicorn → 330 s nginx rather than the
180/210/240/300 it was. If your deploy sits in a slower regime, expect the 502 and read the
`ENGINE_TIMEOUT` entry in Troubleshooting.

## Preflight (needs no Docker daemon)

`python tools/verify_deploy_recipe.py` proves the parts of this stack that fail before a compiler
sees them: every `COPY` source exists in the build context it declares and is not hidden by
`.dockerignore`, the files a Linux container executes carry no CRLF, `.gitattributes` still pins
`eol=lf` for them (this host runs `core.autocrlf=true`, so a fresh clone would otherwise hand
`docker/n8n-import.sh` and the entrypoint to a Linux process with carriage returns), nginx proxies
`/api` and falls back to `index.html`, `docker compose config` validates, and the deploy recipe is
actually tracked by git. It exits non-zero with the failing check named. Run it before `--build`,
and again after editing a Dockerfile, the compose file or `.dockerignore`.

What it cannot prove is the image build itself -- that needs a daemon. On this host the earlier
blocker was WSL's missing optional component (`wsl --status` -> `WSL_E_WSL_OPTIONAL_COMPONENT_REQUIRED`),
cleared with `wsl.exe --install --no-distribution` and a Docker Desktop restart; no reboot was needed.
With Engine 29.6.2 up, the remaining checks are `docker compose up -d --build`,
`bash docker/n8n-import.sh`, and `python tools/verify_container_deploy.py`.
