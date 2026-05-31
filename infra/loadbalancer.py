"""
Phase 2 — Load Balancer Layer

Provisions: Application Load Balancer, target groups, listeners, and routing rules.
Supports weighted routing for A/B testing (Phase 7) and path-based routing for
tier separation (Phase 4 ML inference).

Every function is idempotent.
"""

import logging

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import tag_spec

log = logging.getLogger(__name__)


def create_alb(subnet_ids: list[str], sg_id: str,
               logs_bucket: str = "") -> str:
    """
    Creates internet-facing Application Load Balancer with access logging.
    
    Args:
        subnet_ids: list of public subnet IDs (at least 2 across AZs)
        sg_id: security group ID for ALB
        logs_bucket: S3 bucket for ALB access logs
    
    Returns:
        alb_arn: ready for listener/rule attachment
    """
    elbv2 = aws.get_client("elbv2")
    alb_name = resource_name("alb")
    
    # Check for existing ALB
    try:
        response = elbv2.describe_load_balancers(
            Filters=[
                {"Name": "tag:Name", "Values": [alb_name]},
                {"Name": "tag:Project", "Values": [config.PROJECT]},
            ]
        )
        if response["LoadBalancers"]:
            alb = response["LoadBalancers"][0]
            log.info(f"ALB {alb_name} already exists: {alb['LoadBalancerArn']}")
            return alb["LoadBalancerArn"]
    except Exception as e:
        log.debug(f"Error checking ALB: {e}")
    
    # Create ALB
    log.info(f"Creating ALB {alb_name}")
    response = elbv2.create_load_balancer(
        Name=alb_name,
        Subnets=subnet_ids,
        SecurityGroups=[sg_id],
        Scheme="internet-facing",
        Type="application",
        IpAddressType="ipv4",
        Tags=[
            {"Key": "Name", "Value": alb_name},
            {"Key": "Project", "Value": config.PROJECT},
            {"Key": "Environment", "Value": config.ENV},
            {"Key": "ManagedBy", "Value": "python-boto3"},
        ],
    )
    
    alb_arn = response["LoadBalancers"][0]["LoadBalancerArn"]
    alb_dns = response["LoadBalancers"][0]["DNSName"]
    log.info(f"ALB created: {alb_dns}")
    
    # Enable access logging if bucket provided
    if logs_bucket:
        log.info(f"Enabling access logging to {logs_bucket}")
        try:
            elbv2.modify_load_balancer_attributes(
                LoadBalancerArn=alb_arn,
                Attributes=[
                    {
                        "Key": "access_logs.s3.enabled",
                        "Value": "true",
                    },
                    {
                        "Key": "access_logs.s3.bucket",
                        "Value": logs_bucket,
                    },
                ],
            )
        except Exception as e:
            log.warning(f"Failed to enable ALB access logging: {e}")
    
    return alb_arn


