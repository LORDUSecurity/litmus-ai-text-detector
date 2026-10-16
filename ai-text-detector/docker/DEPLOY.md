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
bash docker/n8n-import.sh             # one-time: import + activate the detection workflow
# wait for n8n to finish booting and register the webhook (see Troubleshooting)
```

Open `http://localhost:8080` and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`
(default `admin` / `changeme-admin` — **change these first**).

Verify the stack:

```bash
docker compose ps                                   # all three "running"/"healthy"
curl -s http://localhost:8080/api/gateway-health     # backend reachable through nginx
docker compose logs -f backend                       # seed summary + gunicorn boot
```

`/api/gateway-health` reports `purge_enabled: true` once the purger has opened n8n's
SQLite. If it shows `purge_enabled: false`, the `n8n_data` volume is not yet shared or
n8n has not created its database — restart the backend after n8n is up.

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
  verdict can take ~2 minutes. nginx `proxy_read_timeout` and gunicorn `--timeout` are
  already set above that. Do not lower them below the slowest lane's timeout.

## How BYOK keys are kept off disk

n8n writes the inbound webhook record — including any `x-primary-api-key` header — into
its SQLite database when an execution starts, and turning run-data saving off does NOT
remove it (measured 2026-10-06). The backend therefore shares the `n8n_data` volume and
runs a purger that deletes those execution rows a second after each detection. Keep the
shared volume mounted; without it the guarantee is lost.

## Troubleshooting

- **Detection 404s / "Cannot reach the detection path"** — the n8n webhook is not
  registered yet. n8n can take a while to register webhooks after boot. Run
  `docker compose restart n8n`, wait, then retry. Confirm with
  `curl -s http://localhost:8080/api/n8n/status` (`webhook_registered: true`).
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

## Preflight (needs no Docker daemon)

`python tools/verify_deploy_recipe.py` proves the parts of this stack that fail before a compiler
sees them: every `COPY` source exists in the build context it declares and is not hidden by
`.dockerignore`, the files a Linux container executes carry no CRLF, `.gitattributes` still pins
`eol=lf` for them (this host runs `core.autocrlf=true`, so a fresh clone would otherwise hand
`docker/n8n-import.sh` and the entrypoint to a Linux process with carriage returns), nginx proxies
`/api` and falls back to `index.html`, `docker compose config` validates, and the deploy recipe is
actually tracked by git. It exits non-zero with the failing check named. Run it before `--build`,
and again after editing a Dockerfile, the compose file or `.dockerignore`.

What it cannot prove is the image build itself: on a Windows host whose WSL optional component is
missing (`wsl --status` -> `WSL_E_WSL_OPTIONAL_COMPONENT_REQUIRED`), Docker Desktop's Linux engine
never starts, so `docker build` has no daemon to ask. Fix the host, then the remaining checks are
`docker compose up -d --build`, `curl http://localhost:8080/gateway-health` through nginx, and one
live detection.
