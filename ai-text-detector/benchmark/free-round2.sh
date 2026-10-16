#!/usr/bin/env bash
# Round 2 of the free-model research, corrected.
#
# Round 1 passed `--wait 15000` / `--wait 12000` while the harness reads `--wait_ms`, so
# every retry slept the 3s default and the log claimed a slow backoff it never did. That
# makes the "both gemma models are 429-blank even at concurrency 1 with a long backoff"
# reading unproven -- it was proven at a *fast* backoff. The harness now rejects unknown
# flags, so this file uses the real names.
#
# OpenRouter work stays sequential: these runs share one key pool, and parallelising them
# is how the first screen ended up with two models at 0/12. The Gutenberg book control is a
# separate invocation (`benchmark/bookultra550.log`) -- it talks to the paid NVIDIA endpoint
# with different keys and cannot contend with the free pool.
#
# Phase 0 exists because round 2 lost 27 minutes to a budget that was already empty. A
# free-models-per-day 429 does not clear with waiting, so spending it on retries is pure loss;
# probe first and refuse to start.
#
#   bash benchmark/free-round2.sh          (log: benchmark/free-round2.log)
set -u
cd "$(dirname "$0")"

KEYS=OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3
OR_ARGS=(--base_env OPENROUTER_BASE_URL --key_env "$KEYS")

echo "### 0. is there any free-tier budget left today?"
node ../tools/probe-or-headroom.js
PROBE=$?
if [ "$PROBE" -ne 0 ]; then
  echo "not starting: probe exit $PROBE (1 = every key out of free-models-per-day, 2 = nothing testable)"
  exit 1
fi

echo "### 1. promote the best two screens to the full 48-text corpus"
for m in "inclusionai/ling-3.0-flash-sante:free" "nvidia/nemotron-3-ultra-550b-a55b:free"; do
  slug=$(printf '%s' "$m" | tr '/:.' '---' | cut -c1-40)
  echo "=== full corpus $m"
  node run-benchmark.js --tag "orfull-$slug" --model "$m" \
    --corpus corpus.json --limit 48 --concurrency 2 --tries 5 --wait_ms 10000 \
    "${OR_ARGS[@]}" 2>&1 | tail -4
  sleep 20
done

echo
echo "### 2. re-attempt the two 429-blanked gemma models with a backoff that is really slow"
for m in "google/gemma-4-26b-a4b-it:free" "google/gemma-4-31b-it:free"; do
  slug=$(printf '%s' "$m" | tr '/:.' '---' | cut -c1-44)
  echo "=== resume $m"
  node run-benchmark.js --tag "or-$slug" --model "$m" \
    --corpus corpus-screen12.json --limit 12 --concurrency 1 --resume \
    --tries 6 --wait_ms 15000 "${OR_ARGS[@]}" 2>&1 | tail -4
  sleep 20
done

echo
echo "=== refreshed screens + full-corpus numbers ==="
node analyze-free-screen.js
node analyze.js --corpus corpus.json 2>/dev/null | tail -20 || node analyze.js | tail -20
