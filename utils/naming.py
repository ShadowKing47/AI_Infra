"""Resource naming conventions — used by every infra module."""

from infra import config


def resource_name(suffix: str) -> str:
    """Returns <project>-<env>-<suffix>  e.g. ai-infra-dev-vpc"""
    return f"{config.PROJECT}-{config.ENV}-{suffix}"
