#!/usr/bin/env bash
# Round 3 = the part of round 2 that the account quota made impossible.
#
# Round 2 was stopped at 23:00Z with all three keys returning `free-models-per-day`, so the second
# 48-text promotion and both gemma resumes never ran. That cap is a calendar reset, not a queue that
# drains, so sleeping between attempts is the only useful move — and polling the probe is how this
# script knows the difference between "waited it out" and "still capped". Nothing here spends a
# request before a key has proved it can serve one.
#
# Overridable: DEADLINE_MIN (give up waiting after), POLL_MIN (how often to re-probe).
#   bash benchmark/free-round3.sh          (log: benchmark/free-round3.log)
set -u
cd "$(dirname "$0")"

DEADLINE_MIN="${DEADLINE_MIN:-240}"
POLL_MIN="${POLL_MIN:-5}"

echo "=== waiting for free-tier headroom (deadline ${DEADLINE_MIN} min, polling every ${POLL_MIN})"
waited=0
while :; do
  node ../tools/probe-or-headroom.js
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

KEYS=OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3
OR_ARGS=(--base_env OPENROUTER_BASE_URL --key_env "$KEYS")

echo
echo "=== 1. promotion 2: nemotron-3-ultra-550b-a55b:free on the full 48-text corpus"
node run-benchmark.js --tag "orfull-nvidia-nemotron-3-ultra-550b-a55b-free" \
  --model "nvidia/nemotron-3-ultra-550b-a55b:free" \
  --corpus corpus.json --limit 48 --concurrency 2 --tries 5 --wait_ms 10000 \
  "${OR_ARGS[@]}" 2>&1 | tail -6
sleep 20

echo
echo "=== 2. the two gemma models, concurrency 1, slow backoff (--wait_ms is the real flag)"
for m in "google/gemma-4-26b-a4b-it:free" "google/gemma-4-31b-it:free"; do
  slug=$(printf '%s' "$m" | tr '/:.' '---' | cut -c1-44)
  echo "--- resume $m"
  node run-benchmark.js --tag "or-$slug" --model "$m" \
    --corpus corpus-screen12.json --limit 12 --concurrency 1 --resume \
    --tries 6 --wait_ms 15000 "${OR_ARGS[@]}" 2>&1 | tail -6
  sleep 20
done

echo
echo "=== 3. import into the backend so the panel and doc-table can see the new rows"
curl -s -X POST http://127.0.0.1:8787/benchmark/import | head -c 300
echo
node analyze-free-screen.js
node doc-table.js hc3-48
