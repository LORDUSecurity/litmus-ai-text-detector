# API Keys & Configuration

## Where to Insert API Keys

### 1. OmniRoute Configuration

Edit `.env`:

```bash
OMNIROUTE_BASE_URL=http://your-omniroute-instance:8080/v1
OMNIROUTE_API_KEY=sk-...your-actual-key...
```

**Obtain from:**
- Set up OmniRoute instance (self-hosted or managed)
- Generate API key in OmniRoute admin panel
- Test with: `curl -H "Authorization: Bearer $OMNIROUTE_API_KEY" $OMNIROUTE_BASE_URL/models`

### 2. Database Password

Edit `.env`:

```bash
DB_PASSWORD=your-secure-random-password-here
```

**Generate secure password:**
```bash
openssl rand -base64 32
```

### 3. n8n Authentication

Edit `.env`:

```bash
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=your-secure-password-here
```

After first login, create additional users in n8n UI.

### 4. (Optional) Direct Provider Keys

If running OmniRoute locally, you may need individual provider keys:

```bash
# Not used if proxying through OmniRoute, but shown for reference:
# CEREBRAS_API_KEY=sk-...
# NVIDIA_API_KEY=...
# OPENROUTER_API_KEY=sk-or-...
```

## Environment Variable Reference

### Required

| Variable | Example | Description |
|----------|---------|-------------|
| `OMNIROUTE_BASE_URL` | `http://localhost:8080/v1` | OmniRoute endpoint |
| `OMNIROUTE_API_KEY` | `sk-xxx` | OmniRoute authentication |
| `DB_PASSWORD` | Random 32-char | PostgreSQL root password |
| `N8N_BASIC_AUTH_PASSWORD` | Random | n8n admin password |

### Models (Optional - use defaults or customize)

| Variable | Default | Options |
|----------|---------|---------|
| `PRIMARY_MODEL` | `cerebras/llama3.1-8b` | Any Cerebras model |
| `SECONDARY_MODEL` | `nvidia/llama-3.1-nemotron-70b-instruct` | Any NVIDIA NIM model |
| `FALLBACK_MODEL` | `openrouter/meta-llama/llama-3.2-3b-instruct:free` | OpenRouter free models |

### Rate Limits (Update based on actual provider limits)

```bash
CEREBRAS_RPM=60              # Requests per minute
CEREBRAS_TPM=1000000         # Tokens per minute
CEREBRAS_RPD=unlimited        # Requests per day

NVIDIA_RPM=30
NVIDIA_TPM=50000

OPENROUTER_RPM=20
OPENROUTER_RPD=200
```

**Find actual limits:**
- Cerebras: https://docs.cerebras.ai/quickstart (free tier docs)
- NVIDIA NIM: https://docs.nvidia.com/nim/ (free tier info)
- OpenRouter: https://openrouter.ai/docs#limits (your account page shows current limits)

### Detection Thresholds

```bash
AI_PROBABILITY_THRESHOLD=0.85        # Probability >= this → AI_GENERATED
AI_CONFIDENCE_THRESHOLD=0.70         # Confidence >= this required

HUMAN_PROBABILITY_THRESHOLD=0.20     # Probability <= this → HUMAN_LIKELY
HUMAN_CONFIDENCE_THRESHOLD=0.70      # Confidence >= this required

MAX_MODEL_DISAGREEMENT=0.30          # |primary - secondary| > this → REVIEW_REQUIRED
```

**Calibrate these** with labeled validation data (see `docs/calibration.md`).

### Ensemble Weights (must sum to 1.0)

```bash
STATISTICAL_WEIGHT=0.60      # Statistical features importance
PRIMARY_MODEL_WEIGHT=0.25    # Primary LLM importance
SECONDARY_MODEL_WEIGHT=0.15  # Secondary LLM importance
```

## Changing Free Providers

### Switch Primary Model

To change from Cerebras to OpenRouter:

1. **Edit `.env`**:
```bash
PRIMARY_MODEL=openrouter/meta-llama/llama-3.1-70b-instruct:free
```

2. **Update rate limits** in `.env`:
```bash
# Reduce from Cerebras limits
OPENROUTER_RPM=10
OPENROUTER_RPD=100
```

3. **No workflow changes needed** — n8n reads `PRIMARY_MODEL` from environment

4. **Restart**:
```bash
docker-compose restart n8n
```

### Switch Secondary Model

Same process, but update `SECONDARY_MODEL`.

### Add a Third Model (Fallback)

The workflow currently doesn't use `FALLBACK_MODEL`, but it's configured for future extension. To implement:

1. Add error handling to primary/secondary nodes
2. Call fallback if both models fail
3. Modify ensemble logic to weight 3 models

## Provider Comparison

| Provider | Free Tier | Rate Limit | Model Quality | Notes |
|----------|-----------|-----------|---------------|-------|
| **Cerebras** | Yes | 60 RPM, 1M TPM | Good (Llama 3.1) | Recommended primary |
| **NVIDIA NIM** | Yes | 30 RPM, 50K TPM | Excellent (Nemotron) | Recommended secondary |
| **OpenRouter** | Yes (select models) | 20 RPM, 200 RPD | Varies | Good fallback pool |

## Monitoring Provider Status

```bash
# Check provider usage in database
docker-compose exec postgres psql -U detector -d ai_detector \
  -c "SELECT provider, model, COUNT(*) as requests, 
           SUM(CASE WHEN error_occurred THEN 1 ELSE 0 END) as errors 
       FROM provider_usage 
       WHERE created_at > NOW() - INTERVAL '1 day' 
       GROUP BY provider, model;"

# View provider performance stats
docker-compose exec postgres psql -U detector -d ai_detector \
  -c "SELECT * FROM provider_performance LIMIT 10;"
```

## Troubleshooting

### "Invalid API Key"
- Verify `OMNIROUTE_API_KEY` is correct
- Check OmniRoute is running and accessible at `OMNIROUTE_BASE_URL`
- Test: `curl -H "Authorization: Bearer $OMNIROUTE_API_KEY" "$OMNIROUTE_BASE_URL/models"`

### "Rate Limit Exceeded"
- Check actual provider limits (may be lower than configured)
- Reduce `*_RPM` and `*_TPM` in `.env`
- Consider adding request queuing

### "Model Not Found"
- Verify model name is correct: `cerebras/llama3.1-8b` (case-sensitive)
- Check provider offers this model in free tier
- Test in OmniRoute directly

### Fallback Chain Strategy

If a provider becomes unavailable:

```
PRIMARY (Cerebras) fails
  ↓
SECONDARY (NVIDIA) attempts
  ↓
FALLBACK (OpenRouter) attempts
  ↓
Returns DETECTION_UNAVAILABLE
```

Currently the workflow calls both primary and secondary in parallel. To implement true fallback chain, modify the workflow to:
1. Try primary
2. If error, try secondary
3. If error, try fallback

This requires updating n8n node error handling.
