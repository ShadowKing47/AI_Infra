"""
Phase 3 — Data Tier: Cache Layer

Provisions: ElastiCache Redis with Multi-AZ replication, encryption (at-rest and in-transit),
automatic failover, and credentials stored in AWS Secrets Manager.

Every function is idempotent — checks for existing resource before creating.
"""

import json
import logging
import time

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import build_tags

log = logging.getLogger(__name__)


def create_cache_subnet_group(name: str, subnet_ids: list[str]) -> str:
    """
    Creates ElastiCache subnet group for Multi-AZ replication.
    
    Args:
        name: logical name (e.g. "redis" → "ai-infra-dev-redis-subnet-group")
        subnet_ids: list of cache subnet IDs (should be in different AZs)
    
    Returns:
        subnet_group_name: ready for use in ElastiCache creation
    """
    elasticache = aws.get_client("elasticache")
    subnet_group_name = resource_name(f"{name}-subnet-group")
    
    # Check for existing cache subnet group
    try:
        response = elasticache.describe_cache_subnet_groups(
            CacheSubnetGroupName=subnet_group_name
        )
        if response["CacheSubnetGroups"]:
            log.info(f"Cache subnet group {subnet_group_name} already exists")
            return subnet_group_name
    except elasticache.exceptions.CacheSubnetGroupNotFoundFault:
        pass
    except Exception as e:
        log.debug(f"Error checking cache subnet group: {e}")
    
    # Create cache subnet group
    log.info(f"Creating cache subnet group {subnet_group_name}")
    elasticache.create_cache_subnet_group(
        CacheSubnetGroupName=subnet_group_name,
        CacheSubnetGroupDescription=f"Subnet group for {config.PROJECT}-{config.ENV} Redis",
        SubnetIds=subnet_ids,
        Tags=build_tags(f"{name}-subnet-group"),
    )
    
    log.info(f"Cache subnet group created: {subnet_group_name}")
    return subnet_group_name


