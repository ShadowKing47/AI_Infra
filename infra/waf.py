"""
Phase 5 — WAF + Security Hardening

Provisions: WAF WebACL with AWS managed rules, rate limiting, and logging to S3.
Attaches to ALB for protection of all endpoints including /api/predict/*.

Every function is idempotent — checks for existing resource before creating.
"""

import json
import logging
import os
from botocore.exceptions import ClientError

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import build_tags

log = logging.getLogger(__name__)

# AWS Managed Rule Group ARNs (standard across regions)
_MANAGED_RULE_GROUPS = {
    "common": "arn:aws:wafv2:{region}:{account}:regional/rulegroup/AWSManagedRulesCommonRuleSet",
    "sqli": "arn:aws:wafv2:{region}:{account}:regional/rulegroup/AWSManagedRulesSQLiRuleSet",
    "known_bad_inputs": "arn:aws:wafv2:{region}:{account}:regional/rulegroup/AWSManagedRulesKnownBadInputsRuleSet",
    "anon_ip": "arn:aws:wafv2:{region}:{account}:regional/rulegroup/AWSManagedRulesAnonymousIpList",
    "admin_protection": "arn:aws:wafv2:{region}:{account}:regional/rulegroup/AWSManagedRulesAdminProtectionRuleSet",
}


def _get_account_id() -> str:
    """Get current AWS account ID."""
    sts = aws.get_client("sts")
    return sts.get_caller_identity()["Account"]


def _get_managed_rule_arn(rule_name: str) -> str:
    """Get ARN for AWS managed rule group."""
    region = aws.region()
    account = _get_account_id()
    template = _MANAGED_RULE_GROUPS.get(rule_name)
    if not template:
        raise ValueError(f"Unknown managed rule group: {rule_name}")
    return template.format(region=region, account=account)


def _is_localstack_pro() -> bool:
    """Check if running on LocalStack Pro (supports WAF actions beyond Count)."""
    return os.getenv("LOCALSTACK_PRO", "").lower() in ("1", "true", "yes")


def _is_resource_not_found(e: Exception) -> bool:
    """Check if exception is a 'ResourceNotFound' type error."""
    return "ResourceNotFound" in type(e).__name__ or "NotFound" in str(e)


def _is_no_such_entity(e: Exception) -> bool:
    """Check if exception is a 'NoSuchEntity' type error."""
    return "NoSuchEntity" in type(e).__name__ or "NoSuchEntity" in str(e)


