# Calibration Guide

## Overview

This guide explains how to calibrate the AI text detection system using human-reviewed labels.

## Why Calibration Matters

The initial thresholds and ensemble weights are **heuristic estimates**. Real-world accuracy depends on:
- Domain (blog vs technical vs creative writing)
- Text length
- Model drift over time
- Provider-specific behaviors

Calibration with labeled data improves precision and recall.

## Step 1: Collect Labeled Data

### From the Review Workflow

1. Route uncertain cases through the review workflow
2. Have human reviewers label them as AI, HUMAN, or INCONCLUSIVE
3. Accumulate at least 100-200 labeled examples

### Query the Database

```sql
SELECT 
    rl.text_hash,
    rl.reviewer_label,
    rl.review_confidence,
    ar.features_json,
    ar.ai_probability as automatic_probability,
    ar.classification as automatic_classification,
    ar.model_results_json
FROM review_labels rl
JOIN analysis_results ar ON ar.id = rl.analysis_result_id
WHERE rl.review_confidence IN ('medium', 'high')
  AND rl.reviewer_label IN ('AI', 'HUMAN');
```

### Export to CSV

```bash
docker-compose exec postgres psql -U detector -d ai_detector \
  -c "COPY (SELECT ...) TO STDOUT WITH CSV HEADER" > labeled_data.csv
```

## Step 2: Analyze Current Performance

```python
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score

# Load data
df = pd.read_csv('labeled_data.csv')

# Map labels
df['true_binary'] = df['reviewer_label'].map({'AI': 1, 'HUMAN': 0})
df['pred_binary'] = (df['automatic_probability'] > 0.5).astype(int)

# Classification report
print(classification_report(df['true_binary'], df['pred_binary'], 
                           target_names=['HUMAN', 'AI']))

# Confusion matrix
print(confusion_matrix(df['true_binary'], df['pred_binary']))

# AUC
auc = roc_auc_score(df['true_binary'], df['automatic_probability'])
print(f"AUC: {auc:.3f}")
```

## Step 3: Optimize Thresholds

### Find Optimal Decision Boundary

```python
from sklearn.metrics import precision_recall_curve, f1_score
import numpy as np

# Calculate precision-recall for different thresholds
precisions, recalls, thresholds = precision_recall_curve(
    df['true_binary'], 
    df['automatic_probability']
)

# Find threshold that maximizes F1
f1_scores = 2 * (precisions * recalls) / (precisions + recalls + 1e-10)
best_idx = np.argmax(f1_scores)
best_threshold = thresholds[best_idx]

print(f"Optimal AI threshold: {best_threshold:.3f}")
print(f"F1 score: {f1_scores[best_idx]:.3f}")
print(f"Precision: {precisions[best_idx]:.3f}")
print(f"Recall: {recalls[best_idx]:.3f}")
```

### Update .env

```bash
AI_PROBABILITY_THRESHOLD=0.72  # Use optimized value
HUMAN_PROBABILITY_THRESHOLD=0.15  # Adjust accordingly
```

## Step 4: Calibrate Ensemble Weights

```python
from scipy.optimize import minimize

# Extract component scores from JSON
df['stat_prob'] = df['model_results_json'].apply(lambda x: x['component_scores']['statistical_probability'])
df['primary_prob'] = df['model_results_json'].apply(lambda x: x['component_scores']['primary_probability'])
df['secondary_prob'] = df['model_results_json'].apply(lambda x: x['component_scores']['secondary_probability'])

# Define loss function
def loss_function(weights):
    w_stat, w_prim, w_sec = weights
    
    # Ensemble prediction
    ensemble = (w_stat * df['stat_prob'] + 
                w_prim * df['primary_prob'] + 
                w_sec * df['secondary_prob'])
    
    # Binary cross-entropy loss
    loss = -np.mean(
        df['true_binary'] * np.log(ensemble + 1e-10) + 
        (1 - df['true_binary']) * np.log(1 - ensemble + 1e-10)
    )
    return loss

# Optimize with constraint that weights sum to 1
from scipy.optimize import minimize
constraint = {'type': 'eq', 'fun': lambda w: np.sum(w) - 1.0}
bounds = [(0, 1), (0, 1), (0, 1)]
initial_weights = [0.60, 0.25, 0.15]

result = minimize(loss_function, initial_weights, 
                 method='SLSQP', bounds=bounds, constraints=constraint)

optimal_weights = result.x
print(f"Optimal weights: stat={optimal_weights[0]:.3f}, "
      f"primary={optimal_weights[1]:.3f}, secondary={optimal_weights[2]:.3f}")
```

