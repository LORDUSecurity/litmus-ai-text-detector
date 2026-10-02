# AI Text Detection - Statistical Features Module

import hashlib
import re
from typing import Dict, List, Any


def normalize_text(text: str) -> str:
    """
    Normalize text for analysis while preserving essential content.
    - Remove excessive whitespace
    - Normalize line breaks
    - Preserve meaningful spacing
    """
    # Normalize line breaks
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\r', '\n', text)
    text = re.sub(r'\n+', '\n\n', text)
    
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text


def calculate_hash(text: str) -> str:
    """
    Calculate SHA-256 hash of normalized text.
    """
    normalized = normalize_text(text)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def count_sentences(text: str) -> int:
    """
    Count the number of sentences in text.
    """
    # Split on sentence-ending punctuation
    sentences = re.split(r'[.!?]+', text)
    # Filter out empty strings
    sentences = [s.strip() for s in sentences if s.strip()]
    return len(sentences)


def count_paragraphs(text: str) -> int:
    """
    Count the number of paragraphs in text.
    """
    # Split on double newlines
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    return len(paragraphs)


def count_words(text: str) -> int:
    """
    Count the number of words in text.
    """
    words = text.split()
    return len(words)


def count_character(text: str) -> int:
    """
    Count the number of characters in text.
    """
    return len(text)


def calculate_average_word_length(text: str) -> float:
    """
    Calculate average word length.
    """
    words = text.split()
    if not words:
        return 0.0
    return sum(len(word) for word in words) / len(words)


def calculate_average_sentence_length(text: str) -> float:
    """
    Calculate average sentence length.
    """
    sentences = count_sentences(text)
    if sentences == 0:
        return 0.0
    return count_character(text) / sentences


