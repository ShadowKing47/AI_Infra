# AI Engineer Infrastructure — Implementation Plan

> **Principle:** Every phase is fully deployable and independently testable. No phase
> leaves the system in a broken state. Later phases extend earlier ones — new files
> only, no existing file is rewritten. DRY is enforced at the Python module level:
> one function, many call sites.

> **Stack:** Python throughout. `boto3` provisions all AWS infrastructure.
> `FastAPI` serves all ML endpoints. LocalStack runs in Docker as the AWS simulator —
> no real account needed. Switching to real AWS is one env-var change.

---

## Repository Layout (source of truth)

```
project/
│
├── infra/                        # boto3 provisioning — one file per AWS service group
│   ├── __init__.py
│   ├── client.py                 # single boto3 session/client factory  ← shared by all modules
│   ├── config.py                 # all config constants (CIDRs, sizes, bucket names)
│   ├── networking.py             # Phase 1 — VPC, subnets, IGW, NAT, route tables, SGs
│   ├── storage.py                # Phase 2 — S3 buckets
│   ├── compute.py                # Phase 2 — Launch templates + ASGs  ← reused in Phase 4
│   ├── loadbalancer.py           # Phase 2 — ALB, listeners, target groups
│   ├── database.py               # Phase 3 — RDS Postgres
│   ├── cache.py                  # Phase 3 — ElastiCache Redis
│   ├── waf.py                    # Phase 5 — WAF WebACL
│   ├── monitoring.py             # Phase 6 — CloudWatch alarms + dashboards
│   └── mlops.py                  # Phase 7 — feature store wiring, drift metric helpers
│
├── utils/                        # Pure helpers — no boto3, no side effects
│   ├── __init__.py
│   ├── naming.py                 # resource_name(suffix) → "<project>-<env>-<suffix>"
│   ├── tagging.py                # build_tags(), tag_spec() — used in every create call
│   └── networking.py             # subnet_cidr(offset) — CIDR math
│
├── app/                          # FastAPI application — runs on EC2 or locally
│   ├── __init__.py
│   ├── main.py                   # FastAPI app entry point, router registration
│   ├── health.py                 # GET /health — ALB health check target
│   ├── predict.py                # POST /api/predict/* — inference endpoints
│   ├── features.py               # feature store client (Redis online, RDS offline)
│   └── models/
│       ├── __init__.py
│       ├── loader.py             # pulls model artefact from S3, caches in memory
│       ├── sentiment.py          # HuggingFace sentiment classifier
│       └── anomaly.py            # Isolation Forest anomaly detector
│
├── mlops/                        # ML pipeline scripts (training, registry, drift)
│   ├── __init__.py
│   ├── train.py                  # Phase 7a — trains model, logs to MLflow
│   ├── promote.py                # Phase 7b — promotes model to stable/
│   ├── drift.py                  # Phase 7d — computes PSI/KL, publishes metrics
│   └── scaler.py                 # Phase 6  — Prophet forecast → ASG schedule
│
├── scripts/
│   ├── __init__.py
│   ├── bootstrap_state.py        # creates S3 state bucket + DynamoDB lock table
│   └── deploy.py                 # orchestrates full infra deploy, phase by phase
│
├── tests/
│   ├── __init__.py
│   ├── test_networking.py
│   ├── test_compute.py
│   ├── test_loadbalancer.py
│   ├── test_app.py
│   └── test_mlops.py
│
├── Dockerfile                    # LocalStack image — run directly with docker run
├── requirements.txt              # pinned dependencies
└── .env.example                  # LOCALSTACK_ENDPOINT, AWS creds, project config
```

---

## Local Demo Environment

### How it works
LocalStack runs as a Docker container that simulates AWS services on `localhost:4566`.
`boto3` is pointed at this endpoint via `LOCALSTACK_ENDPOINT=http://localhost:4566`.
No real AWS account is used at any point.

Switching to real AWS later requires only removing the `LOCALSTACK_ENDPOINT` line from `.env`.
The Python code is identical in both cases.

### `Dockerfile`
```dockerfile
FROM localstack/localstack:3.4

ENV SERVICES=ec2,s3,dynamodb,rds,elasticache,elbv2,autoscaling,iam,\
secretsmanager,cloudwatch,lambda,events,wafv2,logs,sns,firehose
ENV DEFAULT_REGION=us-east-1
```

