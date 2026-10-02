"""
Ensemble Classifier for AI Text Detection
Combines statistical features with model-based scoring
"""

import os
from typing import Dict, Any, Tuple, Optional
from dataclasses import dataclass


@dataclass
class EnsembleWeights:
    """Configurable weights for ensemble"""
    statistical: float = 0.60
    primary_model: float = 0.25
    secondary_model: float = 0.15
    
    def __post_init__(self):
        total = self.statistical + self.primary_model + self.secondary_model
        assert abs(total - 1.0) < 0.001, f"Weights must sum to 1.0, got {total}"


@dataclass
class Thresholds:
    """Configurable decision thresholds"""
    ai_probability: float = 0.85
    ai_confidence: float = 0.70
    human_probability: float = 0.20
    human_confidence: float = 0.70
    max_disagreement: float = 0.30
    
    @classmethod
    def from_env(cls):
        """Load thresholds from environment variables"""
        return cls(
            ai_probability=float(os.getenv('AI_PROBABILITY_THRESHOLD', '0.85')),
            ai_confidence=float(os.getenv('AI_CONFIDENCE_THRESHOLD', '0.70')),
            human_probability=float(os.getenv('HUMAN_PROBABILITY_THRESHOLD', '0.20')),
            human_confidence=float(os.getenv('HUMAN_CONFIDENCE_THRESHOLD', '0.70')),
            max_disagreement=float(os.getenv('MAX_MODEL_DISAGREEMENT', '0.30')),
        )


class EnsembleClassifier:
    """
    Combine statistical and model-based scores into final classification.
    """
    
    def __init__(self, weights: Optional[EnsembleWeights] = None, 
                 thresholds: Optional[Thresholds] = None):
        self.weights = weights or EnsembleWeights(
            statistical=float(os.getenv('STATISTICAL_WEIGHT', '0.60')),
            primary_model=float(os.getenv('PRIMARY_MODEL_WEIGHT', '0.25')),
            secondary_model=float(os.getenv('SECONDARY_MODEL_WEIGHT', '0.15')),
        )
        self.thresholds = thresholds or Thresholds.from_env()
    
    def classify(self, 
                 statistical_probability: float,
                 primary_model_result: Dict[str, Any],
                 secondary_model_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Perform ensemble classification.
        
        Args:
            statistical_probability: AI probability from statistical features (0-1)
            primary_model_result: Result from primary model
            secondary_model_result: Result from secondary model (optional)
        
        Returns:
            Classification result dictionary
        """
        # Extract model probabilities
        primary_prob = primary_model_result.get('ai_probability', 0.5)
        primary_conf = primary_model_result.get('confidence', 0.5)
        
        if secondary_model_result:
            secondary_prob = secondary_model_result.get('ai_probability', 0.5)
            secondary_conf = secondary_model_result.get('confidence', 0.5)
        else:
            # If no secondary, use primary for both
            secondary_prob = primary_prob
            secondary_conf = primary_conf
        
        # Calculate ensemble probability
        ensemble_probability = (
            self.weights.statistical * statistical_probability +
            self.weights.primary_model * primary_prob +
            self.weights.secondary_model * secondary_prob
        )
        
        # Calculate combined confidence
        confidence = (primary_conf + secondary_conf) / 2
        
        # Calculate model disagreement
        disagreement = abs(primary_prob - secondary_prob)
        
        # Make classification decision
        classification, method = self._decide_classification(
            ensemble_probability,
            confidence,
            disagreement
        )
        
        return {
            'classification': classification,
            'ai_probability': round(ensemble_probability, 4),
            'confidence': round(confidence, 4),
            'method': method,
            'review_required': classification == 'REVIEW_REQUIRED',
            'component_scores': {
                'statistical_probability': round(statistical_probability, 4),
                'primary_probability': round(primary_prob, 4),
                'secondary_probability': round(secondary_prob, 4),
                'primary_confidence': round(primary_conf, 4),
                'secondary_confidence': round(secondary_conf, 4),
                'model_disagreement': round(disagreement, 4),
            },
            'weights_used': {
                'statistical': self.weights.statistical,
                'primary_model': self.weights.primary_model,
                'secondary_model': self.weights.secondary_model,
            }
        }
    
    def _decide_classification(self, 
                               probability: float, 
                               confidence: float,
                               disagreement: float) -> Tuple[str, str]:
        """
        Apply thresholds to determine final classification.
        
        Returns:
            (classification, method) tuple
        """
        # Check for AI_GENERATED
        if (probability >= self.thresholds.ai_probability and
            confidence >= self.thresholds.ai_confidence and
            disagreement <= self.thresholds.max_disagreement):
            return 'AI_GENERATED', 'ENSEMBLE_HIGH_CONFIDENCE'
        
        # Check for HUMAN_LIKELY
        if (probability <= self.thresholds.human_probability and
            confidence >= self.thresholds.human_confidence and
            disagreement <= self.thresholds.max_disagreement):
            return 'HUMAN_LIKELY', 'ENSEMBLE_LOW_AI_SIGNAL'
        
        # Check specific review triggers
        if disagreement > self.thresholds.max_disagreement:
            return 'REVIEW_REQUIRED', 'HIGH_MODEL_DISAGREEMENT'
        
        if confidence < 0.5:
            return 'REVIEW_REQUIRED', 'LOW_CONFIDENCE'
        
        # Gray zone: uncertain
        if 0.35 <= probability <= 0.65:
            return 'REVIEW_REQUIRED', 'UNCERTAIN_GRAY_ZONE'
        
        # Edge cases
        if probability > 0.65:
            return 'REVIEW_REQUIRED', 'MODERATE_AI_SIGNAL'
        else:
            return 'REVIEW_REQUIRED', 'MODERATE_HUMAN_SIGNAL'


def validate_model_response(response: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate a model classification response.
    
    Returns:
        (is_valid, error_message) tuple
    """
    if not isinstance(response, dict):
        return False, "Response is not a dictionary"
    
    if 'ai_probability' not in response:
        return False, "Missing 'ai_probability' field"
    
    if 'confidence' not in response:
        return False, "Missing 'confidence' field"
    
    try:
        prob = float(response['ai_probability'])
        conf = float(response['confidence'])
    except (ValueError, TypeError):
        return False, "ai_probability or confidence is not a valid number"
    
    if not (0 <= prob <= 1):
        return False, f"ai_probability {prob} not in [0, 1]"
    
    if not (0 <= conf <= 1):
        return False, f"confidence {conf} not in [0, 1]"
    
    return True, None
