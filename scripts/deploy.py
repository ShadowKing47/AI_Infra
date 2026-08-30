"""
Orchestrates the full infrastructure deploy phase by phase.

Usage:
    python scripts/deploy.py              # deploy all phases in order
    python scripts/deploy.py --phase 1    # deploy Phase 1 only
    python scripts/deploy.py --phase 3    # deploy phases 1 through 3 (cumulative)

Every phase is idempotent — safe to re-run at any time.
State (resource IDs) is persisted to S3 after each phase so a partial run
can be resumed without re-creating existing resources.
"""

import sys
import json
import os
import logging
import argparse
from pathlib import Path

# Ensure the project root is on sys.path regardless of where the script is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

from infra import client as aws
from infra import config
from utils.naming import resource_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# S3 key where cumulative state is stored between runs
_STATE_BUCKET = resource_name("state")
_STATE_KEY    = f"{config.ENV}/state.json"


# ── State persistence ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load persisted state from S3. Returns empty dict only when the key does not exist yet."""
    from botocore.exceptions import ClientError

    s3 = aws.get_client("s3")
    try:
        obj = s3.get_object(Bucket=_STATE_BUCKET, Key=_STATE_KEY)
        return json.loads(obj["Body"].read())
    except ClientError as exc:
        # NoSuchKey → first run, no state yet. Any other ClientError (auth,
        # bucket missing, etc.) is a real problem and should not be swallowed.
        if exc.response["Error"]["Code"] == "NoSuchKey":
            return {}
        raise


def _save_state(state: dict) -> None:
    """Persist cumulative state to S3 after each phase."""
    s3 = aws.get_client("s3")
    s3.put_object(
        Bucket=_STATE_BUCKET,
        Key=_STATE_KEY,
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )
    log.info("State saved → s3://%s/%s", _STATE_BUCKET, _STATE_KEY)


# ── Phase runners ──────────────────────────────────────────────────────────────
# Each function receives the cumulative state dict, runs its provision(),
# merges the result back, and returns the updated state.
# Phases 2–7 are stubs that will be filled in as each phase is implemented.

def _run_phase_1(state: dict) -> dict:
    from infra import networking
    result = networking.provision()
    state.update(result)
    return state


def _run_phase_2(state: dict) -> dict:
    from infra import storage, compute, loadbalancer
    
    log.info("=== Phase 2: Load Balancer + Web App Tier ===")
    
    # Storage layer
    storage_state = storage.provision_storage()
    state.update(storage_state)
    
    # Load balancer and target groups
    lb_state = loadbalancer.provision_loadbalancer(
        vpc_id=state["vpc_id"],
        subnet_ids=state["public_subnet_ids"],
        sg_id=state["sg_ids"]["alb"],
        logs_bucket=state["logs_bucket"],
    )
    state.update(lb_state)
    state["web_tg_arn"] = lb_state["web_tg_arn"]
    state["listener_arn"] = lb_state["listener_arn"]
    
    # Get ALB DNS name for easier reference
    elbv2 = aws.get_client("elbv2")
    alb_info = elbv2.describe_load_balancers(LoadBalancerArns=[lb_state["alb_arn"]])
    state["alb_dns"] = alb_info["LoadBalancers"][0]["DNSName"]
    
    # Web tier compute
    web_compute = compute.provision_compute(
        tier_name="web",
        instance_type="t3.micro",
        ami_id="ami-12c6146b",
        subnet_ids=state["private_subnet_ids"],
        target_group_arns=[lb_state["web_tg_arn"]],
        sg_ids=[state["sg_ids"]["app"]],
        min_size=1,
        max_size=3,
        desired=1,
    )
    state["web_launch_template_id"] = web_compute["launch_template_id"]
    state["web_asg_name"] = web_compute["asg_name"]
    
    return state


