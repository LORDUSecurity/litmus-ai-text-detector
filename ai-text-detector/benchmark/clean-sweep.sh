#!/usr/bin/env bash
# Clean-text re-benchmark sweep. Each model is re-scored on corpus-clean.json with the SAME params
# as its dirty corpus.json baseline (so dirty-vs-clean is an apples-to-apples confound comparison),
# and the flagship lanes additionally get corpus-holdout-clean.json (a disjoint out-of-sample check).
# Providers run sequentially; OpenRouter free is concurrency 1 with 6-key rotation to respect its
# daily cap. run-benchmark.js writes results-<tag>.jsonl + a params: manifest line and never a key.
set -u
cd "$(dirname "$0")"

log() { echo "[$(date +%H:%M:%S)] $*"; }

run() { # tag model base_env key_env corpus conc top_p maxtok
  local tag="$1" model="$2" benv="$3" kenv="$4" corpus="$5" conc="$6" top_p="$7" mt="$8"
  log "START $tag ($model @ $corpus)"
  node run-benchmark.js --tag "$tag" --model "$model" --base_env "$benv" --key_env "$kenv" \
    --corpus "$corpus" --concurrency "$conc" --top_p "$top_p" --max_tokens "$mt" --limit 48 \
    > "log-$tag.txt" 2>&1
  local usable; usable=$(grep -oE 'usable=[0-9]+/[0-9]+' "log-$tag.txt" | tail -1)
  log "DONE  $tag -> $usable"
}

OMNI=OMNIROUTE_BASE_URL; OMNIK=OMNIROUTE_API_KEY
NV=PRIMARY_BASE_URL; NVK=PRIMARY_API_KEY
OR=OPENROUTER_BASE_URL; ORK=OPENROUTER_API_KEY,OPENROUTER_API_KEY_2,OPENROUTER_API_KEY_3,OPENROUTER_API_KEY_4,OPENROUTER_API_KEY_5,OPENROUTER_API_KEY_6

log "===== OmniRoute kiro lanes (main) ====="
run clean-omfull-kiro-claude-sonnet-4-5   kiro/claude-sonnet-4.5  "$OMNI" "$OMNIK" corpus-clean.json 2 0.9 1400
run clean-omfull-kiro-claude-haiku-4-5    kiro/claude-haiku-4.5   "$OMNI" "$OMNIK" corpus-clean.json 2 0.9 1400
run clean-omfull-kiro-minimax-m2-5        kiro/minimax-m2.5       "$OMNI" "$OMNIK" corpus-clean.json 2 0.9 1400
run clean-omfull-kiro-minimax-m2-1        kiro/minimax-m2.1       "$OMNI" "$OMNIK" corpus-clean.json 2 0.9 1400

log "===== OmniRoute sonnet on HOLDOUT (OOS confound check) ====="
run clean-oos-omkiro-sonnet-4-5           kiro/claude-sonnet-4.5  "$OMNI" "$OMNIK" corpus-holdout-clean.json 2 0.9 1400

log "===== NVIDIA free lanes (main) ====="
run clean-nvfull-glm-5-3                  z-ai/glm-5.3                         "$NV" "$NVK" corpus-clean.json 2 0.9 1400
run clean-nvfull-glm-5-3-flash            z-ai/glm-5.3-flash                   "$NV" "$NVK" corpus-clean.json 2 0.9 1400
run clean-nvfull-gemma-4-31b-it           google/gemma-4-31b-it                "$NV" "$NVK" corpus-clean.json 2 0.9 1400
run clean-nvfull-nemotron-3-5-lightning   nvidia/nemotron-3.5-lightning-30b-a3b "$NV" "$NVK" corpus-clean.json 2 0.9 1400

log "===== OpenRouter free lanes (main, concurrency 1, 3-key rotation) ====="
run clean-orfull-nemotron-ultra-free      nvidia/nemotron-3-ultra-550b-a55b:free "$OR" "$ORK" corpus-clean.json 1 0.9 700
run clean-orfull-ling-3-1-flash           inclusionai/ling-3.1-flash             "$OR" "$ORK" corpus-clean.json 1 0.9 1400
run clean-orfull-lfm-2-5                  liquid/lfm-2.5-2.6b:free               "$OR" "$ORK" corpus-clean.json 1 null 1400
run clean-orfull-openrouter-free          openrouter/free                        "$OR" "$ORK" corpus-clean.json 1 0.9 1400

log "===== paid pair on HOLDOUT (disjoint OOS for the deployed detector) ====="
run clean550-holdout  nvidia/nemotron-3-ultra-550b-a55b  "$NV" "$NVK" corpus-holdout-clean.json 3 0.9 700
run cleandiff-holdout google/diffusiongemma-26b-a4b-it   SECONDARY_BASE_URL SECONDARY_API_KEY corpus-holdout-clean.json 1 0.9 700

log "ALL SWEEP JOBS FINISHED"
