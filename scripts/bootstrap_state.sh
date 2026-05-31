#!/usr/bin/env bash
# Run this ONCE before `terraform init` to create the remote state backend.
# Safe to re-run — all commands are idempotent.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT="${PROJECT:-ai-infra}"
ENVIRONMENT="${ENVIRONMENT:-dev}"

BUCKET_NAME="${PROJECT}-${ENVIRONMENT}-tfstate"
TABLE_NAME="${PROJECT}-${ENVIRONMENT}-tfstate-lock"

echo "==> Bootstrapping Terraform remote state"
echo "    Region : $AWS_REGION"
echo "    Bucket : $BUCKET_NAME"
echo "    Table  : $TABLE_NAME"

# ── S3 state bucket ────────────────────────────────────────────────────────────
if aws s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
  echo "    [skip] S3 bucket already exists"
else
  if [ "$AWS_REGION" = "us-east-1" ]; then
    aws s3api create-bucket \
      --bucket "$BUCKET_NAME" \
      --region "$AWS_REGION"
  else
    aws s3api create-bucket \
      --bucket "$BUCKET_NAME" \
      --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
  echo "    [ok] S3 bucket created"
fi

aws s3api put-bucket-versioning \
  --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket "$BUCKET_NAME" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
      "BucketKeyEnabled": true
    }]
  }'

aws s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,\
BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "    [ok] S3 bucket configured (versioning, encryption, public-access-block)"

# ── DynamoDB lock table ─────────────────────────────────────────────────────────
if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$AWS_REGION" \
    2>/dev/null | grep -q ACTIVE; then
  echo "    [skip] DynamoDB table already exists"
else
  aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$AWS_REGION"
  echo "    [ok] DynamoDB lock table created"
fi

echo ""
echo "==> Done. Run terraform init next:"
echo ""
echo "    terraform init \\"
echo "      -backend-config=\"bucket=${BUCKET_NAME}\" \\"
echo "      -backend-config=\"key=${ENVIRONMENT}/terraform.tfstate\" \\"
echo "      -backend-config=\"region=${AWS_REGION}\" \\"
echo "      -backend-config=\"dynamodb_table=${TABLE_NAME}\""
