"""Pure networking math — no boto3, no side effects."""

from infra import config


def subnet_cidr(offset: int) -> str:
    """
    Derives a /24 CIDR from the VPC /16 given an integer offset.
    e.g. VPC=10.0.0.0/16, offset=10  →  10.0.10.0/24
    """
    base = config.VPC_CIDR.split(".")[:2]
    return f"{'.'.join(base)}.{offset}.0/24"
