"""
Anomaly detector — Isolation Forest inference.

Phase 4: Provides anomaly detection for /api/predict/anomaly endpoint.
"""

import logging
from typing import Any

from app.models.loader import get_model, get_model_version

log = logging.getLogger(__name__)

_MODEL_NAME = "anomaly"


def predict(features: list[float]) -> dict:
    """
    Run anomaly detection on input features.
    
    Args:
        features: List of feature values
        
    Returns:
        Dict with is_anomaly, anomaly_score, and model_version
    """
    model = get_model(_MODEL_NAME)
    version = get_model_version(_MODEL_NAME)
    
    # Isolation Forest returns -1 for anomaly, 1 for normal
    # decision_function returns anomaly score (lower = more anomalous)
    prediction = model.predict([features])[0]
    score = model.decision_function([features])[0]
    
    is_anomaly = prediction == -1
    # Normalize score: more negative = more anomalous
    # Convert to 0-1 range where higher = more anomalous
    anomaly_score = float(-score)
    
    return {
        "is_anomaly": bool(is_anomaly),
        "anomaly_score": anomaly_score,
        "model_version": version,
    }


def predict_batch(features_list: list[list[float]]) -> list[dict]:
    """
    Run anomaly detection on multiple feature vectors.
    
    Args:
        features_list: List of feature vectors
        
    Returns:
        List of dicts with is_anomaly, anomaly_score, and model_version
    """
    model = get_model(_MODEL_NAME)
    version = get_model_version(_MODEL_NAME)
    
    predictions = model.predict(features_list)
    scores = model.decision_function(features_list)
    
    results = []
    for pred, score in zip(predictions, scores):
        is_anomaly = pred == -1
        anomaly_score = float(-score)
        results.append({
            "is_anomaly": bool(is_anomaly),
            "anomaly_score": anomaly_score,
            "model_version": version,
        })
    return results


def warm_up() -> None:
    """Warm up the model with a dummy prediction."""
    try:
        # Use a dummy feature vector (assuming typical feature count)
        # The actual feature count will depend on the trained model
        dummy_features = [0.0] * 10  # Will be adjusted based on model
        predict(dummy_features)
        log.info("Anomaly model warmed up")
    except Exception as e:
        log.warning(f"Anomaly warm-up failed: {e}")