"""
FastAPI application — serves all ML inference endpoints.

Phase 2: Basic health check and setup.
Phase 4: Adds model loading on startup via lifespan context.
Phase 7: Adds /api/predict/* endpoints for sentiment and anomaly detection.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import health

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for startup/shutdown events.
    
    Phase 4: Loads ML models from S3 on startup.
    Phase 6: Registers with service discovery on startup.
    """
    # Startup: initialize here in Phase 4+
    log.info("FastAPI application started")
    health.set_model_version("none")  # Phase 4 updates this after loading models
    
    yield
    
    # Shutdown: cleanup here if needed
    log.info("FastAPI application shutting down")


app = FastAPI(
    title="AI Inference API",
    description="Machine learning inference endpoints",
    version="1.0.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(health.router)

# Phase 4 will add:
# from app import predict
# app.include_router(predict.router, prefix="/api/predict")


@app.get("/")
async def root() -> dict:
    """Root endpoint — API info."""
    return {
        "service": "AI Inference API",
        "version": "1.0.0",
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=8080)
