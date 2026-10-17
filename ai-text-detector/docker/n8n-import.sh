#!/usr/bin/env bash
# One-time n8n workflow import + activation for the container stack.
# Run from the repo root AFTER `docker compose up -d`:
#
#   bash docker/n8n-import.sh
#
# n8n does NOT auto-import a mounted JSON file. This drives the in-container CLI to
# import n8n/ai-only-workflow.json (mounted at /n8n-workflows) and then publish + activate
# it so its webhook registers. Per measured behaviour on this workflow, the webhook can take a
# while to register after n8n boots; if a detection 404s on /webhook/ai-detect-ai,
# restart the n8n service (`docker compose restart n8n`), wait, and retry.
set -euo pipefail

# Git Bash (MSYS) rewrites an argument that starts with a slash into a Windows path: run this
# script from Git Bash without the guard below and `--input=/n8n-workflows/ai-only-workflow.json`
# arrives at docker.exe as `C:/Program Files/Git/n8n-workflows/ai-only-workflow.json`, and the CLI
# answers "ENOENT". The paths are container-internal, so conversion must be off, not escaped.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

WF_INPUT="${WF_INPUT:-/n8n-workflows/ai-only-workflow.json}"
WF_ID="${DETECTOR_WORKFLOW_ID:-ai-detection-ai-only}"
COMPOSE="${COMPOSE:-docker compose}"

echo ">> importing ${WF_INPUT} into n8n ..."
$COMPOSE exec -T n8n n8n import:workflow --input="$WF_INPUT"

# n8n 2.x: an import lands INACTIVE, and activeVersionId is only set by publish. `update:workflow
# --active=true` still exists but is deprecated and does not publish a version, so the webhook can
# stay unregistered behind an "active" flag.
echo ">> publishing ${WF_ID} ..."
$COMPOSE exec -T n8n n8n publish:workflow --id="$WF_ID" \
  || echo "!! publish:workflow unavailable on this n8n; falling back to the legacy active flag"

echo ">> activating ${WF_ID} ..."
$COMPOSE exec -T n8n n8n update:workflow --id="$WF_ID" --active=true \
  || echo "!! CLI activation failed (n8n version differences). Activate in the n8n UI: Workflows -> '${WF_ID}' -> toggle Active."

echo ">> restarting n8n so the webhook registers against the published version ..."
$COMPOSE restart n8n

echo ">> done. Internal webhook: http://n8n:5678/webhook/ai-detect-ai"
echo "   The backend already targets it via the n8n_url setting (forced on boot)."
echo "   Confirm from the host:"
echo "     curl -s -c /tmp/j.txt -X POST http://localhost:8080/api/auth/login \\"
echo "       -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"<your ADMIN_PASSWORD>\"}' >/dev/null"
echo "     curl -s -b /tmp/j.txt http://localhost:8080/api/n8n/status   # webhook_registered must be true"
