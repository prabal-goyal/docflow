#!/usr/bin/env bash
# Creates the first admin on the production server without the password ever being printed or
# appearing in SSM command history:
#   1. a random password is stored as a SecureString in SSM (from this machine);
#   2. the server reads it from SSM itself (instance role) and runs create-admin.
# Read the password afterwards with:
#   aws ssm get-parameter --with-decryption --name <prefix>/admin-password --query Parameter.Value --output text
#
# Usage: deploy/create-admin.sh <instance-id> <admin-email> [ssm-prefix] [aws-profile]
set -euo pipefail

INSTANCE="${1:?usage: create-admin.sh <instance-id> <admin-email> [ssm-prefix] [aws-profile]}"
EMAIL="${2:?admin email required}"
PREFIX="${3:-/docflow/prod}"
PROFILE="${4:-docflow}"
PARAM="$PREFIX/admin-password"

if aws ssm get-parameter --profile "$PROFILE" --name "$PARAM" > /dev/null 2>&1; then
  echo "$PARAM already exists; reusing it"
else
  aws ssm put-parameter --profile "$PROFILE" --type SecureString --name "$PARAM" \
    --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > /dev/null
  echo "Stored a new random password in $PARAM"
fi

# Runs on the server. Single quotes: nothing here is expanded on this machine.
REMOTE='cd /opt/docflow && export DOCFLOW_IMAGE=$(cat CURRENT_IMAGE) AWS_REGION=$(cat .env | sed -n "s/^AWS_REGION=//p") && PW=$(aws ssm get-parameter --region "$AWS_REGION" --with-decryption --name '"$PARAM"' --query Parameter.Value --output text) && docker compose -f docker-compose.prod.yml run --rm -T -e DOCFLOW_ADMIN_PASSWORD="$PW" api python -m docflow.cli create-admin --email '"$EMAIL"
PARAMS=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]]}))' "$REMOTE")

ID=$(aws ssm send-command --profile "$PROFILE" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --comment "create admin" \
  --parameters "$PARAMS" --query Command.CommandId --output text)

for _ in $(seq 1 30); do
  STATUS=$(aws ssm get-command-invocation --profile "$PROFILE" --command-id "$ID" \
    --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Success|Failed|Cancelled|TimedOut) break ;; esac
  sleep 3
done
aws ssm get-command-invocation --profile "$PROFILE" --command-id "$ID" --instance-id "$INSTANCE" \
  --query "[StandardOutputContent, StandardErrorContent]" --output text | grep -v "^\s*$" | tail -5
echo "status: $STATUS"
