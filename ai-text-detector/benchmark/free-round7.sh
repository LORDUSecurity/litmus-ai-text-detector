#!/usr/bin/env bash
# Round 7: promote whatever round 6 proved is actually callable, and fit its band.
#
# Round 6 (benchmark/free-round6.log, 2026-10-08) answered 2 texts per model and split the
# non-responders three ways:
#
#   inclusionai/ling-3.1-flash       2/2 clean at BOTH 700 and 1400 tokens, ~5 s each,
#                                    ai-000 -> 0.65/0.68, hu-000 -> 0.35/0.30.
#                                    This is the free successor to ling-3.0-flash-sante:free, which
#                                    the catalogue now lists paid-only (404 "This model is
#                                    unavailable for free"). It is the only candidate worth 48 texts.
#   nvidia/nemotron-3.5-lightning    2/2 but 30-44 s per text and the human row needed salvage
#   dots-studio/dots-3-note-preview  2/2 but 17-21 s, one truncated, 0.65 AI vs 0.68 HUMAN (no separation)
#   nvidia/nemotron-3.5-content-safety  0/2 no_json_object at 1400 -> model-side, not a gateway problem
#
# Budget: one 48-text run at --tries 3, concurrency 2, across three accounts whose free cap
# re-measured 200/usable on all three keys today. Worst case ~120 requests against a 50/account/day
# cap, so if a key caps mid-run the --resume below is re-runnable the next day rather than lossy.
#
#   bash benchmark/free-round7.sh           (log: benchmark/free-round7.log)
set -u
cd "$(dirname "$0")"

KEYS="OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3"
MODEL="${MODEL:-inclusionai/ling-3.1-flash}"
TAG="${TAG:-orfull-ling-3-1-flash}"

echo "=== 1. full 48-text capture at max_tokens 1400 (--resume keeps anything already scored)"
node run-benchmark.js --tag "$TAG" --model "$MODEL" \
  --corpus corpus.json --limit 48 --concurrency 2 --resume \
  --max_tokens 1400 --tries 3 --wait_ms 10000 \
  --base_env OPENROUTER_BASE_URL --key_env "$KEYS" 2>&1 | tail -20

echo
echo "=== 2. yield and answer quality: gateway no-answer vs model bad-answer vs salvage"
node nonresponders.js | grep -E "$TAG|^tag|^capture" || true
node truncation-audit.js "results-${TAG}.jsonl"

echo
echo "=== 3. register the capture in the database and show the panel row"
curl -s -X POST http://127.0.0.1:8787/benchmark/import | head -c 300
echo
node panel-row.js "$TAG"

echo
echo "=== 4. the band this capture supports, grid-searched on the shipped decision logic"
node calibrate.js "results-${TAG}.jsonl" --max-review 0.20 --top 5 | sed -n '1,60p'

echo
echo "=== 5. store the band only once it has been fitted and checked -- this is manual, on purpose:"
echo "    store-band.js scores through the shipped decision node, refuses a band whose tightest"
echo "    margin is under 0.03, and writes provenance into the models row's notes."
echo "      node store-band.js --tag $TAG --ai <cut> --human <cut> --aic 0 --huc 0 --dry-run"
echo "      node store-band.js --tag $TAG --ai <cut> --human <cut> --aic 0 --huc 0 --row <models.id>"