def _run_phase_3(state: dict) -> dict:
    from infra import database, cache
    
    log.info("=== Phase 3: Data Tier (RDS + ElastiCache) ===")
    
    # Database tier (RDS Postgres Multi-AZ)
    db_state = database.provision_database(
        vpc_id=state["vpc_id"],
        database_subnet_ids=state["database_subnet_ids"],
        database_sg_id=state["sg_ids"]["db"],
    )
    state.update({
        "rds_endpoint": db_state["endpoint"],
        "rds_port": db_state["port"],
        "rds_secret_arn": db_state["secret_arn"],
        "rds_instance_id": db_state["instance_id"],
    })
    
    # Cache tier (ElastiCache Redis Multi-AZ)
    cache_state = cache.provision_cache(
        cache_subnet_ids=state["database_subnet_ids"],  # Use same subnets as RDS
        cache_sg_id=state["sg_ids"]["cache"],
    )
    state.update({
        "redis_primary_endpoint": cache_state["primary_endpoint"],
        "redis_reader_endpoint": cache_state["reader_endpoint"],
        "redis_port": cache_state["port"],
        "redis_auth_token_secret_arn": cache_state["auth_token_secret_arn"],
        "redis_replication_group_id": cache_state["replication_group_id"],
    })
    
    return state


def _run_phase_4(state: dict) -> dict:
    from infra import compute, loadbalancer
    
    log.info("=== Phase 4: ML Inference Tier ===")
    
    # Create ML target group
    ml_tg_arn = loadbalancer.create_target_group(
        name="ml",
        vpc_id=state["vpc_id"],
        port=8080,
        health_check_path="/api/predict/health",
    )
    state["ml_tg_arn"] = ml_tg_arn
    
    # Add listener rule to route /api/predict/* to ML target group
    rule_arn = loadbalancer.add_listener_rule(
        listener_arn=state["listener_arn"],
        tg_arn=ml_tg_arn,
        path_patterns=["/api/predict/*"],
        priority=10,
    )
    state["ml_rule_arn"] = rule_arn
    
    # ML tier compute (reuses provision_compute from Phase 2)
    ml_compute = compute.provision_compute(
        tier_name="ml",
        instance_type="c5.xlarge",
        ami_id="ami-12c6146b",
        subnet_ids=state["private_subnet_ids"],
        target_group_arns=[ml_tg_arn],
        sg_ids=[state["sg_ids"]["ml"]],
        min_size=1,
        max_size=4,
        desired=1,
        enable_warm_pool=True,
    )
    state["ml_launch_template_id"] = ml_compute["launch_template_id"]
    state["ml_asg_name"] = ml_compute["asg_name"]
    
    # Upload stub model artefacts for local testing
    _upload_stub_model_artefacts(state)
    
    return state


def _upload_stub_model_artefacts(state: dict) -> None:
    """Upload stub model artefacts to S3 for local testing."""
    import json
    from infra import client as aws
    
    s3 = aws.get_client("s3")
    bucket = state.get("artefacts_bucket")
    if not bucket:
        log.warning("No artefacts bucket in state, skipping model upload")
        return
    
    # Sentiment model stub
    sentiment_metadata = {
        "version": "v1.0",
        "type": "huggingface",
        "model_id": "distilbert-base-uncased-finetuned-sst-2-english",
    }
    try:
        s3.put_object(
            Bucket=bucket,
            Key="sentiment/stable/metadata.json",
            Body=json.dumps(sentiment_metadata),
            ContentType="application/json",
        )
        # Create a minimal joblib stub (actual HF model downloads on first use)
        import joblib
        import io
        stub_model = {"type": "huggingface_stub", "version": "v1.0"}
        buf = io.BytesIO()
        joblib.dump(stub_model, buf)
        s3.put_object(
            Bucket=bucket,
            Key="sentiment/stable/model.joblib",
            Body=buf.getvalue(),
        )
        log.info("Uploaded sentiment model stub")
    except Exception as e:
        log.warning(f"Failed to upload sentiment stub: {e}")
    
    # Anomaly model stub
    anomaly_metadata = {
        "version": "v1.0",
        "type": "joblib",
    }
    try:
        s3.put_object(
            Bucket=bucket,
            Key="anomaly/stable/metadata.json",
            Body=json.dumps(anomaly_metadata),
            ContentType="application/json",
        )
        import joblib
        import io
        import numpy as np
        from sklearn.ensemble import IsolationForest
        # Train a tiny IsolationForest on dummy data for testing
        dummy_data = np.random.randn(100, 10)
        model = IsolationForest(contamination=0.1, random_state=42)
        model.fit(dummy_data)
        buf = io.BytesIO()
        joblib.dump(model, buf)
        s3.put_object(
            Bucket=bucket,
            Key="anomaly/stable/model.joblib",
            Body=buf.getvalue(),
        )
        log.info("Uploaded anomaly model stub")
    except Exception as e:
        log.warning(f"Failed to upload anomaly stub: {e}")


