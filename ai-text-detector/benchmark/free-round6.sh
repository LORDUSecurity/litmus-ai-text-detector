#!/usr/bin/env bash
# Round 6: do the models that never answer actually answer when the two known causes are removed?
#
# Triage of every capture with unscored rows (node benchmark/nonresponders.js --retry --codes)
# split the non-responders into two kinds, and they need opposite treatments:
#
#   gateway-side  google/gemma-4-26b-a4b-it:free  0/12, all http_429 at --tries 6 --wait 15000
#                 google/gemma-4-31b-it:free      0/12, all http_429
#                 -> class-(a) "temporarily rate-limited upstream": a per-model upstream pool,
#                    not our account cap. Only a live call tells us whether it has cleared.
#   model-side    nvidia/nemotron-3.5-lightning:free        0/12, every row json_parse_failed and
#                 the stored content is the schema echoed back
#                 inclusionai dots-3-note-preview:free       4/12, emits `{ "ai_probability":` and stops
#                 nvidia/nemotron-3.5-content-safety:free   0/12, no_json_object
#                 -> waiting does not help. But the dots shape is exactly what a truncated answer
#                    looks like, and the ling lesson from round 4 is that max_tokens 700 was
#                    cutting its JSON in half. So each gets ONE more chance at max_tokens 1400.
#
# inclusionai/ling-3.0-flash-sante:free is not on this list because it no longer exists:
# GET /api/v1/models on 2026-10-08 lists ling-3.0-flash-sante as PAID only, and a chat request to
# the :free id returns 404 "This model is unavailable for free". Its replacement in the free
# catalogue is inclusionai/ling-3.1-flash, which gets probed here under the same hypothesis
# (reasoning-then-JSON needs the bigger budget) and is the candidate for the 48-text promotion.
#
# Budget: 2 texts x <=3 tries per model = <=6 requests each, <=36 worst case, against three
# accounts whose cap re-measured at 200/usable on all three keys today. A refused 429 does not
# consume a paid slot, so the gemma probes are nearly free. Runs are sequential on purpose.
# These probe tags are NOT imported into /benchmark: a 2-row capture would show up in the panel
# next to the real 48-text runs. Import happens after a full-corpus promotion.
#
#   bash benchmark/free-round6.sh          (log: benchmark/free-round6.log)
set -u
cd "$(dirname "$0")"

KEYS="OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3"

probe() {  # probe <tag> <model> <max_tokens> <tries> <wait_ms>
  echo
  echo "=== $2  (tag=$1 mt=$3 tries=$4 wait=$5ms)"
  node run-benchmark.js --tag "$1" --model "$2" \
    --corpus corpus-probe2.json --limit 2 --concurrency 1 \
    --max_tokens "$3" --tries "$4" --wait_ms "$5" \
    --base_env OPENROUTER_BASE_URL --key_env "$KEYS" 2>&1 | tail -12
}

echo "=== 0. headroom gate: is any account usable right now? (probe with a model we know answers)"
node ../tools/probe-or-headroom.js
GATE=$?
if [ "$GATE" -ne 0 ]; then
  echo "gate says exit $GATE (1 = every key capped, 2 = nothing testable) -- not spending further"
  exit "$GATE"
fi

probe probe-dots1400          dots-studio/dots-3-note-preview:free      1400 2 8000
probe probe-lightning1400     nvidia/nemotron-3.5-lightning:free        1400 2 8000
probe probe-content-safety    nvidia/nemotron-3.5-content-safety:free   1400 2 8000
probe probe-ling31            inclusionai/ling-3.1-flash                1400 3 12000
probe probe-ling31-700        inclusionai/ling-3.1-flash                 700 2 8000
probe probe-gemma26b          google/gemma-4-26b-a4b-it:free             700 3 15000
probe probe-gemma31b          google/gemma-4-31b-it:free                 700 3 15000

echo
echo "=== what each probe actually produced (N SCORED NO-ANS BAD YIELD MAXTRY)"
node nonresponders.js | grep -E "probe-|^capture|^tag" || node nonresponders.js | head -5
