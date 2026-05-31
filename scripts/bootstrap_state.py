"""
Creates the S3 state bucket and DynamoDB lock table that deploy.py uses
to persist and look up provisioned resource IDs across runs.

Run this once before the first deploy:
    python scripts/bootstrap_state.py

Safe to re-run — every operation is idempotent.
"""

import sys
import time
import logging
import urllib.request
import urllib.error
from pathlib import Path

# Ensure the project root is on sys.path regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from infra import client as aws
from infra import config
from utils.naming import resource_name

logging.basicConfig(level=logging.INFO, format="    %(message)s")
log = logging.getLogger(__name__)

STATE_BUCKET = resource_name("state")
LOCK_TABLE   = resource_name("state-lock")


# ── LocalStack readiness ───────────────────────────────────────────────────────

def _wait_for_localstack(max_attempts: int = 20, interval: int = 3) -> None:
    """Poll LocalStack health endpoint until it reports all services available."""
    import os
    endpoint = os.getenv("LOCALSTACK_ENDPOINT")
    if not endpoint:
        return  # targeting real AWS — no health check needed

    url = f"{endpoint}/_localstack/health"
    log.info("Waiting for LocalStack at %s …", endpoint)
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    log.info("[ok] LocalStack is ready")
                    return
        except (urllib.error.URLError, OSError):
            pass
        if attempt == max_attempts:
            log.error(
                "[error] LocalStack not ready after %d attempts.\n"
                "        Start it with:  docker run -d -p 4566:4566 localstack/localstack:3.4",
                max_attempts,
            )
            sys.exit(1)
        time.sleep(interval)


# ── S3 state bucket ────────────────────────────────────────────────────────────

def _create_state_bucket() -> None:
    s3 = aws.get_client("s3")

    try:
        s3.head_bucket(Bucket=STATE_BUCKET)
        log.info("[skip] S3 bucket already exists: %s", STATE_BUCKET)
        return
    except s3.exceptions.ClientError:
        pass

    create_kwargs: dict = {"Bucket": STATE_BUCKET}
    if config.REGION != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {
            "LocationConstraint": config.REGION
        }
    s3.create_bucket(**create_kwargs)

    s3.put_bucket_versioning(
        Bucket=STATE_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    s3.put_bucket_encryption(
        Bucket=STATE_BUCKET,
        ServerSideEncryptionConfiguration={
            "Rules": [{
                "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                "BucketKeyEnabled": True,
            }]
        },
    )
    s3.put_public_access_block(
        Bucket=STATE_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls":       True,
            "IgnorePublicAcls":      True,
            "BlockPublicPolicy":     True,
            "RestrictPublicBuckets": True,
        },
    )
    log.info("[ok] S3 state bucket created: %s", STATE_BUCKET)


# ── DynamoDB lock table ────────────────────────────────────────────────────────

def _create_lock_table() -> None:
    ddb = aws.get_client("dynamodb")

    try:
        status = ddb.describe_table(TableName=LOCK_TABLE)["Table"]["TableStatus"]
        if status == "ACTIVE":
            log.info("[skip] DynamoDB lock table already exists: %s", LOCK_TABLE)
            return
    except ddb.exceptions.ResourceNotFoundException:
        pass

    ddb.create_table(
        TableName=LOCK_TABLE,
        AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    log.info("[ok] DynamoDB lock table created: %s", LOCK_TABLE)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    print("==> Bootstrapping state backend")
    print(f"    Bucket : {STATE_BUCKET}")
    print(f"    Table  : {LOCK_TABLE}")
    print()

    _wait_for_localstack()
    _create_state_bucket()
    _create_lock_table()

    print()
    print("==> Done. Run the deploy next:")
    print("    python scripts/deploy.py")


if __name__ == "__main__":
    main()
