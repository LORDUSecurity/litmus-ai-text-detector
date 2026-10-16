# Legacy multi-signal pipeline

> **Not the running system.** This is the design that preceded the AI-only n8n
> workflow: a Docker Compose stack with PostgreSQL, Redis, a Python detector
> service, and provenance/watermark stages. It is kept because parts of it —
> the calibration method, the troubleshooting tree, the review-queue semantics —
> still describe reasoning the current pipeline inherits. For what actually
> starts today, see [../README.md](../README.md) and
> [free-model-research.md](free-model-research.md).

---

## Overview

This system detects AI-generated text through a multi-stage pipeline:

1. **Provenance Check** - Cryptographic signatures (C2PA, content credentials)
2. **Watermark Detection** - AI watermark scanning (pluggable detectors)
3. **Statistical Analysis** - Deterministic linguistic features
4. **Model Scoring** - Ensemble of free-tier LLMs via OmniRoute
5. **Human Review** - Queue uncertain cases for manual review

### Classification Results

- `AI_GENERATED` - Strong evidence of AI generation
- `HUMAN_LIKELY` - No strong AI signal detected (NOT proof of human authorship)
- `REVIEW_REQUIRED` - Uncertain; needs human review
- `DETECTION_UNAVAILABLE` - Analysis could not be completed

## Architecture

```
┌─────────────┐
│   n8n       │  Workflow orchestration
│  Webhooks   │
└──────┬──────┘
       │
       ├──> Normalize & Hash
       │
       ├──> Cache Lookup (PostgreSQL)
       │
       ├──> Provenance Check (Detector Service)
       │
       ├──> Watermark Detection (Detector Service)
       │
       ├──> Statistical Features (Detector Service)
       │
       ├──> OmniRoute (Cerebras, NVIDIA, OpenRouter)
       │    ├─> Primary Model
       │    └─> Secondary Model
       │
       ├──> Ensemble Classification (Detector Service)
       │
       └──> Save Result & Respond
```

## Quick Start

### Prerequisites

- Docker and Docker Compose
- OmniRoute configured with API keys for:
  - Cerebras (free tier)
  - NVIDIA NIM (free tier)
  - OpenRouter (free tier models)

### Setup

1. **Clone and configure environment**

```bash
cd ai-text-detector
cp .env.example .env
```

2. **Edit `.env` file and add your API keys**

Required configuration:
```bash
# OmniRoute endpoint
OMNIROUTE_BASE_URL=http://your-omniroute-host:8080/v1
OMNIROUTE_API_KEY=your-omniroute-api-key

# Database password
DB_PASSWORD=secure-random-password

# n8n authentication
N8N_BASIC_AUTH_PASSWORD=secure-admin-password
```

3. **Start the stack**

```bash
cd docker
docker-compose up -d
```

4. **Wait for services to be healthy**

```bash
docker-compose ps
```

All services should show "healthy" status.

5. **Import n8n workflows**

- Open n8n: http://localhost:5678
- Login with credentials from `.env` (default: admin / change-this-password)
- Go to **Workflows** → **Import from File**
- Import `n8n/main-workflow.json`
- Import `n8n/review-workflow.json`
- Activate both workflows

6. **Test the detection endpoint**

```bash
curl -X POST http://localhost:5678/webhook/ai-detect \
  -H "Content-Type: application/json" \
  -d '{
    "text": "It is important to note that artificial intelligence has fundamentally transformed the technological landscape. However, significant challenges remain in ensuring ethical deployment."
  }'
```

Expected response:
```json
{
  "document_id": "...",
  "classification": "AI_GENERATED",
  "ai_probability": 0.87,
  "confidence": 0.82,
  "method": "ENSEMBLE_HIGH_CONFIDENCE",
  "review_required": false,
  "evidence": {
    "provenance": {},
    "watermark": {},
    "statistics": {...},
    "models": [...]
  },
  "warning": "Human-likely means no strong AI-generation signal was detected; it does not prove human authorship.",
  "versions": {...}
}
```

