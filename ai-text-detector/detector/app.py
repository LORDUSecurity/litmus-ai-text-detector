"""
AI Text Detection API
Flask application providing detection services
"""

import os
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from dotenv import load_dotenv

from features import TextFeatureExtractor, calculate_statistical_probability
from classifier import EnsembleClassifier, validate_model_response

# Load environment
load_dotenv()

app = Flask(__name__)
CORS(app)

# Configuration
DATABASE_URL = os.getenv('DATABASE_URL')
CACHE_ENABLED = os.getenv('CACHE_ENABLED', 'true').lower() == 'true'
CACHE_TTL_HOURS = int(os.getenv('CACHE_TTL_HOURS', '168'))
MIN_TEXT_LENGTH = int(os.getenv('MIN_TEXT_LENGTH', '10'))
MAX_TEXT_LENGTH = int(os.getenv('MAX_TEXT_LENGTH', '50000'))

# Versions
DETECTOR_VERSION = os.getenv('DETECTOR_VERSION', '1.0.0')
FEATURE_SCHEMA_VERSION = os.getenv('FEATURE_SCHEMA_VERSION', '1.0.0')
CLASSIFIER_VERSION = os.getenv('CLASSIFIER_VERSION', '1.0.0')
THRESHOLD_VERSION = os.getenv('THRESHOLD_VERSION', '1.0.0')

# Initialize extractors
feature_extractor = TextFeatureExtractor()
ensemble_classifier = EnsembleClassifier()


def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def compute_text_hash(text: str) -> str:
    """Compute SHA-256 hash of normalized text"""
    normalized = ' '.join(text.split())  # Normalize whitespace
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def check_cache(text_hash: str) -> Optional[Dict[str, Any]]:
    """Check if result exists in cache"""
    if not CACHE_ENABLED:
        return None
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT * FROM analysis_results
                    WHERE text_hash = %s
                      AND detector_version = %s
                      AND feature_schema_version = %s
                      AND classifier_version = %s
                      AND threshold_version = %s
                      AND created_at > NOW() - INTERVAL '%s hours'
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (
                    text_hash,
                    DETECTOR_VERSION,
                    FEATURE_SCHEMA_VERSION,
                    CLASSIFIER_VERSION,
                    THRESHOLD_VERSION,
                    CACHE_TTL_HOURS
                ))
                
                row = cur.fetchone()
                if row:
                    return dict(row)
                return None
    except Exception as e:
        app.logger.error(f"Cache lookup error: {e}")
        return None


