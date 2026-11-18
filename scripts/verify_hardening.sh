#!/usr/bin/env bash
# Verifies the M6a production hardening against a running local stack (after smoke_test.sh):
# HTTPS through Caddy, the request size cap, security headers, JSON logs, login rate limiting with
# real Redis, and a backup restored into a separate database.
set -euo pipefail

HTTPS_URL="${HTTPS_URL:-https://localhost:8443}"
fail() { echo "FAILED: $*"; exit 1; }

echo "1. HTTPS through Caddy, with security headers"
HEADERS=$(curl -ksS -D - -o /dev/null "$HTTPS_URL/health")
echo "$HEADERS" | grep -qi "^HTTP/2 200" || fail "no HTTP/2 200 over HTTPS"
echo "$HEADERS" | grep -qi "^strict-transport-security:" || fail "missing HSTS"
echo "$HEADERS" | grep -qi "^x-content-type-options: nosniff" || fail "missing nosniff"
echo "$HEADERS" | grep -qi "^x-request-id:" || fail "missing X-Request-ID"
echo "$HEADERS" | grep -qi "^server:" && fail "Server header should be removed"
echo "   ok"

echo "2. Requests over 11 MB are refused by the proxy"
head -c 12000000 /dev/zero > /tmp/too-big.bin
STATUS=$(curl -ksS -o /dev/null -w "%{http_code}" -X POST "$HTTPS_URL/submissions" \
  -H "Content-Type: application/octet-stream" --data-binary @/tmp/too-big.bin)
rm -f /tmp/too-big.bin
[[ "$STATUS" == "413" ]] || fail "expected 413, got $STATUS"
echo "   ok (413)"

echo "3. API logs are JSON with a correlation id"
docker compose logs --no-log-prefix api 2>&1 | grep '"logger": "docflow.request"' | tail -1 \
  | python3 -c 'import json, sys; e = json.loads(sys.stdin.read()); assert e["correlation_id"] and e["path"]; print("   ok:", e["method"], e["path"], e["status"], str(e["duration_ms"]) + "ms")'

echo "4. Login rate limit with real Redis (10 failures, then 429)"
for _ in $(seq 1 10); do
  curl -ksS -o /dev/null -X POST "$HTTPS_URL/auth/login" -d "username=ratelimit@example.com&password=wrong-password-1"
done
STATUS=$(curl -ksS -o /dev/null -w "%{http_code}" -X POST "$HTTPS_URL/auth/login" \
  -d "username=ratelimit@example.com&password=wrong-password-1")
[[ "$STATUS" == "429" ]] || fail "expected 429, got $STATUS"
echo "   ok (429)"

echo "5. Backup, then restore into a separate database and compare"
KEY=$(docker compose exec -T worker python -m docflow.cli backup | sed -n 's/^Backup written to //p')
[[ -n "$KEY" ]] || fail "no backup key"
echo "   backup: $KEY"
docker compose exec -T db psql -U docflow -d docflow -qc "DROP DATABASE IF EXISTS restore_check" >/dev/null
docker compose exec -T db psql -U docflow -d docflow -qc "CREATE DATABASE restore_check" >/dev/null
docker compose exec -T worker python -m docflow.cli restore --key "$KEY" \
  --database-url postgresql+psycopg://docflow:docflow@db:5432/restore_check
COUNT_SQL="SELECT (SELECT count(*) FROM users) || '/' || (SELECT count(*) FROM submissions) || '/' || (SELECT count(*) FROM audit_events) || '/' || (SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'audit_events_%')"
LIVE=$(docker compose exec -T db psql -U docflow -d docflow -tAc "$COUNT_SQL")
RESTORED=$(docker compose exec -T db psql -U docflow -d restore_check -tAc "$COUNT_SQL")
echo "   users/submissions/audit_events/audit triggers: live=$LIVE restored=$RESTORED"
[[ "$LIVE" == "$RESTORED" ]] || fail "restored database differs"
docker compose exec -T db psql -U docflow -d docflow -qc "DROP DATABASE restore_check" >/dev/null

echo "6. Restore refuses to overwrite the live database"
if docker compose exec -T worker python -m docflow.cli restore --key "$KEY" \
  --database-url postgresql+psycopg://docflow:docflow@db:5432/docflow 2>/dev/null; then
  fail "restore over the live database was allowed"
fi
echo "   ok"

echo "Hardening checks passed"
