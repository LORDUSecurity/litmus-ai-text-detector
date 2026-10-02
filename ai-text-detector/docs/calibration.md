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