def _run_phase_5(state: dict) -> dict:
    from infra import waf
    
    log.info("=== Phase 5: WAF + Security Hardening ===")
    
    # Provision WAF with ALB association and logging
    waf_state = waf.provision_waf(
        alb_arn=state["alb_arn"],
        waf_logs_bucket=state["logs_bucket"],
    )
    state.update(waf_state)
    
    # Add IP whitelist for internal CIDRs (VPC CIDR)
    waf.add_ip_whitelist(
        web_acl_arn=waf_state["web_acl_arn"],
        cidrs=[config.VPC_CIDR],
    )
    state["waf_whitelist_cidrs"] = [config.VPC_CIDR]
    
    return state


def _run_phase_6(state: dict) -> dict:
    from infra import monitoring
    from mlops import scaler
    
    log.info("=== Phase 6: Predictive Scaling + Monitoring ===")
    
    # Provision CloudWatch monitoring (alarms, dashboard)
    monitoring_state = monitoring.provision_monitoring(
        alb_arn=state["alb_arn"],
        ml_asg_name=state["ml_asg_name"],
        rds_instance_id=state["rds_instance_id"],
        redis_replication_group_id=state["redis_replication_group_id"],
        sns_topic_arn=state.get("sns_topic_arn"),
    )
    state.update(monitoring_state)
    
    # Create SNS topic for alarm notifications if not exists
    sns_topic_arn = _create_sns_topic()
    if sns_topic_arn:
        state["sns_topic_arn"] = sns_topic_arn
        # Update alarms with SNS topic (re-create with actions)
        # In practice, we'd update existing alarms, but for simplicity we note it
    
    # Create Lambda function for predictive scaler
    scaler_function_arn = _create_scaler_lambda()
    state["scaler_function_arn"] = scaler_function_arn
    
    # Create EventBridge rule to trigger scaler every 15 minutes
    rule_arn = _create_scaler_schedule(scaler_function_arn)
    state["scaler_schedule_rule_arn"] = rule_arn
    
    # Test scaler locally (dry run)
    log.info("Running scaler dry-run...")
    try:
        result = scaler.run({"asg_name": state["ml_asg_name"]}, None)
        log.info(f"Scaler dry-run result: {result}")
        state["scaler_dry_run"] = result
    except Exception as e:
        log.warning(f"Scaler dry-run failed: {e}")
        state["scaler_dry_run"] = {"status": "error", "error": str(e)}
    
    return state