def create_redis(subnet_group: str, sg_id: str,
                 node_type: str = "cache.t3.micro",
                 num_cache_nodes: int = 2,
                 automatic_failover: bool = True) -> dict:
    """
    Creates ElastiCache Redis replication group with Multi-AZ, encryption, and failover.
    
    Args:
        subnet_group: Cache subnet group name
        sg_id: VPC security group ID (cache SG)
        node_type: e.g. "cache.t3.micro", "cache.t3.small"
        num_cache_nodes: number of nodes (min 2 for multi-AZ with failover)
        automatic_failover: enable automatic failover
    
    Returns:
        dict with:
            - primary_endpoint: Primary node endpoint (read+write)
            - reader_endpoint: Reader endpoint (read-only, load-balanced)
            - port: Redis port (default 6379)
            - auth_token_secret_arn: Secrets Manager secret with auth token
    """
    elasticache = aws.get_client("elasticache")
    secrets = aws.get_client("secretsmanager")
    
    replication_group_id = resource_name("redis")
    
    # Check for existing replication group
    try:
        response = elasticache.describe_replication_groups(
            ReplicationGroupId=replication_group_id
        )
        if response["ReplicationGroups"]:
            group = response["ReplicationGroups"][0]
            log.info(f"Redis replication group {replication_group_id} already exists")
            
            # Try to retrieve existing secret
            try:
                secret_name = resource_name("redis-auth-token")
                secret = secrets.describe_secret(SecretId=secret_name)
                secret_arn = secret["ARN"]
            except:
                secret_arn = "unknown"
            
            primary_endpoint = group.get("PrimaryEndpoint", {})
            return {
                "primary_endpoint": primary_endpoint.get("Address", ""),
                "reader_endpoint": group.get("ReaderEndpoint", {}).get("Address", ""),
                "port": primary_endpoint.get("Port", 6379),
                "auth_token_secret_arn": secret_arn,
                "replication_group_id": replication_group_id,
            }
    except elasticache.exceptions.ReplicationGroupNotFoundFault:
        pass
    except Exception as e:
        log.debug(f"Error checking Redis replication group: {e}")
    
    # Generate auth token
    import secrets as secrets_module
    auth_token = secrets_module.token_urlsafe(32)
    
    # Create secret for auth token
    secret_name = resource_name("redis-auth-token")
    log.info(f"Creating Secrets Manager secret for Redis auth token")
    
    secret_dict = {
        "auth_token": auth_token,
        "engine": "redis",
        "port": 6379,
    }
    
    try:
        secret_response = secrets.describe_secret(SecretId=secret_name)
        log.info(f"Secret {secret_name} already exists")
        secret_arn = secret_response["ARN"]
    except secrets.exceptions.ResourceNotFoundException:
        try:
            secret_response = secrets.create_secret(
                Name=secret_name,
                SecretString=json.dumps(secret_dict),
                Tags=build_tags("redis-auth-token"),
            )
            secret_arn = secret_response["ARN"]
            log.info(f"Secret created: {secret_arn}")
        except Exception as e:
            log.error(f"Failed to create secret: {e}")
            raise
    
    # Create Redis replication group
    log.info(f"Creating Redis replication group {replication_group_id}")
    
    try:
        response = elasticache.create_replication_group(
            ReplicationGroupId=replication_group_id,
            ReplicationGroupDescription=f"Redis for {config.PROJECT}-{config.ENV}",
            Engine="redis",
            EngineVersion="7.0",  # Latest stable Redis 7
            CacheNodeType=node_type,
            NumCacheClusters=num_cache_nodes,
            CacheSubnetGroupName=subnet_group,
            VpcSecurityGroupIds=[sg_id],
            Port=6379,
            ParameterGroupName="default.redis7",
            AutomaticFailoverEnabled=automatic_failover,
            AtRestEncryptionEnabled=True,
            TransitEncryptionEnabled=True,
            AuthToken=auth_token,
            SnapshotRetentionLimit=7,  # Keep snapshots for 7 days
            SnapshotWindow="03:00-05:00",  # UTC
            PreferredMaintenanceWindow="mon:05:00-mon:07:00",  # UTC, after snapshots
            Tags=build_tags("redis"),
            MultiAZEnabled=True,  # Multi-AZ with automatic failover
            NotificationTopicArn=None,  # Optional: SNS topic for notifications
        )
    except Exception as e:
        log.error(f"Failed to create Redis replication group: {e}")
        raise
    
    group = response["ReplicationGroup"]
    
    # Wait for replication group to be available
    log.info(f"Waiting for Redis replication group to become available...")
    max_attempts = 120
    attempt = 0
    
    while attempt < max_attempts:
        try:
            response = elasticache.describe_replication_groups(
                ReplicationGroupId=replication_group_id
            )
            group = response["ReplicationGroups"][0]
            status = group["Status"]
            
            if status == "available":
                log.info(f"Redis replication group is available")
                break
            else:
                log.info(f"Redis status: {status}, waiting...")
                time.sleep(5)
                attempt += 1
        except Exception as e:
            log.debug(f"Error checking Redis status: {e}")
            time.sleep(5)
            attempt += 1
    
    if attempt >= max_attempts:
        log.warning(f"Redis replication group did not become available within timeout")
    
    primary_endpoint = group.get("PrimaryEndpoint", {})
    reader_endpoint = group.get("ReaderEndpoint", {})
    
    result = {
        "primary_endpoint": primary_endpoint.get("Address", ""),
        "reader_endpoint": reader_endpoint.get("Address", ""),
        "port": primary_endpoint.get("Port", 6379),
        "auth_token_secret_arn": secret_arn,
        "replication_group_id": replication_group_id,
    }
    
    log.info(f"Redis replication group created successfully: {result}")
    return result


def get_redis_config(secret_arn: str) -> dict:
    """
    Fetches Redis configuration from Secrets Manager.
    
    Args:
        secret_arn: ARN of the Secrets Manager secret
    
    Returns:
        dict with auth_token, host, port for Redis connection
    """
    secrets = aws.get_client("secretsmanager")
    
    try:
        response = secrets.get_secret_value(SecretId=secret_arn)
        return json.loads(response["SecretString"])
    except Exception as e:
        log.error(f"Failed to retrieve secret: {e}")
        raise


def provision_cache(cache_subnet_ids: list[str], cache_sg_id: str) -> dict:
    """
    Orchestrator for cache tier provisioning.
    
    Returns:
        dict with primary_endpoint, reader_endpoint, port, auth_token_secret_arn
    """
    log.info("=== Phase 3: Cache Tier ===")
    
    # Create cache subnet group
    subnet_group = create_cache_subnet_group(
        name="redis",
        subnet_ids=cache_subnet_ids,
    )
    
    # Create Redis replication group
    redis_result = create_redis(
        subnet_group=subnet_group,
        sg_id=cache_sg_id,
        node_type="cache.t3.micro",
        num_cache_nodes=2,
        automatic_failover=True,
    )
    
    return redis_result