def save_result(document_id: str, text_hash: str, result: Dict[str, Any]) -> str:
    """Save analysis result to database"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Get version_id
                cur.execute("""
                    SELECT id FROM detector_versions
                    WHERE detector_version = %s
                      AND feature_schema_version = %s
                      AND classifier_version = %s
                      AND threshold_version = %s
                    LIMIT 1
                """, (
                    DETECTOR_VERSION,
                    FEATURE_SCHEMA_VERSION,
                    CLASSIFIER_VERSION,
                    THRESHOLD_VERSION
                ))
                version_row = cur.fetchone()
                version_id = version_row['id'] if version_row else None
                
                # Insert result
                cur.execute("""
                    INSERT INTO analysis_results (
                        document_id,
                        text_hash,
                        classification,
                        ai_probability,
                        confidence,
                        method,
                        provenance_json,
                        watermark_json,
                        features_json,
                        model_results_json,
                        review_required,
                        detector_version,
                        feature_schema_version,
                        classifier_version,
                        threshold_version,
                        version_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                """, (
                    document_id,
                    text_hash,
                    result['classification'],
                    result['ai_probability'],
                    result['confidence'],
                    result['method'],
                    Json(result['evidence'].get('provenance', {})),
                    Json(result['evidence'].get('watermark', {})),
                    Json(result['evidence'].get('statistics', {})),
                    Json(result['evidence'].get('models', [])),
                    result['review_required'],
                    DETECTOR_VERSION,
                    FEATURE_SCHEMA_VERSION,
                    CLASSIFIER_VERSION,
                    THRESHOLD_VERSION,
                    version_id
                ))
                
                result_id = cur.fetchone()['id']
                conn.commit()
                return result_id
    except Exception as e:
        app.logger.error(f"Database save error: {e}")
        raise


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        
        return jsonify({
            'status': 'healthy',
            'version': DETECTOR_VERSION,
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 503


@app.route('/features/extract', methods=['POST'])
def extract_features():
    """Extract statistical features from text"""
    data = request.get_json()
    
    if not data or 'text' not in data:
        return jsonify({'error': 'Missing text field'}), 400
    
    text = data['text']
    
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        return jsonify({'error': f'Text too short (min {MIN_TEXT_LENGTH} characters)'}), 400
    
    if len(text) > MAX_TEXT_LENGTH:
        return jsonify({'error': f'Text too long (max {MAX_TEXT_LENGTH} characters)'}), 400
    
    try:
        features = feature_extractor.extract_all(text)
        statistical_probability = calculate_statistical_probability(features)
        
        return jsonify({
            'features': features,
            'statistical_probability': round(statistical_probability, 4),
            'text_hash': compute_text_hash(text)
        })
    except Exception as e:
        app.logger.error(f"Feature extraction error: {e}")
        return jsonify({'error': 'Feature extraction failed'}), 500


@app.route('/classify', methods=['POST'])
def classify_text():
    """
    Classify text using ensemble of statistical features and model scores.
    
    Expects:
    {
        "features": {...},
        "statistical_probability": 0.5,
        "primary_model_result": {...},
        "secondary_model_result": {...} (optional)
    }
    """
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Validate inputs
    if 'statistical_probability' not in data:
        return jsonify({'error': 'Missing statistical_probability'}), 400
    
    if 'primary_model_result' not in data:
        return jsonify({'error': 'Missing primary_model_result'}), 400
    
    try:
        stat_prob = float(data['statistical_probability'])
        if not (0 <= stat_prob <= 1):
            return jsonify({'error': 'statistical_probability must be in [0, 1]'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid statistical_probability'}), 400
    
    # Validate primary model result
    primary_result = data['primary_model_result']
    is_valid, error = validate_model_response(primary_result)
    if not is_valid:
        return jsonify({'error': f'Invalid primary_model_result: {error}'}), 400
    
    # Validate secondary if present
    secondary_result = data.get('secondary_model_result')
    if secondary_result:
        is_valid, error = validate_model_response(secondary_result)
        if not is_valid:
            return jsonify({'error': f'Invalid secondary_model_result: {error}'}), 400
    
    # Perform classification
    try:
        result = ensemble_classifier.classify(
            stat_prob,
            primary_result,
            secondary_result
        )
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"Classification error: {e}")
        return jsonify({'error': 'Classification failed'}), 500


@app.route('/watermark/detect', methods=['POST'])
def detect_watermark():
    """
    Watermark detection endpoint.
    
    This is a placeholder interface. Implement specific watermark detectors
    for different watermark types (SynthID, OpenAI, etc.)
    """
    data = request.get_json()
    
    if not data or 'text' not in data:
        return jsonify({'error': 'Missing text field'}), 400
    
    # Placeholder response
    # Real implementation would check for specific watermark signatures
    return jsonify({
        'detected': False,
        'provider': None,
        'watermark_type': None,
        'score': None,
        'confidence': None,
        'detector_version': DETECTOR_VERSION,
        'message': 'No watermark detectors currently configured'
    })


@app.route('/provenance/check', methods=['POST'])
def check_provenance():
    """
    Check for cryptographic provenance (C2PA, signatures, etc.)
    
    This is a placeholder. Real implementation would:
    - Parse binary file if provided
    - Verify C2PA manifests
    - Check cryptographic signatures
    - Extract verified metadata
    """
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Placeholder response
    return jsonify({
        'found': False,
        'format': None,
        'signature_present': False,
        'signature_valid': False,
        'ai_claim': False,
        'issuer': None,
        'details': {},
        'message': 'Provenance checking not yet implemented'
    })


@app.route('/cache/lookup', methods=['POST'])
def cache_lookup():
    """Check cache for existing result"""
    data = request.get_json()
    
    if not data or 'text_hash' not in data:
        return jsonify({'error': 'Missing text_hash'}), 400
    
    cached = check_cache(data['text_hash'])
    
    if cached:
        return jsonify({
            'found': True,
            'result': cached
        })
    else:
        return jsonify({
            'found': False
        })


@app.route('/result/save', methods=['POST'])
def save_analysis_result():
    """Save analysis result to database"""
    data = request.get_json()
    
    required_fields = ['document_id', 'text_hash', 'result']
    if not all(field in data for field in required_fields):
        return jsonify({'error': f'Missing required fields: {required_fields}'}), 400
    
    try:
        result_id = save_result(
            data['document_id'],
            data['text_hash'],
            data['result']
        )
        
        return jsonify({
            'success': True,
            'result_id': result_id
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/review/submit', methods=['POST'])
def submit_review():
    """Submit human review label"""
    data = request.get_json()
    
    required_fields = ['analysis_result_id', 'reviewer_label']
    if not all(field in data for field in required_fields):
        return jsonify({'error': f'Missing required fields: {required_fields}'}), 400
    
    valid_labels = ['AI', 'HUMAN', 'INCONCLUSIVE', 'MIXED', 'INVALID']
    if data['reviewer_label'] not in valid_labels:
        return jsonify({'error': f'Invalid reviewer_label. Must be one of: {valid_labels}'}), 400
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Get original analysis
                cur.execute("""
                    SELECT document_id, text_hash, classification, 
                           ai_probability, confidence, features_json, model_results_json
                    FROM analysis_results
                    WHERE id = %s
                """, (data['analysis_result_id'],))
                
                analysis = cur.fetchone()
                if not analysis:
                    return jsonify({'error': 'Analysis result not found'}), 404
                
                # Insert review label
                cur.execute("""
                    INSERT INTO review_labels (
                        analysis_result_id,
                        document_id,
                        text_hash,
                        automatic_classification,
                        automatic_probability,
                        automatic_confidence,
                        reviewer_label,
                        reviewer_id,
                        reviewer_notes,
                        review_confidence,
                        text_sample,
                        features_json,
                        model_scores_json
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                """, (
                    data['analysis_result_id'],
                    analysis['document_id'],
                    analysis['text_hash'],
                    analysis['classification'],
                    analysis['ai_probability'],
                    analysis['confidence'],
                    data['reviewer_label'],
                    data.get('reviewer_id'),
                    data.get('reviewer_notes'),
                    data.get('review_confidence'),
                    data.get('text_sample'),
                    analysis['features_json'],
                    analysis['model_results_json']
                ))
                
                review_id = cur.fetchone()['id']
                
                # Update analysis review status
                cur.execute("""
                    UPDATE analysis_results
                    SET review_status = 'reviewed', updated_at = NOW()
                    WHERE id = %s
                """, (data['analysis_result_id'],))
                
                conn.commit()
                
                return jsonify({
                    'success': True,
                    'review_id': review_id
                })
    except Exception as e:
        app.logger.error(f"Review submission error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/stats', methods=['GET'])
def get_stats():
    """Get detection statistics"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(*) FILTER (WHERE classification = 'AI_GENERATED') as ai_count,
                        COUNT(*) FILTER (WHERE classification = 'HUMAN_LIKELY') as human_count,
                        COUNT(*) FILTER (WHERE classification = 'REVIEW_REQUIRED') as review_count,
                        AVG(ai_probability) as avg_probability,
                        AVG(confidence) as avg_confidence
                    FROM analysis_results
                    WHERE created_at > NOW() - INTERVAL '7 days'
                """)
                
                stats = cur.fetchone()
                
                return jsonify(dict(stats))
    except Exception as e:
        app.logger.error(f"Stats error: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv('DETECTOR_SERVICE_PORT', '5000'))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