### Running LocalStack
```bash
# Build and start
docker build -t ai-infra-localstack .
docker run -d -p 4566:4566 --name localstack ai-infra-localstack

# Verify it's up
curl http://localhost:4566/_localstack/health
```

### `.env.example`
```bash
LOCALSTACK_ENDPOINT=http://localhost:4566
AWS_DEFAULT_REGION=us-east-1
AWS_ACCESS_KEY_ID=test        # any non-empty value — LocalStack ignores it
AWS_SECRET_ACCESS_KEY=test
PROJECT_NAME=ai-infra
ENVIRONMENT=dev
```

### One-command demo (after LocalStack is running)
```bash
pip install -r requirements.txt
cp .env.example .env

python scripts/bootstrap_state.py   # creates S3 state bucket + DynamoDB table
python scripts/deploy.py            # provisions all phases against LocalStack

uvicorn app.main:app --port 8080    # starts the FastAPI app locally

curl http://localhost:8080/health
curl -X POST http://localhost:8080/api/predict/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "This project is excellent"}'
```

### LocalStack free tier coverage
| Phase | Key resources | Free tier |
|---|---|---|
| 1 | VPC, subnets, SGs, IGW, NAT, route tables | Full |
| 2 | ALB, ASG, S3, IAM | Full |
| 3 | RDS, ElastiCache, Secrets Manager | Full |
| 4 | S3 artefacts, CloudWatch custom metrics | Full |
| 5 | WAF WebACL | Count mode only (Pro for Block) |
| 6 | Lambda, EventBridge, CloudWatch alarms | Full |
| 7 | DynamoDB, SNS, MLflow | Full |

---

## Shared Foundation (every phase imports these)

### `infra/client.py`
Single boto3 session — `endpoint_url` is `localhost:4566` (LocalStack) or `None` (real AWS).
Every infra module calls `get_client("ec2")` — nothing creates its own session.

### `infra/config.py`
Pure data constants — no functions.
CIDRs, AZ list, instance types, ASG sizes, bucket definitions.
Changing `ENVIRONMENT="prod"` fans out to every resource name automatically.

### `utils/naming.py`
`resource_name(suffix)` → `"ai-infra-dev-<suffix>"`.
One place — changing the naming convention touches one line.

### `utils/tagging.py`
`build_tags(suffix, extra=None)` and `tag_spec(resource_type, suffix)`.
Every boto3 create call uses these — no tag block is ever written twice.

### `utils/networking.py`
`subnet_cidr(offset)` — pure CIDR math, no boto3.
`offset=10, VPC=10.0.0.0/16 → 10.0.10.0/24`

---

## Phase 1 — Core Network Foundation

**Significance:** Every other resource lives inside this VPC. Establishing the network
topology, subnet layout, and security group trust model here means every later phase
just reads state from `provision()` — no networking logic is repeated.

### `infra/networking.py`

| Function | Responsibility |
|---|---|
| `create_vpc()` | Creates VPC with DNS support/hostnames. Returns `vpc_id`. Idempotent. |
| `create_subnets(vpc_id)` | Creates 3 tiers × 2 AZs = 6 subnets. CIDRs from offset table in config. Returns `{"public": [...], "private": [...], "database": [...]}`. |
| `create_internet_gateway(vpc_id)` | Creates + attaches IGW. Returns `igw_id`. |
| `create_nat_gateways(public_subnet_ids)` | One NAT + EIP per AZ. Polls until `available`. Returns `[nat_id, ...]` in AZ order. |
| `create_route_tables(vpc_id, igw_id, nat_ids, subnets)` | Public RT → IGW. Per-AZ private RTs → AZ-local NAT. Database subnets share private RTs. |
| `create_security_groups(vpc_id)` | Creates 5 SGs with no inline rules. Returns `{"alb": id, "app": id, "ml": id, "db": id, "cache": id}`. |
| `attach_security_group_rules(sgs)` | Adds all ingress rules after all SG IDs are known — avoids circular reference. Duplicate rules silently skipped. |
| `provision()` | Orchestrator — calls all above in order. Returns full state dict consumed by later phases. |

