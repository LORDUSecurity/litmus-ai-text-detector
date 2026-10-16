#!/usr/bin/env bash
# Round 5: what is nvidia/nemotron-3-ultra-550b-a55b:free's real yield on the 48-text corpus?
#
# Round 3 recorded 29/48 usable and 19 failures, and the summary called all 19 of them JSON parse
# failures. They were not: every one is an HTTP 200 carrying {"error":{"status":503}} -- "Upstream
# error from Nvidia: Service temporarily overloaded" -- at attempt 1, in a run launched with
# --tries 5. The harness only retried on the *transport* status, so OpenRouter's relayed upstream
# error was never retried and never rotated to another key. That is fixed (transientInBody, pinned
# by test-retry.js), so the 19 texts are re-callable and 29/48 is a floor caused by our own code,
# not a measurement of the free pool.
#
# Why this matters beyond one row: the free mirror is the only candidate for a free *primary*, and
# "it answers 60% of texts" vs "it answers nearly all of them but scores lower than the paid
# endpoint" are different verdicts. The score-distribution findings (AUC 0.897, AI mean 0.736, min
# 0.60) came from the 29 answers and are unaffected either way.
#
# Budget: --resume keeps the 29 rows already scored and calls only the 19 missing, at --tries 2 and
# concurrency 1 = at most 38 requests. Round 4 needs ~12. They share three OpenRouter accounts and
# one unproven daily cap, so this waits for BOTH round 3 and round 4 to exit before probing.
#
#   bash benchmark/free-round5.sh          (log: benchmark/free-round5.log)
set -u
cd "$(dirname "$0")"

DEADLINE_MIN="${DEADLINE_MIN:-300}"
POLL_MIN="${POLL_MIN:-10}"
QUEUE_WAIT_MAX_MIN="${QUEUE_WAIT_MAX_MIN:-120}"
MODEL="nvidia/nemotron-3-ultra-550b-a55b:free"
TAG="orfull-nvidia-nemotron-3-ultra-550b-a55b-free"
FILE="results-${TAG}.jsonl"

# Anything still holding the day's free budget. The PS filter is left out on purpose: quoting
# -Filter "Name='node.exe'" through bash is how a process check silently returned a clean 0 twice.
# The leading bracket in each name is load-bearing: this very grep line appears in the process list,
# so a plain "free-round3" pattern would match its own caller and report the queue as busy forever.
busy() {
  powershell -NoProfile -Command "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine" 2>/dev/null \
    | grep -qE "[f]ree-round3\.sh|[f]ree-round4\.sh|[q]oder-round4-chain|[r]un-benchmark\.js --tag (or-google-gemma|orling1400)"
}

waited=0
while busy; do
  if [ "$waited" -ge "$QUEUE_WAIT_MAX_MIN" ]; then
    echo "round 3/4 still running after ${waited} min -- not starting a third concurrent spender"
    exit 1
  fi
  echo "round 3/4 still holds the free budget (${waited} min waited), sleeping 5"
  sleep 300
  waited=$((waited + 5))
done
echo "queue clear after ${waited} min"
waited=0   # the headroom deadline counts from here, not from when the script started

echo
echo "=== waiting for free-tier headroom (deadline ${DEADLINE_MIN} min, polling every ${POLL_MIN})"
while :; do
  node ../tools/probe-or-headroom.js "$MODEL"
  PROBE=$?
  if [ "$PROBE" -eq 0 ]; then echo "headroom found"; break; fi
  if [ "$PROBE" -ne 1 ]; then
    echo "not a quota problem (probe exit $PROBE = nothing testable: transport or no key) -- stopping"
    exit 2
  fi
  if [ "$waited" -ge "$DEADLINE_MIN" ]; then
    echo "still capped after ${waited} min -- giving up rather than burning probe requests"
    exit 1
  fi
  sleep $((POLL_MIN * 60))
  waited=$((waited + POLL_MIN))
done

echo
echo "=== 1. resume only the 19 missing texts (the 29 already scored are kept untouched)"
node run-benchmark.js --tag "$TAG" --model "$MODEL" \
  --corpus corpus.json --limit 48 --concurrency 1 --resume \
  --tries 2 --wait_ms 10000 \
  --base_env OPENROUTER_BASE_URL \
  --key_env OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3 2>&1 | tail -30

echo
echo "=== 2. the same file, re-audited: did the recovered rows arrive clean?"
node truncation-audit.js "$FILE"

echo
echo "=== 3. no-answer vs bad-answer, and the new yield as the panel computes it"
curl -s -X POST http://127.0.0.1:8787/benchmark/import | head -c 300
echo
curl -s http://127.0.0.1:8787/benchmark/models \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
      const r=JSON.parse(s).find(x=>x.tag==="orfull-nvidia-nemotron-3-ultra-550b-a55b-free");
      if(!r){console.log("row not found in /benchmark/models");process.exit(1);}
      console.log("n="+r.n_scored+"/"+r.n_rows+" no_answer="+r.n_no_answer+" bad_answer="+r.n_parse_failed
        +" acc/scored="+r.accuracy_over_scored+" acc/corpus="+r.accuracy_over_corpus
        +" medAI="+r.median_ai_text+" medHU="+r.median_human_text+" sep="+r.separation);
    })'

echo
echo "=== 4. the published comparison table, regenerated from the database"
node doc-table.js hc3-48
