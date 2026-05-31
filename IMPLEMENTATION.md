# AI Engineer Infrastructure — Implementation Plan

> **Principle:** Every phase is fully deployable and independently testable. No phase leaves the system in a broken state. Later phases extend earlier ones without rewriting them — DRY is enforced at the Terraform module level: one module, many instantiations.

---

## Architecture Overview

```
Internet
   │
  WAF
   │
  ALB  ─────────────────────────── (weighted routing for A/B model testing)
   │
  ┌─────────────┬──────────────┐
  │  App ASG    │  ML Infer    │   EC2 Auto Scaling Groups
  │  (web tier) │  ASG         │
  └──────┬──────┴──────┬───────┘
         │             │
  ┌──────▼─────────────▼───────┐
  │       Private Subnet       │
  │  ElastiCache   RDS         │   Data tier
  └────────────────────────────┘
         │
        S3  (training data / model artefacts / MLflow store)
         │
     CloudWatch  (metrics, alarms, drift dashboards)
         │
      Lambda  (scheduler: drift checks, retrain triggers)
```

---

## Module Structure (DRY layout)

```
terraform/
├── main.tf                  # root: wires all modules together
├── variables.tf             # all input vars in one place
├── outputs.tf               # all exposed values
├── terraform.tfvars         # environment-specific values (gitignored for secrets)
│
└── modules/
    ├── networking/          # VPC, subnets, IGW, NAT, route tables, SGs
    ├── compute/             # reusable ASG + launch template (used for web & ML tiers)
    ├── loadbalancer/        # ALB, listeners, target groups (supports weighted TGs)
    ├── database/            # RDS (Postgres), parameter groups, subnet groups
    ├── cache/               # ElastiCache Redis, subnet groups
    ├── storage/             # S3 buckets (training data, artefacts, logs)
    ├── waf/                 # WAF WebACL, rules, association
    ├── monitoring/          # CloudWatch dashboards, alarms, log groups
    ├── lambda/              # reusable Lambda wrapper (drift checker, retrain trigger)
    └── mlops/               # MLflow config, feature store wiring, model registry pointers
```

Each module exposes a stable `outputs.tf` so downstream modules reference outputs, never hard-coded ARNs or IDs.

---

## Phase 1 — Core Network Foundation

**Significance:** Every other resource lives inside this VPC. Getting the CIDR design, subnet layout, and security group rules right here prevents painful rebuilds later. This phase also establishes the Terraform state backend so the whole team works off the same lock.

**Deliverables**
- S3 remote state bucket + DynamoDB lock table (bootstrapped outside Terraform, then imported)
- VPC with public / private / database subnet tiers across 2 AZs
- Internet Gateway, NAT Gateway (one per AZ for HA)
- Route tables wired to correct subnets
- Base security groups: `sg_alb`, `sg_app`, `sg_ml`, `sg_db`, `sg_cache` with least-privilege ingress/egress rules

**Files touched**
- `modules/networking/` (full implementation)
- `main.tf` (networking block)
- `backend.tf` (S3 + DynamoDB remote state)

**Edge cases**
| Scenario | Mitigation |
|---|---|
| NAT Gateway AZ failure | One NAT per AZ; private route tables point to AZ-local NAT |
| CIDR exhaustion as more subnets are added | Reserve /16 VPC, assign /24 per subnet — 256 subnets available |
| Security group circular dependency (ALB → App → DB) | Define SGs first with no rules, then attach ingress rules as separate `aws_security_group_rule` resources |
| Remote state bucket not yet existing | Bootstrap script `scripts/bootstrap_state.sh` creates S3 + DynamoDB before `terraform init` |

**Validation**
```bash
terraform validate && terraform plan   # zero resource errors
aws ec2 describe-subnets --filters "Name=vpc-id,Values=<vpc_id>"  # 6 subnets across 2 AZs
```

---

## Phase 2 — Load Balancer + Web App Tier

**Significance:** Establishes the public-facing entry point and proves the three-tier pattern works end-to-end with a placeholder app before any ML is introduced. The `compute` module written here is reused verbatim for the ML inference tier in Phase 4.

