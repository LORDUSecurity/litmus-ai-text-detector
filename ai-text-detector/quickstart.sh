#!/usr/bin/env bash
# Quick Start Script for AI Text Detection Pipeline

set -e

echo "=== AI Text Detection Pipeline - Quick Start ==="
echo

# Check if .env exists
if [ ! -f .env ]; then
    echo "Creating .env from template..."
    cp .env.example .env
    echo "✓ .env created"
    echo
    echo "⚠️  IMPORTANT: Edit .env and add your API keys before continuing!"
    echo "   Required: OMNIROUTE_BASE_URL, OMNIROUTE_API_KEY, DB_PASSWORD"
    echo
    read -p "Press Enter when ready to continue..."
fi

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker first."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose not found. Please install docker-compose first."
    exit 1
fi

echo "Starting services..."
cd docker
docker-compose up -d

echo
echo "Waiting for services to be healthy..."
sleep 10

# Check health
echo "Checking detector service..."
curl -s http://localhost:5000/health | python3 -m json.tool || echo "⚠️  Detector not ready yet"

echo
echo "Checking n8n..."
curl -s http://localhost:5678/healthz > /dev/null && echo "✓ n8n is ready" || echo "⚠️  n8n not ready yet"

echo
echo "=== Setup Complete ==="
echo
echo "Next steps:"
echo "1. Open n8n: http://localhost:5678"
echo "2. Login with credentials from .env (default: admin / change-this-password)"
echo "3. Import workflows from n8n/main-workflow.json and n8n/review-workflow.json"
echo "4. Activate both workflows"
echo "5. Test the API:"
echo
echo "   curl -X POST http://localhost:5678/webhook/ai-detect \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d @examples/ai_sample.json"
echo
echo "View logs:"
echo "  docker-compose logs -f"
echo
echo "Stop services:"
echo "  docker-compose down"
