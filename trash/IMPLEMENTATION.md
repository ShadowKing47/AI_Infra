# AI Engineer Infrastructure — Implementation Plan

> **Principle:** Every phase is fully deployable and independently testable. No phase leaves
> the system in a broken state. Later phases extend earlier ones without rewriting them —
> DRY is enforced at the Python module level: one function/class, many call sites.

> **Stack:** Python throughout. `boto3` provisions all AWS infrastructure. `FastAPI` serves
> all ML endpoints. LocalStack is the local AWS simulator — no real account needed.
> The same Python code targets LocalStack locally and real AWS in production by
> switching one endpoint URL environment variable.

> **No AWS account needed.** Everything runs locally via LocalStack + Docker.
> `LOCALSTACK_ENDPOINT=http://localhost:4566` is the only difference between
> local and cloud execution.

---

## Repository Layout

```
project/
│
├── infra/                        # boto3 provisioning — mirrors the old Terraform modules
│   ├── __init__.py
│   ├── client.py                 # single boto3 session/client factory (DRY: used everywhere)
│   ├── config.py                 # all config (region, CIDRs, names) in one place
│   ├── networking.py             # Phase 1 — VPC, subnets, IGW, NAT, SGs
│   ├── storage.py                # Phase 2 — S3 buckets
│   ├── compute.py                # Phase 2 — Launch templates, ASGs (reused in Phase 4)
│   ├── loadbalancer.py           # Phase 2 — ALB, listeners, target groups
│   ├── database.py               # Phase 3 — RDS Postgres
│   ├── cache.py                  # Phase 3 — ElastiCache Redis
│   ├── waf.py                    # Phase 5 — WAF WebACL
│   ├── monitoring.py             # Phase 6 — CloudWatch alarms, dashboards
│   └── mlops.py                  # Phase 7 — feature store wiring, drift metrics
│
├── app/                          # FastAPI application — runs on EC2 / locally
│   ├── __init__.py
│   ├── main.py                   # FastAPI app entry point, router registration
│   ├── health.py                 # GET /health — used by ALB health checks
│   ├── predict.py                # POST /api/predict/* — ML inference endpoints
│   ├── features.py               # feature store client (Redis online, RDS offline)
│   └── models/                   # model loading + inference logic
│       ├── __init__.py
│       ├── loader.py             # pulls model artefact from S3, caches in memory
│       ├── sentiment.py          # HuggingFace sentiment classifier
│       └── anomaly.py            # Isolation Forest anomaly detector
│
├── mlops/                        # ML pipeline scripts (training, registry, drift)
│   ├── train.py                  # Phase 7a — trains model, logs to MLflow
│   ├── promote.py                # Phase 7b — promotes model to stable/
│   ├── drift.py                  # Phase 7d — computes PSI/KL, publishes metrics
│   └── scaler.py                 # Phase 6  — Prophet forecast → ASG schedule
│
├── scripts/
│   ├── bootstrap_state.py        # creates S3 state bucket + DynamoDB lock table
│   └── deploy.py                 # orchestrates full infra deploy (calls infra/ in order)
│
├── tests/
│   ├── test_networking.py
│   ├── test_compute.py
│   ├── test_loadbalancer.py
│   ├── test_app.py
│   └── test_mlops.py
│
├── docker-compose.yml            # LocalStack + app service
├── requirements.txt
└── .env.example                  # LOCALSTACK_ENDPOINT, AWS_REGION, etc.
```

---

## Shared Foundation (used by every phase)

### `infra/client.py`
```python
# Single boto3 session — endpoint_url is None for real AWS, localhost for LocalStack.
# Every infra module imports get_client() — no module creates its own session.

import os
import boto3

ENDPOINT = os.getenv("LOCALSTACK_ENDPOINT")   # None in prod → real AWS

def get_client(service: str):
    return boto3.client(service, endpoint_url=ENDPOINT, region_name=_region())

def get_resource(service: str):
    return boto3.resource(service, endpoint_url=ENDPOINT, region_name=_region())

def _region() -> str:
    return os.getenv("AWS_DEFAULT_REGION", "us-east-1")
```