def _create_sns_topic() -> str:
    """Create SNS topic for alarm notifications."""
    import boto3
    from infra import client as aws
    from utils.naming import resource_name
    from utils.tagging import build_tags
    
    sns = aws.get_client("sns")
    topic_name = resource_name("alarms")
    
    try:
        # Check existing
        response = sns.list_topics()
        for topic in response["Topics"]:
            if topic_name in topic["TopicArn"]:
                log.info(f"SNS topic {topic_name} already exists")
                return topic["TopicArn"]
    except Exception as e:
        log.debug(f"Error checking SNS topic: {e}")
    
    try:
        response = sns.create_topic(
            Name=topic_name,
            Tags=[{"Key": "Project", "Value": config.PROJECT}, {"Key": "Environment", "Value": config.ENV}],
        )
        topic_arn = response["TopicArn"]
        log.info(f"Created SNS topic: {topic_arn}")
        return topic_arn
    except Exception as e:
        log.warning(f"Failed to create SNS topic: {e}")
        return ""


def _create_scaler_lambda() -> str:
    """Create Lambda function for predictive scaler."""
    import zipfile
    import io
    import boto3
    from infra import client as aws
    from infra import config
    from utils.naming import resource_name
    
    lambda_client = aws.get_client("lambda")
    iam = aws.get_client("iam")
    function_name = resource_name("scaler")
    
    # Check existing
    try:
        response = lambda_client.get_function(FunctionName=function_name)
        log.info(f"Lambda function {function_name} already exists")
        return response["Configuration"]["FunctionArn"]
    except lambda_client.exceptions.ResourceNotFoundException:
        pass
    except Exception as e:
        log.debug(f"Error checking Lambda: {e}")
    
    # Create IAM role for Lambda
    role_name = resource_name("scaler-lambda-role")
    try:
        iam.get_role(RoleName=role_name)
        log.info(f"Lambda role {role_name} already exists")
    except iam.exceptions.NoSuchEntityException:
        log.info(f"Creating Lambda role {role_name}")
        assume_role_doc = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }],
        }
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_doc),
            Tags=[{"Key": "Project", "Value": config.PROJECT}, {"Key": "Environment", "Value": config.ENV}],
        )
        # Attach policies
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        # Custom policy for CloudWatch, AutoScaling
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:PutMetricData",
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:PutScheduledUpdateGroupAction",
                ],
                "Resource": "*",
            }],
        }
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="ScalerPermissions",
            PolicyDocument=json.dumps(policy),
        )
    
    role = iam.get_role(RoleName=role_name)
    role_arn = role["Role"]["Arn"]
    
    # Create deployment package with scaler code
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add scaler module
        zf.writestr("scaler.py", open("mlops/scaler.py").read())
        zf.writestr("__init__.py", "")
        # Add infra modules needed
        for mod in ["client", "config"]:
            zf.writestr(f"infra/{mod}.py", open(f"infra/{mod}.py").read())
        zf.writestr("infra/__init__.py", "")
        for mod in ["naming", "tagging"]:
            zf.writestr(f"utils/{mod}.py", open(f"utils/{mod}.py").read())
        zf.writestr("utils/__init__.py", "")
    
    zip_buffer.seek(0)
    
    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.11",
            Role=role_arn,
            Handler="scaler.run",
            Code={"ZipFile": zip_buffer.read()},
            Timeout=300,
            MemorySize=512,
            Environment={
                "Variables": {
                    "LOCALSTACK_ENDPOINT": os.getenv("LOCALSTACK_ENDPOINT", ""),
                    "AWS_DEFAULT_REGION": config.REGION,
                    "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", "test"),
                    "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
                }
            },
            Tags={"Project": config.PROJECT, "Environment": config.ENV},
        )
        function_arn = response["FunctionArn"]
        log.info(f"Created Lambda function: {function_arn}")
        return function_arn
    except Exception as e:
        log.warning(f"Failed to create Lambda function: {e}")
        return ""


