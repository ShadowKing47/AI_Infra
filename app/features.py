"""
Feature Store Client — Online/Offline Feature Management

Implements a dual-path feature store:
- Online path: Redis (sub-millisecond lookup for inference)
- Offline path: RDS (training joins, drift analysis, audit trail)
- Fallback: Redis miss → RDS cold path

Used in Phase 4 (inference) and Phase 7 (training/drift detection).
"""

import json
import logging
from typing import Any, Optional, Dict
from datetime import datetime, timedelta

import redis
import sqlalchemy as sa
from sqlalchemy import create_engine, Column, String, JSON, DateTime
from sqlalchemy.orm import declarative_base, Session

log = logging.getLogger(__name__)

Base = declarative_base()


class FeatureRecord(Base):
    """ORM model for storing features in RDS."""
    __tablename__ = "features"
    
    entity_id = Column(String(255), primary_key=True, index=True)
    feature_data = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    version = Column(String(50), default="1.0")


class FeatureStore:
    """
    Dual-path feature store for ML inference and training.
    
    Online path (inference):
        Entity feature request → Redis lookup (sub-ms) → cache hit
        
    Offline path (training/drift):
        Batch query → RDS scan/join → historical features
        
    Fallback:
        Redis eviction/miss → RDS cold read → back to Redis (optional)
    """
    
    def __init__(self, redis_config: dict, db_connection_string: str):
        """
        Initialize feature store with Redis and RDS connections.
        
        Args:
            redis_config: dict with 'primary_endpoint', 'port', 'auth_token'
            db_connection_string: SQLAlchemy connection URL for RDS
        """
        self.redis_config = redis_config
        self.db_connection_string = db_connection_string
        
        # Initialize Redis connection
        try:
            self.redis_client = redis.Redis(
                host=redis_config.get("primary_endpoint", "localhost"),
                port=redis_config.get("port", 6379),
                password=redis_config.get("auth_token", ""),
                decode_responses=True,
                socket_connect_timeout=5,
                socket_keepalive=True,
                health_check_interval=30,
            )
            # Test connection
            self.redis_client.ping()
            log.info("Redis connection established")
        except Exception as e:
            log.warning(f"Failed to connect to Redis: {e}")
            self.redis_client = None
        
        # Initialize RDS connection
        try:
            self.engine = create_engine(db_connection_string)
            # Create tables
            Base.metadata.create_all(self.engine)
            log.info("RDS connection established and tables created")
        except Exception as e:
            log.warning(f"Failed to connect to RDS: {e}")
            self.engine = None
    
    def get(self, entity_id: str, fallback_to_rds: bool = True) -> Optional[Dict[str, Any]]:
        """
        Get features for an entity (online path → offline path).
        
        Args:
            entity_id: unique identifier for the entity
            fallback_to_rds: if Redis miss, query RDS and cache result
        
        Returns:
            dict of features or None if not found
        """
        # Try Redis first (online path)
        if self.redis_client:
            try:
                key = f"features:{entity_id}"
                cached = self.redis_client.get(key)
                if cached:
                    log.debug(f"Cache hit for {entity_id}")
                    return json.loads(cached)
            except Exception as e:
                log.warning(f"Redis read failed: {e}")
        
        # Fall back to RDS (offline path)
        if fallback_to_rds and self.engine:
            try:
                with Session(self.engine) as session:
                    record = session.query(FeatureRecord).filter(
                        FeatureRecord.entity_id == entity_id
                    ).first()
                    
                    if record:
                        features = record.feature_data
                        log.debug(f"Cache miss, loaded from RDS for {entity_id}")
                        
                        # Optional: write back to Redis for future hits
                        if self.redis_client:
                            self.set(entity_id, features, ttl=86400)
                        
                        return features
            except Exception as e:
                log.warning(f"RDS read failed: {e}")
        
        log.warning(f"Features not found for {entity_id}")
        return None
    
    def set(self, entity_id: str, features: dict, ttl: int = 86400) -> bool:
        """
        Set features for an entity (online + offline paths).
        
        Args:
            entity_id: unique identifier
            features: dict of feature name → value
            ttl: Redis TTL in seconds (default 1 day)
        
        Returns:
            True if successful, False otherwise
        """
        success = False
        
        # Write to Redis (online path)
        if self.redis_client:
            try:
                key = f"features:{entity_id}"
                self.redis_client.setex(
                    key,
                    ttl,
                    json.dumps(features),
                )
                log.debug(f"Cached features for {entity_id} in Redis (TTL: {ttl}s)")
                success = True
            except Exception as e:
                log.warning(f"Redis write failed: {e}")
        
        # Write to RDS (offline path + audit trail)
        if self.engine:
            try:
                with Session(self.engine) as session:
                    record = session.query(FeatureRecord).filter(
                        FeatureRecord.entity_id == entity_id
                    ).first()
                    
                    if record:
                        record.feature_data = features
                        record.updated_at = datetime.utcnow()
                    else:
                        record = FeatureRecord(
                            entity_id=entity_id,
                            feature_data=features,
                        )
                        session.add(record)
                    
                    session.commit()
                    log.debug(f"Persisted features for {entity_id} in RDS")
                    success = True
            except Exception as e:
                log.warning(f"RDS write failed: {e}")
        
        return success
    
    def batch_get(self, entity_ids: list[str]) -> Dict[str, dict]:
        """
        Get features for multiple entities (offline path).
        Used for training datasets.
        
        Args:
            entity_ids: list of entity IDs
        
        Returns:
            dict mapping entity_id → features
        """
        results = {}
        
        if not self.engine:
            log.warning("RDS not available for batch_get")
            return results
        
        try:
            with Session(self.engine) as session:
                records = session.query(FeatureRecord).filter(
                    FeatureRecord.entity_id.in_(entity_ids)
                ).all()
                
                for record in records:
                    results[record.entity_id] = record.feature_data
                
                log.info(f"Batch loaded {len(results)} entities from RDS")
        except Exception as e:
            log.error(f"Batch read failed: {e}")
        
        return results
    
    def get_historical(self, entity_id: str, 
                      since: Optional[datetime] = None) -> list[dict]:
        """
        Get feature history for an entity (audit trail / drift analysis).
        Used in Phase 7 for drift detection.
        
        Args:
            entity_id: entity to query
            since: only return records after this datetime (default: 24h ago)
        
        Returns:
            list of feature records ordered by time
        """
        results = []
        
        if not self.engine:
            log.warning("RDS not available for get_historical")
            return results
        
        if since is None:
            since = datetime.utcnow() - timedelta(hours=24)
        
        try:
            with Session(self.engine) as session:
                records = session.query(FeatureRecord).filter(
                    FeatureRecord.entity_id == entity_id,
                    FeatureRecord.updated_at >= since,
                ).order_by(FeatureRecord.updated_at).all()
                
                for record in records:
                    results.append({
                        "timestamp": record.updated_at.isoformat(),
                        "features": record.feature_data,
                        "version": record.version,
                    })
                
                log.info(f"Retrieved {len(results)} historical records for {entity_id}")
        except Exception as e:
            log.error(f"Historical query failed: {e}")
        
        return results
    
    def health_check(self) -> dict:
        """
        Check health of both Redis and RDS connections.
        
        Returns:
            dict with 'redis' and 'rds' health status
        """
        health = {
            "redis": "unhealthy",
            "rds": "unhealthy",
        }
        
        # Check Redis
        if self.redis_client:
            try:
                self.redis_client.ping()
                health["redis"] = "healthy"
            except Exception as e:
                log.warning(f"Redis health check failed: {e}")
        
        # Check RDS
        if self.engine:
            try:
                with self.engine.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
                health["rds"] = "healthy"
            except Exception as e:
                log.warning(f"RDS health check failed: {e}")
        
        return health
    
    def delete(self, entity_id: str) -> bool:
        """
        Delete features for an entity from both Redis and RDS.
        
        Args:
            entity_id: entity to delete
        
        Returns:
            True if successful
        """
        success = False
        
        # Delete from Redis
        if self.redis_client:
            try:
                key = f"features:{entity_id}"
                self.redis_client.delete(key)
                log.debug(f"Deleted {entity_id} from Redis")
                success = True
            except Exception as e:
                log.warning(f"Redis delete failed: {e}")
        
        # Delete from RDS
        if self.engine:
            try:
                with Session(self.engine) as session:
                    session.query(FeatureRecord).filter(
                        FeatureRecord.entity_id == entity_id
                    ).delete()
                    session.commit()
                    log.debug(f"Deleted {entity_id} from RDS")
                    success = True
            except Exception as e:
                log.warning(f"RDS delete failed: {e}")
        
        return success


# Global feature store instance (initialized on app startup)
_feature_store: Optional[FeatureStore] = None


def initialize_feature_store(redis_config: dict, db_connection_string: str) -> FeatureStore:
    """
    Initialize global feature store instance.
    Called from app.main.py in Phase 4+ lifespan.
    """
    global _feature_store
    _feature_store = FeatureStore(redis_config, db_connection_string)
    return _feature_store


def get_feature_store() -> Optional[FeatureStore]:
    """Get the global feature store instance."""
    return _feature_store