## Configuration

### Free-Tier Models

Configure models in `.env`:

```bash
# Recommended free-tier models
PRIMARY_MODEL=cerebras/llama3.1-8b
SECONDARY_MODEL=nvidia/llama-3.1-nemotron-70b-instruct
FALLBACK_MODEL=openrouter/meta-llama/llama-3.2-3b-instruct:free
```

### Rate Limits

Update these based on actual provider limits:

```bash
CEREBRAS_RPM=60        # Requests per minute
CEREBRAS_TPM=1000000   # Tokens per minute

NVIDIA_RPM=30
NVIDIA_TPM=50000

OPENROUTER_RPM=20
OPENROUTER_RPD=200     # Requests per day
```

### Detection Thresholds

Calibrate these based on your labeled validation data:

```bash
AI_PROBABILITY_THRESHOLD=0.85
AI_CONFIDENCE_THRESHOLD=0.70
HUMAN_PROBABILITY_THRESHOLD=0.20
HUMAN_CONFIDENCE_THRESHOLD=0.70
MAX_MODEL_DISAGREEMENT=0.30
```

### Ensemble Weights

Adjust the importance of each component (must sum to 1.0):

```bash
STATISTICAL_WEIGHT=0.60      # Statistical features
PRIMARY_MODEL_WEIGHT=0.25    # Primary LLM
SECONDARY_MODEL_WEIGHT=0.15  # Secondary LLM
```

## API Endpoints

### Main Detection Webhook

```bash
POST /webhook/ai-detect
Content-Type: application/json

{
  "document_id": "optional-id",
  "text": "Text to analyze",
  "source_url": "optional",
  "content_type": "optional",
  "metadata": {},
  "requested_by": "optional"
}
```

### Detector Service Endpoints

The Python detector service (port 5000) provides these endpoints:

- `GET /health` - Health check
- `POST /features/extract` - Extract statistical features
- `POST /classify` - Ensemble classification
- `POST /watermark/detect` - Watermark detection (placeholder)
- `POST /provenance/check` - Provenance verification (placeholder)
- `POST /cache/lookup` - Check cache
- `POST /result/save` - Save result to database
- `POST /review/submit` - Submit human review
- `GET /stats` - Get detection statistics

### Review Workflow

```bash
POST /webhook/ai-review
Content-Type: application/json

{
  "analysis_result_id": "uuid",
  "reviewer_label": "AI|HUMAN|INCONCLUSIVE|MIXED|INVALID",
  "reviewer_id": "optional",
  "reviewer_notes": "optional",
  "review_confidence": "low|medium|high"
}
```

## Database

PostgreSQL schema includes:

- `analysis_results` - All detection results with full evidence
- `review_labels` - Human-reviewed ground truth for calibration
- `provider_usage` - API usage tracking and error monitoring
- `detector_versions` - Schema versioning for result reproducibility

### Useful Queries

**Review queue (prioritized)**
```sql
SELECT * FROM review_queue LIMIT 10;
```

**Provider performance**
```sql
SELECT * FROM provider_performance;
```

**Cache statistics**
```sql
SELECT * FROM cache_statistics WHERE date > NOW() - INTERVAL '7 days';
```

**Model agreement analysis**
```sql
SELECT * FROM model_agreement_stats;
```

## Calibration

The system is designed for calibration with labeled data:

1. **Collect human-reviewed labels**
   - Route uncertain cases to review workflow
   - Accumulate labeled examples in `review_labels` table

2. **Extract labeled dataset**
```sql
SELECT 
    rl.text_hash,
    rl.reviewer_label,
    ar.features_json,
    ar.model_results_json,
    ar.classification as automatic_classification,
    ar.ai_probability as automatic_probability
FROM review_labels rl
JOIN analysis_results ar ON ar.id = rl.analysis_result_id
WHERE rl.review_confidence IN ('medium', 'high');
```

