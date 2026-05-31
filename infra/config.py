import os
from dotenv import load_dotenv

load_dotenv()

PROJECT = os.getenv("PROJECT_NAME", "ai-infra")
ENV     = os.getenv("ENVIRONMENT",  "dev")
REGION  = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# ── Network ────────────────────────────────────────────────────────────────────

VPC_CIDR = "10.0.0.0/16"
AZS      = [f"{REGION}a", f"{REGION}b"]

# Subnet CIDR offsets per tier — /24 slices out of the /16.
# Public 0-9, Private 10-19, Database 20-29 — leaves room for new tiers.
SUBNET_OFFSETS = {
    "public":   range(0,  2),
    "private":  range(10, 12),
    "database": range(20, 22),
}

# ── Compute ────────────────────────────────────────────────────────────────────

INSTANCE_TYPES = {
    "web": "t3.micro",
    "ml":  "c5.xlarge",
}

ASG_SIZES = {
    "web": {"min": 1, "max": 4,  "desired": 1},
    "ml":  {"min": 1, "max": 4,  "desired": 1},
}

# ── Storage ────────────────────────────────────────────────────────────────────

BUCKETS = {
    "alb-logs":   {"versioning": False, "glacier_days": 0,  "expiry_days": 90},
    "artefacts":  {"versioning": True,  "glacier_days": 90, "expiry_days": 0},
    "mlflow":     {"versioning": True,  "glacier_days": 0,  "expiry_days": 0},
    "training":   {"versioning": True,  "glacier_days": 180,"expiry_days": 0},
    "waf-logs":   {"versioning": False, "glacier_days": 0,  "expiry_days": 30},
}

