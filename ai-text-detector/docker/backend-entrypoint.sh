#!/bin/sh
# Container entrypoint for the Flask backend.
#
# Why an entrypoint and not just `gunicorn ...`:
#   1. seed.py reads the lane keys / models / thresholds from the /app/.env FILE (not
#      from os.environ), so the compose file bind-mounts .env and we run the seed here.
#      It is idempotent and never overwrites an admin's later UI edits to existing rows
#      (except a key whose ciphertext changed).
#   2. seed.py defaults the n8n_url setting to http://localhost:5678 -- correct on a dev
#      host, wrong between containers. We force it to the compose service name so the
#      backend forwards detections to http://n8n:5678 and the purger finds n8n's SQLite.
#   3. gunicorn must NOT use --preload: wsgi.py starts the BYOK-purge thread at import,
#      and a thread started before fork() does not survive into the workers. Without
#      preload each worker imports wsgi and runs its own purger (idempotent, safe).
set -e

cd /app

echo "[entrypoint] seeding database from /app/.env (providers, keys, models, benchmarks, settings)"
python -m backend.seed

if [ -n "${N8N_URL:-}" ]; then
  echo "[entrypoint] forcing n8n_url=${N8N_URL} webhook_path=${WEBHOOK_PATH:-/webhook/ai-detect-ai}"
  python - <<'PY'
import os
from backend import db

db.init_db()
conn = db.connect()
try:
    db.set_setting(conn, "n8n_url", os.environ["N8N_URL"])
    wp = os.environ.get("WEBHOOK_PATH")
    if wp:
        db.set_setting(conn, "webhook_path", wp)
    # Mirror the compose-level switches into settings so the admin UI shows the truth.
    for env_key, setting_key in (("ALLOWED_LLM_HOSTS", "allowed_llm_hosts"),
                                 ("ALLOW_BYOK", "allow_byok")):
        val = os.environ.get(env_key)
        if val:
            db.set_setting(conn, setting_key, val)
    conn.commit()
finally:
    conn.close()
PY
fi

WORKERS="${GUNICORN_WORKERS:-2}"
THREADS="${GUNICORN_THREADS:-8}"
# 240, not 180: the backend waits `lane timeout_ms + 30 s` for n8n, and the shipped primary lane's
# ceiling is 180 s, so a slow-but-legal detection runs to 210 s. Keep this above that and below
# nginx's proxy_read_timeout (300 s) -- the compose file carries the same default and the reason.
TIMEOUT="${GUNICORN_TIMEOUT:-240}"

echo "[entrypoint] starting gunicorn on 0.0.0.0:8787 (gthread workers=${WORKERS} threads=${THREADS} timeout=${TIMEOUT}s)"
exec gunicorn \
  --bind 0.0.0.0:8787 \
  --worker-class gthread \
  --workers "${WORKERS}" \
  --threads "${THREADS}" \
  --timeout "${TIMEOUT}" \
  --access-logfile - \
  --error-logfile - \
  backend.wsgi:app