def create_target_group(name: str, vpc_id: str, port: int = 8080,
                        health_check_path: str = "/health",
                        protocol: str = "HTTP") -> str:
    """
    Creates ALB target group with health check configuration.
    
    Args:
        name: logical name (e.g. "web" → "ai-infra-dev-tg-web")
        vpc_id: VPC ID
        port: target port
        health_check_path: path for ALB health checks
        protocol: HTTP or HTTPS
    
    Returns:
        target_group_arn: ready for ASG attachment or listener routing
    """
    elbv2 = aws.get_client("elbv2")
    tg_name = resource_name(f"tg-{name}")
    
    # Check for existing target group
    try:
        response = elbv2.describe_target_groups(
            Filters=[
                {"Name": "tag:Name", "Values": [tg_name]},
                {"Name": "tag:Project", "Values": [config.PROJECT]},
            ]
        )
        if response["TargetGroups"]:
            tg = response["TargetGroups"][0]
            log.info(f"Target group {tg_name} already exists: {tg['TargetGroupArn']}")
            return tg["TargetGroupArn"]
    except Exception as e:
        log.debug(f"Error checking target group: {e}")
    
    # Create target group
    log.info(f"Creating target group {tg_name}")
    response = elbv2.create_target_group(
        Name=tg_name,
        Protocol=protocol,
        Port=port,
        VpcId=vpc_id,
        HealthCheckProtocol=protocol,
        HealthCheckPath=health_check_path,
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        Matcher={"HttpCode": "200"},
        TargetType="instance",
        Tags=[
            {"Key": "Name", "Value": tg_name},
            {"Key": "Project", "Value": config.PROJECT},
            {"Key": "Environment", "Value": config.ENV},
            {"Key": "ManagedBy", "Value": "python-boto3"},
        ],
    )
    
    tg_arn = response["TargetGroups"][0]["TargetGroupArn"]
    
    # Set additional attributes
    elbv2.modify_target_group_attributes(
        TargetGroupArn=tg_arn,
        Attributes=[
            {
                "Key": "deregistration_delay.timeout_seconds",
                "Value": "30",  # Connection draining
            },
            {
                "Key": "stickiness.enabled",
                "Value": "true",
            },
            {
                "Key": "stickiness.type",
                "Value": "lb_cookie",
            },
            {
                "Key": "stickiness.lb_cookie.duration_seconds",
                "Value": "86400",
            },
        ],
    )
    
    log.info(f"Target group created: {tg_arn}")
    return tg_arn


def create_listener(alb_arn: str, default_tg_arn: str,
                    port: int = 80, protocol: str = "HTTP") -> str:
    """
    Creates ALB listener with default action forwarding to target group.
    
    Args:
        alb_arn: ALB ARN
        default_tg_arn: default target group ARN for forward action
        port: listener port
        protocol: HTTP or HTTPS
    
    Returns:
        listener_arn: ready for rule attachment
    """
    elbv2 = aws.get_client("elbv2")
    
    # Check for existing listener on this ALB and port
    try:
        response = elbv2.describe_listeners(LoadBalancerArn=alb_arn)
        for listener in response["Listeners"]:
            if listener["Port"] == port:
                log.info(f"Listener on port {port} already exists: {listener['ListenerArn']}")
                return listener["ListenerArn"]
    except Exception as e:
        log.debug(f"Error checking listeners: {e}")
    
    # Create listener
    log.info(f"Creating listener on port {port}")
    response = elbv2.create_listener(
        LoadBalancerArn=alb_arn,
        Protocol=protocol,
        Port=port,
        DefaultActions=[
            {
                "Type": "forward",
                "TargetGroupArn": default_tg_arn,
            }
        ],
    )
    
    listener_arn = response["Listeners"][0]["ListenerArn"]
    log.info(f"Listener created: {listener_arn}")
    return listener_arn


def add_listener_rule(listener_arn: str, tg_arn: str,
                      path_patterns: list[str],
                      priority: int = None) -> str:
    """
    Adds a path-pattern forwarding rule to an existing listener.
    Used to route /api/predict/* to ML tier in Phase 4.
    
    Args:
        listener_arn: listener ARN
        tg_arn: target group ARN to forward to
        path_patterns: e.g. ["/api/predict/*"]
        priority: rule priority (auto-incremented if None)
    
    Returns:
        rule_arn
    """
    elbv2 = aws.get_client("elbv2")
    
    # If priority not specified, auto-increment
    if priority is None:
        try:
            response = elbv2.describe_rules(ListenerArn=listener_arn)
            # Find max priority (excluding default rule which has priority 'default')
            existing = [r["Priority"] for r in response["Rules"] if r["Priority"] != "default"]
            priority = max([int(p) for p in existing] or [0]) + 1
        except Exception as e:
            log.debug(f"Error auto-incrementing priority: {e}")
            priority = 1
    
    # Check if rule already exists for these patterns
    try:
        response = elbv2.describe_rules(ListenerArn=listener_arn)
        for rule in response["Rules"]:
            if rule["Priority"] != "default":
                conditions = rule.get("Conditions", [])
                for cond in conditions:
                    if cond["Field"] == "path-pattern" and set(cond["Values"]) == set(path_patterns):
                        log.info(f"Rule for paths {path_patterns} already exists: {rule['RuleArn']}")
                        return rule["RuleArn"]
    except Exception as e:
        log.debug(f"Error checking rules: {e}")
    
    # Create rule
    log.info(f"Adding rule for paths {path_patterns} with priority {priority}")
    response = elbv2.create_rule(
        ListenerArn=listener_arn,
        Conditions=[
            {
                "Field": "path-pattern",
                "Values": path_patterns,
            }
        ],
        Priority=priority,
        Actions=[
            {
                "Type": "forward",
                "TargetGroupArn": tg_arn,
            }
        ],
    )
    
    rule_arn = response["Rules"][0]["RuleArn"]
    log.info(f"Rule created: {rule_arn}")
    return rule_arn


