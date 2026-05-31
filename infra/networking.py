"""
Phase 1 — Core Network Foundation

Provisions: VPC, subnets (public/private/database), Internet Gateway,
NAT Gateways, route tables, and five security groups.

Every create_* function is idempotent — checks for an existing resource
by Name tag before creating a new one.
"""

import time
import logging

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import build_tags, tag_spec
from utils.networking import subnet_cidr

log = logging.getLogger(__name__)


# ── Internal lookup helper ─────────────────────────────────────────────────────

_DESCRIBE_MAP = {
    "vpc":              ("describe_vpcs",              "Vpcs",             "VpcId"),
    "subnet":           ("describe_subnets",           "Subnets",          "SubnetId"),
    "internet-gateway": ("describe_internet_gateways", "InternetGateways", "InternetGatewayId"),
    "nat-gateway":      ("describe_nat_gateways",      "NatGateways",      "NatGatewayId"),
    "route-table":      ("describe_route_tables",      "RouteTables",      "RouteTableId"),
    "security-group":   ("describe_security_groups",   "SecurityGroups",   "GroupId"),
}


def _find_existing(resource_type: str, name_value: str) -> str | None:
    """
    Tag-based lookup before creating any resource.
    Returns existing resource ID or None.
    """
    ec2 = aws.get_client("ec2")
    method, key, id_field = _DESCRIBE_MAP[resource_type]

    filters = [
        {"Name": "tag:Name",    "Values": [name_value]},
        {"Name": "tag:Project", "Values": [config.PROJECT]},
    ]
    if resource_type == "nat-gateway":
        filters = [
            {"Name": "tag:Name", "Values": [name_value]},
            {"Name": "state",    "Values": ["available", "pending"]},
        ]

    resp  = getattr(ec2, method)(Filters=filters)
    items = resp[key]
    return items[0][id_field] if items else None


def _wait_for_nat(nat_id: str, poll_interval: int = 10, max_attempts: int = 30) -> None:
    """Poll until NAT Gateway reaches 'available' state."""
    ec2 = aws.get_client("ec2")
    log.info("Waiting for NAT Gateway %s…", nat_id)
    for _ in range(max_attempts):
        state = ec2.describe_nat_gateways(
            NatGatewayIds=[nat_id]
        )["NatGateways"][0]["State"]
        if state == "available":
            return
        if state == "failed":
            raise RuntimeError(f"NAT Gateway {nat_id} entered failed state")
        time.sleep(poll_interval)
    raise TimeoutError(f"NAT Gateway {nat_id} did not become available")


# ── VPC ────────────────────────────────────────────────────────────────────────

def create_vpc() -> str:
    """Create VPC with DNS support/hostnames enabled. Idempotent. Returns vpc_id."""
    vpc_name = resource_name("vpc")
    if existing := _find_existing("vpc", vpc_name):
        log.info("VPC already exists: %s", existing)
        return existing

    ec2 = aws.get_client("ec2")
    resp   = ec2.create_vpc(CidrBlock=config.VPC_CIDR, TagSpecifications=tag_spec("vpc", "vpc"))
    vpc_id = resp["Vpc"]["VpcId"]

    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    log.info("Created VPC: %s  CIDR=%s", vpc_id, config.VPC_CIDR)
    return vpc_id


# ── Subnets ────────────────────────────────────────────────────────────────────

def create_subnets(vpc_id: str) -> dict[str, list[str]]:
    """
    Create one /24 subnet per AZ for each of three tiers.
    Returns {"public": [...], "private": [...], "database": [...]}.
    """
    ec2     = aws.get_client("ec2")
    result: dict[str, list[str]] = {"public": [], "private": [], "database": []}

    for tier, offsets in config.SUBNET_OFFSETS.items():
        for idx, offset in enumerate(offsets):
            az    = config.AZS[idx]
            cidr  = subnet_cidr(offset)
            label = f"subnet-{tier}-{az}"

            if existing := _find_existing("subnet", resource_name(label)):
                log.info("Subnet exists: %s  (%s %s)", existing, tier, az)
                result[tier].append(existing)
                continue

            resp      = ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=cidr,
                AvailabilityZone=az,
                TagSpecifications=tag_spec("subnet", label, extra=[{"Key": "Tier", "Value": tier}]),
            )
            subnet_id = resp["Subnet"]["SubnetId"]

            if tier == "public":
                ec2.modify_subnet_attribute(
                    SubnetId=subnet_id,
                    MapPublicIpOnLaunch={"Value": True},
                )

            log.info("Created %s subnet: %s  AZ=%s  CIDR=%s", tier, subnet_id, az, cidr)
            result[tier].append(subnet_id)

    return result


