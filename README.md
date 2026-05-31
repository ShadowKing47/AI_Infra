# AI Infrastructure — End-to-End MLOps Platform

A production-ready, phase-based infrastructure-as-code project that provisions a complete AI/ML platform on AWS (or LocalStack). Every phase is independently deployable, testable, and extends the previous one without rewriting existing code.

## 🎯 Project Vision

Build a **fully functional MLOps platform** where:
- Infrastructure is code (Python + boto3, not Terraform)
- Every phase leaves the system deployable and working
- Code is reusable (DRY enforced at the module level)
- Local development mirrors production (LocalStack)
- Models move from training → registry → A/B testing → drift monitoring seamlessly

## 📋 Current Status

✅ **Phase 1: Core Network Foundation**
- VPC with public/private/database subnets across multiple AZs
- Internet Gateway, NAT Gateways, route tables
- Five security groups with proper trust model
- Idempotent provisioning (safe to re-run)

✅ **Phase 2: Load Balancer + Web App Tier**
- Application Load Balancer (ALB) with access logging
- Target groups, listeners, path-based routing rules
- Auto Scaling Groups with CPU target-tracking
- EC2 launch templates with IMDSv2, CloudWatch monitoring, IAM roles
- FastAPI application with `/health` endpoint
- 32 unit tests covering all components