### Update .env

```bash
STATISTICAL_WEIGHT=0.58
PRIMARY_MODEL_WEIGHT=0.27
SECONDARY_MODEL_WEIGHT=0.15
```

## Step 5: Validate on Holdout Set

Split your labeled data 80/20 train/test:

```python
from sklearn.model_selection import train_test_split

train_df, test_df = train_test_split(df, test_size=0.2, random_state=42)

# Train on train_df (steps 3-4)
# Validate on test_df

# Calculate final metrics on holdout
# ...
```

## Step 6: Deploy Calibrated Settings

1. **Update `.env`** with new thresholds and weights
2. **Increment version numbers**:
   ```bash
   THRESHOLD_VERSION=1.1.0
   CLASSIFIER_VERSION=1.1.0
   ```
3. **Restart services**:
   ```bash
   docker-compose restart detector n8n
   ```
4. **Document changes** in a changelog

## Step 7: Monitor Performance

After deployment:

```sql
-- Compare pre/post calibration
SELECT 
    classifier_version,
    COUNT(*) as total,
    AVG(CASE WHEN classification = 'REVIEW_REQUIRED' THEN 1 ELSE 0 END) as review_rate,
    AVG(confidence) as avg_confidence
FROM analysis_results
GROUP BY classifier_version
ORDER BY created_at DESC;
```

## Continuous Calibration

1. **Monthly**: Re-run calibration with new labeled data
2. **Model updates**: Recalibrate when changing PRIMARY_MODEL or SECONDARY_MODEL
3. **Domain shift**: Recalibrate if deploying to new content domain

## Advanced: Model-Specific Calibration

For different domains, maintain separate threshold configs:

```python
# config/thresholds.yaml
domains:
  blog:
    ai_threshold: 0.82
    human_threshold: 0.18
  technical:
    ai_threshold: 0.88
    human_threshold: 0.12
  creative:
    ai_threshold: 0.75
    human_threshold: 0.25
```

Modify workflow to select thresholds based on `content_type`.

## Troubleshooting

**High false positive rate (human flagged as AI)**
→ Increase `AI_PROBABILITY_THRESHOLD`

**High false negative rate (AI flagged as human)**
→ Decrease `AI_PROBABILITY_THRESHOLD`

**Too many review_required**
→ Decrease `MAX_MODEL_DISAGREEMENT` or lower confidence thresholds

**Model disagreement causing issues**
→ Try single-model classification or add third model for tiebreaking

---

## The AI-only pipeline (no database, no detector service)

Everything above calibrates the Docker pipeline against reviewed rows in Postgres. The
AI-only n8n workflow has no review store, so it was calibrated offline against a fixed
labeled corpus: 48 HC3 texts (24 real human, 24 real ChatGPT) across 5 domains, scored
through the same prompt and defensive parser the workflow ships
(`benchmark/run-benchmark.js` writes `results-<tag>.jsonl`).

### Measured on 2026-10-06 (NVIDIA NIM free tier, temp 0.1)

| model | AUC | median AI | median HUMAN | p50 / p90 latency | verdict |
|---|---|---|---|---|---|
| meta/muse-glimmer-30b | 0.988 | 0.835 | 0.42-0.65 (unstable) | 13.5s / 45.4s | best score, slow, 2/48 null responses |
| google/diffusiongemma-26b-a4b-it | 0.953 | 0.98 | 0.50 (bimodal, see below) | 0.66s / 1.2s | **shipped tie-break lane** |
| nvidia/nemotron-3-ultra-550b-a55b | 0.934 | 0.815 | 0.25 | 10.9s / 21.2s | listed but not servable (404 on every completion) — superseded as shipped primary 2026-10-16, task #119 |
| openai/gpt-oss-20b | 0.780 | 0.70 | 0.60 | 5.2s / 11.0s | weak |
| nvidia/nemotron-3-nano-omni-30b | 0.737 | 0.85 | 0.62 | 11.5s / 17.8s | weak |
| poolside/laguna-xs-2.1 | 0.678 | 0.85 | 0.75 | 1.1s / 1.9s | weak |
| nvidia/nemotron-3-super-120b-a12b | 0.654 | 0.62 | 0.38 | 3.5s / 10.0s | near coin-flip |
| meta/llama-3.2-11b-vision-instruct | 0.296 | 0.20 | 0.70 | 3.0s / 6.5s | anti-correlated, do not use |

