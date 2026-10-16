# syntax=docker/dockerfile:1
# Flask backend (gunicorn) for the AI text detector.
#
# Build context is the REPO ROOT (see docker-compose.yml: build.context: .), so the
# COPY paths below are relative to the root and the single root .dockerignore governs
# what is visible. The .env file and backend/data are excluded from the context and
# supplied at runtime (bind mount + named volume), so no secret or stale database is
# ever baked into a layer.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    BACKEND_HOST=0.0.0.0 \
    BACKEND_PORT=8787

WORKDIR /app

# Install Python dependencies first for layer caching. gunicorn is added HERE rather
# than to backend/requirements.txt because gunicorn cannot run on the Windows host used
# for local development (there the app runs via `python -m backend.app`).
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt gunicorn

# Application package + the measured benchmark data seed.py imports to populate the
# model ranking. backend/data and backend/tests are excluded by .dockerignore.
COPY backend ./backend
COPY benchmark ./benchmark

# Entrypoint: seed the DB, point n8n_url at the compose service, then exec gunicorn.
COPY docker/backend-entrypoint.sh /usr/local/bin/backend-entrypoint.sh
# Neutralise any CRLF a Windows checkout introduced before chmod, or `#!/bin/sh` fails.
RUN sed -i 's/\r$//' /usr/local/bin/backend-entrypoint.sh \
    && chmod +x /usr/local/bin/backend-entrypoint.sh

EXPOSE 8787

# /gateway-health is public and returns 200 even when n8n is still booting, so it is a
# safe liveness target. Uses urllib (no curl in the slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/gateway-health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/backend-entrypoint.sh"]
