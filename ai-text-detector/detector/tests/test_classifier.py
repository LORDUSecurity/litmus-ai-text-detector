"""
Tests for Ensemble Classifier
"""

import pytest
from classifier import EnsembleClassifier, EnsembleWeights, Thresholds, validate_model_response


@pytest.fixture
def classifier():
    return EnsembleClassifier()


def test_validate_model_response_valid():
    """Test validation of valid model response"""
    response = {
        'ai_probability': 0.75,
        'confidence': 0.85,
        'signals': []
    }
    
    is_valid, error = validate_model_response(response)
    assert is_valid
    assert error is None


def test_validate_model_response_missing_field():
    """Test validation with missing field"""
    response = {
        'ai_probability': 0.75
        # Missing confidence
    }
    
    is_valid, error = validate_model_response(response)
    assert not is_valid
    assert 'confidence' in error


def test_validate_model_response_out_of_range():
    """Test validation with out-of-range values"""
    response = {
        'ai_probability': 1.5,  # Invalid
        'confidence': 0.85
    }
    
    is_valid, error = validate_model_response(response)
    assert not is_valid


def test_ensemble_ai_generated(classifier):
    """Test AI_GENERATED classification"""
    statistical_prob = 0.90
    primary = {'ai_probability': 0.88, 'confidence': 0.85}
    secondary = {'ai_probability': 0.87, 'confidence': 0.83}
    
    result = classifier.classify(statistical_prob, primary, secondary)
    
    assert result['classification'] == 'AI_GENERATED'
    assert result['ai_probability'] > 0.85
    assert result['confidence'] > 0.70


def test_ensemble_human_likely(classifier):
    """Test HUMAN_LIKELY classification"""
    statistical_prob = 0.10
    primary = {'ai_probability': 0.15, 'confidence': 0.80}
    secondary = {'ai_probability': 0.12, 'confidence': 0.82}
    
    result = classifier.classify(statistical_prob, primary, secondary)
    
    assert result['classification'] == 'HUMAN_LIKELY'
    assert result['ai_probability'] < 0.20


def test_ensemble_review_high_disagreement(classifier):
    """Test REVIEW_REQUIRED due to high disagreement"""
    statistical_prob = 0.50
    primary = {'ai_probability': 0.30, 'confidence': 0.75}
    secondary = {'ai_probability': 0.75, 'confidence': 0.75}  # 0.45 disagreement
    
    result = classifier.classify(statistical_prob, primary, secondary)
    
    assert result['classification'] == 'REVIEW_REQUIRED'
    assert result['method'] == 'HIGH_MODEL_DISAGREEMENT'


def test_ensemble_review_low_confidence(classifier):
    """Test REVIEW_REQUIRED due to low confidence"""
    statistical_prob = 0.50
    primary = {'ai_probability': 0.50, 'confidence': 0.30}
    secondary = {'ai_probability': 0.52, 'confidence': 0.35}
    
    result = classifier.classify(statistical_prob, primary, secondary)
    
    assert result['classification'] == 'REVIEW_REQUIRED'
    assert result['method'] == 'LOW_CONFIDENCE'


def test_ensemble_weights_sum():
    """Test that weights sum to 1.0"""
    weights = EnsembleWeights(
        statistical=0.60,
        primary_model=0.25,
        secondary_model=0.15
    )
    
    total = weights.statistical + weights.primary_model + weights.secondary_model
    assert abs(total - 1.0) < 0.001


def test_ensemble_weights_invalid_sum():
    """Test that invalid weights raise error"""
    with pytest.raises(AssertionError):
        EnsembleWeights(
            statistical=0.50,
            primary_model=0.30,
            secondary_model=0.30  # Sum = 1.10
        )


def test_ensemble_no_secondary_model(classifier):
    """Test classification with only primary model"""
    statistical_prob = 0.70
    primary = {'ai_probability': 0.75, 'confidence': 0.80}
    
    result = classifier.classify(statistical_prob, primary, None)
    
    assert 'classification' in result
    assert result['component_scores']['secondary_probability'] == 0.75  # Uses primary


def test_ensemble_probability_calculation():
    """Test weighted probability calculation"""
    weights = EnsembleWeights(
        statistical=0.60,
        primary_model=0.25,
        secondary_model=0.15
    )
    
    classifier = EnsembleClassifier(weights=weights)
    
    result = classifier.classify(
        statistical_probability=0.80,
        primary_model_result={'ai_probability': 0.70, 'confidence': 0.75},
        secondary_model_result={'ai_probability': 0.60, 'confidence': 0.75}
    )
    
    expected = 0.60 * 0.80 + 0.25 * 0.70 + 0.15 * 0.60
    assert abs(result['ai_probability'] - expected) < 0.01


def test_thresholds_from_env(monkeypatch):
    """Test loading thresholds from environment"""
    monkeypatch.setenv('AI_PROBABILITY_THRESHOLD', '0.90')
    monkeypatch.setenv('AI_CONFIDENCE_THRESHOLD', '0.75')
    
    thresholds = Thresholds.from_env()
    
    assert thresholds.ai_probability == 0.90
    assert thresholds.ai_confidence == 0.75


def test_component_scores_included(classifier):
    """Test that component scores are included in result"""
    result = classifier.classify(
        statistical_probability=0.60,
        primary_model_result={'ai_probability': 0.65, 'confidence': 0.70},
        secondary_model_result={'ai_probability': 0.62, 'confidence': 0.68}
    )
    
    assert 'component_scores' in result
    assert 'statistical_probability' in result['component_scores']
    assert 'primary_probability' in result['component_scores']
    assert 'model_disagreement' in result['component_scores']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