The medians are the point of the table. The 550B and diffusiongemma disagree by ~0.5 on
almost every text because they sit on different scales, not because they disagree about
the text. A weighted average of two differently-scaled numbers is not a statistic anyone
calibrated, and measuring it confirmed the intuition: adding any second lane as a blend
input lost 1-21 correct texts versus the primary alone.

Convention note (2026-10-06): the medians in this table are true medians (the mean of the
two middle values for an even-length list). They were first written as `probs[len // 2]`,
the upper of the middle pair, which is a different number for an even list. Only one cell
changed meaningfully: diffusiongemma's median HUMAN was 0.75 under that convention and 0.50
under this one, because its 24 human scores are bimodal -- twelve at 0.15-0.25, twelve at
0.75-0.98, nothing in between -- so the middle of that distribution is an empty gap and
whichever value lands there is an artefact of the tie-break, not a property of the model.
That is why its median is quoted as "bimodal" rather than as a number, and why the ~0.5
scale offset above is stated per text. `node benchmark/doc-table.js` regenerates the
full-corpus table from the same code the panel reads.

### Decision rule now in production

1. The primary decides whenever its own thresholds allow it
   (`AI_PROBABILITY_THRESHOLD` / `HUMAN_PROBABILITY_THRESHOLD` + confidence floors).
2. Otherwise the secondary answers, read against `SECONDARY_DECISION_THRESHOLD` (0.90):
   at or above it the verdict is AI, below it HUMAN. Confidence is not a gate here; measured
   AUC of self-reported confidence against correctness was ~0.65, i.e. near noise.
3. If the secondary was never called or failed, the primary's deferral stays
   `REVIEW_REQUIRED`. If the primary failed outright, the secondary is the fallback and
   uses its own threshold too.

The secondary lane fires on 10 of 48 texts (21%), and it is the 0.66s lane, so its latency
is off the hot path.

### Reproduce

```
node benchmark/run-benchmark.js --tag diffgemma26b --model google/diffusiongemma-26b-a4b-it
node benchmark/tiebreak-holdout.js benchmark/results-ultra550.jsonl benchmark/results-diffgemma26b.jsonl
node n8n/test-ensemble.js replay benchmark/results-ultra550.jsonl benchmark/results-diffgemma26b.jsonl
node benchmark/tiebreak-sweep.js                 # all pairs, 660 combinations
node benchmark/band-sweep.js ultra550 diffgemma26b   # band grid + fold stability
node benchmark/band-margins.js ultra550 diffgemma26b 0.69 0.40 0.90
node benchmark/boundary-rule-test.js             # which texts land exactly on a cut
```

The third command runs the **shipped** Code node out of `ai-only-workflow.json`, so the
reported tally cannot drift from what the workflow does. On the 2026-10-06 scores after the
band re-tune below: **45 correct / 3 wrong / 0 review**, versus 37 / 3 / 8 for the retired
weighted blend and 0.917 / 0.917 on the two-fold holdout (primary alone: 0.708 / 0.833).

### Band re-tune (same day, second pass)

A 660-combination sweep (`benchmark/tiebreak-sweep.js`) and a 140-point band grid
(`benchmark/band-sweep.js`, `benchmark/band-margins.js`) were run over every measured model
pair. One value changed: **`HUMAN_PROBABILITY_THRESHOLD` 0.45 → 0.40**, which took the pair
from 44/4/0 to 45/3/0 and, more importantly, from 1 false negative to 0.