**Idempotency pattern — used in every `create_*` function:**
```python
def _find_existing(resource_type, name_value) -> str | None:
    # Tag-based lookup before any create call.
    # Returns existing resource ID or None — never creates a duplicate.
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| NAT Gateway AZ failure | One NAT per AZ; private RTs point to AZ-local NAT only |
| CIDR exhaustion | `/16` VPC with `/24` subnets; offset groups (public 0–9, private 10–19, db 20–29) |
| SG circular dependency | All 5 SGs created with no rules first; `attach_security_group_rules()` runs after all IDs known |
| Crash mid-provision | Every function checks by Name tag before creating — re-running resumes safely |

### `scripts/bootstrap_state.py`
Creates S3 state bucket and DynamoDB lock table before any phase runs.
Waits for LocalStack health check before proceeding.

### `scripts/deploy.py`
```python
# python scripts/deploy.py           → deploys all phases in order
# python scripts/deploy.py --phase 1 → deploys Phase 1 only
# python scripts/deploy.py --phase 3 → deploys phases 1–3 (cumulative)
```
Each phase calls its `infra/*.provision()` and passes the returned state dict into the next phase.

**Validation**
```bash
python scripts/deploy.py --phase 1
python - <<'EOF'
import boto3, os
ec2 = boto3.client("ec2", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
print([v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]])
print([(s["SubnetId"], s["AvailabilityZone"]) for s in ec2.describe_subnets()["Subnets"]])
print([g["GroupName"] for g in ec2.describe_security_groups()["SecurityGroups"]])
EOF
```

---

## Phase 2 — Load Balancer + Web App Tier

**Significance:** Establishes the public-facing entry point. `compute.py` written here
is reused verbatim for the ML tier in Phase 4 — same function, different arguments.

### `infra/storage.py`

| Function | Responsibility |
|---|---|
| `create_bucket(logical_name, versioning, glacier_days, expiry_days)` | Encryption + public-access-block always applied. Lifecycle only when days > 0. Idempotent. |
| `attach_alb_log_policy(bucket_name)` | Grants ELB service account `s3:PutObject` — called before `create_alb()`. |

### `infra/compute.py`

| Function | Responsibility |
|---|---|
| `create_launch_template(tier_name, instance_type, ami_id, sg_ids, user_data, profile_arn)` | IMDSv2 enforced. Creates minimal SSM instance profile if `profile_arn` empty. Returns `launch_template_id`. |
| `create_asg(tier_name, launch_template_id, subnet_ids, target_group_arns, min_size, max_size, desired, enable_warm_pool)` | CPU target-tracking at 60%. Warm pool optional (ML tier). Returns `asg_name`. |
| `provision_compute(tier_name, **kwargs)` | Orchestrator. Returns `{launch_template_id, asg_name}`. |

**DRY reuse — same function, Phase 2 and Phase 4:**
```python
provision_compute("web", instance_type="t3.micro", ...)          # Phase 2
provision_compute("ml",  instance_type="c5.xlarge",              # Phase 4 — zero new code
                  enable_warm_pool=True, ...)
```

### `infra/loadbalancer.py`

| Function | Responsibility |
|---|---|
| `create_alb(subnet_ids, sg_id, logs_bucket)` | Internet-facing ALB with access logging. Returns `alb_arn`. |
| `create_target_group(name, vpc_id, port, health_check_path)` | Deregistration delay 30s. Returns `tg_arn`. |
| `create_listener(alb_arn, default_tg_arn)` | HTTP:80 listener → web TG. Returns `listener_arn`. |
| `add_listener_rule(listener_arn, tg_arn, path_patterns, priority)` | Path-pattern rule. Used in Phase 4 for `/api/predict/*`. Auto-increments priority on collision. |
| `add_weighted_rule(listener_arn, tg_v1_arn, weight_v1, tg_v2_arn, weight_v2, path_patterns, priority)` | Weighted forward for A/B testing (Phase 7c). |

### `app/main.py` + `app/health.py`
```python
app = FastAPI(title="AI Inference API")
app.include_router(health.router)
app.include_router(predict.router, prefix="/api/predict")

@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": loader.get_model_version()}
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Health check failing on cold start | `health_check_grace_period=120`; `/health` returns 200 before model loads |
| ALB log delivery failing | `attach_alb_log_policy()` called before `create_alb()` |
| Listener rule priority collision | `add_listener_rule()` scans existing rules and auto-increments |

**Validation**
```bash
python scripts/deploy.py --phase 2
uvicorn app.main:app --port 8080
curl http://localhost:8080/health
# {"status": "ok", "version": "none"}
```

---

## Phase 3 — Data Tier (RDS + ElastiCache)

**Significance:** Durable storage for predictions, ground truth, and feature history.
ElastiCache is the online feature store in Phase 7.

### `infra/database.py`

| Function | Responsibility |
|---|---|
| `create_db_subnet_group(subnet_ids)` | RDS subnet group across database subnets. |
| `create_rds(subnet_group, sg_id, db_name, instance_class)` | Multi-AZ Postgres. Encryption, 7-day backups, deletion protection in non-dev. Credentials in Secrets Manager. Returns `{endpoint, secret_arn}`. |
| `get_connection_string(secret_arn)` | Fetches from Secrets Manager. Returns SQLAlchemy URL. Never logged. |

### `infra/cache.py`

| Function | Responsibility |
|---|---|
| `create_cache_subnet_group(subnet_ids)` | ElastiCache subnet group. |
| `create_redis(subnet_group, sg_id, node_type)` | Replication group, 1 replica, auto-failover. At-rest + in-transit encryption. Auth token in Secrets Manager. Returns `{primary_endpoint, auth_token_secret_arn}`. |

### `app/features.py`
```python
class FeatureStore:
    def get(self, entity_id: str) -> dict:
        # Redis first (sub-ms); falls back to RDS on miss

    def set(self, entity_id: str, features: dict, ttl: int = 86400) -> None:
        # Writes to Redis with TTL; async write-through to RDS
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| RDS failover exhausting connections | RDS Proxy in front; pool survives failover |
| Redis eviction under memory pressure | `maxmemory-policy allkeys-lru`; cold path falls back to RDS |
| Accidental RDS destroy | `deletion_protection=True` on non-dev; guarded by `config.ENV != "dev"` |

**Validation**
```bash
python scripts/deploy.py --phase 3
python - <<'EOF'
import boto3, os
rds = boto3.client("rds", endpoint_url=os.getenv("LOCALSTACK_ENDPOINT"))
print([(i["DBInstanceIdentifier"], i["DBInstanceStatus"])
       for i in rds.describe_db_instances()["DBInstances"]])
EOF
```

---

## Phase 4 — ML Inference Tier

**Significance:** First ML-specific infrastructure. `provision_compute("ml", ...)` reuses
Phase 2's compute module — zero new infra code. ALB gains a path rule for `/api/predict/*`.

### `app/models/loader.py`

| Function | Responsibility |
|---|---|
| `load_model(model_name)` | Pulls artefact from `s3://.../stable/`. Caches in memory. Health returns 503 until loaded. |
| `get_model_version()` | Reads version from artefact metadata. |

### `app/models/sentiment.py` + `app/models/anomaly.py`
```python
# sentiment.py — HuggingFace pipeline
def predict(text: str) -> dict:   # returns {label, score, model_version}

# anomaly.py — Isolation Forest
def predict(features: list[float]) -> dict:   # returns {is_anomaly, anomaly_score}
```

### `app/predict.py`
```python
@router.post("/sentiment")
async def predict_sentiment(body: SentimentRequest) -> SentimentResponse: ...

@router.post("/anomaly")
async def predict_anomaly(body: AnomalyRequest) -> AnomalyResponse: ...
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Model artefact missing on boot | Polls S3 with exponential backoff; `/health` returns 503 until resolved |
| Large model cold-start | Warm pool pre-loads model; instance enters service only after loaded |

**Validation**
```bash
python scripts/deploy.py --phase 4
curl -X POST http://localhost:8080/api/predict/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "This project is excellent"}'
# {"label": "POSITIVE", "score": 0.99, "model_version": "v1.0"}
```

---

## Phase 5 — WAF + Security Hardening

### `infra/waf.py`

| Function | Responsibility |
|---|---|
| `create_web_acl(name, alb_arn)` | WAF WebACL with Common + SQLi rule sets + rate-limit. Starts in Count mode. Returns `web_acl_arn`. |
| `enable_waf_logging(web_acl_arn, firehose_arn)` | WAF logs → Firehose → S3. |
| `add_ip_whitelist(web_acl_arn, cidrs)` | IP set rule evaluated before rate-limit. |
| `set_rule_action(web_acl_arn, rule_name, action)` | Switches `COUNT` → `BLOCK` after log review. |

**Edge cases**
| Scenario | Mitigation |
|---|---|
| False-positives on valid JSON | Start in Count mode; switch manually after 48h log review |
| LocalStack free tier | Count-only mode; guarded by `LOCALSTACK_PRO` env check |

---

## Phase 6 — Predictive Auto-Scaling + Infrastructure ML

### `infra/monitoring.py`

| Function | Responsibility |
|---|---|
| `create_alarm(name, namespace, metric, threshold, comparison, alarm_actions)` | Generic CloudWatch alarm factory. |
| `create_dashboard(name, widgets)` | CloudWatch dashboard. |
| `put_metric(namespace, metric_name, value, dimensions)` | Publishes custom metric datapoint. |

### `mlops/scaler.py`

| Function | Responsibility |
|---|---|
| `fetch_request_history(asg_name, days)` | Reads CloudWatch `RequestCount`. Returns DataFrame. |
| `forecast_load(history, horizon_minutes)` | Prophet forecast. Flags high-uncertainty intervals. |
| `schedule_scaling_action(asg_name, desired, at, expiry_minutes)` | `PutScheduledUpdateGroupAction`. Auto-expires after `expiry_minutes`. |
| `run(event, context)` | Lambda handler — fetch → forecast → schedule. |

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Bad forecast over-scales | Sets `MinSize` not `DesiredCapacity`; auto-expires after 60 min |
| CloudWatch metric gaps | Forward-fill before passing to Prophet |

---

## Phase 7 — MLOps Pipeline

### `mlops/train.py`

| Function | Responsibility |
|---|---|
| `prepare_dataset(s3_key)` | Downloads from S3, validates schema. Returns DataFrame. |
| `train(dataset, model_type)` | Trains model. Checkpoints per epoch. Returns `(model, metrics)`. |
| `log_run(model, metrics, params)` | Logs to MLflow. Returns `run_id`. |
| `run(event, context)` | Lambda handler. |

### `mlops/promote.py`

| Function | Responsibility |
|---|---|
| `get_best_run(experiment_name, metric)` | Queries MLflow for highest-metric run. Returns `run_id`. |
| `promote(run_id, model_name)` | Uploads to `stable/`. Checks no A/B test active (SSM flag). Returns S3 key. |

### `mlops/drift.py`

| Function | Responsibility |
|---|---|
| `fetch_predictions(since)` | Queries RDS for prediction log + ground truth. Returns DataFrame. |
| `compute_psi(reference, current, buckets)` | Population Stability Index. PSI > 0.2 = significant drift. |
| `compute_kl(reference, current)` | KL divergence. |
| `publish_drift_metrics(psi, kl)` | Calls `monitoring.put_metric()`. |
| `trigger_retrain_if_needed(psi)` | Invokes training Lambda if PSI > 0.2. 24h cooldown via SSM. Skips if < 500 samples. |
| `run(event, context)` | Lambda handler. |

**A/B testing — reuses Phase 2, zero new code:**
```python
loadbalancer.add_weighted_rule(
    listener_arn=listener_arn,
    tg_v1_arn=tg_v1, weight_v1=90,
    tg_v2_arn=tg_v2, weight_v2=10,
    path_patterns=["/api/predict/*"], priority=10,
)
```

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Spot training interrupted | Checkpoints per epoch to S3; resumes from checkpoint |
| Both A/B versions unhealthy | ALB 503; `HealthyHostCount < 1` alarm fires immediately |
| Drift alarm on holiday traffic | `evaluation_periods=3` (18h sustained) before trigger |
| Circular retrain loop | 24h cooldown enforced via SSM timestamp |
| PSI on sparse labels | Skipped with warning if sample count < 500 |

---

## Phased Rollout Summary

| Phase | Files added | Resume skill |
|---|---|---|
| 1 | `infra/networking.py`, `scripts/bootstrap_state.py`, `scripts/deploy.py` | AWS networking automation, boto3 IaC |
| 2 | `infra/storage.py`, `infra/compute.py`, `infra/loadbalancer.py`, `app/main.py`, `app/health.py` | ALB + ASG automation, FastAPI |
| 3 | `infra/database.py`, `infra/cache.py`, `app/features.py` | RDS + Redis, feature store design |
| 4 | `app/models/`, `app/predict.py` | ML model serving, inference API |
| 5 | `infra/waf.py` | Security engineering |
| 6 | `mlops/scaler.py`, `infra/monitoring.py` | Predictive scaling, MLOps foundations |
| 7 | `mlops/train.py`, `mlops/promote.py`, `mlops/drift.py` | End-to-end MLOps pipeline |

Each phase adds new files only. No existing file is rewritten.
