#!/usr/bin/env bash
# End-to-end check against a running stack (docker compose up): creates users, uploads a PDF
# and waits for the worker to finish with it. Expects a fresh database.
#
# Usage: scripts/smoke_test.sh [expected_status] [expected_reason_substring]
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
EXPECTED_STATUS="${1:-extraction_failed}"
EXPECTED_REASON="${2:-OPENAI_API_KEY is not set}"
ADMIN_EMAIL="smoke-admin@example.com"
ADMIN_PASSWORD="smoke-admin-password-1"
SUBMITTER_EMAIL="smoke-submitter@example.com"
TEMP_PASSWORD="smoke-temporary-pass-1"
SUBMITTER_PASSWORD="smoke-submitter-pass-1"
PDF="tests/fixtures/blank-1-page.pdf"

json() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
login() { curl -fsS -X POST "$BASE_URL/auth/login" -d "username=$1&password=$2" | json "['access_token']"; }

echo "1. Create admin"
docker compose run --rm -T -e DOCFLOW_ADMIN_PASSWORD="$ADMIN_PASSWORD" api \
  python -m docflow.cli create-admin --email "$ADMIN_EMAIL"
ADMIN_TOKEN=$(login "$ADMIN_EMAIL" "$ADMIN_PASSWORD")

echo "2. Admin creates a submitter, who sets their own password"
curl -fsS -o /dev/null -X POST "$BASE_URL/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d "{\"email\":\"$SUBMITTER_EMAIL\",\"password\":\"$TEMP_PASSWORD\",\"role\":\"submitter\"}"
TEMP_TOKEN=$(login "$SUBMITTER_EMAIL" "$TEMP_PASSWORD")
curl -fsS -X POST "$BASE_URL/auth/change-password" \
  -H "Authorization: Bearer $TEMP_TOKEN" -H "Content-Type: application/json" \
  -d "{\"current_password\":\"$TEMP_PASSWORD\",\"new_password\":\"$SUBMITTER_PASSWORD\"}"
TOKEN=$(login "$SUBMITTER_EMAIL" "$SUBMITTER_PASSWORD")

echo "3. Upload a PDF"
SUBMISSION_ID=$(curl -fsS -X POST "$BASE_URL/submissions" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: smoke-1" \
  -F "file=@$PDF;type=application/pdf" | json "['id']")
echo "   submission $SUBMISSION_ID"

echo "4. Wait for the worker"
for _ in $(seq 1 30); do
  BODY=$(curl -fsS "$BASE_URL/submissions/$SUBMISSION_ID" -H "Authorization: Bearer $TOKEN")
  STATUS=$(echo "$BODY" | json "['status']")
  if [[ "$STATUS" != "uploaded" && "$STATUS" != "extracting" ]]; then break; fi
  sleep 1
done

REASON=$(echo "$BODY" | json "['failure_reason']")
PAGES=$(echo "$BODY" | json "['document']['page_count']")
echo "   status=$STATUS pages=$PAGES reason=$REASON"

if [[ "$STATUS" != "$EXPECTED_STATUS" || "$REASON" != *"$EXPECTED_REASON"* || "$PAGES" != "1" ]]; then
  echo "FAILED: expected status=$EXPECTED_STATUS, reason containing '$EXPECTED_REASON', pages=1"
  exit 1
fi
echo "Smoke test passed"
