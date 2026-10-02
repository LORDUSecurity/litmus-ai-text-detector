# Test Data and Examples

This directory contains sample texts for testing the AI text detection pipeline.

## Sample Test Cases

### 1. Human-Written Text (human_sample.txt)
Informal, personal blog-style writing with natural variation.

### 2. AI-Generated Text (ai_sample.txt)
Typical AI-generated content with formal structure and hedging language.

### 3. Edge Cases

- Very short text (< 50 words)
- Very long text (> 10,000 words)
- Mixed content (human with AI sections)
- Technical documentation
- Poetry and creative writing
- Social media posts

## Running Tests

```bash
cd detector

# Extract features
curl -X POST http://localhost:5000/features/extract \
  -H "Content-Type: application/json" \
  -d @examples/human_sample.json

# Full detection pipeline
curl -X POST http://localhost:5678/webhook/ai-detect \
  -H "Content-Type: application/json" \
  -d @examples/ai_sample.json
```

## Test Results Format

Expected response structure:
```json
{
  "document_id": "doc_123",
  "classification": "AI_GENERATED",
  "ai_probability": 0.87,
  "confidence": 0.82,
  "method": "ENSEMBLE_HIGH_CONFIDENCE",
  "review_required": false,
  "evidence": {
    "statistics": {...},
    "models": [...]
  }
}
```

## Calibration Dataset

For calibration, collect at least 100-200 labeled examples:
- 50+ confirmed AI-generated
- 50+ confirmed human-written
- 25+ uncertain/mixed cases

Structure:
```json
{
  "text": "...",
  "true_label": "AI|HUMAN|MIXED",
  "source": "ChatGPT|GPT-4|Human Author|etc",
  "domain": "blog|news|technical|creative",
  "length_words": 250
}
```
