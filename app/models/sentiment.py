"""
Sentiment classifier — HuggingFace pipeline inference.

Phase 4: Provides sentiment prediction for /api/predict/sentiment endpoint.
"""

import logging
from typing import Any

from app.models.loader import get_model, get_model_version

log = logging.getLogger(__name__)

_MODEL_NAME = "sentiment"


def predict(text: str) -> dict:
    """
    Run sentiment classification on input text.
    
    Args:
        text: Input text to classify
        
    Returns:
        Dict with label, score, and model_version
    """
    model = get_model(_MODEL_NAME)
    version = get_model_version(_MODEL_NAME)
    
    if hasattr(model, '__call__') and hasattr(model, 'model'):
        # HuggingFace pipeline
        result = model(text)[0]
        label = result["label"]
        score = float(result["score"])
    else:
        # Fallback for other model types (sklearn, etc.)
        prediction = model.predict([text])[0]
        proba = model.predict_proba([text])[0] if hasattr(model, "predict_proba") else [0.5, 0.5]
        label = "POSITIVE" if prediction == 1 else "NEGATIVE"
        score = float(max(proba))
    
    return {
        "label": label,
        "score": score,
        "model_version": version,
    }


def predict_batch(texts: list[str]) -> list[dict]:
    """
    Run sentiment classification on multiple texts.
    
    Args:
        texts: List of input texts
        
    Returns:
        List of dicts with label, score, and model_version
    """
    model = get_model(_MODEL_NAME)
    version = get_model_version(_MODEL_NAME)
    
    if hasattr(model, '__call__') and hasattr(model, 'model'):
        # HuggingFace pipeline - supports batch
        results = model(texts)
        return [
            {
                "label": r["label"],
                "score": float(r["score"]),
                "model_version": version,
            }
            for r in results
        ]
    else:
        # Fallback for other model types
        predictions = model.predict(texts)
        probas = model.predict_proba(texts) if hasattr(model, "predict_proba") else None
        
        results = []
        for i, pred in enumerate(predictions):
            label = "POSITIVE" if pred == 1 else "NEGATIVE"
            score = float(max(probas[i])) if probas is not None else 0.5
            results.append({
                "label": label,
                "score": score,
                "model_version": version,
            })
        return results


def warm_up() -> None:
    """Warm up the model with a dummy prediction."""
    try:
        predict("warm up")
        log.info("Sentiment model warmed up")
    except Exception as e:
        log.warning(f"Sentiment warm-up failed: {e}")