**Deliverables**
- ALB in public subnets with HTTP → HTTPS redirect listener
- ACM certificate (DNS-validated via Route 53 or manual)
- Default target group pointing to web ASG
- `compute` module: launch template (Amazon Linux 2023, user-data script), ASG with min/max/desired, CloudWatch-based scaling policies
- Web tier ASG deploying a simple health-check app (NGINX or FastAPI stub)
- ALB access logs → S3 (used by ML anomaly model in Phase 6)

**Files touched**
- `modules/loadbalancer/` (full implementation)
- `modules/compute/` (full implementation — parameterised for reuse)
- `main.tf` (loadbalancer + web_asg blocks)

**DRY note:** `modules/compute` accepts `tier_name`, `instance_type`, `ami_id`, `user_data`, `min_size`, `max_size` as variables. Phase 4 calls the same module with different values — no duplication.

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Health check failing on cold start | `health_check_grace_period = 120` in ASG; app returns 200 on `/health` within grace window |
| Certificate not yet validated when ALB listener is created | Use `create_before_destroy` lifecycle on cert; listener depends on cert validation record |
| ASG launch template version drift | Pin `latest_version` on launch template reference; use `instance_refresh` for rolling updates |
| ALB returning 502 during deployment | Enable connection draining (deregistration delay 30s) on target group |
| Cross-AZ traffic costs | Enable ALB cross-zone load balancing; use AZ-aware target group routing for latency-sensitive paths |

**Validation**
```bash
curl -I https://<alb_dns>/health   # 200 OK
aws elbv2 describe-target-health --target-group-arn <arn>  # healthy
```

---

## Phase 3 — Data Tier (RDS + ElastiCache)

**Significance:** Provides durable storage for predictions, ground truth labels, feature history, and experiment metadata. ElastiCache will serve as the online feature store in the MLOps phases. Getting Multi-AZ and encryption right here avoids a destructive rebuild later.

**Deliverables**
- RDS PostgreSQL (Multi-AZ, encrypted at rest with KMS, automated backups 7-day retention)
- RDS parameter group tuned for ML workloads (higher `max_connections`, `pg_stat_statements` enabled)
- ElastiCache Redis cluster (replication group, at-rest + in-transit encryption, auth token via Secrets Manager)
- Secrets Manager secrets for DB credentials (rotated every 30 days)
- App tier IAM role updated to allow `secretsmanager:GetSecretValue`

**Files touched**
- `modules/database/` (full implementation)
- `modules/cache/` (full implementation)
- `main.tf` (database + cache blocks)

**Edge cases**
| Scenario | Mitigation |
|---|---|
| RDS failover causing connection pool exhaustion | Use RDS Proxy in front of RDS; pool sits outside the instance, survives failover |
| ElastiCache node failure | Replication group with 1 replica per shard; automatic failover enabled |
| Secret rotation breaking active connections | Rotation Lambda updates secret version; app reads secret at startup and caches with TTL, not per-request |
| DB schema migrations during rolling ASG deploys | Migrations run as a one-off ECS task / Lambda before ASG instance refresh, not inside app startup |
| Terraform destroying RDS on variable change | `lifecycle { prevent_destroy = true }` on RDS resource; `deletion_protection = true` on the RDS instance |

**Validation**
```bash
# From a bastion or SSM session into app EC2:
psql -h <rds_endpoint> -U app -d appdb -c "\l"
redis-cli -h <elasticache_endpoint> -p 6379 PING  # PONG
```

---

## Phase 4 — ML Inference Tier

**Significance:** This is the first ML-specific infrastructure. A dedicated ASG for inference keeps ML workloads isolated from web traffic — separate scaling policies, instance types (GPU-optional), and security boundaries. The ALB gains a second target group routed by path (`/api/predict/*`).