3. **Calibrate thresholds and weights**
   - Use scikit-learn or similar to find optimal decision boundaries
   - Update `.env` with calibrated values
   - Increment `THRESHOLD_VERSION` or `CLASSIFIER_VERSION`
   - Restart services

4. **Measure performance**
```python
from sklearn.metrics import classification_report, confusion_matrix

# Load labeled data
# Calculate metrics on holdout set
# Compare before/after calibration
```

## Changing Providers

To switch from Cerebras to a different primary provider:

1. **Update `.env`**
```bash
PRIMARY_MODEL=openrouter/anthropic/claude-3-haiku:free
```

2. **Adjust rate limits**
```bash
OPENROUTER_RPM=10
```

3. **Restart n8n**
```bash
docker-compose restart n8n
```

The workflow reads these environment variables; no workflow editing needed.

## Extending Watermark Detection

The watermark detection endpoint is a placeholder. To add real watermark detectors:

1. **Implement detector in `detector/watermark_detectors.py`**

```python
def detect_synthid(text: str) -> dict:
    # Real SynthID detection logic
    return {
        'detected': True,
        'provider': 'Google',
        'watermark_type': 'SynthID',
        'score': 0.92,
        'confidence': 0.88
    }
```

2. **Update `app.py` watermark endpoint**

```python
@app.route('/watermark/detect', methods=['POST'])
def detect_watermark():
    text = request.get_json()['text']
    
    # Try each detector
    result = detect_synthid(text)
    if result['detected']:
        return jsonify(result)
    
    # Try other detectors...
    
    return jsonify({'detected': False})
```

3. **Restart detector service**

```bash
docker-compose restart detector
```

## Extending Provenance Checking

To add C2PA / content credentials verification:

1. **Install c2pa-python or similar**
```bash
pip install c2pa-python
```

2. **Implement in `detector/provenance.py`**

```python
from c2pa import verify_file

def check_c2pa_provenance(file_path: str) -> dict:
    manifest = verify_file(file_path)
    return {
        'found': True,
        'format': 'C2PA',
        'signature_valid': manifest.is_valid,
        'ai_claim': 'ai_generative_training' in manifest.assertions
    }
```

3. **Update workflow to handle file uploads**
   - Add file upload handling to webhook
   - Save temporary file
   - Call provenance check with file path

## Monitoring

View logs:
```bash
docker-compose logs -f detector
docker-compose logs -f n8n
```

Check health:
```bash
curl http://localhost:5000/health
curl http://localhost:5678/healthz
```

Database metrics:
```bash
docker-compose exec postgres psql -U detector -d ai_detector -c "SELECT * FROM provider_performance;"
```

## Troubleshooting

### Provider Rate Limits

If you hit rate limits:
- Check `provider_usage` table for rate limit events
- Reduce rate limit configs in `.env`
- Implement request queuing/throttling

### Model Errors

If models return errors:
- Check OmniRoute logs
- Verify API keys are valid
- Try fallback model
- Check `provider_usage` for error details

### Cache Issues

If cache isn't working:
- Verify `CACHE_ENABLED=true`
- Check PostgreSQL connection
- Review `text_hash` normalization

### Low Accuracy

If detection accuracy is poor:
- Collect more labeled examples
- Calibrate thresholds with real data
- Adjust ensemble weights
- Try different models
- Add domain-specific features

## Development

Run tests:
```bash
cd detector
pip install -r requirements.txt
pytest tests/ -v
```

Run detector service locally:
```bash
export DATABASE_URL=postgresql://detector:password@localhost:5432/ai_detector
python app.py
```

## Security Notes

- **Never log sensitive text** in production (set `LOG_SENSITIVE_TEXT=false`)
- **Rotate API keys** regularly
- **Use strong passwords** for database and n8n
- **Enable HTTPS** in production (configure reverse proxy)
- **Validate input** - max text length enforced
- **Rate limit webhooks** to prevent abuse

## License

[Your License Here]

## Support

For issues or questions:
- Open a GitHub issue
- Check documentation at [your-docs-url]
- Email: [your-email]
