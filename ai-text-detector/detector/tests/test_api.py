#!/usr/bin/env python3
"""
Tests for Flask API endpoints
"""

import pytest
import json
from app import app as flask_app


@pytest.fixture
def client():
    """Create test client"""
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as client:
        yield client


def test_health_check(client):
    """Test health check endpoint"""
    response = client.get('/health')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] in ['healthy', 'unhealthy']


def test_extract_features_success(client):
    """Test successful feature extraction"""
    response = client.post('/features/extract',
                          json={'text': 'This is a test sentence for feature extraction. It has multiple sentences.'})
    
    assert response.status_code == 200
    data = json.loads(response.data)
    
    assert 'features' in data
    assert 'statistical_probability' in data
    assert 'text_hash' in data
    assert data['features']['word_count'] > 0


def test_extract_features_missing_text(client):
    """Test feature extraction with missing text"""
    response = client.post('/features/extract', json={})
    assert response.status_code == 400


def test_extract_features_text_too_short(client):
    """Test feature extraction with too-short text"""
    response = client.post('/features/extract', json={'text': 'Short'})
    assert response.status_code == 400


def test_classify_success(client):
    """Test successful classification"""
    payload = {
        'statistical_probability': 0.65,
        'primary_model_result': {
            'ai_probability': 0.70,
            'confidence': 0.75
        },
        'secondary_model_result': {
            'ai_probability': 0.68,
            'confidence': 0.73
        }
    }
    
    response = client.post('/classify', json=payload)
    assert response.status_code == 200
    
    data = json.loads(response.data)
    assert 'classification' in data
    assert 'ai_probability' in data
    assert 'confidence' in data
    assert data['classification'] in ['AI_GENERATED', 'HUMAN_LIKELY', 'REVIEW_REQUIRED']


def test_classify_invalid_probability(client):
    """Test classification with invalid probability"""
    payload = {
        'statistical_probability': 1.5,  # Invalid
        'primary_model_result': {
            'ai_probability': 0.70,
            'confidence': 0.75
        }
    }
    
    response = client.post('/classify', json=payload)
    assert response.status_code == 400


def test_classify_missing_primary(client):
    """Test classification missing primary model result"""
    payload = {
        'statistical_probability': 0.65
    }
    
    response = client.post('/classify', json=payload)
    assert response.status_code == 400


def test_watermark_detect(client):
    """Test watermark detection (placeholder)"""
    response = client.post('/watermark/detect', json={'text': 'Some text'})
    assert response.status_code == 200
    
    data = json.loads(response.data)
    assert 'detected' in data
    assert 'detector_version' in data


def test_provenance_check(client):
    """Test provenance checking (placeholder)"""
    response = client.post('/provenance/check', json={'metadata': {}})
    assert response.status_code == 200
    
    data = json.loads(response.data)
    assert 'found' in data
    assert 'signature_valid' in data


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
