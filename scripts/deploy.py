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
    log.info("=== Phase 3: Data Tier (RDS + ElastiCache)  [not yet implemented] ===")
    return state


def _run_phase_4(state: dict) -> dict:
    log.info("=== Phase 4: ML Inference Tier  [not yet implemented] ===")
    return state


def _run_phase_5(state: dict) -> dict:
    log.info("=== Phase 5: WAF + Security Hardening  [not yet implemented] ===")
    return state


def _run_phase_6(state: dict) -> dict:
    log.info("=== Phase 6: Predictive Scaling + MLflow  [not yet implemented] ===")
    return state


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
