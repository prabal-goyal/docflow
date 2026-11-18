#!/usr/bin/env bash
# Runs ON the server (sent by CI through SSM Run Command) after the deploy bundle has been copied
# to /opt/docflow. Renders secrets, pulls the new image, runs migrations, restarts the stack and
# waits until every service is healthy. Records the previous image for rollback.
#
# Usage: remote-deploy.sh <image>          e.g. 1234.dkr.ecr.ap-south-1.amazonaws.com/docflow:<sha>
set -euo pipefail

IMAGE="${1:?usage: remote-deploy.sh <image>}"
export AWS_REGION="${AWS_REGION:?set AWS_REGION}"
cd /opt/docflow
# The prefix of the last deploy (recorded by render-env.sh), so rollbacks need not repeat it.
SSM_PREFIX="${SSM_PREFIX:-$(sed -n 's/^SSM_PREFIX=//p' .env 2>/dev/null || true)}"
SSM_PREFIX="${SSM_PREFIX:-/docflow/prod}"

bash render-env.sh "$SSM_PREFIX"

REGISTRY="${IMAGE%%/*}"
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$REGISTRY"

export DOCFLOW_IMAGE="$IMAGE"
COMPOSE=(docker compose -f docker-compose.prod.yml)
"${COMPOSE[@]}" pull --quiet
# --wait: return only when every service is healthy (or fail), so CI knows the deploy worked.
"${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 300

if [[ -f CURRENT_IMAGE && "$(cat CURRENT_IMAGE)" != "$IMAGE" ]]; then
  cp CURRENT_IMAGE PREVIOUS_IMAGE
fi
echo "$IMAGE" > CURRENT_IMAGE
docker image prune -f > /dev/null
echo "Deployed $IMAGE"
