"""
Phase 2 — Compute Layer

Provisions: EC2 Launch Templates and Auto Scaling Groups for the web tier.
The provision_compute() function is reused in Phase 4 for the ML inference tier.

Every function is idempotent — checks for existing resource before creating.
"""

import json
import logging
import time

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import build_tags, tag_spec

log = logging.getLogger(__name__)


def create_launch_template(tier_name: str, instance_type: str, ami_id: str,
                           sg_ids: list[str], user_data: str = "",
                           profile_arn: str = "") -> str:
    """
    Creates (or updates) a launch template for the given tier.
    
    Args:
        tier_name: "web" or "ml" (used in resource naming)
        instance_type: e.g. "t3.micro", "c5.xlarge"
        ami_id: Amazon Machine Image ID (e.g. ami-12345678)
        sg_ids: list of security group IDs to attach
        user_data: optional base64-encoded user data script
        profile_arn: optional IAM instance profile ARN (created if empty)
    
    Returns:
        launch_template_id: ready for use in ASG
    """
    ec2 = aws.get_client("ec2")
    iam = aws.get_client("iam")
    template_name = resource_name(f"{tier_name}-launch-template")
    
    # Check for existing launch template
    try:
        response = ec2.describe_launch_templates(
            Filters=[
                {"Name": "tag:Name", "Values": [template_name]},
                {"Name": "tag:Project", "Values": [config.PROJECT]},
            ]
        )
        if response["LaunchTemplates"]:
            template = response["LaunchTemplates"][0]
            log.info(f"Launch template {template_name} already exists: {template['LaunchTemplateId']}")
            return template["LaunchTemplateId"]
    except Exception as e:
        log.debug(f"Error checking launch template: {e}")
    
    # Create minimal IAM instance profile if not provided
    if not profile_arn:
        profile_name = resource_name(f"{tier_name}-instance-profile")
        role_name = resource_name(f"{tier_name}-instance-role")
        
        # Check if role already exists
        try:
            iam.get_role(RoleName=role_name)
            log.info(f"IAM role {role_name} already exists")
        except iam.exceptions.NoSuchEntityException:
            # Create role with SSM and CloudWatch permissions
            assume_role_doc = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ec2.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
            log.info(f"Creating IAM role {role_name}")
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(assume_role_doc),
                Tags=build_tags(f"{tier_name}-instance-role"),
            )
            
            # Attach managed policies for SSM and CloudWatch
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            )
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
            )
        
        # Check if instance profile exists
        try:
            iam.get_instance_profile(InstanceProfileName=profile_name)
            log.info(f"Instance profile {profile_name} already exists")
        except iam.exceptions.NoSuchEntityException:
            log.info(f"Creating instance profile {profile_name}")
            iam.create_instance_profile(InstanceProfileName=profile_name)
            iam.add_role_to_instance_profile(
                InstanceProfileName=profile_name,
                RoleName=role_name,
            )
        
        # Get the instance profile ARN
        profile = iam.get_instance_profile(InstanceProfileName=profile_name)
        profile_arn = profile["InstanceProfile"]["Arn"]
    
    # Create launch template
    log.info(f"Creating launch template {template_name}")
    response = ec2.create_launch_template(
        LaunchTemplateName=template_name,
        LaunchTemplateData={
            "ImageId": ami_id,
            "InstanceType": instance_type,
            "SecurityGroupIds": sg_ids,
            "IamInstanceProfile": {"Arn": profile_arn},
            "Monitoring": {"Enabled": True},  # Enable detailed monitoring
            "MetadataOptions": {
                "HttpTokens": "required",  # Enforce IMDSv2
                "HttpPutResponseHopLimit": 1,
            },
            "TagSpecifications": tag_spec("instance", f"{tier_name}-instance", extra=[
                {"Key": "Tier", "Value": tier_name},
            ]),
            "UserData": user_data,
        },
        TagSpecifications=tag_spec("launch-template", f"{tier_name}-launch-template"),
    )
    
    template_id = response["LaunchTemplate"]["LaunchTemplateId"]
    log.info(f"Launch template created: {template_id}")
    return template_id


