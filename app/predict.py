"""
FastAPI prediction endpoints — ML inference API.

Phase 4: POST /api/predict/sentiment and /api/predict/anomaly.
Routed by ALB path rule from loadbalancer.add_listener_rule().
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models.loader import load_model, preload_models, set_models_loaded, are_models_loaded
from app.models.sentiment import predict as predict_sentiment, predict_batch as predict_batch_sentiment, warm_up as warm_up_sentiment
from app.models.anomaly import predict as predict_anomaly, predict_batch as predict_batch_anomaly, warm_up as warm_up_anomaly

log = logging.getLogger(__name__)

router = APIRouter(tags=["predict"])


# ── Request/Response Models ──────────────────────────────────────────────────────

class SentimentRequest(BaseModel):
    """Single sentiment prediction request."""
    text: str = Field(..., min_length=1, max_length=10000, description="Text to classify")


class SentimentBatchRequest(BaseModel):
    """Batch sentiment prediction request."""
    texts: list[str] = Field(..., min_length=1, max_length=100, description="List of texts to classify")


class SentimentResponse(BaseModel):
    """Sentiment prediction response."""
    label: str = Field(..., description="POSITIVE or NEGATIVE")
    score: float = Field(..., ge=0.0, le=1.0, description="Confidence score")
    model_version: str = Field(..., description="Model version identifier")


class SentimentBatchResponse(BaseModel):
    """Batch sentiment prediction response."""
    predictions: list[SentimentResponse]


class AnomalyRequest(BaseModel):
    """Single anomaly detection request."""
    features: list[float] = Field(..., min_length=1, max_length=1000, description="Feature vector")


class AnomalyBatchRequest(BaseModel):
    """Batch anomaly detection request."""
    features_list: list[list[float]] = Field(..., min_length=1, max_length=100, description="List of feature vectors")


class AnomalyResponse(BaseModel):
    """Anomaly detection response."""
    is_anomaly: bool = Field(..., description="Whether the input is anomalous")
    anomaly_score: float = Field(..., description="Anomaly score (lower = more anomalous)")
    model_version: str = Field(..., description="Model version identifier")


class AnomalyBatchResponse(BaseModel):
    """Batch anomaly detection response."""
    predictions: list[AnomalyResponse]


class PredictHealthResponse(BaseModel):
    """Prediction service health response."""
    models_loaded: bool
    sentiment_loaded: bool
    anomaly_loaded: bool
    versions: dict[str, str]


# ── Startup/Shutdown ─────────────────────────────────────────────────────────────

async def initialize_models() -> None:
    """Initialize ML models on startup. Called from main.py lifespan."""
    log.info("Initializing ML models...")
    
    # Preload required models
    results = preload_models(["sentiment", "anomaly"])
    
    all_loaded = all(results.values())
    set_models_loaded(all_loaded)
    
    if all_loaded:
        # Warm up models
        warm_up_sentiment()
        warm_up_anomaly()
        log.info("All models loaded and warmed up successfully")
    else:
        failed = [name for name, ok in results.items() if not ok]
        log.error(f"Failed to load models: {failed}")
        # Don't raise - let health endpoint handle 503


# ── Endpoints ────────────────────────────────────────────────────────────────────

@router.get("/health", response_model=PredictHealthResponse)
async def predict_health() -> PredictHealthResponse:
    """
    Prediction service health check.
    
    Returns 503 if models are not loaded (used by ALB health check in Phase 4).
    """
    from app.models.loader import get_model_version, _MODEL_CACHE
    
    sentiment_loaded = "sentiment" in _MODEL_CACHE
    anomaly_loaded = "anomaly" in _MODEL_CACHE
    models_loaded = are_models_loaded()
    
    versions = {}
    if sentiment_loaded:
        versions["sentiment"] = get_model_version("sentiment")
    if anomaly_loaded:
        versions["anomaly"] = get_model_version("anomaly")
    
    if not models_loaded:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Models not loaded yet",
        )
    
    return PredictHealthResponse(
        models_loaded=models_loaded,
        sentiment_loaded=sentiment_loaded,
        anomaly_loaded=anomaly_loaded,
        versions=versions,
    )


@router.post("/sentiment", response_model=SentimentResponse)
async def sentiment_endpoint(request: SentimentRequest) -> SentimentResponse:
    """
    Predict sentiment of input text.
    
    POST /api/predict/sentiment
    """
    if not are_models_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Models not loaded",
        )
    
    try:
        result = predict_sentiment(request.text)
        return SentimentResponse(**result)
    except Exception as e:
        log.error(f"Sentiment prediction failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Prediction failed",
        )


@router.post("/sentiment/batch", response_model=SentimentBatchResponse)
async def sentiment_batch_endpoint(request: SentimentBatchRequest) -> SentimentBatchResponse:
    """
    Predict sentiment for multiple texts.
    
    POST /api/predict/sentiment/batch
    """
    if not are_models_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Models not loaded",
        )
    
    try:
        results = predict_batch_sentiment(request.texts)
        return SentimentBatchResponse(
            predictions=[SentimentResponse(**r) for r in results]
        )
    except Exception as e:
        log.error(f"Batch sentiment prediction failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Batch prediction failed",
        )


@router.post("/anomaly", response_model=AnomalyResponse)
async def anomaly_endpoint(request: AnomalyRequest) -> AnomalyResponse:
    """
    Detect anomaly in input features.
    
    POST /api/predict/anomaly
    """
    if not are_models_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Models not loaded",
        )
    
    try:
        result = predict_anomaly(request.features)
        return AnomalyResponse(**result)
    except Exception as e:
        log.error(f"Anomaly prediction failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Prediction failed",
        )


@router.post("/anomaly/batch", response_model=AnomalyBatchResponse)
async def anomaly_batch_endpoint(request: AnomalyBatchRequest) -> AnomalyBatchResponse:
    """
    Detect anomalies in multiple feature vectors.
    
    POST /api/predict/anomaly/batch
    """
    if not are_models_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Models not loaded",
        )
    
    try:
        results = predict_batch_anomaly(request.features_list)
        return AnomalyBatchResponse(
            predictions=[AnomalyResponse(**r) for r in results]
        )
    except Exception as e:
        log.error(f"Batch anomaly prediction failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Batch prediction failed",
        )