The reason is structural, not a lucky number. Model probabilities arrive with two decimals,
and the decisive test is inclusive, so a score sitting exactly on a threshold ends the
deferral band instead of being treated as uncertain. `ai-018` scored exactly 0.45 and was
declared HUMAN. At 0.40 it defers and the tie-break (0.98) resolves it correctly. Three texts
hit a cut exactly in this corpus (`benchmark/boundary-rule-test.js` prints them). The
alternative rule — make an exact hit defer — measured identically (45/3/0) at one extra
second-lane call, so the simpler change was kept.

What was measured and **rejected**, with the numbers, so it does not get re-litigated:

- **`SECONDARY_DECISION_THRESHOLD` 0.90 → 0.95** (46/2/0). The whole gain is `hu-004`, which
  diffusiongemma scores 0.94: a 0.01 margin inside a gap whose other side is 0.98, on a model
  that emits only 8 distinct values. Live run-to-run jitter on this pipeline is far larger
  than 0.01, so this is fitting a text, not finding an operating point. At 0.96 the cut would
  sit 0.02 under the AI cluster.
- **`meta/muse-glimmer-30b` as primary + the 550B as secondary, band 0.75/0.40** (47/1/0,
  foldmin 0.958, zero decisions within 0.02 of a cut, reproduced on an independent second run
  of the model). Rejected on cost and transfer risk: p50 13.5s with a 45.4s p90, 1-2 null
  responses per 48, ~33% of requests buying a second 10.9s call (≈17s expected vs ≈11s), and
  an AI cut at 0.75 that sits only 0.07 above the model's own human median. The band was
  calibrated for the 550B's scale; it is wrong for every other model in the table.
- **Raising `AI_PROBABILITY_THRESHOLD` to 0.80** (46/1/1): same texts correct as 0.69/0.40
  but the remaining wrong one is `hu-009` at exactly 0.95 against a 0.95 cut — a boundary hit
  dressed up as an improvement.
- **Band tuning cannot rescue a weak lane.** With a secondary attached, `openai/gpt-oss-20b`
  peaks at 38/48 and `nemotron-3-super-120b` at 34/48 at any point in the grid.

Caveats worth carrying forward: 48 texts means one text is 2 points of accuracy, HC3's AI
class is GPT-3.5-era text, diffusiongemma emits only 8 distinct probabilities so the 0.90
cut has no margin under it, and 5 of 12 borderline texts changed category between two
identical runs. Judge any change on aggregates, never on one text.

## Live verification of the tie-break build (2026-10-06, after deploy)

The regenerated workflow was imported, `n8n publish:workflow` set `activeVersionId`, and all
48 texts were fired through the real webhook
(`benchmark/live-all48-tiebreak.log`, 513s, concurrency 3, 0 request errors):

- accuracy **0.854 (41/48)**, precision on decided calls **0.872 (41/47)**, review rate
  **0.021 (1/48)**
- **6 wrong verdicts, all of them false positives on human text** (`hu-003`, `hu-004`,
  `hu-009`, `hu-010`, `hu-019`, `hu-020`), **0 false negatives**
- `methods = {SINGLE_MODEL_PRIMARY_AI: 21, SINGLE_MODEL_PRIMARY_HUMAN: 15,
  SINGLE_MODEL_PRIMARY_UNCERTAIN: 1, SINGLE_MODEL_SECONDARY_HUMAN: 1,
  TIE_BREAK_AI: 9, TIE_BREAK_HUMAN: 1}` — the primary decided 37 texts, the tie-break 10

Compared with the same build's previous live run (accuracy 0.646, review rate 0.333, 1 false
positive), the tie-break did what it was designed to do: it resolved the deferrals that used
to pile up in `REVIEW_REQUIRED`. Accuracy went up and the review rate collapsed by 16×. Six
human texts became confidently wrong in the process.

Against the offline replay of the shipped node
(`node n8n/test-ensemble.js replay benchmark/results-ultra550.jsonl
benchmark/results-diffgemma26b.jsonl --secondary-threshold 0.9` → 45 correct / 3 wrong /
0 review, wrong = `hu-004`, `hu-009`, `hu-011`), live is 4 texts worse, and *how* it is worse
is structural rather than noise:

