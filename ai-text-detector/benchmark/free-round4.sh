#!/usr/bin/env bash
# Round 4: is ling-3.0-flash-sante:free's 35% failure rate a max_tokens problem or a
# format-following problem?
#
# The existing captures already answer most of it (benchmark/truncation-audit.js): on 48 texts,
# 11 were cut off at max_tokens 700 and *all 11 still parsed* -- 9 only because the JSON salvager
# patched the truncated object -- while all 17 outright failures were answers that finished
# naturally (missing_ai_probability 10, no_json_object 5, json_parse_failed 2). So headroom cannot
# rescue the failures; the open question is whether the cap corrupted the *scores*, i.e. whether a
# salvaged half-JSON gives the same verdict as the whole one.
#
# This run doubles the cap on the 12-text screen (12 requests, not 48) and compare.js reports the
# per-text drift against the 700-token capture over the texts both scored.
#
# Sequencing: run this AFTER benchmark/free-round3.sh. Both gate on the same :free headroom probe,
# the account cap is per calendar day, and round 3 needs ~72 of the day's requests for the 48-text
# promotion plus the two gemma resumes. Firing them together risks starving the promotion.
#
#   bash benchmark/free-round4.sh          (log: benchmark/free-round4.log)
set -u
cd "$(dirname "$0")"

DEADLINE_MIN="${DEADLINE_MIN:-240}"
POLL_MIN="${POLL_MIN:-5}"
MODEL="inclusionai/ling-3.0-flash-sante:free"
TAG="orling1400-screen12"
BASE="results-or-inclusionai-ling-3-0-flash-sante-free.jsonl"
NEW="results-${TAG}.jsonl"

echo "=== waiting for free-tier headroom (deadline ${DEADLINE_MIN} min, polling every ${POLL_MIN})"
waited=0
while :; do
  node ../tools/probe-or-headroom.js "$MODEL"
  PROBE=$?
  if [ "$PROBE" -eq 0 ]; then echo "headroom found after ${waited} min of waiting"; break; fi
  if [ "$PROBE" -ne 1 ]; then
    echo "not a quota problem (probe exit $PROBE = nothing testable: transport or no key) -- stopping"
    exit 2
  fi
  if [ "$waited" -ge "$DEADLINE_MIN" ]; then
    echo "still capped after ${waited} min -- giving up rather than blocking indefinitely"
    exit 1
  fi
  sleep $((POLL_MIN * 60))
  waited=$((waited + POLL_MIN))
done

echo
echo "=== 1. the same 12 texts at double the cap, concurrency 1"
node run-benchmark.js --tag "$TAG" --model "$MODEL" \
  --corpus corpus-screen12.json --limit 12 --concurrency 1 \
  --max_tokens 1400 --tries 4 --wait_ms 15000 \
  --base_env OPENROUTER_BASE_URL \
  --key_env OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3 2>&1 | tail -8

echo
echo "=== 2. did the cap cause any failure, and how many scores are salvage-only?"
node truncation-audit.js "$BASE" "$NEW"

echo
echo "=== 3. paired drift: same texts, 700 vs 1400 tokens"
node compare.js "$BASE" "$NEW"

echo
echo "=== 4. import so the panel can see the row"
curl -s -X POST http://127.0.0.1:8787/benchmark/import | head -c 300
echo