def calculate_median_sentence_length(text: str) -> float:
    """
    Calculate median sentence length.
    """
    sentences = count_sentences(text)
    if sentences == 0:
        return 0.0
    
    # Get sentence lengths
    lengths = []
    for sentence in re.split(r'[.!?]+', text):
        sentence = sentence.strip()
        if sentence:
            lengths.append(len(sentence))
    
    if not lengths:
        return 0.0
    
    lengths.sort()
    n = len(lengths)
    if n % 2 == 0:
        return (lengths[n//2 - 1] + lengths[n//2]) / 2
    else:
        return lengths[n//2]


def calculate_sentence_length_variance(text: str) -> float:
    """
    Calculate variance of sentence lengths.
    """
    sentences = count_sentences(text)
    if sentences <= 1:
        return 0.0
    
    lengths = []
    for sentence in re.split(r'[.!?]+', text):
        sentence = sentence.strip()
        if sentence:
            lengths.append(len(sentence))
    
    if len(lengths) <= 1:
        return 0.0
    
    mean = sum(lengths) / len(lengths)
    variance = sum((x - mean) ** 2 for x in lengths) / len(lengths)
    return variance


def calculate_sentence_length_stddev(text: str) -> float:
    """
    Calculate standard deviation of sentence lengths.
    """
    return calculate_sentence_length_variance(text) ** 0.5


def calculate_type_token_ratio(text: str) -> float:
    """
    Calculate type-token ratio (unique words / total words).
    """
    words = text.split()
    if not words:
        return 0.0
    
    unique_words = len(set(words))
    return unique_words / len(words)


def calculate_repetition_ratio(text: str) -> float:
    """
    Calculate repetition ratio (total words / unique words).
    """
    words = text.split()
    if not words:
        return 0.0
    
    unique_words = len(set(words))
    if unique_words == 0:
        return 0.0
    
    return len(words) / unique_words


def calculate_hapax_ratio(text: str) -> float:
    """
    Calculate hapax ratio (words that appear only once / total words).
    """
    words = text.split()
    if not words:
        return 0.0
    
    from collections import Counter
    word_counts = Counter(words)
    hapax_words = sum(1 for count in word_counts.values() if count == 1)
    return hapax_words / len(words)


def calculate_punctuation_density(text: str) -> float:
    """
    Calculate punctuation density (punctuation marks / total characters).
    """
    if not text:
        return 0.0
    
    punctuation = sum(1 for char in text if char in '.,;:!?"\'')
    return punctuation / len(text)


def calculate_digit_density(text: str) -> float:
    """
    Calculate digit density (digits / total characters).
    """
    if not text:
        return 0.0
    
    digits = sum(1 for char in text if char.isdigit())
    return digits / len(text)


def calculate_uppercase_density(text: str) -> float:
    """
    Calculate uppercase letter density (uppercase letters / total characters).
    """
    if not text:
        return 0.0
    
    uppercase = sum(1 for char in text if char.isupper())
    return uppercase / len(text)


def calculate_heading_frequency(text: str) -> int:
    """
    Count heading frequency (lines starting with uppercase followed by lowercase).
    """
    if not text:
        return 0
    
    lines = text.split('\n')
    headings = 0
    for line in lines:
        line = line.strip()
        if line and line[0].isupper() and len(line) > 1 and line[1].islower():
            headings += 1
    
    return headings


def calculate_hedging_phrase_count(text: str) -> int:
    """
    Count hedging phrases commonly used in AI-generated text.
    """
    if not text:
        return 0
    
    hedging_phrases = [
        'it is important to note',
        'it should be noted',
        'it is worth noting',
        'it is crucial to understand',
        'it is essential to recognize',
        'it is necessary to consider',
        'it is vital to acknowledge',
        'it is worth mentioning',
        'notably',
        'significantly',
        'importantly',
        'moreover',
        'furthermore',
        'additionally',
        'consequently',
        'therefore',
        'thus',
        'hence',
        'accordingly',
        'in conclusion',
        'in summary',
        'to summarize',
        'overall',
        'ultimately',
        'in essence',
        'fundamentally',
        'basically',
        'essentially',
        'generally speaking',
        'broadly speaking',
        'typically',
        'usually',
        'commonly',
        'often',
        'frequently',
        'generally',
        'in most cases',
        'for the most part',
        'by and large',
        'on the whole',
        'all things considered',
        'taking everything into account',
        'it should be emphasized',
        'it bears mentioning',
        'it goes without saying',
        'needless to say',
        'obviously',
        'clearly',
        'evidently',
        'apparently',
        'presumably',
        'supposedly',
        'allegedly',
        'reportedly',
        'seemingly',
        'ostensibly',
    ]
    
    text_lower = text.lower()
    count = 0
    for phrase in hedging_phrases:
        count += text_lower.count(phrase.lower())
    
    return count


def calculate_formal_starter_count(text: str) -> int:
    """
    Count formal sentence starters (transition phrases).
    """
    if not text:
        return 0
    
    import re
    sentences = re.split(r'[.!?]+', text)
    
    formal_starters = [
        'moreover',
        'furthermore',
        'additionally',
        'consequently',
        'therefore',
        'thus',
        'hence',
        'accordingly',
        'nevertheless',
        'nonetheless',
        'however',
        'in contrast',
        'on the other hand',
        'conversely',
        'similarly',
        'likewise',
        'in addition',
        'for example',
        'for instance',
        'specifically',
        'in particular',
        'notably',
        'importantly',
        'significantly',
        'first',
        'second',
        'third',
        'finally',
        'lastly',
        'subsequently',
        'previously',
        'earlier',
        'ultimately',
        'overall',
        'in conclusion',
        'to conclude',
        'in summary',
        'to summarize',
    ]
    
    count = 0
    for sentence in sentences:
        sentence = sentence.strip().lower()
        if not sentence:
            continue
        first_words = ' '.join(sentence.split()[:3])
        for starter in formal_starters:
            if first_words.startswith(starter):
                count += 1
                break
    
    return count


def calculate_contraction_count(text: str) -> int:
    """
    Count contractions (informal language marker).
    """
    if not text:
        return 0
    
    import re
    contractions = re.findall(r"\b\w+'(?:t|s|re|ve|ll|d|m)\b", text.lower())
    return len(contractions)


def calculate_bullet_frequency(text: str) -> int:
    """
    Count bullet frequency (lines starting with common bullet characters).
    """
    if not text:
        return 0
    
    bullets = 0
    bullet_starts = ['•', '-', '*', '•', '◦', '‣', '›']
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if line and any(line.startswith(bullet) for bullet in bullet_starts):
            bullets += 1
    
    return bullets


def calculate_url_frequency(text: str) -> int:
    """
    Count URL frequency (using simple regex pattern).
    """
    if not text:
        return 0
    
    import re
    url_pattern = r'https?://[^\s]+'
    return len(re.findall(url_pattern, text))


def calculate_quotation_frequency(text: str) -> int:
    """
    Count quotation frequency (quotes, both single and double).
    """
    if not text:
        return 0
    
    import re
    quote_pattern = r'["\']{2,}["\']'
    # Simple count of quote pairs
    double_quotes = text.count('"')
    single_quotes = text.count("'")
    return (double_quotes + single_quotes) // 2


def extract_statistical_features(text: str) -> Dict[str, Any]:
    """
    Extract all statistical features from text.
    """
    if not text:
        return {}
    
    normalized_text = normalize_text(text)
    
    features = {
        "character_count": count_character(normalized_text),
        "word_count": count_words(normalized_text),
        "sentence_count": count_sentences(normalized_text),
        "paragraph_count": count_paragraphs(normalized_text),
        "average_word_length": calculate_average_word_length(normalized_text),
        "average_sentence_length": calculate_average_sentence_length(normalized_text),
        "median_sentence_length": calculate_median_sentence_length(normalized_text),
        "sentence_length_variance": calculate_sentence_length_variance(normalized_text),
        "sentence_length_stddev": calculate_sentence_length_stddev(normalized_text),
        "type_token_ratio": calculate_type_token_ratio(normalized_text),
        "unique_word_count": len(set(normalized_text.split())),
        "repetition_ratio": calculate_repetition_ratio(normalized_text),
        "hapax_ratio": calculate_hapax_ratio(normalized_text),
        "punctuation_density": calculate_punctuation_density(normalized_text),
        "digit_density": calculate_digit_density(normalized_text),
        "uppercase_density": calculate_uppercase_density(normalized_text),
        "heading_frequency": calculate_heading_frequency(normalized_text),
        "heading_count": calculate_heading_frequency(normalized_text),  # Alias
        "bullet_frequency": calculate_bullet_frequency(normalized_text),
        "bullet_count": calculate_bullet_frequency(normalized_text),  # Alias
        "url_frequency": calculate_url_frequency(normalized_text),
        "url_count": calculate_url_frequency(normalized_text),  # Alias
        "quotation_frequency": calculate_quotation_frequency(normalized_text),
        "quotation_count": calculate_quotation_frequency(normalized_text),  # Alias
        "hedging_phrase_count": calculate_hedging_phrase_count(normalized_text),
        "hedging_phrase_density": calculate_hedging_phrase_count(normalized_text) / max(count_words(normalized_text), 1),
        "formal_starter_count": calculate_formal_starter_count(normalized_text),
        "contraction_count": calculate_contraction_count(normalized_text),
    }
    
    return features


def calculate_model_derived_statistics(token_log_probs: List[float]) -> Dict[str, Any]:
    """
    Calculate model-derived statistics from token-level log probabilities.
    
    Args:
        token_log_probs: List of log probabilities for each token
        
    Returns:
        Dictionary with calculated statistics
    """
    if not token_log_probs:
        return {}
    
    import numpy as np
    
    log_probs = np.array(token_log_probs)
    
    return {
        "mean_token_log_probability": float(np.mean(log_probs)),
        "median_token_log_probability": float(np.median(log_probs)),
        "log_probability_variance": float(np.var(log_probs)),
        "negative_log_likelihood": float(-np.mean(log_probs)),
        "entropy_like_statistics": float(-np.sum(log_probs * np.exp(log_probs)) / np.sum(np.exp(log_probs))) if len(log_probs) > 0 else 0.0
    }


def validate_features(features: Dict[str, Any]) -> bool:
    """
    Validate that all features have reasonable values.

    Args:
        features: Dictionary of extracted features
        
    Returns:
        Boolean indicating if features are valid
    """
    # Basic validation - all counts should be non-negative
    for key, value in features.items():
        if isinstance(value, (int, float)) and value < 0:
            return False

    # Text-based features should be reasonable
    if features.get("character_count", 0) > 10_000_000:
        return False

    return True


class TextFeatureExtractor:
    """Extract statistical features from text for AI detection."""
    
    def __init__(self):
        pass
    
    def extract_all(self, text: str) -> Dict[str, Any]:
        """Extract all statistical features from text."""
        return extract_statistical_features(text)


def calculate_statistical_probability(features: Dict[str, Any]) -> float:
    """
    Calculate AI probability from statistical features.
    
    This is a heuristic-based calculation. For production, calibrate with labeled data.
    
    Returns:
        Probability between 0 (human) and 1 (AI)
    """
    if not features:
        return 0.5
    
    # Check minimum data requirements
    word_count = features.get('word_count', 0)
    sentence_count = features.get('sentence_count', 0)
    
    if word_count < 50 or sentence_count < 3:
        # Insufficient data for reliable statistical analysis
        return 0.5
    
    # AI indicators (increase probability)
    ai_score = 0.0
    
    # High sentence length uniformity (low variance)
    sent_variance = features.get('sentence_length_variance', 0)
    avg_sent_len = features.get('average_sentence_length', 0)
    if avg_sent_len > 0:
        sent_uniformity = 1.0 - min(sent_variance / (avg_sent_len * 0.5), 1.0)
        if sent_uniformity > 0.7:  # Very uniform sentence lengths
            ai_score += 0.15
    
    # High hedging phrase density
    hedging_density = features.get('hedging_phrase_density', 0)
    if hedging_density > 0.3:
        ai_score += 0.15
    elif hedging_density > 0.15:
        ai_score += 0.10
    elif hedging_density > 0.05:
        ai_score += 0.05
    
    # High formal starter count
    formal_starters = features.get('formal_starter_count', 0)
    if formal_starters > 5:
        ai_score += 0.10
    elif formal_starters > 2:
        ai_score += 0.05
    
    # Low repetition ratio (AI tends to repeat less)
    repetition = features.get('repetition_ratio', 0)
    if repetition < 0.05:
        ai_score += 0.10
    elif repetition < 0.10:
        ai_score += 0.05
    
    # High type-token ratio (diverse vocabulary)
    ttr = features.get('type_token_ratio', 0)
    if ttr > 0.7:
        ai_score += 0.05
    
    # Human indicators (decrease probability)
    human_score = 0.0
    
    # High sentence length variation
    if sent_variance > avg_sent_len * 0.3 and avg_sent_len > 0:
        human_score += 0.15
    
    # Low hedging phrases
    if hedging_density < 0.03:
        human_score += 0.10
    
    # High repetition (human-like)
    if repetition > 0.15:
        human_score += 0.10
    elif repetition > 0.10:
        human_score += 0.05
    
    # Informal language markers
    contractions = features.get('contraction_count', 0)
    if contractions > 3:
        human_score += 0.05
    
    # Base probability
    base_prob = 0.5
    
    # Combine scores
    probability = base_prob + ai_score - human_score
    
    # Clamp to [0, 1]
    return max(0.0, min(1.0, probability))