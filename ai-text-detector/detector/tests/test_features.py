"""
Tests for Statistical Feature Extraction
"""

import pytest
from features import TextFeatureExtractor, calculate_statistical_probability


@pytest.fixture
def extractor():
    return TextFeatureExtractor()


def test_empty_text(extractor):
    """Test feature extraction on empty text"""
    features = extractor.extract_all("")
    assert features['word_count'] == 0
    assert features['sentence_count'] == 0


def test_simple_text(extractor):
    """Test feature extraction on simple text"""
    text = "Hello world. This is a test. It has three sentences."
    features = extractor.extract_all(text)
    
    assert features['sentence_count'] == 3
    assert features['word_count'] == 11
    assert features['paragraph_count'] == 1
    assert features['unique_word_count'] == 11  # All unique


def test_repeated_text(extractor):
    """Test repetition detection"""
    text = "The quick brown fox. The quick brown fox. The quick brown fox."
    features = extractor.extract_all(text)
    
    assert features['repetition_ratio'] > 0.5  # High repetition


def test_ai_like_text(extractor):
    """Test AI-indicative features"""
    text = """
    It's important to note that machine learning has fundamentally changed 
    the landscape. However, it's worth noting that significant challenges 
    remain. Moreover, the technology continues to evolve. In conclusion, 
    we must carefully consider these developments.
    """
    
    features = extractor.extract_all(text)
    
    assert features['hedging_phrase_count'] > 2
    assert features['formal_starter_count'] >= 2


def test_statistical_probability_short():
    """Test probability calculation for short text"""
    features = {'word_count': 5, 'sentence_count': 1}
    prob = calculate_statistical_probability(features)
    assert prob == 0.5  # Insufficient data


def test_statistical_probability_ai_signals():
    """Test probability calculation with AI signals"""
    features = {
        'word_count': 100,
        'sentence_count': 5,
        'sentence_length_uniformity': 0.8,  # High uniformity
        'hedging_phrase_density': 0.4,      # Many hedging phrases
        'formal_starter_count': 2,
        'repetition_ratio': 0.03,           # Low repetition
        'type_token_ratio': 0.5,
        'sentence_length_stddev': 2,
        'average_sentence_length': 20,
    }
    
    prob = calculate_statistical_probability(features)
    assert prob > 0.5  # Should indicate AI


def test_statistical_probability_human_signals():
    """Test probability calculation with human signals"""
    features = {
        'word_count': 100,
        'sentence_count': 8,
        'sentence_length_uniformity': 0.2,  # Low uniformity
        'hedging_phrase_density': 0.05,     # Few hedging
        'formal_starter_count': 0,
        'repetition_ratio': 0.18,           # More repetition
        'type_token_ratio': 0.45,
        'sentence_length_stddev': 8,
        'average_sentence_length': 12,
    }
    
    prob = calculate_statistical_probability(features)
    assert prob < 0.5  # Should indicate human-like


def test_type_token_ratio(extractor):
    """Test type-token ratio calculation"""
    # All unique words
    text1 = "The quick brown fox jumps over lazy dog."
    features1 = extractor.extract_all(text1)
    assert features1['type_token_ratio'] == 1.0
    
    # Repeated words
    text2 = "The the the fox fox fox."
    features2 = extractor.extract_all(text2)
    assert features2['type_token_ratio'] < 0.5


def test_sentence_length_stats(extractor):
    """Test sentence length statistics"""
    text = "Short. A bit longer sentence here. Very long sentence with many words in it."
    features = extractor.extract_all(text)
    
    assert features['average_sentence_length'] > 0
    assert features['median_sentence_length'] > 0
    assert features['sentence_length_variance'] > 0


def test_punctuation_density(extractor):
    """Test punctuation density calculation"""
    text_low = "Hello world"
    text_high = "Hello, world! How are you? I'm fine, thanks."
    
    features_low = extractor.extract_all(text_low)
    features_high = extractor.extract_all(text_high)
    
    assert features_high['punctuation_density'] > features_low['punctuation_density']


def test_structural_features(extractor):
    """Test structural feature detection"""
    text = """
    # Heading
    
    - Bullet one
    - Bullet two
    
    Check this URL: https://example.com
    
    Someone said "this is a quote".
    """
    
    features = extractor.extract_all(text)
    
    assert features['heading_count'] >= 1
    assert features['bullet_count'] >= 2
    assert features['url_count'] >= 1
    assert features['quotation_count'] >= 2


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
