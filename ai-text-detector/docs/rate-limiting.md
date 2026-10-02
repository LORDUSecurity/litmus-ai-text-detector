# Rate Limiting Guide

## Overview

Rate limiting prevents abuse and respects free-tier provider quotas. The system implements:

1. **Configuration-based limits** — all limits in `.env`, no hardcoding
2. **Per-provider throttling** — respect each provider's quotas
3. **Error recovery** — detect rate-limit events and fail gracefully

## Provider Quotas

### Cerebras (Free Tier)

```bash
CEREBRAS_RPM=60              # 60 requests per minute
CEREBRAS_TPM=1000000         # 1M tokens per minute
CEREBRAS_RPD=unlimited        # Unlimited per day
```

**How to verify:**
- Check Cerebras console after API key creation
- Docs: https://docs.cerebras.ai/

### NVIDIA NIM (Free Tier)

```bash
NVIDIA_RPM=30               # 30 requests per minute
NVIDIA_TPM=50000            # 50K tokens per minute
```

**How to verify:**
- NVIDIA NIM free tier documentation
- Your account details at nvidia.com/nim

### OpenRouter (Free Tier)

```bash
OPENROUTER_RPM=20           # 20 requests per minute
OPENROUTER_RPD=200          # 200 requests per day
```

**How to verify:**
- OpenRouter dashboard shows your actual limits
- Limits vary by model (free models have lower limits)

## Implementing Rate Limiting in n8n

The workflow implements rate limiting through:

1. **Sequential processing** — not unlimited parallel requests
2. **Provider tracking** — log each request to database
3. **Retry with backoff** — exponential backoff on rate-limit errors

### n8n Retry Logic

In `main-workflow.json`, model nodes have:

```json
"options": {
  "retry": {
    "enabled": true,
    "maxTries": 3,
    "waitBetweenTries": 2000
  }
}
```

This retries with 2-second delay between attempts (exponential in HTTP client).

### Database Tracking

Every API call is logged:

```sql
SELECT 
    provider,
    model,
    DATE_TRUNC('minute', created_at) as minute,
    COUNT(*) as requests,
    SUM(CASE WHEN rate_limited THEN 1 ELSE 0 END) as rate_limited_events
FROM provider_usage
WHERE created_at > NOW() - INTERVAL '1 hour'
GROUP BY provider, model, minute
ORDER BY minute DESC;
```

## Detecting Rate Limits

### HTTP 429 Response

```
HTTP/1.1 429 Too Many Requests
Retry-After: 60
```

The detector service logs these events:

```python
if response.status_code == 429:
    # Log rate limit event
    provider_usage.rate_limited = True
    provider_usage.error_occurred = True
```

### Symptoms

- Detection requests returning 429 errors
- Provider usage table shows `rate_limited = true`
- Workflow retries exhausted

### Monitor Current Usage

```bash
# Real-time provider usage (last 5 minutes)
docker-compose exec postgres psql -U detector -d ai_detector << 'EOF'
SELECT 
    provider,
    model,
    COUNT(*) as requests_last_5min,
    MAX(created_at) as latest_at
FROM provider_usage
WHERE created_at > NOW() - INTERVAL '5 minutes'
GROUP BY provider, model;
EOF
```

## Adjusting Limits

### When to Reduce Limits

**Symptom:** Consistent 429 errors
**Action:** Reduce `*_RPM` by 20-30%

```bash
# Before
CEREBRAS_RPM=60

# After (hit 429 limit)
CEREBRAS_RPM=45
```

Restart services:
```bash
docker-compose restart n8n
```

### When to Increase Limits

**Symptom:** Unused quota, fast processing
**Action:** Increase `*_RPM` by 10-20%

```bash
# Before
CEREBRAS_RPM=45

# After (verified no rate limits)
CEREBRAS_RPM=55
```

## Advanced: Request Queuing

For high-volume detection, implement a request queue:

```python
# detector/queue.py
import redis
import json
from datetime import datetime, timedelta

class RateLimitedQueue:
    def __init__(self, provider: str, rpm: int):
        self.provider = provider
        self.rpm = rpm
        self.redis = redis.Redis()
        
    def can_request(self) -> bool:
        """Check if request allowed under rate limit"""
        key = f"rate_limit:{self.provider}"
        count = self.redis.get(key)
        
        if count is None or int(count) < self.rpm:
            self.redis.incr(key)
            self.redis.expire(key, 60)
            return True
        return False
    
    def wait_if_needed(self):
        """Block until rate limit allows request"""
        import time
        while not self.can_request():
            time.sleep(1)
```

Usage in workflow:

```python
queue = RateLimitedQueue('cerebras', 60)
queue.wait_if_needed()
# Now safe to call API
```

## Provider Fallback

When a provider hits rate limits, automatically try secondary:

```json
{
  "id": "primary-model-with-fallback",
  "name": "Primary Model (with fallback)",
  "type": "n8n-nodes-base.if",
  "parameters": {
    "conditions": {
      "boolean": [
        {
          "value1": "={{$json.error_code === 429}}",
          "value2": true
        }
      ]
    }
  }
}
```

If primary returns 429, route to secondary model.

## Cost Implications

**Free tier** means **no charges**, but:
- Rate limits are strictly enforced
- Exceeding limits blocks requests temporarily (no overage charges)
- Providers reserve right to revoke free access
- Monitor usage to stay within bounds

### Cost Estimation

At current limits:

| Provider | Requests/Day | Avg Tokens | Cost |
|----------|-------------|-----------|------|
| Cerebras (60 RPM) | 86,400 | 1M tokens | Free |
| NVIDIA (30 RPM) | 43,200 | 50K tokens | Free |
| OpenRouter (200 RPD) | 200 | Varies | Free |

**Total:** ~130K requests/day, entirely free-tier.

For 1,000 daily detections:
- ~3 requests per detection (primary + secondary + features)
- ~3,000 requests/day
- **Well within free limits**

## Monitoring Dashboard

Query for rate limit status:

```bash
# Create a simple dashboard
docker-compose exec postgres psql -U detector -d ai_detector << 'EOF'
SELECT 
    provider,
    model,
    AVG(response_latency_ms) as avg_latency_ms,
    COUNT(*) FILTER (WHERE error_occurred) as errors_24h,
    COUNT(*) FILTER (WHERE rate_limited) as rate_limits_24h,
    COUNT(*) as total_requests_24h
FROM provider_usage
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY provider, model
ORDER BY total_requests_24h DESC;
EOF
```

Export this query as a scheduled job to monitor trends.

## Respecting Provider Terms

- **Don't abuse free tier** — it's provided for testing/low-volume use
- **Monitor your usage** — aim for 10-20% of quota, not 90%
- **Rotate providers** — if one limits you, switch to another
- **Upgrade when needed** — if you outgrow free tier, consider paid plans
- **Read their ToS** — each provider has specific free-tier conditions

## Troubleshooting

### "Rate limit exceeded" in logs

Check current usage:
```sql
SELECT COUNT(*) as requests_per_minute
FROM provider_usage
WHERE created_at > NOW() - INTERVAL '1 minute'
  AND provider = 'cerebras';
```

If > configured RPM, reduce limits in `.env`.

### Requests queuing up

If you see many retries:
- Increase `maxTries` or `waitBetweenTries` in n8n
- Implement request queue (see Advanced section)
- Consider processing fewer concurrent requests

### Provider returned 503

Not a rate limit (429), but service unavailable. Workflow retries automatically. If persistent, check provider status page.
