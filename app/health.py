"""Health check endpoint for ALB monitoring."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])

# Global state for model version (set by main.py on startup)
_MODEL_VERSION = "none"


def set_model_version(version: str) -> None:
    """Called by main.py after model loading."""
    global _MODEL_VERSION
    _MODEL_VERSION = version


@router.get("/health")
async def health() -> dict:
    """
    Health check endpoint used by ALB health checks.
    Returns 200 with status and model version.
    
    In Phase 4, this will return 503 until the model is loaded,
    preventing the ALB from marking the instance healthy.
    """
    return {
        "status": "ok",
        "version": _MODEL_VERSION,
    }
