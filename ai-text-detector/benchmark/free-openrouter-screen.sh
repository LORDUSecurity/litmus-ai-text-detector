#!/usr/bin/env bash
# Rank the OpenRouter :free tier as an AI-text detector.
#
# Only the models that answered the catalogue probe are screened (a GET /models
# entry that 404s on call is worthless here). Each is run over corpus-screen12.json
# -- 6 AI + 6 HUMAN, evenly spaced across the HC3 configs -- with the exact prompt
# and parser the shipped workflow uses, so the numbers are comparable with
# results-ultra550.jsonl et al. 12 texts rather than 48 because the free tier is
# rate limited; the top scorers then get the full corpus.
#
#   bash benchmark/free-openrouter-screen.sh
#
# Writes benchmark/results-or-<slug>.jsonl and a screen summary. Keys come from
# .env through the harness; nothing here prints one.
set -u
cd "$(dirname "$0")"

MODELS=(
  "nvidia/nemotron-3-ultra-550b-a55b:free"
  "google/gemma-4-26b-a4b-it:free"
  "google/gemma-4-31b-it:free"
  "nvidia/nemotron-3-super-120b-a12b:free"
  "cohere/north-mini-code:free"
  "inclusionai/ling-3.0-flash-sante:free"
  "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
  "dots-studio/dots-3-note-preview:free"
  "nvidia/nemotron-3.5-lightning:free"
  "nvidia/nemotron-3.5-content-safety:free"
)

KEYS=OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3,OPENROUTER_API_KEY_4,OPENROUTER_API_KEY_5,OPENROUTER_API_KEY_6

for m in "${MODELS[@]}"; do
  slug=$(printf '%s' "$m" | tr '/:.' '---' | cut -c1-44)
  echo "=== $m -> results-or-$slug.jsonl"
  node run-benchmark.js --tag "or-$slug" --model "$m" \
    --corpus corpus-screen12.json --limit 12 --concurrency 2 \
    --base_env OPENROUTER_BASE_URL --key_env "$KEYS" 2>&1 | tail -5
  sleep 4
done

echo
echo "=== screen summary (AI mean vs HUMAN mean on 12 texts) ==="
node analyze-free-screen.js
