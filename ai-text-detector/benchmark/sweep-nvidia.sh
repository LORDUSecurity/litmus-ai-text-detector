#!/usr/bin/env bash
# NVIDIA-side clean-text lanes, sequential on the PRIMARY key (no internal contention). Order is
# deliberate: the DEPLOYED-ENSEMBLE holdout pair first (the headline OOS number), because 550B and
# diffusiongemma answer reliably, THEN the free-tier models, each gated with a 2-text probe before a
# full run so a saturated free pool fails fast instead of grinding 48 rows x tries x timeout.
# (Measured 2026-10-11: integrate endpoint + key healthy -- 550B answered in 12s -- but the free
#  pool for glm-5.3 etc. was silent past 150s.)
set -u
cd "$(dirname "$0")"
log() { echo "[$(date +%H:%M:%S)] $*"; }
NV=PRIMARY_BASE_URL; NVK=PRIMARY_API_KEY
SECOND=SECONDARY_BASE_URL; SECK=SECONDARY_API_KEY

run() { # tag model benv kenv corpus conc top_p maxtok timeout tries
  local tag="$1" model="$2" benv="$3" kenv="$4" corpus="$5" conc="$6" top_p="$7" mt="$8" to="$9" tr="${10}"
  log "START $tag ($model @ $corpus)"
  node run-benchmark.js --tag "$tag" --model "$model" --base_env "$benv" --key_env "$kenv" \
    --corpus "$corpus" --concurrency "$conc" --top_p "$top_p" --max_tokens "$mt" \
    --tries "$tr" --timeout "$to" --limit 48 > "log-$tag.txt" 2>&1
  log "DONE  $tag -> $(grep -oE 'usable=[0-9]+/[0-9]+' "log-$tag.txt" | tail -1)"
}

log "===== paid pair on HOLDOUT (disjoint OOS for the deployed detector) ====="
run clean550-holdout  nvidia/nemotron-3-ultra-550b-a55b "$NV" "$NVK" corpus-holdout-clean.json 3 0.9 700 90 4
run cleandiff-holdout google/diffusiongemma-26b-a4b-it  "$SECOND" "$SECK" corpus-holdout-clean.json 1 0.9 700 120 4

log "===== NVIDIA free lanes: gate then full-run responders ====="
: > nvfree-status.txt
for m in "z-ai/glm-5.3|clean-nvfull-glm-5-3" \
         "z-ai/glm-5.3-flash|clean-nvfull-glm-5-3-flash" \
         "google/gemma-4-31b-it|clean-nvfull-gemma-4-31b-it" \
         "nvidia/nemotron-3.5-lightning-30b-a3b|clean-nvfull-nemotron-3-5-lightning"; do
  model="${m%%|*}"; tag="${m##*|}"
  log "GATE $tag ($model): 2 texts, 1 try, 150s"
  node run-benchmark.js --tag "$tag-gate" --model "$model" --base_env "$NV" --key_env "$NVK" \
    --corpus corpus-clean.json --concurrency 2 --top_p 0.9 --max_tokens 1400 \
    --tries 1 --timeout 150 --limit 2 > "log-$tag-gate.txt" 2>&1
  u=$(grep -oE 'usable=[0-9]+' "log-$tag-gate.txt" | tail -1 | grep -oE '[0-9]+')
  if [ "${u:-0}" -ge 1 ]; then
    log "GATE OK $tag (usable=$u) -> full run"
    echo "$tag $model GATE_OK usable=$u" >> nvfree-status.txt
    rm -f "results-$tag-gate.jsonl"
    node run-benchmark.js --tag "$tag" --model "$model" --base_env "$NV" --key_env "$NVK" \
      --corpus corpus-clean.json --concurrency 2 --top_p 0.9 --max_tokens 1400 \
      --tries 2 --timeout 180 --limit 48 > "log-$tag.txt" 2>&1
    log "DONE  $tag -> $(grep -oE 'usable=[0-9]+/[0-9]+' "log-$tag.txt" | tail -1)"
  else
    log "GATE DEAD $tag (usable=${u:-0}) -> skip full run"
    echo "$tag $model GATE_DEAD" >> nvfree-status.txt
    rm -f "results-$tag-gate.jsonl"
  fi
done

log "NVIDIA SWEEP FINISHED"
# Gate runs are liveness probes, not scored lanes: drop their results files and their manifest
# lines so seed.py never attributes a 2-row gate to a real model id.
grep -v -- '-gate"' runs-manifest.log > _m.tmp && mv _m.tmp runs-manifest.log
rm -f results-*-gate.jsonl log-*-gate.txt
cat nvfree-status.txt
