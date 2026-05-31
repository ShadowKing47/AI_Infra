#!/usr/bin/env bash
# Run this ONCE before `tflocal init` to create the remote state backend.
# Safe to re-run — all commands are idempotent.
#
# Modes:
#   LOCAL=true  (default) — targets LocalStack at localhost:4566
#   LOCAL=false           — targets real AWS (requires valid credentials)
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-ai-infra}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
LOCAL="${LOCAL:-true}"

BUCKET_NAME="${PROJECT}-${ENVIRONMENT}-tfstate"
TABLE_NAME="${PROJECT}-${ENVIRONMENT}-tfstate-lock"

# ── CLI alias: awslocal when LOCAL=true, plain aws otherwise ──────────────────
if [ "$LOCAL" = "true" ]; then
  LOCALSTACK_ENDPOINT="http://localhost:4566"
  AWS_CMD="aws --endpoint-url=$LOCALSTACK_ENDPOINT"
  INIT_CMD="tflocal"
  echo "==> Bootstrapping Terraform remote state (LocalStack)"
else
  AWS_CMD="aws"
  INIT_CMD="terraform"
  echo "==> Bootstrapping Terraform remote state (real AWS)"
fi

echo "    Region : $AWS_REGION"
echo "    Bucket : $BUCKET_NAME"
echo "    Table  : $TABLE_NAME"

# ── LocalStack health check ───────────────────────────────────────────────────
if [ "$LOCAL" = "true" ]; then
  echo "    Waiting for LocalStack to be ready..."
  for i in $(seq 1 20); do
    if curl -sf http://localhost:4566/_localstack/health | grep -q '"s3": "available"'; then
      echo "    [ok] LocalStack is ready"
      break
    fi
    if [ "$i" -eq 20 ]; then
      echo "    [error] LocalStack not ready after 20 attempts. Is it running?"
      echo "            Run: docker compose up -d"
      exit 1
    fi
    sleep 3
  done
fi

# ── S3 state bucket ────────────────────────────────────────────────────────────
if $AWS_CMD s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
  echo "    [skip] S3 bucket already exists"
else
  if [ "$AWS_REGION" = "us-east-1" ]; then
    $AWS_CMD s3api create-bucket \
      --bucket "$BUCKET_NAME" \
      --region "$AWS_REGION"
  else
    $AWS_CMD s3api create-bucket \
      --bucket "$BUCKET_NAME" \
      --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  echo "    [ok] S3 bucket created"
fi

$AWS_CMD s3api put-bucket-versioning \
  --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled

$AWS_CMD s3api put-bucket-encryption \
  --bucket "$BUCKET_NAME" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
      "BucketKeyEnabled": true
    }]
  }'

$AWS_CMD s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "    [ok] S3 bucket configured (versioning, encryption, public-access-block)"

# ── DynamoDB lock table ────────────────────────────────────────────────────────
if $AWS_CMD dynamodb describe-table --table-name "$TABLE_NAME" --region "$AWS_REGION" \
    2>/dev/null | grep -q ACTIVE; then
  echo "    [skip] DynamoDB table already exists"
else
  $AWS_CMD dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$AWS_REGION"
  echo "    [ok] DynamoDB lock table created"
fi

echo ""
echo "==> Done. Run ${INIT_CMD} init next:"
echo ""
echo "    ${INIT_CMD} init \\"
echo "      -backend-config=\"bucket=${BUCKET_NAME}\" \\"
echo "      -backend-config=\"key=${ENVIRONMENT}/terraform.tfstate\" \\"
echo "      -backend-config=\"region=${AWS_REGION}\" \\"
echo "      -backend-config=\"dynamodb_table=${TABLE_NAME}\""