def create_web_acl(name: str, alb_arn: str) -> str:
    """
    Creates WAF WebACL with managed rules and rate limiting.
    
    Args:
        name: logical name for the WebACL
        alb_arn: ALB ARN to associate with
        
    Returns:
        web_acl_arn: ready for association with ALB
    """
    waf = aws.get_client("wafv2")
    web_acl_name = resource_name(f"waf-{name}")
    scope = "REGIONAL"  # ALB uses REGIONAL scope
    
    # Check for existing WebACL
    try:
        response = waf.list_web_acls(Scope=scope)
        for acl in response["WebACLs"]:
            if acl["Name"] == web_acl_name:
                log.info(f"WebACL {web_acl_name} already exists: {acl['ARN']}")
                # Ensure it's associated with the ALB
                _associate_web_acl(waf, acl["ARN"], alb_arn)
                return acl["ARN"]
    except Exception as e:
        log.debug(f"Error checking WebACL: {e}")
    
    # Determine rule action: COUNT for LocalStack free tier, BLOCK for Pro/prod
    default_action = "BLOCK" if _is_localstack_pro() or config.ENV != "dev" else "COUNT"
    log.info(f"Creating WebACL {web_acl_name} with default action: {default_action}")
    
    # Build rules
    rules = []
    priority = 1
    
    # Rule 1: AWS Managed Common Rule Set (OWASP Top 10)
    rules.append({
        "Name": "CommonRuleSet",
        "Priority": priority,
        "Statement": {
            "ManagedRuleGroupStatement": {
                "VendorName": "AWS",
                "Name": "AWSManagedRulesCommonRuleSet",
                "Version": "Latest",
                "ExcludedRules": [],  # Can exclude specific rules if needed
            }
        },
        "OverrideAction": {"None": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "CommonRuleSet",
        },
    })
    priority += 1
    
    # Rule 2: SQL Injection protection
    rules.append({
        "Name": "SQLiRuleSet",
        "Priority": priority,
        "Statement": {
            "ManagedRuleGroupStatement": {
                "VendorName": "AWS",
                "Name": "AWSManagedRulesSQLiRuleSet",
                "Version": "Latest",
            }
        },
        "OverrideAction": {"None": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "SQLiRuleSet",
        },
    })
    priority += 1
    
    # Rule 3: Known bad inputs
    rules.append({
        "Name": "KnownBadInputsRuleSet",
        "Priority": priority,
        "Statement": {
            "ManagedRuleGroupStatement": {
                "VendorName": "AWS",
                "Name": "AWSManagedRulesKnownBadInputsRuleSet",
                "Version": "Latest",
            }
        },
        "OverrideAction": {"None": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "KnownBadInputsRuleSet",
        },
    })
    priority += 1
    
    # Rule 4: Anonymous IP list (VPN, Tor, hosting providers)
    rules.append({
        "Name": "AnonymousIpRuleSet",
        "Priority": priority,
        "Statement": {
            "ManagedRuleGroupStatement": {
                "VendorName": "AWS",
                "Name": "AWSManagedRulesAnonymousIpList",
                "Version": "Latest",
            }
        },
        "OverrideAction": {"None": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "AnonymousIpRuleSet",
        },
    })
    priority += 1
    
    # Rule 5: Rate limiting on /api/predict/* (1000 requests per 5 minutes per IP)
    rules.append({
        "Name": "PredictRateLimit",
        "Priority": priority,
        "Statement": {
            "RateBasedStatement": {
                "Limit": 1000,
                "AggregateKeyType": "IP",
                "ScopeDownStatement": {
                    "ByteMatchStatement": {
                        "SearchString": "/api/predict/",
                        "FieldToMatch": {"UriPath": {}},
                        "TextTransformations": [
                            {"Priority": 0, "Type": "NONE"}
                        ],
                        "PositionalConstraint": "STARTS_WITH",
                    }
                },
            }
        },
        "Action": {"Block": {}},
        "VisibilityConfig": {
            "SampledRequestsEnabled": True,
            "CloudWatchMetricsEnabled": True,
            "MetricName": "PredictRateLimit",
        },
    })
    priority += 1
    
    # Create WebACL
    try:
        response = waf.create_web_acl(
            Name=web_acl_name,
            Scope=scope,
            DefaultAction={default_action: {}},
            Rules=rules,
            VisibilityConfig={
                "SampledRequestsEnabled": True,
                "CloudWatchMetricsEnabled": True,
                "MetricName": web_acl_name,
            },
            Tags=build_tags(f"waf-{name}"),
        )
        web_acl_arn = response["Summary"]["ARN"]
        log.info(f"WebACL created: {web_acl_arn}")
    except waf.exceptions.WAFDuplicateItemException:
        # Race condition - might have been created by another process
        response = waf.list_web_acls(Scope=scope)
        for acl in response["WebACLs"]:
            if acl["Name"] == web_acl_name:
                web_acl_arn = acl["ARN"]
                log.info(f"WebACL already exists: {web_acl_arn}")
                break
        else:
            raise
    
    # Associate with ALB
    _associate_web_acl(waf, web_acl_arn, alb_arn)
    
    return web_acl_arn


def _associate_web_acl(waf, web_acl_arn: str, alb_arn: str) -> None:
    """Associate WebACL with ALB if not already associated."""
    try:
        waf.associate_web_acl(
            WebACLArn=web_acl_arn,
            ResourceArn=alb_arn,
        )
        log.info(f"Associated WebACL with ALB")
    except waf.exceptions.WAFNonexistentItemException:
        log.warning(f"WebACL or ALB not found for association")
    except Exception as e:
        # Might already be associated
        if "already associated" not in str(e).lower():
            log.warning(f"Failed to associate WebACL: {e}")


def enable_waf_logging(web_acl_arn: str, firehose_arn: str) -> None:
    """
    Enables WAF logging to Kinesis Firehose.
    
    Args:
        web_acl_arn: WebACL ARN
        firehose_arn: Firehose delivery stream ARN (should deliver to S3 waf-logs/ prefix)
    """
    waf = aws.get_client("wafv2")
    
    log.info(f"Enabling WAF logging to Firehose: {firehose_arn}")
    
    try:
        waf.put_logging_configuration(
            LoggingConfiguration={
                "ResourceArn": web_acl_arn,
                "LogDestinationConfigs": [firehose_arn],
                "RedactedFields": [
                    {"SingleHeader": {"Name": "authorization"}},
                    {"SingleHeader": {"Name": "cookie"}},
                    {"SingleQueryArgument": {"Name": "token"}},
                ],
            }
        )
        log.info("WAF logging enabled")
    except Exception as e:
        log.error(f"Failed to enable WAF logging: {e}")
        raise


