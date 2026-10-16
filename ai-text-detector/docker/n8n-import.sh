#!/usr/bin/env bash
# One-time n8n workflow import + activation for the container stack.
# Run from the repo root AFTER `docker compose up -d`:
#
#   bash docker/n8n-import.sh
#
# n8n does NOT auto-import a mounted JSON file. This drives the in-container CLI to
# import n8n/ai-only-workflow.json (mounted at /n8n-workflows) and then activate it so
# its webhook registers. Per measured behaviour on this workflow, the webhook can take a
# while to register after n8n boots; if a detection 404s on /webhook/ai-detect-ai,
# restart the n8n service (`docker compose restart n8n`), wait, and retry.
set -euo pipefail

WF_INPUT="${WF_INPUT:-/n8n-workflows/ai-only-workflow.json}"
WF_ID="${DETECTOR_WORKFLOW_ID:-ai-detection-ai-only}"
COMPOSE="${COMPOSE:-docker compose}"

echo ">> importing ${WF_INPUT} into n8n ..."
$COMPOSE exec -T n8n n8n import:workflow --input="$WF_INPUT"

echo ">> activating ${WF_ID} ..."
$COMPOSE exec -T n8n n8n update:workflow --id="$WF_ID" --active=true \
  || echo "!! CLI activation failed (n8n version differences). Activate in the n8n UI: Workflows -> '${WF_ID}' -> toggle Active."

echo ">> done. Internal webhook: http://n8n:5678/webhook/ai-detect-ai"
echo "   The backend already targets it via the n8n_url setting (forced on boot)."
