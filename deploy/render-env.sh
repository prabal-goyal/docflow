#!/usr/bin/env bash
# Runs on the EC2 instance before each deploy. Reads DocFlow's secrets from SSM Parameter Store
# (using the instance's IAM role, no keys) and writes the files the production compose file reads.
# The files are readable by root only and never leave the instance.
#
# Usage: sudo deploy/render-env.sh <ssm-prefix>      e.g. sudo deploy/render-env.sh /docflow/prod
set -euo pipefail

PREFIX="${1:?usage: render-env.sh <ssm-prefix>}"
OUT_DIR=/opt/docflow
REGION="${AWS_REGION:?set AWS_REGION}"

param() {
  aws ssm get-parameter --region "$REGION" --with-decryption --name "$PREFIX/$1" \
    --query Parameter.Value --output text
}

umask 077  # files created below are readable by root only
mkdir -p "$OUT_DIR"

DB_PASSWORD=$(param db-password)
printf '%s' "$DB_PASSWORD" > "$OUT_DIR/postgres_password"
# The postgres container reads the password file as its own user.
chmod 0444 "$OUT_DIR/postgres_password"

cat > "$OUT_DIR/docflow.env" <<EOF
DOCFLOW_ENVIRONMENT=production
DOCFLOW_DATABASE_URL=postgresql+psycopg://docflow:${DB_PASSWORD}@db:5432/docflow
DOCFLOW_JWT_SECRET=$(param jwt-secret)
DOCFLOW_REDIS_URL=redis://redis:6379/0
DOCFLOW_S3_BUCKET=$(param s3-bucket)
DOCFLOW_S3_REGION=${REGION}
DOCFLOW_WEBHOOK_SECRET_KMS_KEY_ID=$(param kms-key-id)
OPENAI_API_KEY=$(param openai-api-key)
EOF
chmod 0600 "$OUT_DIR/docflow.env"

# Non-secret values the compose file itself needs (read automatically from the project directory).
cat > "$OUT_DIR/.env" <<EOF
AWS_REGION=${REGION}
DOCFLOW_SITE_ADDRESS=$(param site-address)
EOF
echo "Rendered $OUT_DIR/docflow.env from $PREFIX"