def create_firehose_for_waf_logs(bucket_name: str, role_arn: str = "") -> str:
    """
    Creates Kinesis Firehose delivery stream for WAF logs to S3.
    
    Args:
        bucket_name: S3 bucket for WAF logs
        role_arn: IAM role ARN for Firehose (created if empty)
        
    Returns:
        firehose_arn: Delivery stream ARN
    """
    firehose = aws.get_client("firehose")
    iam = aws.get_client("iam")
    
    stream_name = resource_name("waf-logs-firehose")
    
    # Check for existing stream
    try:
        response = firehose.describe_delivery_stream(DeliveryStreamName=stream_name)
        stream = response["DeliveryStreamDescription"]
        log.info(f"Firehose stream {stream_name} already exists: {stream['DeliveryStreamARN']}")
        return stream["DeliveryStreamARN"]
    except Exception as e:
        if _is_resource_not_found(e):
            pass
        else:
            log.debug(f"Error checking Firehose: {e}")
    
    # Create IAM role for Firehose if not provided
    if not role_arn:
        role_name = resource_name("firehose-waf-logs-role")
        try:
            iam.get_role(RoleName=role_name)
            log.info(f"Firehose role {role_name} already exists")
        except Exception as e:
            if _is_no_such_entity(e):
                log.info(f"Creating Firehose role {role_name}")
                assume_role_doc = {
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Principal": {"Service": "firehose.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }],
                }
                iam.create_role(
                    RoleName=role_name,
                    AssumeRolePolicyDocument=json.dumps(assume_role_doc),
                    Tags=build_tags("firehose-waf-logs-role"),
                )
                
                # Attach policy for S3 write
                policy = {
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": [
                            "s3:AbortMultipartUpload",
                            "s3:GetBucketLocation",
                            "s3:GetObject",
                            "s3:ListBucket",
                            "s3:ListBucketMultipartUploads",
                            "s3:PutObject",
                        ],
                        "Resource": [
                            f"arn:aws:s3:::{bucket_name}",
                            f"arn:aws:s3:::{bucket_name}/*",
                        ],
                    }],
                }
            else:
                raise
            iam.put_role_policy(
                RoleName=role_name,
                PolicyName="S3WritePolicy",
                PolicyDocument=json.dumps(policy),
            )
        
        role = iam.get_role(RoleName=role_name)
        role_arn = role["Role"]["Arn"]
    
    # Create Firehose delivery stream
    log.info(f"Creating Firehose stream {stream_name}")
    response = firehose.create_delivery_stream(
        DeliveryStreamName=stream_name,
        DeliveryStreamType="DirectPut",
        S3DestinationConfiguration={
            "RoleARN": role_arn,
            "BucketARN": f"arn:aws:s3:::{bucket_name}",
            "Prefix": "waf-logs/",
            "ErrorOutputPrefix": "waf-logs/errors/",
            "BufferingHints": {
                "SizeInMBs": 5,
                "IntervalInSeconds": 60,
            },
            "CompressionFormat": "GZIP",
            "EncryptionConfiguration": {
                "NoEncryptionConfig": "NoEncryption",
            },
            "CloudWatchLoggingOptions": {
                "Enabled": True,
                "LogGroupName": f"/aws/kinesisfirehose/{stream_name}",
                "LogStreamName": "S3Delivery",
            },
        },
        Tags=build_tags("waf-logs-firehose"),
    )
    
    firehose_arn = response["DeliveryStreamARN"]
    log.info(f"Firehose stream created: {firehose_arn}")
    
    # Wait for active state
    import time
    for _ in range(30):
        status = firehose.describe_delivery_stream(DeliveryStreamName=stream_name)["DeliveryStreamDescription"]["DeliveryStreamStatus"]
        if status == "ACTIVE":
            break
        time.sleep(2)
    
    return firehose_arn


