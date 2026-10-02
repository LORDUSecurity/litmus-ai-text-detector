# Deployment & Production Setup

## Pre-Production Checklist

- [ ] All API keys configured in `.env` (never commit `.env` to git)
- [ ] Database password randomized (not default)
- [ ] n8n admin password set to strong value
- [ ] Rate limits verified with actual provider quotas
- [ ] Docker and docker-compose installed
- [ ] OmniRoute instance running and accessible
- [ ] Test data validated (run example workflows)
- [ ] Database migrations applied
- [ ] Logs configured appropriately
- [ ] Backups configured for PostgreSQL

## Production Deployment

### 1. Secure Environment

```bash
# Create production .env
cp .env.example .env.prod

# Use strong passwords
# For PASSWORD, use: openssl rand -base64 32
# For API_KEY, generate from provider console

# Keep .env.prod out of version control
echo ".env.prod" >> .gitignore
```

### 2. Scale Infrastructure

For production workloads:

```yaml
# docker-compose.yml adjustments for production
services:
  detector:
    deploy:
      replicas: 3  # Multiple instances for HA
      resources:
        limits:
          cpus: '2'
          memory: 2G
  
  postgres:
    # Use managed PostgreSQL for production
    # Or configure volume for persistence
    volumes:
      - postgres_backup:/var/lib/postgresql/data
```

### 3. Enable HTTPS

```bash
# Install nginx/traefik for reverse proxy
# Configure SSL certificates (Let's Encrypt)
# Update N8N_PROTOCOL=https
```

### 4. Persistent Storage

```bash
# Use named volumes for data durability
volumes:
  postgres_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/data/postgres  # Host path
```

### 5. Monitoring & Alerting

```bash
# Set up monitoring
# - Prometheus for metrics
# - ELK stack for logs
# - Grafana for dashboards
```

Configure log shipping:

```yaml
# docker-compose.yml
services:
  detector:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

### 6. Backup Strategy

```bash
# Daily PostgreSQL backups
docker-compose exec postgres pg_dump -U detector ai_detector \
  | gzip > backups/ai_detector_$(date +%Y%m%d).sql.gz

# Store backups off-host
aws s3 cp backups/ai_detector_*.sql.gz s3://my-bucket/backups/
```

### 7. Upgrade Path

```bash
# Before updating images
docker-compose down
docker-compose pull

# Test in staging
docker-compose -f docker-compose.test.yml up

# Migrate database if needed
docker-compose exec postgres psql -U detector ai_detector < migrations/v1.1.sql

# Start production
docker-compose up -d
```

## High Availability

### Multi-Zone Deployment

```bash
# Run detector service in multiple zones
# Use load balancer (HAProxy, AWS ELB) to distribute
```

### Database Replication

```bash
# PostgreSQL primary-replica setup
# For production databases, use managed RDS or similar
```

### Failover Configuration

```bash
# Set up automatic failover
# - Primary detector fails → traffic routes to secondary
# - Primary database fails → replica promoted
```

## Security Hardening

### Network Isolation

```yaml
services:
  n8n:
    networks:
      - internal  # Only accessible from internal network
  
  detector:
    networks:
      - internal
    
  postgres:
    networks:
      - internal  # Never exposed publicly
```

### Secret Management

**Don't use `.env` files in production. Use:**

- **Kubernetes Secrets** (if using K8s)
- **AWS Secrets Manager**
- **HashiCorp Vault**
- **Docker Secrets** (Docker Swarm)

Example with Vault:

```bash
# Retrieve secrets at startup
export OMNIROUTE_API_KEY=$(vault kv get -field=api_key secret/omniroute)
export DB_PASSWORD=$(vault kv get -field=password secret/postgres)

docker-compose up -d
```

### Access Control

```bash
# Restrict n8n to internal IPs only
# - Use firewall rules
# - Configure reverse proxy authentication
# - Enable n8n SSO/SAML

N8N_AUTHENTICATION_METHOD=saml
N8N_SAML_METADATA_URL=https://identity.company.com/metadata
```

### Audit Logging

```sql
-- Track all review submissions
SELECT 
    reviewer_id,
    COUNT(*) as reviews,
    MIN(reviewed_at) as first_review,
    MAX(reviewed_at) as latest_review
