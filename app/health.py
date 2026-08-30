"""Health check endpoint for ALB monitoring."""

from fastapi import APIRouter, HTTPException, status

router = APIRouter(tags=["health"])

# Global state for model version (set by main.py on startup)
_MODEL_VERSION = "none"


def set_model_version(version: str) -> None:
    """Called by main.py after model loading."""
    global _MODEL_VERSION
    _MODEL_VERSION = version


def is_model_loaded() -> bool:
    """Check if models are loaded (not 'loading', 'failed', or 'none')."""
    # Also check loader's state for tests
    try:
        from app.models.loader import are_models_loaded
        return _MODEL_VERSION not in ("none", "loading", "failed") and are_models_loaded()
    except ImportError:
        return _MODEL_VERSION not in ("none", "loading", "failed")


@router.get("/health")
async def health() -> dict:
    """
    Health check endpoint used by ALB health checks.
    Returns 200 with status and model version.
    
    In Phase 4, this will return 503 until the model is loaded,
    preventing the ALB from marking the instance healthy.
    """
    if not is_model_loaded():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Models not ready: {_MODEL_VERSION}",
        )
    
    return {
        "status": "ok",
        "version": _MODEL_VERSION,
    }