def add_ip_whitelist(web_acl_arn: str, cidrs: list[str]) -> None:
    """
    Adds IP set rule evaluated before rate-limit rule.
    Used to exempt known partner/internal IPs from rate limiting.
    
    Args:
        web_acl_arn: WebACL ARN
        cidrs: List of CIDR blocks to whitelist (e.g., ["10.0.0.0/8", "192.168.1.0/24"])
    """
    if not cidrs:
        log.info("No CIDRs provided for whitelist, skipping")
        return
    
    waf = aws.get_client("wafv2")
    
    # Create IP set
    ip_set_name = resource_name("waf-whitelist-ips")
    scope = "REGIONAL"
    
    try:
        ip_set = waf.create_ip_set(
            Name=ip_set_name,
            Scope=scope,
            IPAddressVersion="IPV4",
            Addresses=cidrs,
            Tags=build_tags("waf-whitelist-ips"),
        )
        ip_set_arn = ip_set["Summary"]["ARN"]
        log.info(f"Created IP set for whitelist: {ip_set_arn}")
    except Exception as e:
        if "WAFDuplicateItemException" in type(e).__name__:
            # Get existing
            response = waf.list_ip_sets(Scope=scope, Limit=100)
            for ip_set in response["IPSets"]:
                if ip_set["Name"] == ip_set_name:
                    ip_set_arn = ip_set["ARN"]
                    # Update addresses
                    waf.update_ip_set(
                        Name=ip_set_name,
                        Scope=scope,
                        Id=ip_set["Id"],
                        LockToken=waf.get_ip_set(Name=ip_set_name, Scope=scope, Id=ip_set["Id"])["LockToken"],
                        Addresses=cidrs,
                    )
                    log.info(f"Updated IP set: {ip_set_arn}")
                    break
        else:
            raise
    except Exception as e:
        log.error(f"Failed to create IP set: {e}")
        raise
    
    # Add rule to WebACL (high priority so it's evaluated first)
    try:
        # Get current WebACL to get lock token and existing rules
        web_acl = waf.get_web_acl(Name=resource_name("waf-ml"), Scope=scope, Id="")
        lock_token = web_acl["LockToken"]
        existing_rules = web_acl["WebACL"]["Rules"]
        
        # Check if whitelist rule already exists
        for rule in existing_rules:
            if rule["Name"] == "IPWhitelist":
                log.info("IP Whitelist rule already exists")
                return
        
        # Add new rule at priority 0 (evaluated first)
        new_rule = {
            "Name": "IPWhitelist",
            "Priority": 0,
            "Statement": {
                "IPSetReferenceStatement": {
                    "ARN": ip_set_arn,
                }
            },
            "Action": {"Allow": {}},
            "VisibilityConfig": {
                "SampledRequestsEnabled": True,
                "CloudWatchMetricsEnabled": True,
                "MetricName": "IPWhitelist",
            },
        }
        
        updated_rules = [new_rule] + existing_rules
        
        # Re-number priorities for existing rules
        for i, rule in enumerate(updated_rules[1:], 1):
            rule["Priority"] = i
        
        waf.update_web_acl(
            Name=resource_name("waf-ml"),
            Scope=scope,
            Id="",
            DefaultAction=web_acl["WebACL"]["DefaultAction"],
            Rules=updated_rules,
            VisibilityConfig=web_acl["WebACL"]["VisibilityConfig"],
            LockToken=lock_token,
        )
        log.info("Added IP whitelist rule to WebACL")
    except Exception as e:
        log.error(f"Failed to add IP whitelist rule: {e}")
        raise


def set_rule_action(web_acl_name: str, rule_name: str, action: str) -> None:
    """
    Changes a rule's action (COUNT -> BLOCK or vice versa).
    Used after reviewing false positives in Count mode.
    
    Args:
        web_acl_name: WebACL name
        rule_name: Rule name to modify
        action: "COUNT" or "BLOCK"
    """
    waf = aws.get_client("wafv2")
    scope = "REGIONAL"
    
    try:
        web_acl = waf.get_web_acl(Name=web_acl_name, Scope=scope, Id="")
        lock_token = web_acl["LockToken"]
        rules = web_acl["WebACL"]["Rules"]
        
        for rule in rules:
            if rule["Name"] == rule_name:
                if action == "BLOCK":
                    rule["Action"] = {"Block": {}}
                elif action == "COUNT":
                    rule["OverrideAction"] = {"Count": {}}
                    rule["Action"] = {"Count": {}}
                else:
                    raise ValueError(f"Invalid action: {action}")
                
                waf.update_web_acl(
                    Name=web_acl_name,
                    Scope=scope,
                    Id="",
                    DefaultAction=web_acl["WebACL"]["DefaultAction"],
                    Rules=rules,
                    VisibilityConfig=web_acl["WebACL"]["VisibilityConfig"],
                    LockToken=lock_token,
                )
                log.info(f"Changed rule {rule_name} action to {action}")
                return
        
        log.warning(f"Rule {rule_name} not found in WebACL")
    except Exception as e:
        log.error(f"Failed to set rule action: {e}")
        raise


def provision_waf(alb_arn: str, waf_logs_bucket: str) -> dict:
    """
    Orchestrator for WAF provisioning.
    
    Args:
        alb_arn: ALB ARN to protect
        waf_logs_bucket: S3 bucket for WAF logs
        
    Returns:
        dict with web_acl_arn, firehose_arn
    """
    log.info("=== Phase 5: WAF + Security Hardening ===")
    
    # Create WebACL
    web_acl_arn = create_web_acl("ml", alb_arn)
    
    # Create Firehose for WAF logs
    firehose_arn = create_firehose_for_waf_logs(waf_logs_bucket)
    
    # Enable WAF logging
    enable_waf_logging(web_acl_arn, firehose_arn)
    
    return {
        "web_acl_arn": web_acl_arn,
        "waf_firehose_arn": firehose_arn,
    }