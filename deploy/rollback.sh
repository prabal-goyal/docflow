#!/usr/bin/env bash
# Rolls production back to the image deployed before the current one (recorded on the server in
# /opt/docflow/PREVIOUS_IMAGE), using the same health-checked deploy script as CI. Running it again
# rolls forward: the two images swap.
#
# Caution: the previous image must work with the current database schema. Releases that add
# migrations must stay backward compatible (see docs/runbook.md), or rollback needs a downgrade.
#
# Usage: deploy/rollback.sh <instance-id> [aws-profile]
set -euo pipefail

INSTANCE="${1:?usage: rollback.sh <instance-id> [aws-profile]}"
PROFILE="${2:-docflow}"

REMOTE='set -euo pipefail
cd /opt/docflow
test -s PREVIOUS_IMAGE || { echo "No PREVIOUS_IMAGE recorded; nothing to roll back to"; exit 1; }
FROM=$(cat CURRENT_IMAGE); TO=$(cat PREVIOUS_IMAGE)
echo "switching: ${FROM##*:} -> ${TO##*:}"
AWS_REGION=$(sed -n "s/^AWS_REGION=//p" .env) bash remote-deploy.sh "$TO" | tail -1
echo "running now: $(docker inspect --format "{{.Config.Image}}" docflow-api-1 | sed "s/.*://")"'

PARAMS=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["900"]}))' "$REMOTE")
ID=$(aws ssm send-command --profile "$PROFILE" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --comment "rollback" \
  --parameters "$PARAMS" --query Command.CommandId --output text)
for _ in $(seq 1 90); do
  STATUS=$(aws ssm get-command-invocation --profile "$PROFILE" --command-id "$ID" \
    --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Success|Failed|Cancelled|TimedOut) break ;; esac
  sleep 5
done
aws ssm get-command-invocation --profile "$PROFILE" --command-id "$ID" --instance-id "$INSTANCE" \
  --query "[StandardOutputContent, StandardErrorContent]" --output text \
  | grep -E "switching|Deployed|running now|No PREVIOUS|rror" || true
echo "status: $STATUS"
[[ "$STATUS" == "Success" ]]
