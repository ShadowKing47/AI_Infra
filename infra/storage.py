"""
Phase 2 — Storage Layer

Provisions: S3 buckets for application artefacts, logs, and state with
encryption, versioning, lifecycle policies, and access controls.

Every create_* function is idempotent — checks for existing bucket before creating.
"""

import json
import logging

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import tag_spec

log = logging.getLogger(__name__)

# AWS ELB account IDs by region (for ALB access logging)
_ELB_ACCOUNTS = {
    "us-east-1":      "127311923021",
    "us-east-2":      "033677994240",
    "us-west-1":      "027434742980",
    "us-west-2":      "797873946194",
    "eu-west-1":      "156460612806",
    "eu-central-1":   "054676820928",
    "ap-northeast-1": "582318560864",
    "ap-southeast-1": "114774131450",
    "ap-southeast-2": "783225319266",
}


def create_bucket(logical_name: str, versioning: bool = False,
                  glacier_days: int = 0, expiry_days: int = 0) -> str:
    """
    Creates S3 bucket with encryption, public-access-block, and optional lifecycle.
    
    Args:
        logical_name: suffix for bucket name (e.g. "artefacts" → "ai-infra-dev-artefacts")
        versioning: enable object versioning
        glacier_days: transition to Glacier after N days (0 = disabled)
        expiry_days: delete objects after N days (0 = disabled)
    
    Returns:
        bucket_name: idempotent, checks for existing bucket by name
    """
    s3 = aws.get_client("s3")
    bucket_name = resource_name(logical_name)
    
    # Check if bucket already exists
    try:
        s3.head_bucket(Bucket=bucket_name)
        log.info(f"Bucket {bucket_name} already exists, skipping creation")
        return bucket_name
    except s3.exceptions.NoSuchBucket:
        pass
    except Exception as e:
        log.warning(f"Error checking bucket {bucket_name}: {e}")
    
    # Create bucket
    log.info(f"Creating S3 bucket {bucket_name}")
    try:
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": aws.region()}
            if aws.region() != "us-east-1" else None,
        )
    except Exception as e:
        # LocalStack might not support CreateBucketConfiguration, try without it
        if "InvalidBucketName" not in str(e) and "BucketAlreadyOwnedByYou" not in str(e):
            log.warning(f"Retrying bucket creation without LocationConstraint: {e}")
            try:
                s3.create_bucket(Bucket=bucket_name)
            except Exception as retry_e:
                log.error(f"Failed to create bucket {bucket_name}: {retry_e}")
                raise
    
    # Enable versioning
    if versioning:
        log.info(f"Enabling versioning on {bucket_name}")
        s3.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )
    
    # Block public access
    log.info(f"Blocking public access on {bucket_name}")
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    
    # Enable encryption
    log.info(f"Enabling encryption on {bucket_name}")
    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256",
                    }
                }
            ]
        },
    )
    
    # Apply lifecycle rules if requested
    if glacier_days > 0 or expiry_days > 0:
        rules = []
        if glacier_days > 0:
            rules.append({
                "Id": "TransitionToGlacier",
                "Status": "Enabled",
                "Transitions": [
                    {
                        "Days": glacier_days,
                        "StorageClass": "GLACIER",
                    }
                ],
            })
        if expiry_days > 0:
            rules.append({
                "Id": "DeleteOldObjects",
                "Status": "Enabled",
                "Expiration": {"Days": expiry_days},
            })
        
        log.info(f"Applying lifecycle rules to {bucket_name}")
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket_name,
            LifecycleConfiguration={"Rules": rules},
        )
    
    log.info(f"Bucket {bucket_name} created successfully")
    return bucket_name


def attach_alb_log_policy(bucket_name: str) -> None:
    """
    Grants the AWS ELB service account s3:PutObject permission on the bucket.
    Must be called before ALB is created, so ALB logs can be written.
    """
    s3 = aws.get_client("s3")
    region = aws.region()
    
    # Get ELB account ID for the region
    elb_account = _ELB_ACCOUNTS.get(region)
    if not elb_account:
        log.warning(f"Unknown ELB account ID for region {region}, using us-east-1 default")
        elb_account = _ELB_ACCOUNTS["us-east-1"]
    
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "AWS": f"arn:aws:iam::{elb_account}:root"
                },
                "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            }
        ],
    }
    
    log.info(f"Attaching ALB log policy to {bucket_name}")
    try:
        s3.put_bucket_policy(Bucket=bucket_name, Policy=json.dumps(policy))
        log.info(f"ALB log policy attached to {bucket_name}")
    except Exception as e:
        log.error(f"Failed to attach ALB log policy: {e}")
        raise


def provision_storage() -> dict:
    """
    Orchestrator — creates all storage buckets for Phase 2.
    Returns dict of bucket names for use by later phases.
    """
    log.info("=== Phase 2: Storage Layer ===")
    
    # Create artefacts bucket (for model files in Phase 4+)
    artefacts_bucket = create_bucket("artefacts")
    
    # Create logs bucket with ALB log policy pre-attached
    logs_bucket = create_bucket("logs", glacier_days=90, expiry_days=365)
    attach_alb_log_policy(logs_bucket)
    
    return {
        "artefacts_bucket": artefacts_bucket,
        "logs_bucket": logs_bucket,
    }