**Deliverables**
- ML inference ASG using the same `compute` module (different `instance_type`, `user_data`)
- `user_data` bootstraps: Python 3.11, FastAPI, model artefact pull from S3 on boot
- ALB listener rule: `path-pattern /api/predict/*` → ML target group
- S3 bucket for model artefacts (versioning enabled, lifecycle rule: move to Glacier after 90 days)
- IAM instance profile for ML ASG: `s3:GetObject` on artefacts bucket, `cloudwatch:PutMetricData`
- CloudWatch custom metrics: `InferenceLatencyP99`, `InferenceThroughput`, `ModelVersion`
- ASG scaling policy on `InferenceThroughput` (target tracking)

**Files touched**
- `modules/storage/` (artefacts bucket)
- `main.tf` (ml_asg block reusing `modules/compute`)
- `modules/monitoring/` (custom metrics + alarms)

**DRY note:** `modules/compute` is called a second time. Zero new compute module code.

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Model artefact not yet in S3 on first boot | `user_data` polls S3 with exponential backoff (max 5 min); instance fails health check and ASG replaces it — ALB never routes to it |
| Cold-start inference latency spike | Warm pool on ASG (`warm_pool { min_size = 1 }`); instances pre-load model before entering service |
| Model file too large for `user_data` timeout | `user_data` pulls artefact asynchronously; health endpoint returns 503 until model is loaded |
| GPU instance not available in AZ | Multi-AZ ASG with `capacity_rebalance = true`; mixed instances policy falls back to CPU instance type |
| Inference memory leak over time | CloudWatch alarm on EC2 memory % (custom metric via CW agent); triggers ASG instance refresh on breach |

**Validation**
```bash
curl -X POST https://<alb_dns>/api/predict/sentiment \
  -H "Content-Type: application/json" \
  -d '{"text": "Terraform is great"}'
# {"label": "POSITIVE", "score": 0.99, "model_version": "v1.2"}
```

---

## Phase 5 — WAF + Security Hardening

**Significance:** Attaches an AWS WAF WebACL to the ALB to protect both the web and ML tiers. Also wires WAF logs into S3 for the anomaly detection model built in Phase 6. Closes the security gap that would otherwise leave the ML endpoint open to abuse.

**Deliverables**
- WAF WebACL associated with ALB
- Managed rule groups: `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesKnownBadInputsRuleSet`, `AWSManagedRulesSQLiRuleSet`
- Rate-limit rule: 1000 req/5 min per IP on `/api/predict/*` (protects inference budget)
- WAF logs → Kinesis Firehose → S3 (`waf-logs/` prefix, partitioned by date)
- CloudWatch alarm: WAF block rate spike (> 5% of requests blocked → SNS alert)
- Security group audit: verify no `0.0.0.0/0` ingress except ALB SG on ports 80/443

**Files touched**
- `modules/waf/` (full implementation)
- `main.tf` (waf block)
- `modules/monitoring/` (WAF alarm added)

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Legitimate ML client IPs rate-limited | IP set whitelist for known internal/partner CIDRs; applied before rate-limit rule (priority ordering) |
| WAF false-positive blocking valid JSON payloads | `AWSManagedRulesCommonRuleSet` applied in Count mode first; review logs 48h before switching to Block |
| Firehose delivery failures losing WAF logs | Firehose S3 backup bucket with error prefix; CloudWatch alarm on `DeliveryToS3.Records` drop |
| WAF association failing if ALB ARN changes | WAF association references ALB module output; Terraform dependency graph handles ordering |

**Validation**
```bash
# Should be blocked (SQLi attempt):
curl "https://<alb_dns>/api/predict/sql?input=1'+OR+'1'='1"
# HTTP 403

# WAF logs in S3:
aws s3 ls s3://<logs_bucket>/waf-logs/$(date +%Y/%m/%d)/
```

---

## Phase 6 — Predictive Auto-Scaling + Infrastructure ML

**Significance:** This is where infrastructure becomes AI-powered. A Lambda function runs on a cron, reads CloudWatch metrics history, feeds a Prophet/LSTM model, and schedules ASG scaling actions 30 min ahead of predicted load spikes. This closes the "reactive scaling" gap and is a genuine differentiator on an AI Engineer resume.