def create_asg(tier_name: str, launch_template_id: str, subnet_ids: list[str],
               target_group_arns: list[str], min_size: int = 1,
               max_size: int = 4, desired: int = 1,
               enable_warm_pool: bool = False) -> str:
    """
    Creates Auto Scaling Group with CPU target-tracking policy.
    
    Args:
        tier_name: "web" or "ml"
        launch_template_id: from create_launch_template()
        subnet_ids: list of subnet IDs to launch instances in
        target_group_arns: list of ALB target group ARNs
        min_size: minimum instances
        max_size: maximum instances
        desired: desired capacity
        enable_warm_pool: pre-warm instances for Phase 4 ML inference
    
    Returns:
        asg_name: ready for use in monitoring/scaling policies
    """
    asg = aws.get_client("autoscaling")
    asg_name = resource_name(f"{tier_name}-asg")
    
    # Check for existing ASG
    try:
        response = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        if response["AutoScalingGroups"]:
            log.info(f"ASG {asg_name} already exists")
            return asg_name
    except Exception as e:
        log.debug(f"Error checking ASG: {e}")
    
    # Create ASG
    log.info(f"Creating ASG {asg_name}")
    asg.create_auto_scaling_group(
        AutoScalingGroupName=asg_name,
        LaunchTemplate={
            "LaunchTemplateId": launch_template_id,
            "Version": "$Latest",
        },
        MinSize=min_size,
        MaxSize=max_size,
        DesiredCapacity=desired,
        VPCZoneIdentifier=",".join(subnet_ids),
        TargetGroupARNs=target_group_arns,
        HealthCheckType="ELB",
        HealthCheckGracePeriod=120,  # Give instances time to boot and pass ALB health check
        Tags=[
            {
                "Key": "Name",
                "Value": resource_name(f"{tier_name}-instance"),
                "PropagateAtLaunch": True,
                "ResourceId": asg_name,
                "ResourceType": "auto-scaling-group",
            },
            {
                "Key": "Tier",
                "Value": tier_name,
                "PropagateAtLaunch": True,
                "ResourceId": asg_name,
                "ResourceType": "auto-scaling-group",
            },
        ],
    )
    
    # Create CPU target-tracking scaling policy
    log.info(f"Attaching CPU target-tracking policy to {asg_name}")
    asg.put_scaling_policy(
        AutoScalingGroupName=asg_name,
        PolicyName=resource_name(f"{tier_name}-cpu-scaling"),
        PolicyType="TargetTrackingScaling",
        TargetTrackingConfiguration={
            "PredefinedMetricSpecification": {
                "PredefinedMetricType": "ASGAverageCPUUtilization",
            },
            "TargetValue": 60.0,
            "ScaleOutCooldown": 60,
            "ScaleInCooldown": 300,
        },
    )
    
    # Add warm pool if requested (Phase 4 ML tier)
    if enable_warm_pool:
        log.info(f"Creating warm pool for {asg_name}")
        asg.put_warm_pool(
            AutoScalingGroupName=asg_name,
            MaxGroupPreparedCapacity=max_size,
            PoolState="Running",
        )
    
    log.info(f"ASG {asg_name} created successfully")
    return asg_name


def provision_compute(tier_name: str, instance_type: str = "t3.micro",
                      ami_id: str = "", subnet_ids: list[str] = None,
                      target_group_arns: list[str] = None,
                      sg_ids: list[str] = None, user_data: str = "",
                      min_size: int = 1, max_size: int = 4, desired: int = 1,
                      enable_warm_pool: bool = False) -> dict:
    """
    Orchestrator for a single compute tier.
    
    Returns:
        dict with launch_template_id and asg_name
    """
    log.info(f"Provisioning compute tier: {tier_name}")
    
    # If no AMI specified, use a default (in real deployment, this would be from config)
    if not ami_id:
        ami_id = "ami-12c6146b"  # Default Amazon Linux 2 AMI (LocalStack compatible)
    
    if not subnet_ids:
        subnet_ids = []
    if not target_group_arns:
        target_group_arns = []
    if not sg_ids:
        sg_ids = []
    
    # Create launch template
    template_id = create_launch_template(
        tier_name=tier_name,
        instance_type=instance_type,
        ami_id=ami_id,
        sg_ids=sg_ids,
        user_data=user_data,
    )
    
    # Create ASG
    asg_name = create_asg(
        tier_name=tier_name,
        launch_template_id=template_id,
        subnet_ids=subnet_ids,
        target_group_arns=target_group_arns,
        min_size=min_size,
        max_size=max_size,
        desired=desired,
        enable_warm_pool=enable_warm_pool,
    )
    
    return {
        "launch_template_id": template_id,
        "asg_name": asg_name,
    }
