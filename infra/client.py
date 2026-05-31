import os
import boto3
from dotenv import load_dotenv

load_dotenv()

_ENDPOINT = os.getenv("LOCALSTACK_ENDPOINT")   # None in prod → real AWS
_REGION   = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# Module-level session — one per process, thread-safe for read operations.
_session = boto3.Session(
    aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID",     "test"),
    aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    region_name           = _REGION,
)


def get_client(service: str):
    """Return a boto3 client pointed at LocalStack or real AWS depending on env."""
    return _session.client(service, endpoint_url=_ENDPOINT)


def get_resource(service: str):
    """Return a boto3 resource pointed at LocalStack or real AWS depending on env."""
    return _session.resource(service, endpoint_url=_ENDPOINT)


def region() -> str:
    return _REGION