**Deliverables**
- `modules/lambda/` — reusable Lambda module (runtime, role, env vars, CloudWatch log group)
- `predictive_scaler` Lambda: reads CloudWatch `RequestCount` 7-day history, runs Prophet forecast, writes scheduled ASG actions via `autoscaling:PutScheduledUpdateGroupAction`
- EventBridge rule: triggers `predictive_scaler` every 30 min
- `infra_anomaly_detector` Lambda (second instantiation of `modules/lambda`): reads ALB + CloudWatch metrics, runs Isolation Forest, publishes `InfraAnomaly` custom metric, triggers SNS alert
- MLflow tracking server (EC2 t3.small or Fargate) writing experiment data to S3 + RDS

**Files touched**
- `modules/lambda/` (full implementation)
- `modules/mlops/` (MLflow server)
- `main.tf` (predictive_scaler + anomaly_detector blocks, both using `modules/lambda`)

**DRY note:** Both Lambdas use `modules/lambda`. The module takes `function_name`, `handler`, `runtime`, `environment_variables`, `schedule_expression`. No Lambda config is duplicated.

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Prophet model cold-start in Lambda > 15 min limit | Package model as a Lambda layer; pre-warm with provisioned concurrency (1 instance) |
| Forecast wrong — schedules too many instances | Scaling action sets `min_size` not `desired_capacity`; hard cap at `max_size`; auto-expires after 1h |
| Lambda can't assume ASG IAM role | Lambda execution role explicitly grants `autoscaling:PutScheduledUpdateGroupAction` on specific ASG ARNs only |
| CloudWatch metric gaps (sparse data) | Forward-fill gaps before feeding to Prophet; flag high-uncertainty forecasts and skip scheduling |
| MLflow server single point of failure | S3-backed artifact store + RDS-backed metadata store; MLflow server itself is stateless and can be restarted |

**Validation**
```bash
# Invoke predictive scaler manually:
aws lambda invoke --function-name predictive_scaler out.json && cat out.json

# Check scheduled actions were created:
aws autoscaling describe-scheduled-actions --auto-scaling-group-name ml-inference-asg
```

---

## Phase 7 — MLOps Pipeline (Training → Registry → A/B Deploy → Drift)

**Significance:** This is the capstone phase. It closes the ML loop: data in S3 → training job → model in registry → A/B deployment via ALB weighted routing → drift detection via Lambda → automated retrain trigger. Every bullet on the resume card maps to a concrete resource.

**Deliverables**

### 7a — Model Training Pipeline
- S3 training data bucket (versioning + lifecycle: Glacier after 180 days)
- EC2 Spot `training_runner` (launched on-demand by Lambda, not always-on)
- `training_trigger` Lambda: validates new data in S3, launches training Spot instance via ASG with `desired_capacity = 1`, resets to 0 on completion
- MLflow experiment tracking: parameters, metrics, artefact URI logged per run

### 7b — Model Registry & Versioned Deployment
- S3 artefact path convention: `s3://artefacts/<model_name>/<version>/model.tar.gz`
- `model_promoter` Lambda: reads MLflow run metrics, compares against baseline, uploads to `stable/` prefix if passes threshold
- ML inference ASG `user_data` reads `stable/` prefix — new instances always boot the promoted model

### 7c — A/B Testing via ALB Weighted Routing
- Second ML inference ASG: `ml_asg_v2` (same `compute` module, different `ami_id` / `user_data` pointing to new model)
- ALB weighted target groups: `v1_weight = 90`, `v2_weight = 10` (variables, adjustable without full redeploy)
- CloudWatch dashboard: `ModelVersion` metric per target group — live traffic split visible

### 7d — Drift Monitoring + Auto-Retrain
- `drift_checker` Lambda (third `modules/lambda` instantiation): runs every 6h, queries RDS for prediction logs + ground truth, computes PSI (Population Stability Index) and KL divergence, publishes `FeatureDrift` and `LabelDrift` custom metrics
- CloudWatch alarm: `FeatureDrift > 0.2` → SNS → triggers `training_trigger` Lambda (auto-retrain)
- Feature store: ElastiCache Redis (online, sub-ms lookup at inference time) + RDS `features` schema (offline, training joins)