| text | offline primary | offline secondary | live primary | live secondary | live verdict |
|------|-----------------|-----------------|--------------|----------------|--------------|
| hu-003 | 0.25 | 0.15 | **0.75** | not called | `SINGLE_MODEL_PRIMARY_AI` wrong |
| hu-004 | 0.65 | 0.94 | 0.65 | **0.98** | `TIE_BREAK_AI` wrong |
| hu-009 | 0.75 | 0.95 | 0.65 | 0.95 | `TIE_BREAK_AI` wrong |
| hu-010 | 0.38 | 0.98 | **0.60** | 0.98 | `TIE_BREAK_AI` wrong |
| hu-019 | 0.35 | 0.95 | **0.55** | 0.94 | `TIE_BREAK_AI` wrong |
| hu-020 | 0.62 | 0.85 | **0.72** | not called | `SINGLE_MODEL_PRIMARY_AI` wrong |

The primary drifted upward on human prose (0.25-0.38 offline → 0.55-0.75 live on five of
these texts). Four of them land inside the 0.40-0.69 deferral band, which hands the decision
to diffusiongemma — and it scores 0.94-0.98 on every one, above the 0.90 cut. So the band does
not "catch uncertainty and ask a second opinion" here; on human text it routes the decision to
a lane whose scale sits roughly 0.3 higher, and that lane answers AI.

Why the sweep could not show this: offline, the primary *decided* `hu-010` (0.38) and
`hu-019` (0.35) as HUMAN, so the secondary was never consulted on them and the replay never
put those texts through the tie-break path. **45/3/0 therefore understates the tie-break's
false-positive rate** — it scored the pair on the subset of texts where the primary happened to
behave. Do not read a replay number as the operating point of the pair without also checking
how many of the texts the tie-break was allowed to see.

Open question this raises (deliberately left untuned): the tie-break fires exactly when the
primary is unsure, and on this corpus "unsure" means a human text scoring 0.55-0.65. Any
threshold raised to protect those texts (`0.95`, measured and rejected above) is defending a
0.01 margin on a model with 8 distinct outputs. The alternative shape is to stop letting the
secondary *create* an AI verdict from a human-leaning deferral, but that is a policy change to
the node, not a number, and it needs its own measurement on a corpus where the primary
actually drifts.

### Resolved 2026-10-06: the tie-break keeps that authority, and the false positives are not its fault

**Scope, added 2026-10-16:** this is a finding about *nemotron-3-ultra-550b's* band, and the
promotion of `z-ai/glm-5.3-flash` voids it as a general rule. A tighter band on a stronger primary
leaves the tie-break with no AI texts to catch: on glm's 0.80/0.55 band the lane decided 3 of 48
texts and got all 3 wrong (three human texts accused as AI), so on the shipped default the same
authority is accuracy-negative. See round 25c of `free-model-research.md` for the re-measurement,
which also retracts the first (wrong-ruler) pass at it. The arithmetic below stands for the pair
it was taken on; re-running it today gives 44 correct / 4 wrong on
`results-ultra550.jsonl` + `results-diffgemma26b.jsonl`.

`benchmark/tiebreak-authority.js` runs the shipped node over the captures and splits the
deferrals by which way the primary was leaning (band midpoint 0.545).

Under the shipped rule (secondary decides at ≥ 0.90):

| primary position | n | decided by | wrong |
|---|---|---|---|
| `deferred` / leaning HUMAN | 3 | 1 primary-h alone, 1 tie-break HUMAN, **1 tie-break AI** | 0 |
| `deferred` / leaning AI | 8 | 6 tie-break AI, 2 tie-break HUMAN | 1 |
| `above-band` (primary decided AI alone) | 20 | 20 primary | 2 |
| `below-band` (primary decided HUMAN alone) | 17 | 17 primary | 0 |

1. **The class the proposed restriction targets contains exactly one text** — `ai-018`,
   primary 0.45 (leaning human), secondary 0.98, truth AI. The tie-break's AI verdict
   there was *correct*. Suppressing "secondary creates an AI verdict while the primary
   leans human" removes one right answer and zero wrong ones.
2. **The sweep measures the cost of that restriction**: at the shipped 0.90 cut it takes
   45 correct / 3 wrong down to 41 / 3 (four correct calls turned into reviews), and even
   at 0.95 it buys one fewer wrong for five more reviews. There is no cut where the
   restriction pays for itself.