### `infra/config.py`
```python
# All names, CIDRs, and sizing in one place.
# Changing environment = "prod" here fans out to every resource name.

PROJECT   = os.getenv("PROJECT_NAME", "ai-infra")
ENV       = os.getenv("ENVIRONMENT",  "dev")

VPC_CIDR  = "10.0.0.0/16"
AZS       = ["us-east-1a", "us-east-1b"]

def name(suffix: str) -> str:
    """Consistent resource naming: ai-infra-dev-<suffix>"""
    return f"{PROJECT}-{ENV}-{suffix}"
```

---

## Phase 1 — Core Network Foundation

**Significance:** Every other resource lives inside this VPC. Establishing the network
topology, subnet layout, and security group trust model here means every later phase
just calls `get_vpc_id()` or `get_sg_id("app")` — no networking logic is repeated.

### `infra/networking.py` — functions

```python
def create_vpc() -> str:
    """Creates VPC with DNS support. Returns vpc_id. Idempotent via tag lookup."""

def create_subnets(vpc_id: str) -> dict[str, list[str]]:
    """
    Creates 3 subnet tiers across all AZs.
    Returns {"public": [...], "private": [...], "database": [...]}.
    CIDRs derived from VPC CIDR + tier offset — no manual CIDR management.
    """

def create_internet_gateway(vpc_id: str) -> str:
    """Creates and attaches IGW. Returns igw_id."""

def create_nat_gateways(public_subnet_ids: list[str]) -> list[str]:
    """
    Creates one NAT Gateway per AZ (or one if single_nat=True).
    Allocates EIPs. Returns list of nat_gateway_ids.
    Waits for 'available' state before returning.
    """

def create_route_tables(vpc_id: str, igw_id: str, nat_ids: list[str],
                        subnets: dict) -> None:
    """
    Public RT  → IGW.
    Private RTs → AZ-local NAT (one RT per AZ).
    Database subnets share private RTs.
    """

def create_security_groups(vpc_id: str) -> dict[str, str]:
    """
    Creates sg_alb, sg_app, sg_ml, sg_db, sg_cache with no inline rules.
    Returns {"alb": sg_id, "app": sg_id, ...}.
    """

def attach_security_group_rules(sgs: dict[str, str]) -> None:
    """
    Attaches all ingress/egress rules after all SGs exist — avoids
    circular dependency (sg_app references sg_alb, sg_db references sg_app).
    """

def provision_networking() -> dict:
    """
    Orchestrator — calls all above in order.
    Returns full networking state dict saved to state store.
    Idempotent: checks for existing resources by tag before creating.
    """
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| NAT Gateway AZ failure | One NAT per AZ; private route tables point to AZ-local NAT |
| CIDR exhaustion | VPC /16 with /24 subnets; offset groups (public 0–9, private 10–19, db 20–29) leave expansion room |
| SG circular dependency | All SGs created first with no rules; `attach_security_group_rules()` runs after all IDs are known |
| Partial deploy (crash mid-way) | Every function checks for existing resource by `Name` tag before creating — safe to re-run |

**Validation**
```bash
docker compose up -d
python scripts/deploy.py --phase 1