# ── Internet Gateway ───────────────────────────────────────────────────────────

def create_internet_gateway(vpc_id: str) -> str:
    """Create and attach Internet Gateway. Idempotent. Returns igw_id."""
    igw_name = resource_name("igw")
    if existing := _find_existing("internet-gateway", igw_name):
        log.info("IGW already exists: %s", existing)
        return existing

    ec2    = aws.get_client("ec2")
    resp   = ec2.create_internet_gateway(TagSpecifications=tag_spec("internet-gateway", "igw"))
    igw_id = resp["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    log.info("Created IGW: %s", igw_id)
    return igw_id


# ── NAT Gateways ───────────────────────────────────────────────────────────────

def create_nat_gateways(public_subnet_ids: list[str]) -> list[str]:
    """
    Create one NAT Gateway per AZ, each with its own Elastic IP.
    Waits for 'available' state before returning.
    Returns nat_gateway_ids in AZ order matching public_subnet_ids.
    """
    ec2     = aws.get_client("ec2")
    nat_ids: list[str] = []

    for idx, subnet_id in enumerate(public_subnet_ids):
        label = f"nat-{idx}"
        if existing := _find_existing("nat-gateway", resource_name(label)):
            log.info("NAT Gateway already exists: %s", existing)
            nat_ids.append(existing)
            continue

        eip    = ec2.allocate_address(Domain="vpc", TagSpecifications=tag_spec("elastic-ip", f"nat-eip-{idx}"))
        resp   = ec2.create_nat_gateway(
            SubnetId=subnet_id,
            AllocationId=eip["AllocationId"],
            TagSpecifications=tag_spec("natgateway", label),
        )
        nat_id = resp["NatGateway"]["NatGatewayId"]
        _wait_for_nat(nat_id)

        log.info("Created NAT Gateway: %s  subnet=%s", nat_id, subnet_id)
        nat_ids.append(nat_id)

    return nat_ids


# ── Route Tables ───────────────────────────────────────────────────────────────

def create_route_tables(
    vpc_id: str,
    igw_id: str,
    nat_ids: list[str],
    subnets: dict[str, list[str]],
) -> None:
    """
    Public RT  → IGW (one table, all public subnets).
    Private RTs → AZ-local NAT (one table per AZ).
    Database subnets share the private RTs — no direct internet egress needed.
    """
    ec2 = aws.get_client("ec2")

    def _create_rt(label: str) -> str:
        if existing := _find_existing("route-table", resource_name(label)):
            return existing
        resp = ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=tag_spec("route-table", label),
        )
        return resp["RouteTable"]["RouteTableId"]

    # Public RT
    pub_rt_id = _create_rt("rt-public")
    ec2.create_route(RouteTableId=pub_rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    for subnet_id in subnets["public"]:
        ec2.associate_route_table(RouteTableId=pub_rt_id, SubnetId=subnet_id)
    log.info("Public route table: %s", pub_rt_id)

    # Per-AZ private RTs
    for idx, nat_id in enumerate(nat_ids):
        az        = config.AZS[idx]
        priv_rt   = _create_rt(f"rt-private-{az}")
        ec2.create_route(RouteTableId=priv_rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)
        for subnet_id in [subnets["private"][idx], subnets["database"][idx]]:
            ec2.associate_route_table(RouteTableId=priv_rt, SubnetId=subnet_id)
        log.info("Private route table: %s  AZ=%s", priv_rt, az)


# ── Security Groups ────────────────────────────────────────────────────────────

def create_security_groups(vpc_id: str) -> dict[str, str]:
    """
    Create five security groups with no inline rules.
    Rules are added in attach_security_group_rules() after all IDs are known —
    prevents the circular reference problem (sg_app references sg_alb, etc.).
    Returns {"alb": sg_id, "app": sg_id, "ml": sg_id, "db": sg_id, "cache": sg_id}.
    """
    ec2 = aws.get_client("ec2")
    definitions = {
        "alb":   "ALB: HTTP/HTTPS from internet",
        "app":   "App tier: traffic from ALB only",
        "ml":    "ML tier: traffic from ALB only",
        "db":    "RDS: Postgres from app and ml tiers only",
        "cache": "Redis: from app and ml tiers only",
    }
    sgs: dict[str, str] = {}

    for key, description in definitions.items():
        label = f"sg-{key}"
        if existing := _find_existing("security-group", resource_name(label)):
            log.info("Security group exists: %s  (%s)", existing, key)
            sgs[key] = existing
            continue

        resp    = ec2.create_security_group(
            GroupName=resource_name(label),
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=tag_spec("security-group", label),
        )
        sgs[key] = resp["GroupId"]
        log.info("Created security group: %s  (%s)", sgs[key], key)

    return sgs


def attach_security_group_rules(sgs: dict[str, str]) -> None:
    """
    Add all ingress/egress rules once every group ID is known.
    Idempotent — duplicate rule errors are silently skipped.
    """
    ec2 = aws.get_client("ec2")

    def _ingress(sg_id: str, port: int,
                 source_sg: str | None = None,
                 cidr: str | None = None,
                 description: str = "") -> None:
        perm: dict = {"IpProtocol": "tcp", "FromPort": port, "ToPort": port}
        if source_sg:
            perm["UserIdGroupPairs"] = [{"GroupId": source_sg, "Description": description}]
        else:
            perm["IpRanges"] = [{"CidrIp": cidr, "Description": description}]
        try:
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[perm])
        except ec2.exceptions.from_code("InvalidPermission.Duplicate"):
            pass

    # ALB — internet facing
    _ingress(sgs["alb"], 80,   cidr="0.0.0.0/0",    description="HTTP from internet")
    _ingress(sgs["alb"], 443,  cidr="0.0.0.0/0",    description="HTTPS from internet")

    # App and ML tiers — from ALB only
    _ingress(sgs["app"], 8080, source_sg=sgs["alb"], description="From ALB")
    _ingress(sgs["ml"],  8080, source_sg=sgs["alb"], description="From ALB")

    # RDS — from app and ml tiers only
    _ingress(sgs["db"],  5432, source_sg=sgs["app"], description="Postgres from app tier")
    _ingress(sgs["db"],  5432, source_sg=sgs["ml"],  description="Postgres from ml tier")

    # ElastiCache — from app and ml tiers only
    _ingress(sgs["cache"], 6379, source_sg=sgs["app"], description="Redis from app tier")
    _ingress(sgs["cache"], 6379, source_sg=sgs["ml"],  description="Redis from ml tier")

    log.info("Security group rules attached")


# ── Orchestrator ───────────────────────────────────────────────────────────────

def provision() -> dict:
    """
    Phase 1 entry point. Idempotent — safe to call repeatedly.
    Returns the full networking state dict consumed by later phases.
    """
    log.info("=== Phase 1: Networking ===")

    vpc_id  = create_vpc()
    subnets = create_subnets(vpc_id)
    igw_id  = create_internet_gateway(vpc_id)
    nat_ids = create_nat_gateways(subnets["public"])
    create_route_tables(vpc_id, igw_id, nat_ids, subnets)
    sgs     = create_security_groups(vpc_id)
    attach_security_group_rules(sgs)

    state = {
        "vpc_id":              vpc_id,
        "public_subnet_ids":   subnets["public"],
        "private_subnet_ids":  subnets["private"],
        "database_subnet_ids": subnets["database"],
        "igw_id":              igw_id,
        "nat_ids":             nat_ids,
        "sg_ids":              sgs,
    }
    log.info("Phase 1 complete: %s", state)
    return state