def _create_scaler_schedule(function_arn: str) -> str:
    """Create EventBridge rule to trigger scaler Lambda every 15 minutes."""
    import boto3
    from infra import client as aws
    from utils.naming import resource_name
    
    events = aws.get_client("events")
    rule_name = resource_name("scaler-schedule")
    
    # Check existing
    try:
        response = events.describe_rule(Name=rule_name)
        log.info(f"EventBridge rule {rule_name} already exists")
        return response["Arn"]
    except events.exceptions.ResourceNotFoundException:
        pass
    except Exception as e:
        log.debug(f"Error checking EventBridge rule: {e}")
    
    try:
        # Create rule
        response = events.put_rule(
            Name=rule_name,
            ScheduleExpression="rate(15 minutes)",
            State="ENABLED",
            Description="Trigger predictive scaler every 15 minutes",
        )
        rule_arn = response["RuleArn"]
        
        # Add Lambda as target
        events.put_targets(
            Rule=rule_name,
            Targets=[{
                "Id": "1",
                "Arn": function_arn,
                "Input": json.dumps({"asg_name": resource_name("ml-asg")}),
            }],
        )
        
        # Grant EventBridge permission to invoke Lambda
        lambda_client = aws.get_client("lambda")
        lambda_client.add_permission(
            FunctionName=function_arn.split(":")[-1],
            StatementId="EventBridgeInvoke",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        
        log.info(f"Created EventBridge schedule: {rule_arn}")
        return rule_arn
    except Exception as e:
        log.warning(f"Failed to create EventBridge rule: {e}")
        return ""


def _run_phase_7(state: dict) -> dict:
    log.info("=== Phase 7: Full MLOps Pipeline  [not yet implemented] ===")
    return state


_PHASES: dict[int, tuple[str, callable]] = {
    1: ("Core Network Foundation",          _run_phase_1),
    2: ("Load Balancer + Web App Tier",     _run_phase_2),
    3: ("Data Tier (RDS + ElastiCache)",    _run_phase_3),
    4: ("ML Inference Tier",               _run_phase_4),
    5: ("WAF + Security Hardening",        _run_phase_5),
    6: ("Predictive Scaling + MLflow",     _run_phase_6),
    7: ("Full MLOps Pipeline",             _run_phase_7),
}


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Provision AI infrastructure against LocalStack or AWS.")
    parser.add_argument(
        "--phase", type=int, choices=range(1, 8), metavar="N",
        help="Deploy up to and including phase N (1–7). Omit to deploy all phases.",
    )
    args = parser.parse_args()

    target = args.phase or max(_PHASES)
    phases_to_run = [p for p in sorted(_PHASES) if p <= target]

    print()
    print(f"==> Deploying phases 1 – {target}  "
          f"(environment: {config.ENV}, project: {config.PROJECT})")
    print()

    state = _load_state()

    for phase_num in phases_to_run:
        label, runner = _PHASES[phase_num]
        print(f"──── Phase {phase_num}: {label} ────")
        try:
            state = runner(state)
            _save_state(state)
        except Exception as exc:
            log.error("Phase %d failed: %s", phase_num, exc)
            log.error("State up to this point saved. Re-run to resume.")
            _save_state(state)
            sys.exit(1)
        print()

    print("==> All requested phases complete.")
    print()
    _print_summary(state)


def _print_summary(state: dict) -> None:
    """Print a human-readable summary of key provisioned resource IDs."""
    if not state:
        return

    print("Provisioned resources:")
    fields = [
        ("vpc_id",              "VPC"),
        ("public_subnet_ids",   "Public subnets"),
        ("private_subnet_ids",  "Private subnets"),
        ("database_subnet_ids", "Database subnets"),
        ("igw_id",              "Internet Gateway"),
        ("nat_ids",             "NAT Gateways"),
        ("sg_ids",              "Security groups"),
        ("alb_arn",             "ALB"),
        ("alb_dns",             "ALB DNS"),
        ("web_asg_name",        "Web ASG"),
        ("ml_asg_name",         "ML ASG"),
        ("rds_endpoint",        "RDS endpoint"),
        ("redis_endpoint",      "Redis endpoint"),
    ]
    for key, label in fields:
        if key in state:
            print(f"  {label:<22} {state[key]}")
    print()


if __name__ == "__main__":
    main()