python - <<'EOF'
import boto3, os
ec2 = boto3.client("ec2", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
vpcs = ec2.describe_vpcs(Filters=[{"Name":"tag:Project","Values":["ai-infra"]}])
print("VPCs:", [v["VpcId"] for v in vpcs["Vpcs"]])
subnets = ec2.describe_subnets()
print("Subnets:", len(subnets["Subnets"]), "across tiers")
EOF
```

---

## Phase 2 — Load Balancer + Web App Tier

**Significance:** Establishes the public-facing entry point and proves the three-tier pattern
works end-to-end. The `compute.py` module written here is reused verbatim in Phase 4 for
the ML inference tier — different arguments, zero new code.

### `infra/storage.py` — functions

```python
def create_bucket(logical_name: str, versioning: bool = False,
                  glacier_days: int = 0, expiry_days: int = 0) -> str:
    """
    Creates bucket named <project>-<env>-<logical_name>.
    Applies encryption, public-access-block, optional versioning and lifecycle.
    Returns bucket name. Idempotent.
    """

def attach_alb_log_policy(bucket_name: str) -> None:
    """Grants ELB service account s3:PutObject on the ALB logs bucket."""
```

### `infra/compute.py` — functions

```python
def create_launch_template(tier_name: str, instance_type: str, ami_id: str,
                            sg_ids: list[str], user_data: str = "",
                            profile_arn: str = "") -> str:
    """
    Creates (or updates) a launch template for the given tier.
    Enforces IMDSv2, detailed monitoring, tag propagation.
    Creates a minimal SSM-enabled IAM instance profile if profile_arn is empty.
    Returns launch_template_id.
    """

def create_asg(tier_name: str, launch_template_id: str, subnet_ids: list[str],
               target_group_arns: list[str], min_size: int = 1,
               max_size: int = 4, desired: int = 1,
               enable_warm_pool: bool = False) -> str:
    """
    Creates Auto Scaling Group.
    Attaches CPU target-tracking policy (60% threshold).
    Adds warm pool when enable_warm_pool=True (used for ML tier in Phase 4).
    Returns asg_name.
    """

def provision_compute(tier_name: str, **kwargs) -> dict:
    """Orchestrator for one compute tier. Returns {launch_template_id, asg_name}."""
```

### `infra/loadbalancer.py` — functions

```python
def create_alb(subnet_ids: list[str], sg_id: str,
               logs_bucket: str) -> str:
    """
    Creates internet-facing ALB with access logging enabled.
    Returns alb_arn.
    """

def create_target_group(name: str, vpc_id: str, port: int = 8080,
                        health_check_path: str = "/health") -> str:
    """
    Creates ALB target group. Connection draining = 30s.
    Returns target_group_arn.
    """

def create_listener(alb_arn: str, default_tg_arn: str) -> str:
    """
    Creates HTTP:80 listener (HTTP only in LocalStack; HTTPS via ACM in prod).
    Default action forwards to web target group.
    Returns listener_arn.
    """

def add_listener_rule(listener_arn: str, tg_arn: str,
                      path_patterns: list[str], priority: int) -> str:
    """
    Adds a path-pattern forwarding rule to an existing listener.
    Used in Phase 4 to route /api/predict/* to the ML target group.
    Returns rule_arn.
    """

def add_weighted_rule(listener_arn: str,
                      tg_v1_arn: str, weight_v1: int,
                      tg_v2_arn: str, weight_v2: int,
                      path_patterns: list[str], priority: int) -> str:
    """
    Weighted forward action for A/B model testing (Phase 7c).
    Returns rule_arn.
    """
```

### `app/health.py`
```python
@router.get("/health")
async def health() -> dict:
    """Returns {"status": "ok", "version": MODEL_VERSION}. Used by ALB health check."""
```

### `app/main.py`
```python
app = FastAPI(title="AI Inference API")
app.include_router(health.router)
app.include_router(predict.router, prefix="/api/predict")
# Phase 4 adds model loading on startup via lifespan context
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Health check failing on cold start | `health_check_grace_period=120` in `create_asg()`; `/health` returns 200 before model loads |
| ASG desired capacity fighting autoscaler | `ignore_desired_on_update=True` flag in `create_asg()` — Terraform equivalent of `ignore_changes` |
| ALB log delivery failing | `attach_alb_log_policy()` is called before `create_alb()` — bucket policy exists before ALB tries to write |
| `add_listener_rule` priority collision | Helper scans existing rules and auto-increments priority if requested value is taken |

**Validation**
```bash
python scripts/deploy.py --phase 2

python - <<'EOF'
import boto3, os
elb = boto3.client("elbv2", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
albs = elb.describe_load_balancers()
print("ALBs:", [a["DNSName"] for a in albs["LoadBalancers"]])
tgs = elb.describe_target_groups()
print("Target groups:", [t["TargetGroupName"] for t in tgs["TargetGroups"]])
EOF

# Hit the health endpoint (app running locally):
uvicorn app.main:app --port 8080 &
curl http://localhost:8080/health
# {"status": "ok", "version": "none"}
```

---

## Phase 3 — Data Tier (RDS + ElastiCache)

**Significance:** Provides durable storage for predictions, ground truth labels, feature
history, and experiment metadata. ElastiCache serves as the online feature store in Phase 7.

### `infra/database.py` — functions

```python
def create_subnet_group(name: str, subnet_ids: list[str]) -> str:
    """Creates RDS DB subnet group across database subnets. Returns group name."""

def create_rds(subnet_group: str, sg_id: str,
               db_name: str = "appdb",
               instance_class: str = "db.t3.micro") -> dict:
    """
    Creates Multi-AZ Postgres RDS instance with:
    - Encryption at rest (AES256)
    - 7-day automated backups
    - Deletion protection (skipped in dev via ENV check)
    - Credentials written to Secrets Manager
    Waits for 'available' state. Returns {endpoint, secret_arn}.
    """

def get_connection_string(secret_arn: str) -> str:
    """Fetches credentials from Secrets Manager, returns SQLAlchemy URL. Never logs the value."""
```

### `infra/cache.py` — functions

```python
def create_cache_subnet_group(name: str, subnet_ids: list[str]) -> str:
    """Creates ElastiCache subnet group. Returns group name."""

def create_redis(subnet_group: str, sg_id: str,
                 node_type: str = "cache.t3.micro") -> dict:
    """
    Creates Redis replication group (1 primary + 1 replica).
    At-rest + in-transit encryption, auth token via Secrets Manager.
    Automatic failover enabled. Returns {primary_endpoint, auth_token_secret_arn}.
    """
```

### `app/features.py`
```python
class FeatureStore:
    """
    Online path  → Redis (sub-ms lookup at inference time).
    Offline path → RDS features schema (training joins, drift queries).
    Falls back to RDS if Redis key is missing or evicted.
    """
    def get(self, entity_id: str) -> dict: ...
    def set(self, entity_id: str, features: dict, ttl: int = 86400) -> None: ...
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| RDS failover exhausting connection pool | RDS Proxy sits in front; pool survives failover without app restart |
| Redis eviction under memory pressure | `maxmemory-policy allkeys-lru`; TTL on every key; cold path falls back to RDS |
| Secret rotation breaking live connections | App reads secret at startup with TTL cache; rotation Lambda stages new version before invalidating old |
| Terraform destroy equivalent on RDS | `deletion_protection=True` set on non-dev environments; guarded by `ENV != "dev"` check in Python |

**Validation**
```bash
python scripts/deploy.py --phase 3

python - <<'EOF'
import boto3, os
rds = boto3.client("rds", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
instances = rds.describe_db_instances()
print("RDS:", [(i["DBInstanceIdentifier"], i["DBInstanceStatus"])
               for i in instances["DBInstances"]])

ec = boto3.client("elasticache", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
groups = ec.describe_replication_groups()
print("Redis:", [(g["ReplicationGroupId"], g["Status"])
                 for g in groups["ReplicationGroups"]])
EOF
```

---

## Phase 4 — ML Inference Tier

**Significance:** The first ML-specific infrastructure. A second call to `provision_compute()`
with `tier_name="ml"` deploys the inference ASG — zero new infra code. The ALB gains a
path rule routing `/api/predict/*` to the ML target group.

### `app/models/loader.py`
```python
def load_model(model_name: str) -> Any:
    """
    Pulls model artefact from S3 stable/ prefix on first call, caches in memory.
    Returns 503 from health endpoint until model is loaded — ASG health check
    will not mark instance healthy until this resolves.
    """

def get_model_version() -> str:
    """Reads version tag from loaded artefact metadata."""
```

### `app/models/sentiment.py`
```python
def predict(text: str) -> dict:
    """HuggingFace pipeline inference. Returns {label, score, model_version}."""
```

### `app/models/anomaly.py`
```python
def predict(features: list[float]) -> dict:
    """Isolation Forest inference. Returns {is_anomaly, anomaly_score}."""
```

### `app/predict.py`
```python
@router.post("/sentiment")
async def predict_sentiment(body: SentimentRequest) -> SentimentResponse:
    """POST /api/predict/sentiment — routed here by ALB path rule."""

@router.post("/anomaly")
async def predict_anomaly(body: AnomalyRequest) -> AnomalyResponse:
    """POST /api/predict/anomaly"""
```

**`provision_compute()` reuse in Phase 4 — no new infra code:**
```python
# Phase 2 (web tier):
provision_compute("web",  instance_type="t3.micro",  ...)

# Phase 4 (ML tier) — same function, different args:
provision_compute("ml",   instance_type="c5.xlarge", enable_warm_pool=True, ...)
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Model artefact missing from S3 on boot | `loader.py` polls S3 with exponential backoff; health returns 503 until loaded; ALB never marks instance healthy |
| Inference memory leak over time | CloudWatch custom metric on memory %; alarm triggers instance refresh via `update_asg()` |
| Large model cold-start > ALB timeout | Warm pool pre-loads model; instance only enters service after model is in memory |

**Validation**
```bash
python scripts/deploy.py --phase 4

# Upload a stub model artefact
python - <<'EOF'
import boto3, os, json
s3 = boto3.client("s3", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
s3.put_object(Bucket="ai-infra-dev-artefacts",
              Key="sentiment/stable/model.json",
              Body=json.dumps({"version": "v1.0", "type": "stub"}))
print("Artefact uploaded")
EOF

# Hit the inference endpoint (app running locally with model loaded):
curl -X POST http://localhost:8080/api/predict/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "Terraform is great"}'
# {"label": "POSITIVE", "score": 0.99, "model_version": "v1.0"}
```

---

## Phase 5 — WAF + Security Hardening

**Significance:** Attaches WAF to the ALB and feeds WAF logs to S3 for the anomaly model.

### `infra/waf.py` — functions

```python
def create_web_acl(name: str, alb_arn: str) -> str:
    """
    Creates WAF WebACL with:
    - AWSManagedRulesCommonRuleSet (Count mode first, Block after review)
    - AWSManagedRulesSQLiRuleSet
    - Rate-limit rule: 1000 req/5 min per IP on /api/predict/*
    Associates with ALB. Returns web_acl_arn.
    """

def enable_waf_logging(web_acl_arn: str, firehose_arn: str) -> None:
    """Routes WAF logs → Kinesis Firehose → S3 waf-logs/ prefix."""

def add_ip_whitelist(web_acl_arn: str, cidrs: list[str]) -> None:
    """
    Adds IP set rule evaluated before rate-limit rule.
    Used to exempt known partner/internal IPs from rate limiting.
    """
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| WAF false-positives on valid JSON | Managed rules start in Count mode; `set_rule_action(rule, "BLOCK")` called manually after log review |
| LocalStack free tier WAF limitation | WAF module applies in Count-only mode on free tier; guarded by `LOCALSTACK_PRO` env check |

**Validation**
```bash
python scripts/deploy.py --phase 5

python - <<'EOF'
import boto3, os
waf = boto3.client("wafv2", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
acls = waf.list_web_acls(Scope="REGIONAL")
print("WAF ACLs:", [a["Name"] for a in acls["WebACLs"]])
EOF
```

---

## Phase 6 — Predictive Auto-Scaling + Infrastructure ML

**Significance:** Infrastructure becomes AI-powered. A Python Lambda reads CloudWatch
history, runs a Prophet forecast, and schedules ASG actions 30 min ahead of predicted spikes.

### `mlops/scaler.py` — functions

```python
def fetch_request_history(asg_name: str, days: int = 7) -> pd.DataFrame:
    """Reads CloudWatch RequestCount metric history. Returns tidy DataFrame."""

def forecast_load(history: pd.DataFrame, horizon_minutes: int = 30) -> pd.DataFrame:
    """
    Fits Facebook Prophet on history.
    Returns forecast with yhat, yhat_lower, yhat_upper for next horizon_minutes.
    Flags high-uncertainty forecasts (yhat_upper/yhat > 2) — those are skipped.
    """

def schedule_scaling_action(asg_name: str, desired: int,
                             at: datetime, expiry_minutes: int = 60) -> None:
    """
    Calls autoscaling:PutScheduledUpdateGroupAction.
    Action expires after expiry_minutes so a bad forecast self-heals.
    """

def run(event: dict, context: Any) -> dict:
    """Lambda handler — orchestrates fetch → forecast → schedule."""
```

### `infra/monitoring.py` — functions

```python
def create_alarm(name: str, namespace: str, metric: str,
                 threshold: float, comparison: str,
                 alarm_actions: list[str]) -> str:
    """Generic CloudWatch alarm factory. Returns alarm_arn."""

def create_dashboard(name: str, widgets: list[dict]) -> None:
    """Creates CloudWatch dashboard from widget definitions."""

def put_metric(namespace: str, metric_name: str, value: float,
               dimensions: dict) -> None:
    """Publishes a custom metric datapoint. Used by drift checker and scaler."""
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Prophet Lambda cold-start > 15 min | Model packaged as Lambda layer; provisioned concurrency = 1 keeps it warm |
| Forecast schedules too many instances | Action sets `MinSize` not `DesiredCapacity`; hard cap at `max_size`; auto-expires after 60 min |
| CloudWatch metric gaps | `fetch_request_history()` forward-fills gaps before passing to Prophet |

**Validation**
```bash
python scripts/deploy.py --phase 6

# Run scaler locally (simulates Lambda invocation):
python -c "from mlops.scaler import run; print(run({}, None))"

python - <<'EOF'
import boto3, os
asg = boto3.client("autoscaling", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
actions = asg.describe_scheduled_actions(AutoScalingGroupName="ai-infra-dev-ml-asg")
print("Scheduled actions:", actions["ScheduledUpdateGroupActions"])
EOF
```

---

## Phase 7 — MLOps Pipeline (Training → Registry → A/B Deploy → Drift)

**Significance:** Closes the full ML loop. Every resume bullet maps to a concrete Python function.

### 7a — `mlops/train.py`

```python
def prepare_dataset(s3_key: str) -> pd.DataFrame:
    """Downloads training data from S3, validates schema, returns DataFrame."""

def train(dataset: pd.DataFrame, model_type: str) -> tuple[Any, dict]:
    """Trains model. Returns (model_object, metrics_dict)."""

def log_run(model: Any, metrics: dict, params: dict) -> str:
    """Logs experiment to MLflow (params, metrics, artefact). Returns run_id."""

def run(event: dict, context: Any) -> dict:
    """Lambda handler — orchestrates prepare → train → log."""
```

### 7b — `mlops/promote.py`

```python
def get_best_run(experiment_name: str, metric: str = "f1") -> str:
    """Queries MLflow for run with highest metric. Returns run_id."""

def promote(run_id: str, model_name: str) -> str:
    """
    Downloads artefact from MLflow run, uploads to s3://artefacts/<name>/stable/.
    Checks no A/B test is active before promoting (reads SSM flag).
    Returns new S3 key.
    """
```

### 7c — A/B testing via `add_weighted_rule()` (already in Phase 2)

```python
# No new infra functions — Phase 2's add_weighted_rule() handles this:
add_weighted_rule(
    listener_arn=listener_arn,
    tg_v1_arn=tg_v1, weight_v1=90,
    tg_v2_arn=tg_v2, weight_v2=10,
    path_patterns=["/api/predict/*"],
    priority=10,
)
```

### 7d — `mlops/drift.py`

```python
def fetch_predictions(since: datetime) -> pd.DataFrame:
    """Queries RDS prediction log + ground truth labels. Returns DataFrame."""

def compute_psi(reference: pd.Series, current: pd.Series,
                buckets: int = 10) -> float:
    """Population Stability Index. PSI > 0.2 = significant drift."""

def compute_kl(reference: pd.Series, current: pd.Series) -> float:
    """KL divergence between reference and current distributions."""

def publish_drift_metrics(psi: float, kl: float) -> None:
    """Calls monitoring.put_metric() for FeatureDrift and LabelDrift namespaces."""

def trigger_retrain_if_needed(psi: float) -> None:
    """
    If PSI > 0.2, invokes training_trigger Lambda.
    Enforces 24h cooldown via SSM Parameter Store timestamp.
    Skips if fewer than 500 labelled samples are available.
    """

def run(event: dict, context: Any) -> dict:
    """Lambda handler — orchestrates fetch → PSI/KL → publish → retrain check."""
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Spot training instance interrupted | `train.py` checkpoints to S3 every epoch; next run resumes from checkpoint |
| Both A/B versions unhealthy | ALB returns 503; CloudWatch alarm on `HealthyHostCount < 1` fires immediately |
| Drift alarm during holiday traffic shift | `evaluation_periods=3` (18h sustained) before trigger; manual suppress via SNS filter |
| Circular retrain loop | `trigger_retrain_if_needed()` enforces 24h minimum gap via SSM timestamp |
| PSI on sparse data | Skipped with warning log if labelled sample count < 500 |

**Validation**
```bash
python scripts/deploy.py --phase 7

# Run drift checker locally:
python -c "from mlops.drift import run; print(run({}, None))"

# Check drift metrics published:
python - <<'EOF'
import boto3, os
cw = boto3.client("cloudwatch", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
metrics = cw.list_metrics(Namespace="MLOps/DriftMonitor")
print("Drift metrics:", [m["MetricName"] for m in metrics["Metrics"]])
EOF
```

---

## Cross-Cutting Concerns

### Idempotency pattern (every `infra/` function)
```python
def _find_existing(ec2, resource_type: str, name: str) -> str | None:
    """
    Tag-based lookup before creating any resource.
    Returns existing resource ID or None.
    Used in every create_* function — never creates a duplicate.
    """
```

### Tagging (every boto3 create call)
```python
def _tags(suffix: str) -> list[dict]:
    return [
        {"Key": "Name",        "Value": config.name(suffix)},
        {"Key": "Project",     "Value": config.PROJECT},
        {"Key": "Environment", "Value": config.ENV},
        {"Key": "ManagedBy",   "Value": "python-boto3"},
    ]
```

### Secrets — never logged, never hard-coded
```python
# Credentials always written to Secrets Manager at creation time.
# Retrieved via get_connection_string(secret_arn) — value never surfaces in logs.
```

### `scripts/deploy.py` — orchestrator
```python
def main():
    """
    python scripts/deploy.py --phase 1   # deploys only phase 1
    python scripts/deploy.py             # deploys all phases in order
    Each phase checks its own resources exist before re-creating them.
    """
```

---

## Local Demo Environment

### Prerequisites
```bash
brew install docker
pip install boto3 fastapi uvicorn prophet scikit-learn mlflow pandas
```

### `.env.example`
```bash
LOCALSTACK_ENDPOINT=http://localhost:4566
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
PROJECT_NAME=ai-infra
ENVIRONMENT=dev
```

### One-command demo
```bash
cp .env.example .env
docker compose up -d                    # starts LocalStack
python scripts/deploy.py               # provisions all phases
uvicorn app.main:app --port 8080 &     # starts FastAPI

curl http://localhost:8080/health
curl -X POST http://localhost:8080/api/predict/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "This project is excellent"}'
```

### LocalStack free tier coverage
| Phase | Resources | Free tier |
|---|---|---|
| 1 | VPC, subnets, SGs, IGW, NAT, route tables | Full |
| 2 | ALB, ASG, S3, IAM | Full |
| 3 | RDS, ElastiCache, Secrets Manager | Full |
| 4 | S3 artefacts, CloudWatch custom metrics | Full |
| 5 | WAF WebACL | Count mode only (Pro for Block) |
| 6 | Lambda, EventBridge, CloudWatch alarms | Full |
| 7 | DynamoDB, SNS, MLflow | Full |

---

## Phased Rollout Summary

| Phase | Python modules added | Resume skill unlocked |
|---|---|---|
| 1 | `infra/networking.py` | AWS networking, boto3 IaC |
| 2 | `infra/storage.py`, `infra/compute.py`, `infra/loadbalancer.py`, `app/main.py`, `app/health.py` | ALB + ASG automation, FastAPI |
| 3 | `infra/database.py`, `infra/cache.py`, `app/features.py` | RDS + Redis, feature store |
| 4 | `app/models/`, `app/predict.py` | ML model serving, inference API |
| 5 | `infra/waf.py` | Security engineering |
| 6 | `mlops/scaler.py`, `infra/monitoring.py` | Predictive scaling, MLOps foundations |
| 7 | `mlops/train.py`, `mlops/promote.py`, `mlops/drift.py` | End-to-end MLOps pipeline |

Each phase adds new files only — no existing file is rewritten.