## 🏗️ Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│ Phase 7: MLOps Pipeline                                         │
│ (Training → Registry → A/B Deploy → Drift Monitoring)          │
├─────────────────────────────────────────────────────────────────┤
│ Phase 6: Predictive Auto-Scaling + Infrastructure ML           │
│ (CloudWatch → Prophet → Scheduled Scaling Actions)             │
├─────────────────────────────────────────────────────────────────┤
│ Phase 5: WAF + Security Hardening                              │
│ (Web Application Firewall, rate limiting, IP whitelisting)     │
├─────────────────────────────────────────────────────────────────┤
│ Phase 4: ML Inference Tier                                      │
│ (Model serving on EC2, path-based routing to /api/predict/*)   │
├─────────────────────────────────────────────────────────────────┤
│ Phase 3: Data Tier                                              │
│ (RDS Postgres Multi-AZ, ElastiCache Redis, Secrets Manager)    │
├─────────────────────────────────────────────────────────────────┤
│ Phase 2: Load Balancer + Web App (✅ COMPLETE)                 │
│ (ALB, ASG, FastAPI, target groups, path routing)               │
├─────────────────────────────────────────────────────────────────┤
│ Phase 1: Core Network (✅ COMPLETE)                            │
│ (VPC, subnets, IGW, NAT, route tables, security groups)        │
└─────────────────────────────────────────────────────────────────┘
```

## 📦 Tech Stack

**Infrastructure Provisioning**
- `boto3` — Python AWS SDK
- `localstack` — Local AWS simulator for development

**Web Framework & Serving**
- `fastapi` — Modern async Python web framework
- `uvicorn` — ASGI server

**Data & Machine Learning**
- `pandas`, `numpy` — Data manipulation
- `scikit-learn` — ML algorithms
- `transformers` — HuggingFace NLP models
- `torch` — PyTorch

**MLOps & Monitoring**
- `mlflow` — Experiment tracking & model registry
- `prophet` — Time series forecasting
- `prometheus-client` — Metrics

**Testing**
- `pytest` — Unit/integration testing
- Mocking for boto3 clients

## 📁 Project Structure

```
ai-infra/
├── infra/                          # AWS provisioning modules
│   ├── client.py                   # Shared boto3 session factory
│   ├── config.py                   # Config (region, names, CIDRs)
│   ├── networking.py               # Phase 1: VPC, subnets, SGs
│   ├── storage.py                  # Phase 2: S3 buckets
│   ├── compute.py                  # Phase 2: Launch templates, ASGs
│   ├── loadbalancer.py             # Phase 2: ALB, target groups, routing
│   ├── database.py                 # Phase 3: RDS Postgres
│   ├── cache.py                    # Phase 3: ElastiCache Redis
│   ├── waf.py                      # Phase 5: WAF WebACL
│   ├── monitoring.py               # Phase 6: CloudWatch alarms
│   └── mlops.py                    # Phase 7: Feature store wiring
│
├── app/                            # FastAPI application
│   ├── main.py                     # FastAPI app + lifespan
│   ├── health.py                   # /health endpoint
│   ├── predict.py                  # /api/predict/* endpoints
│   ├── features.py                 # Feature store client
│   └── models/                     # Model loading & inference
│
├── mlops/                          # ML pipeline scripts
│   ├── train.py                    # Training + MLflow logging
│   ├── promote.py                  # Model promotion to stable/
│   ├── drift.py                    # Drift detection (PSI/KL)
│   └── scaler.py                   # Predictive scaling (Prophet)
│
├── scripts/
│   ├── deploy.py                   # Phase orchestrator
│   └── bootstrap_state.py          # Initialize S3 state bucket
│
├── tests/
│   ├── test_storage.py
│   ├── test_compute.py
│   ├── test_loadbalancer.py
│   └── test_app.py
│
├── docker-compose.yml              # LocalStack + app service
├── requirements.txt                # Production dependencies
├── requirements-dev.txt            # Development tools
├── requirements-local.txt          # LocalStack dependencies
└── .env.example                    # Environment template
```

## 🚀 Quick Start

### Prerequisites
```bash
# Python 3.9+
python --version

# Docker (for LocalStack)
docker --version
```

### Setup

```bash
# 1. Clone and enter project
git clone <repo>
cd ai-infra

# 2. Create virtual environment
python3.9 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt -r requirements-dev.txt -r requirements-local.txt

# 4. Configure environment
cp .env.example .env
```

### Deploy Phases

```bash
# Start LocalStack
docker compose up -d

# Deploy Phase 1 only
python scripts/deploy.py --phase 1

# Deploy Phase 1 + 2
python scripts/deploy.py --phase 2

# Deploy all phases
python scripts/deploy.py

# Run tests
pytest tests/
```

### Run FastAPI Locally

```bash
# Start FastAPI server
uvicorn app.main:app --reload --port 8080

# Test health endpoint
curl http://localhost:8080/health
# {"status": "ok", "version": "none"}
```

## 🔑 Key Design Principles

1. **Idempotency**: Every `create_*` function checks for existing resources by Name tag before creating, enabling safe re-runs

2. **DRY (Don't Repeat Yourself)**: 
   - `provision_compute()` reused for web tier (Phase 2) and ML tier (Phase 4)
   - Single boto3 session factory (`client.py`)
   - Centralized resource naming (`utils/naming.py`)

3. **Phase Independence**: Each phase is deployable and testable standalone; later phases don't rewrite earlier code

4. **State Persistence**: S3-backed state allows resuming partial deployments

5. **Local-First**: LocalStack enables full development without AWS account; same code switches to real AWS via env var

6. **Comprehensive Testing**: Unit tests for all infrastructure modules with mocked boto3 clients

## 📊 Phases Breakdown

| Phase | Focus | Key Components | Skills |
|-------|-------|---|---|
| 1 | Network | VPC, subnets, IGW, NAT, SGs | AWS networking, boto3 IaC |
| 2 | Web App | ALB, ASG, S3, FastAPI | Load balancing, auto-scaling, Python web |
| 3 | Data | RDS, ElastiCache, Secrets | Databases, caching, security |
| 4 | ML Inference | Model serving, endpoints | ML ops, inference optimization |
| 5 | Security | WAF, rate limiting | Security engineering |
| 6 | Scaling | Predictive auto-scaling | Time series, infrastructure ML |
| 7 | MLOps Loop | Training, registry, A/B, drift | End-to-end ML pipeline |

## 🧪 Testing

Run all tests:
```bash
pytest tests/ -v
```

Run specific test:
```bash
pytest tests/test_loadbalancer.py::test_create_alb_new -v
```

With coverage:
```bash
pytest tests/ --cov=infra --cov=app --cov=scripts
```

## 📝 Configuration

Edit `.env` to customize:
```bash
LOCALSTACK_ENDPOINT=http://localhost:4566  # LocalStack URL
AWS_DEFAULT_REGION=us-east-1
PROJECT_NAME=ai-infra
ENVIRONMENT=dev
```

## 🔗 Integration Points

- **Phase 1 → 2**: Networking resources (VPC, subnets, SGs)
- **Phase 2 → 3**: Target group ARNs for health checks
- **Phase 2 → 4**: ALB reused for ML tier routing
- **Phase 4 → 7**: Model artefacts stored in S3 (Phase 2)

## 📚 Documentation

- [PHASE_2_COMPLETE.md](PHASE_2_COMPLETE.md) — Phase 2 implementation details
- [REQUIREMENTS.md](REQUIREMENTS.md) — Dependencies & installation guide
- [IMPLEMENTATION.md](trash/IMPLEMENTATION.md) — Full design spec for all 7 phases

## 🎓 What You'll Learn

- AWS infrastructure provisioning with Python (boto3)
- Auto Scaling, Load Balancing, VPC design
- FastAPI + async Python web frameworks
- ML model serving & inference optimization
- Time series forecasting for predictive scaling
- Infrastructure-as-Code best practices
- End-to-end MLOps pipeline design

## 🛣️ Roadmap

- [ ] Phase 3: RDS + ElastiCache (database tier)
- [ ] Phase 4: ML model serving on EC2
- [ ] Phase 5: WAF and security hardening
- [ ] Phase 6: Predictive auto-scaling with Prophet
- [ ] Phase 7: Complete MLOps loop (training → registry → drift)
- [ ] Add Kubernetes variant (ECS/EKS)
- [ ] Add monitoring dashboards
- [ ] Multi-region deployment

## 💡 Why This Approach?

Instead of a single monolithic deployment, this project demonstrates how to build infrastructure incrementally. You can:
- Deploy Phase 1 and stop there if you need just networking
- Add Phase 2 to get a web tier without rewriting Phase 1
- Swap in ML models in Phase 4 without touching earlier phases
- Extend with MLOps in Phase 7

This modularity is what real production systems need.

## 📧 Contributing

This is a personal learning project, but insights and feedback welcome!

## 📄 License

MIT

---

**Built with** Python, boto3, FastAPI, and LocalStack  
**Status**: Phase 1 ✅ Phase 2 ✅ Phases 3–7 🚧  
**Last Updated**: May 31, 2026
