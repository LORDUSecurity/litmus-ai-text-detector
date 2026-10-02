# Project Structure

```
ai-text-detector/
├── README.md                          # Main documentation
├── .env.example                       # Environment template (copy to .env)
├── quickstart.sh                      # Quick start script
│
├── n8n/                               # n8n workflow files
│   ├── main-workflow.json             # Main detection pipeline
│   └── review-workflow.json           # Human review queue
│
├── detector/                          # Python detector service
│   ├── app.py                         # Flask API application
│   ├── features.py                    # Statistical feature extraction
│   ├── classifier.py                  # Ensemble classification logic
│   ├── requirements.txt               # Python dependencies
│   ├── Dockerfile                     # Container image definition
│   │
│   └── tests/                         # Test suite
│       ├── test_features.py           # Feature extraction tests
│       ├── test_classifier.py         # Ensemble tests
│       └── test_api.py                # API endpoint tests
│
├── database/                          # Database schema
│   └── schema.sql                     # PostgreSQL schema definition
│
├── docker/                            # Docker configuration
│   └── docker-compose.yml             # Multi-service orchestration
│
├── docs/                              # Documentation
│   ├── api-keys.md                    # API key configuration
│   ├── rate-limiting.md               # Rate limit guide
│   ├── calibration.md                 # Threshold calibration
│   └── production-deployment.md       # Production setup
│
└── examples/                          # Test data
    ├── human_sample.json              # Human-written example
    ├── ai_sample.json                 # AI-generated example
    └── README.md                      # Examples guide
```

## Key Files

### Configuration
- `.env.example` - Copy to `.env` and fill in API keys

### n8n Workflows
- `n8n/main-workflow.json` - Main detection pipeline (import into n8n)
- `n8n/review-workflow.json` - Review queue workflow (import into n8n)

### Python Service
- `detector/app.py` - REST API endpoints
- `detector/features.py` - Statistical analysis
- `detector/classifier.py` - Ensemble decision logic

### Database
- `database/schema.sql` - PostgreSQL tables and views

### Docker
- `docker/docker-compose.yml` - Start entire stack

## File Naming Conventions

- `*.json` - Configuration and workflow files
- `*.py` - Python source code
- `*.sql` - Database schemas and queries
- `*.md` - Documentation
- `*.yml` / `*.yaml` - Docker Compose
- `Dockerfile` - Container image definition
- `requirements.txt` - Python dependencies
