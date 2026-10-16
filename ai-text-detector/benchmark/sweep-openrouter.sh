#!/usr/bin/env bash
# OpenRouter-free clean-text lanes. Separate endpoint + its own 6 keys, so this can run
# concurrently with sweep-nvidia.sh without contending on the NVIDIA key. Params mirror each
# model's dirty corpus.json baseline (temp 0.1, prompt direct, concurrency 1 with 6-key rotation
# to respect the daily free cap); --timeout 120 gives the free pool more patience than the old 90s
# default without hanging forever.
set -u
cd "$(dirname "$0")"
log() { echo "[$(date +%H:%M:%S)] $*"; }

OR=OPENROUTER_BASE_URL
ORK=OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3,OPENROUTER_API_KEY_4,OPENROUTER_API_KEY_5,OPENROUTER_API_KEY_6

run() { # tag model corpus conc top_p maxtok
  local tag="$1" model="$2" corpus="$3" conc="$4" top_p="$5" mt="$6"
  log "START $tag ($model @ $corpus)"
  node run-benchmark.js --tag "$tag" --model "$model" --base_env "$OR" --key_env "$ORK" \
    --corpus "$corpus" --concurrency "$conc" --top_p "$top_p" --max_tokens "$mt" \
    --tries 4 --timeout 120 --limit 48 > "log-$tag.txt" 2>&1
  log "DONE  $tag -> $(grep -oE 'usable=[0-9]+/[0-9]+' "log-$tag.txt" | tail -1)"
}

log "===== OpenRouter free lanes (main) ====="
run clean-orfull-nemotron-ultra-free nvidia/nemotron-3-ultra-550b-a55b:free corpus-clean.json 1 0.9 700
run clean-orfull-ling-3-1-flash      inclusionai/ling-3.1-flash              corpus-clean.json 1 0.9 1400
run clean-orfull-lfm-2-5             liquid/lfm-2.5-2.6b:free               corpus-clean.json 1 null 1400
run clean-orfull-openrouter-free     openrouter/free                        corpus-clean.json 1 0.9 1400

log "OPENROUTER SWEEP FINISHED"
