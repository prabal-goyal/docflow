#!/usr/bin/env bash
# Deploys the checked-out commit from a workstation, doing what the CI deploy job does: build and
# push the image, upload the deploy bundle, run remote-deploy.sh on the server through SSM, check
# /ready over HTTPS. For when GitHub Actions is unavailable, or for an environment GitHub does not
# deploy (a drill or a test copy). Targets come from the selected Terraform workspace's outputs.
#
# Usage (from the repository root): deploy/manual-deploy.sh [aws-profile]
set -euo pipefail

PROFILE="${1:-docflow}"
out() { terraform -chdir=infra output -raw "$1"; }
REGION=$(out region)
REPOSITORY=$(out ecr_repository_url)
BUCKET=$(out bucket)
INSTANCE=$(out instance_id)
HOST=$(out hostname)
PREFIX=$(out ssm_prefix)
aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

TAG=$(git rev-parse HEAD)
if [[ -n "$(git status --porcelain)" ]]; then
  # Tags are immutable and mean "this commit"; uncommitted changes get a tag that says so.
  TAG="$TAG-dirty-$(date +%s)"
  echo "Warning: uncommitted changes; deploying them as $TAG"
fi
IMAGE="$REPOSITORY:$TAG"
BUNDLE="s3://$BUCKET/deploy/$TAG/"
echo "Deploying $TAG to $INSTANCE ($REGION, $PREFIX)"

aws_ ecr get-login-password | docker login --username AWS --password-stdin "${IMAGE%%/*}"
if aws_ ecr describe-images --repository-name "${REPOSITORY#*/}" --image-ids "imageTag=$TAG" > /dev/null 2>&1; then
  echo "Image $TAG already in ECR"
else
  docker build -t "$IMAGE" .
  docker push "$IMAGE"
fi

aws_ s3 cp deploy/ "$BUNDLE" --recursive --only-show-errors \
  --exclude "*" --include "docker-compose.prod.yml" --include "Caddyfile" --include "*.sh"

PARAMS=$(python3 -c 'import json, sys; print(json.dumps({"commands": sys.argv[1:], "executionTimeout": ["900"]}))' \
  "aws s3 cp $BUNDLE /opt/docflow/ --recursive --only-show-errors" \
  "AWS_REGION=$REGION SSM_PREFIX=$PREFIX bash /opt/docflow/remote-deploy.sh $IMAGE")
ID=$(aws_ ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
  --comment "manual deploy ${TAG::7}" --parameters "$PARAMS" --query Command.CommandId --output text)
for _ in $(seq 1 90); do
  STATUS=$(aws_ ssm get-command-invocation --command-id "$ID" --instance-id "$INSTANCE" \
    --query Status --output text 2>/dev/null || echo Pending)
  case "$STATUS" in Success|Failed|Cancelled|TimedOut) break ;; esac
  sleep 10
done
aws_ ssm get-command-invocation --command-id "$ID" --instance-id "$INSTANCE" \
  --query "[StandardOutputContent, StandardErrorContent]" --output text | grep -v "^\s*$" | tail -15
[[ "$STATUS" == "Success" ]] || { echo "Deploy finished with status $STATUS"; exit 1; }

for _ in $(seq 1 30); do
  curl -fsS "https://$HOST/ready" && { echo; echo "Live: https://$HOST"; exit 0; }
  sleep 5
done
echo "Deployed, but https://$HOST/ready did not answer"
exit 1