3. **Where the false positives actually come from**: 2 of the 3 offline wrongs are the
   primary deciding AI *by itself* above 0.69 — the tie-break never saw them. In the live
   run the four tie-break false positives sat at primary 0.55-0.65, i.e. on the *AI-leaning*
   side of the band, so a human-leaning restriction would not have blocked a single one
   (`hu-019` at 0.55 is above the midpoint by 0.005).

So the shape of the decision is sound; what is unsound is the assumption that the primary
is stable. A drifting primary moves human text from `below-band` into the band and then
into the AI half of it, which is exactly the live table above. The countermeasure is
therefore about the primary — re-measure its band position on a fresh capture, and treat a
`TIE_BREAK_AI` whose primary sits just above 0.40 as the least trustworthy verdict the
pipeline emits — not about removing the secondary's authority.

Caveats on the sample: n=3 deferrals lean human and n=1 of them reaches an AI verdict, so
point 1 rests on a single text. The secondary also jitters across its own cut — the
deterministic loopback probe (`tools/verify_tuning_echo.py`) saw the same fixed input score
0.95 on one run and 0.85 on the next, straddling the 0.90 cut. This decision is "keep the
rule, the evidence points elsewhere", not "the rule is proven safe at scale".

### The mirror question: what does the *missing floor* on the human side cost? (measured 2026-10-06)

The rule restricted above is "may the secondary create an AI verdict". Its mirror is the fact
that `SECONDARY_DECISION_THRESHOLD` is one-sided: at or above the cut the tie-break says AI,
*below* it the tie-break says HUMAN, with no floor. A live request (backend `detections` row 19,
`POST /detect`) shows the shape: primary 0.62 → defers, secondary 0.85, output **HUMAN_LIKELY at
reported confidence 0.92** — while 0.85 is above the primary's own AI cut of 0.69. The input was
a 240-character passage written for this check to read like AI-flavoured corporate prose, so it is
an anecdote, not a labelled corpus row: it says the rule can emit a confident human answer on an
AI-leaning score, it cannot say how often.

`benchmark/tiebreak-floor.js` replays the shipped node over the same capture and prices a floor F,
read as "the tie-break may answer HUMAN only when the secondary scored ≤ F" — so the shipped rule
already *is* F = the AI cut, and lowering F is what moves verdicts into review:

```
tie-break HUMAN verdicts: 3; of them the deciding lane scored >= the primary's AI cut: 3
  hu-005  secondary p=0.85  truth=HUMAN  reported correct
  hu-015  secondary p=0.85  truth=HUMAN  reported correct
  hu-020  secondary p=0.85  truth=HUMAN  reported correct

floor F | secondary in (F, cut) -> review | wrong dropped | right dropped | acc over remaining | answers out of 48
 0.90   |   0                            |   0           |   0             | 0.938            | 48/48   <- shipped
 0.85   |   0                            |   0           |   0             | 0.938            | 48/48
 0.75   |   3                            |   0           |   3             | 0.933            | 45/48
 0.69   |   3                            |   0           |   3             | 0.933            | 45/48
```

So on this corpus a floor removes zero errors and three correct answers, and the same one-sided-rule
"fix" that was rejected on the AI side is rejected here for the same reason: n=3. Two details in that
table are worth more than the verdict.

1. **F = 0.85 is a no-op.** All three contradictory verdicts sit at exactly 0.85, because the
   secondary answers in ~0.05 quanta — a floor anywhere in (0.85, 0.90] changes nothing at all, and
   the only floors that bite are ones that also swallow the primary's whole AI band.
2. **The accuracy barely moves (0.938 → 0.933) while coverage drops 48 → 45.** A floor buys nothing
   measurable here, which is what an n=3 class looks like: the sample cannot price the risk.

What the two analyses agree on is the real defect — **the tie-break inherits the secondary's
AI-side noise as a human verdict, and 0.85 is exactly the jitter value the loopback probe caught
straddling the 0.90 cut.** The reported confidence (0.92) is the secondary's self-claim, not a
measure of distance from the cut, which is why the output reads certain at the moment the deciding
lane is least distinguishable from the primary's AI band. Nothing here justifies changing the rule;
if a floor is ever added it has to be measured on a capture where the primary actually drifts, and
a `TIE_BREAK_HUMAN` whose secondary sits at 0.85 should be read as the least trustworthy human
answer the pipeline emits.
