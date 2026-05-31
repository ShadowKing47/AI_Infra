"""Tag helpers — every boto3 create call uses build_tags()."""

from infra import config
from utils.naming import resource_name


def build_tags(suffix: str, extra: list[dict] | None = None) -> list[dict]:
    """
    Returns the standard tag list for a boto3 TagSpecification.
    Pass extra=[{"Key": "Tier", "Value": "public"}] for resource-specific tags.
    """
    base = [
        {"Key": "Name",        "Value": resource_name(suffix)},
        {"Key": "Project",     "Value": config.PROJECT},
        {"Key": "Environment", "Value": config.ENV},
        {"Key": "ManagedBy",   "Value": "python-boto3"},
    ]
    return base + (extra or [])


def tag_spec(resource_type: str, suffix: str,
             extra: list[dict] | None = None) -> list[dict]:
    """
    Returns a ready-to-use TagSpecifications list for boto3 create calls.
    e.g. tag_spec("vpc", "vpc") → [{"ResourceType": "vpc", "Tags": [...]}]
    """
    return [{"ResourceType": resource_type, "Tags": build_tags(suffix, extra)}]
