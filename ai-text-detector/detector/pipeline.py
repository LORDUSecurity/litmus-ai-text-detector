#!/usr/bin/env python3
"""
AI Text Detection Pipeline Wrapper
Runs full pipeline: features -> OmniRoute models -> ensemble classification
"""

import os
import json
import requests
from typing import Dict, Any, Optional

# Configuration from environment
DETECTOR_URL = os.getenv("DETECTOR_URL", "http://localhost:5000")
OMNIROUTE_URL = os.getenv("OMNIROUTE_BASE_URL", "http://localhost:8080/v1")
OMNIROUTE_KEY = os.getenv("OMNIROUTE_API_KEY", "")
PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "cerebras/llama3.1-8b")
SECONDARY_MODEL = os.getenv("SECONDARY_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")

CLASSIFICATION_PROMPT = """You are an AI text detection classifier. Analyze the provided statistical features and return ONLY valid JSON with no markdown formatting. Your response must validate against this exact schema:
{"ai_probability": number 0-1, "confidence": number 0-1, "signals": [{"name": string, "direction": "AI" or "HUMAN", "strength": number 0-1}]}"""

def extract_features(text: str) -> Dict[str, Any]:
    """Extract statistical features from text via detector service."""
    response = requests.post(
        f"{DETECTOR_URL}/features/extract",
        json={"text": text},
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def call_omniroute(model: str, features: Dict[str, Any]) -> Dict[str, Any]:
    """Call OmniRoute for model classification."""
    if not OMNIROUTE_KEY:
        raise ValueError("OMNIROUTE_API_KEY not configured")
    
    user_content = f"Analyze these statistical features and determine AI generation probability:\n\n{json.dumps(features, indent=2)}\n\nReturn ONLY valid JSON with ai_probability (0-1), confidence (0-1), and signals array. No markdown, no explanation."
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLASSIFICATION_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,
        "max_tokens": 500,
        "response_format": {"type": "json_object"}
    }
    
    response = requests.post(
        f"{OMNIROUTE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {OMNIROUTE_KEY}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )
    response.raise_for_status()
    
    result = response.json()
    content = result["choices"][0]["message"]["content"]
    return json.loads(content)


def validate_model_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize model response."""
    required = ["ai_probability", "confidence", "signals"]
    for field in required:
        if field not in response:
            raise ValueError(f"Missing required field: {field}")
    
    # Clamp values
    response["ai_probability"] = max(0.0, min(1.0, float(response["ai_probability"])))
    response["confidence"] = max(0.0, min(1.0, float(response["confidence"])))
    
    return response


def classify_ensemble(
    statistical_prob: float,
    primary_result: Dict[str, Any],
    secondary_result: Dict[str, Any],
    stat_weight: float = 0.60,
    primary_weight: float = 0.25,
    secondary_weight: float = 0.15
) -> Dict[str, Any]:
    """Combine results using ensemble weights."""
    ensemble_prob = (
        stat_weight * statistical_prob +
        primary_weight * primary_result["ai_probability"] +
        secondary_weight * secondary_result["ai_probability"]
    )
    
    confidence = (primary_result["confidence"] + secondary_result["confidence"]) / 2
    disagreement = abs(primary_result["ai_probability"] - secondary_result["ai_probability"])
    
    # Decision thresholds (configurable)
    AI_THRESHOLD = 0.85
    HUMAN_THRESHOLD = 0.20
    MIN_CONFIDENCE = 0.70
    MAX_DISAGREEMENT = 0.30
    
    if ensemble_prob >= AI_THRESHOLD and confidence >= MIN_CONFIDENCE and disagreement <= MAX_DISAGREEMENT:
        classification = "AI_GENERATED"
        method = "ENSEMBLE_HIGH_CONFIDENCE"
    elif ensemble_prob <= HUMAN_THRESHOLD and confidence >= MIN_CONFIDENCE and disagreement <= MAX_DISAGREEMENT:
        classification = "HUMAN_LIKELY"
        method = "ENSEMBLE_LOW_AI_SIGNAL"
    elif disagreement > MAX_DISAGREEMENT:
        classification = "REVIEW_REQUIRED"
        method = "HIGH_MODEL_DISAGREEMENT"
    elif confidence < 0.5:
        classification = "REVIEW_REQUIRED"
        method = "LOW_CONFIDENCE"
    elif 0.35 <= ensemble_prob <= 0.65:
        classification = "REVIEW_REQUIRED"
        method = "UNCERTAIN_GRAY_ZONE"
    elif ensemble_prob > 0.65:
        classification = "REVIEW_REQUIRED"
        method = "MODERATE_AI_SIGNAL"
    else:
        classification = "REVIEW_REQUIRED"
        method = "MODERATE_HUMAN_SIGNAL"
    
    return {
        "classification": classification,
        "ai_probability": round(ensemble_prob, 4),
        "confidence": round(confidence, 4),
        "method": method,
        "review_required": classification == "REVIEW_REQUIRED",
        "component_scores": {
            "statistical_probability": round(statistical_prob, 4),
            "primary_probability": round(primary_result["ai_probability"], 4),
            "secondary_probability": round(secondary_result["ai_probability"], 4),
            "primary_confidence": round(primary_result["confidence"], 4),
            "secondary_confidence": round(secondary_result["confidence"], 4),
            "model_disagreement": round(disagreement, 4)
        },
        "weights_used": {
            "statistical": stat_weight,
            "primary_model": primary_weight,
            "secondary_model": secondary_weight
        }
    }


def detect_text(text: str, save_result: bool = True) -> Dict[str, Any]:
    """
    Run full detection pipeline on text.
    
    Args:
        text: Text to analyze
        save_result: Whether to save to detector service database
        
    Returns:
        Full classification result with evidence
    """
    # 1. Extract features
    features_response = extract_features(text)
    features = features_response["features"]
    statistical_prob = features_response["statistical_probability"]
    text_hash = features_response["text_hash"]
    
    # 2. Call primary model (Cerebras via OmniRoute)
    try:
        primary_result = call_omniroute(PRIMARY_MODEL, features)
        primary_result = validate_model_response(primary_result)
    except Exception as e:
        print(f"Warning: Primary model failed: {e}")
        primary_result = {"ai_probability": 0.5, "confidence": 0.5, "signals": []}
    
    # 3. Call secondary model (NVIDIA NIM via OmniRoute)
    try:
        secondary_result = call_omniroute(SECONDARY_MODEL, features)
        secondary_result = validate_model_response(secondary_result)
    except Exception as e:
        print(f"Warning: Secondary model failed: {e}")
        secondary_result = {"ai_probability": 0.5, "confidence": 0.5, "signals": []}
    
    # 4. Ensemble classification
    result = classify_ensemble(
        statistical_prob,
        primary_result,
        secondary_result
    )
    
    # 5. Build full response
    full_result = {
        "document_id": f"doc_{text_hash[:12]}",
        "text_hash": text_hash,
        **result,
        "evidence": {
            "provenance": {},
            "watermark": {},
            "statistics": features,
            "models": [
                {"provider": "primary", "model": PRIMARY_MODEL, **primary_result},
                {"provider": "secondary", "model": SECONDARY_MODEL, **secondary_result}
            ]
        },
        "warning": "Human-likely means no strong AI-generation signal was detected; it does not prove human authorship.",
        "versions": {
            "detector": os.getenv("DETECTOR_VERSION", "1.0.0"),
            "features": os.getenv("FEATURE_SCHEMA_VERSION", "1.0.0"),
            "classifier": os.getenv("CLASSIFIER_VERSION", "1.0.0"),
            "thresholds": os.getenv("THRESHOLD_VERSION", "1.0.0")
        }
    }
    
    # 5. Save to database if requested
    if save_result:
        try:
            save_response = requests.post(
                f"{DETECTOR_URL}/result/save",
                json={
                    "document_id": full_result["document_id"],
                    "text_hash": text_hash,
                    "result": full_result
                },
                timeout=10
            )
            save_response.raise_for_status()
        except Exception as e:
            print(f"Warning: Failed to save result: {e}")
    
    return full_result


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python pipeline.py \"Your text here\"")
        print("Or set TEXT environment variable")
        sys.exit(1)
    
    text = sys.argv[1]
    
    # Check environment
    if not OMNIROUTE_KEY:
        print("ERROR: OMNIROUTE_API_KEY not set in environment")
        print("Set it in .env or export OMNIROUTE_API_KEY=your_key")
        sys.exit(1)
    
    print(f"Analyzing text ({len(text)} chars)...")
    
    try:
        result = detect_text(text)
        
        print(f"\n{'='*60}")
        print(f"Classification: {result['classification']}")
        print(f"AI Probability: {result['ai_probability']:.2%}")
        print(f"Confidence: {result['confidence']:.2%}")
        print(f"Method: {result['method']}")
        print(f"Review Required: {result['review_required']}")
        print(f"\nComponent Scores:")
        for k, v in result['component_scores'].items():
            print(f"  {k}: {v:.4f}")
        print(f"\nModels Used:")
        for m in result['evidence']['models']:
            print(f"  {m['provider']} ({m['model']}): {m['ai_probability']:.2%} (conf: {m['confidence']:.2%})")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)