**Files touched**
- `modules/mlops/` (MLflow, feature store schema, drift metrics)
- `modules/storage/` (training data bucket added)
- `main.tf` (ml_asg_v2, training_trigger, model_promoter, drift_checker)

**Edge cases**
| Scenario | Mitigation |
|---|---|
| Training Spot instance interrupted mid-run | Spot interruption handler (2-min notice Lambda) checkpoints model to S3; training resumes from checkpoint |
| Both A/B model versions unhealthy simultaneously | ALB health check fails both TGs → ALB returns 503; CloudWatch alarm fires immediately; on-call notified |
| Drift alarm firing during known data distribution shift (holiday traffic) | Alarm has `evaluation_periods = 3` (18h of sustained drift before trigger); manual suppress via SNS filter |
| PSI calculation on sparse label data | Minimum 500 labelled samples required; `drift_checker` skips and logs warning if below threshold |
| Feature store Redis eviction under memory pressure | Set `maxmemory-policy allkeys-lru`; TTL on all feature keys (24h); cold path falls back to RDS |
| Model promoter racing with ongoing A/B test | `model_promoter` checks if A/B test is active (flag in DynamoDB/Parameter Store); skips promotion until test concludes |
| Circular retrain loop (drift triggers retrain, new model still drifts) | Retrain cooldown: `training_trigger` Lambda checks SSM Parameter Store for last train timestamp; enforces 24h minimum gap |

**Validation**
```bash
# Trigger a manual training run:
aws lambda invoke --function-name training_trigger \
  --payload '{"manual": true, "data_version": "2026-05-31"}' out.json

# Check A/B split is live:
aws elbv2 describe-target-groups --load-balancer-arn <alb_arn>
# v1_weight=90, v2_weight=10

# Check drift metrics:
aws cloudwatch get-metric-statistics \
  --namespace "MLOps/DriftMonitor" \
  --metric-name "FeatureDrift" \
  --start-time $(date -u -v-1d +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 21600 --statistics Average
```

---

## Cross-Cutting Concerns (apply in every phase)

### Tagging Strategy
All resources tagged with:
```hcl
locals {
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Phase       = var.phase   # "1" through "7"
  }
}
```
Every module merges `local.common_tags` with resource-specific tags. No tag block is written twice.

### Secrets
- RDS password, Redis auth token, MLflow DB URL → Secrets Manager
- Never in `terraform.tfvars`, never in state file (use `sensitive = true` on all outputs containing secrets)
- `.gitignore` must include `terraform.tfvars`, `*.tfstate`, `*.tfstate.backup`, `.terraform/`

### IAM Least Privilege
- Each ASG instance profile, Lambda execution role, and EC2 training role gets its own policy
- No `*` actions; no `*` resources — every policy scoped to specific ARNs or ARN patterns
- Policies defined inline in each module's `iam.tf`

### Terraform State Hygiene
- One workspace per environment (`dev`, `staging`, `prod`)
- State locking via DynamoDB — prevents concurrent `terraform apply` races
- `terraform plan` output reviewed in CI before every `apply`

### Cost Guard-rails
- `max_size` on every ASG is a Terraform variable with a sensible default (e.g. `8` for inference)
- EC2 Spot for training — saves ~70% vs On-Demand
- S3 lifecycle rules on every bucket (logs, WAF events, training data, artefacts)
- NAT Gateway: one per AZ (not per subnet) — biggest single source of unexpected VPC egress cost

---

## Phased Rollout Summary

| Phase | Core resource added | Resume skill unlocked |
|---|---|---|
| 1 | VPC, subnets, SGs | Terraform IaC, network design |
| 2 | ALB, ASG, web tier | Auto-scaling, load balancing |
| 3 | RDS, ElastiCache | Database design, caching |
| 4 | ML inference ASG, S3 artefacts | ML model serving on AWS |
| 5 | WAF, rate limiting | Security engineering |
| 6 | Predictive scaling Lambda, MLflow | AI-powered infra, MLOps foundations |
| 7 | A/B deploy, drift monitor, feature store | End-to-end MLOps pipeline |

Each phase merges cleanly into `main.tf` — no phase requires rewriting a previous one.