def add_weighted_rule(listener_arn: str,
                      tg_v1_arn: str, weight_v1: int,
                      tg_v2_arn: str, weight_v2: int,
                      path_patterns: list[str],
                      priority: int = None) -> str:
    """
    Adds a weighted forward rule for A/B model testing (Phase 7c).
    Distributes traffic between two target groups by weight.
    
    Args:
        listener_arn: listener ARN
        tg_v1_arn: first target group ARN (e.g. v1 model)
        weight_v1: weight for v1 (e.g. 90)
        tg_v2_arn: second target group ARN (e.g. v2 model)
        weight_v2: weight for v2 (e.g. 10)
        path_patterns: e.g. ["/api/predict/*"]
        priority: rule priority (auto-incremented if None)
    
    Returns:
        rule_arn
    """
    elbv2 = aws.get_client("elbv2")
    
    # If priority not specified, auto-increment
    if priority is None:
        try:
            response = elbv2.describe_rules(ListenerArn=listener_arn)
            existing = [r["Priority"] for r in response["Rules"] if r["Priority"] != "default"]
            priority = max([int(p) for p in existing] or [0]) + 1
        except Exception as e:
            log.debug(f"Error auto-incrementing priority: {e}")
            priority = 1
    
    # Create weighted rule
    log.info(f"Adding weighted rule {path_patterns}: {weight_v1}% v1, {weight_v2}% v2")
    response = elbv2.create_rule(
        ListenerArn=listener_arn,
        Conditions=[
            {
                "Field": "path-pattern",
                "Values": path_patterns,
            }
        ],
        Priority=priority,
        Actions=[
            {
                "Type": "forward",
                "ForwardConfig": {
                    "TargetGroups": [
                        {
                            "TargetGroupArn": tg_v1_arn,
                            "Weight": weight_v1,
                        },
                        {
                            "TargetGroupArn": tg_v2_arn,
                            "Weight": weight_v2,
                        },
                    ],
                },
            }
        ],
    )
    
    rule_arn = response["Rules"][0]["RuleArn"]
    log.info(f"Weighted rule created: {rule_arn}")
    return rule_arn


def provision_loadbalancer(vpc_id: str, subnet_ids: list[str],
                           sg_id: str, logs_bucket: str = "") -> dict:
    """
    Orchestrator for load balancer and target group creation.
    
    Returns:
        dict with alb_arn, web_tg_arn, listener_arn
    """
    log.info("Provisioning load balancer tier")
    
    # Create ALB
    alb_arn = create_alb(
        subnet_ids=subnet_ids,
        sg_id=sg_id,
        logs_bucket=logs_bucket,
    )
    
    # Create web target group
    web_tg_arn = create_target_group(
        name="web",
        vpc_id=vpc_id,
        port=8080,
        health_check_path="/health",
    )
    
    # Create listener with web target group as default
    listener_arn = create_listener(
        alb_arn=alb_arn,
        default_tg_arn=web_tg_arn,
        port=80,
    )
    
    return {
        "alb_arn": alb_arn,
        "web_tg_arn": web_tg_arn,
        "listener_arn": listener_arn,
    }