FROM review_labels
GROUP BY reviewer_id;

-- Track API key usage
-- (Note: never log actual API keys)
SELECT 
    DATE(created_at) as date,
    provider,
    COUNT(*) as api_calls
FROM provider_usage
GROUP BY DATE(created_at), provider;
```

## Disaster Recovery

### Backup Frequency

- **Daily** — Full PostgreSQL dump to S3
- **Hourly** — n8n workflow backups
- **Real-time** — Database transaction logs to cloud storage

### Recovery Procedures

```bash
# Restore from backup
gunzip < backups/ai_detector_20260917.sql.gz | \
  docker-compose exec -T postgres psql -U detector ai_detector

# Restore n8n workflows
docker-compose exec n8n n8n export:workflow --backup
```

### RTO/RPO Targets

- **RTO** (Recovery Time Objective): < 1 hour
- **RPO** (Recovery Point Objective): < 1 hour of data loss

## Performance Tuning

### Database Optimization

```sql
-- Analyze performance
ANALYZE analysis_results;

-- Index optimization
CREATE INDEX CONCURRENTLY idx_analysis_results_created_at_partial
ON analysis_results(created_at DESC) 
WHERE review_required = true;

-- Vacuum regularly
VACUUM ANALYZE analysis_results;
```

### Connection Pooling

```yaml
# Use PgBouncer for connection pooling
pgbouncer:
  image: pgbouncer:latest
  environment:
    DATABASES_HOST: postgres
    DATABASES_POOL_SIZE: 25
```

### Caching Strategy

For repeated detections:

```bash
# Redis caching layer
services:
  redis:
    image: redis:alpine
    volumes:
      - redis_data:/data
```

Update detector to check Redis before database.

## Monitoring Metrics

Key metrics to track:

```
Availability:
- Uptime percentage
- Detection endpoint response times
- Provider API response times

Performance:
- Requests per second
- Average detection latency
- Cache hit rate

Errors:
- HTTP 5xx errors
- Provider rate-limit events
- Database connection failures

Business:
- Detections per day
- AI vs Human classification split
- Review queue depth
```

### Sample Prometheus Query

```promql
# Request rate by classification
rate(n8n_workflow_execution_total{classification="AI_GENERATED"}[5m])

# Error rate
rate(n8n_workflow_execution_errors_total[5m])

# Provider latency (p95)
histogram_quantile(0.95, rate(provider_request_duration_ms_bucket[5m]))
```

## Compliance

### Data Protection

- **GDPR**: Implement right to deletion
- **CCPA**: Provide data export functionality
- **HIPAA**: If processing medical text, ensure BAA and encryption

```sql
-- Right to deletion
DELETE FROM analysis_results 
WHERE document_id = $1 AND created_at < NOW() - INTERVAL '30 days';

DELETE FROM review_labels 
WHERE analysis_result_id IN (
  SELECT id FROM analysis_results WHERE document_id = $1
);
```

### Audit Trail

```sql
-- Who classified what, when
SELECT 
    ar.document_id,
    ar.classification,
    ar.created_at,
    rl.reviewer_id,
    rl.reviewed_at
FROM analysis_results ar
LEFT JOIN review_labels rl ON rl.analysis_result_id = ar.id
ORDER BY ar.created_at DESC;
```

## Support & Maintenance

### Runbook for Common Issues

**"Detector service unhealthy"**
1. Check logs: `docker-compose logs detector`
2. Verify database connection: `docker-compose exec detector curl localhost:5000/health`
3. Restart: `docker-compose restart detector`

**"n8n workflows failing"**
1. Check n8n logs: `docker-compose logs n8n`
2. Verify OmniRoute connection: `curl $OMNIROUTE_BASE_URL/models`
3. Check API key expiration

**"High detection latency"**
1. Check provider performance: `SELECT * FROM provider_performance;`
2. Check model disagreement: `SELECT * FROM model_agreement_stats;`
3. Consider reducing complexity or enabling caching

### Regular Maintenance

- **Weekly**: Review error logs, clear old provider_usage entries
- **Monthly**: Run database VACUUM, review performance metrics
- **Quarterly**: Recalibrate thresholds with new labeled data
- **Annually**: Security audit, update dependencies
