# Runbook: operating DocFlow in production

Commands run from a workstation with the `docflow` AWS profile (`aws sso login --profile docflow`)
unless they say "on the server". For an environment other than production, export `AWS_REGION`
with its region and pass its SSM prefix where a script takes one. The server has no SSH; open a shell with Session Manager:

```bash
aws ssm start-session --profile docflow --target <instance-id>    # needs the Session Manager plugin
sudo -i && cd /opt/docflow
export DOCFLOW_IMAGE=$(cat CURRENT_IMAGE) AWS_REGION=$(sed -n 's/^AWS_REGION=//p' .env)
alias dc='docker compose -f docker-compose.prod.yml'
```

## Deploy

Merge to `main`. CI deploys after all checks pass and verifies `/ready` over HTTPS. If the
deploy job fails, read its output (it prints the server's output), fix forward with a new commit,
or roll back (below).

Without GitHub Actions: `deploy/manual-deploy.sh` deploys the checked-out commit the same way.

## Roll back

```bash
deploy/rollback.sh <instance-id>         # switches to the image in PREVIOUS_IMAGE; run again to undo
```

**Migrations and rollback:** rollback swaps the application image, not the database schema. It
is safe when the newer release added no migration, or only backward-compatible ones (new nullable
columns, new tables). Write migrations in two steps (*expand*: add, deploy, backfill; *contract*:
remove the old structure in a later release) so the previous image always works with the
current schema. A breaking migration needs `alembic downgrade` on the server before rolling back.

## Read logs

CloudWatch Logs group `/docflow`, one stream per container (`docflow-api-1`, `docflow-worker-1`,
...). Every line is JSON; API lines carry a `correlation_id` that is also returned to clients as
`X-Request-ID`, and worker lines carry the Celery task id.

```bash
aws logs tail /docflow --profile docflow --follow --log-stream-names docflow-api-1
aws logs filter-log-events --profile docflow --log-group-name /docflow \
  --filter-pattern '{ $.level = "ERROR" }' --max-items 20
aws logs filter-log-events --profile docflow --log-group-name /docflow \
  --filter-pattern '{ $.correlation_id = "<id from X-Request-ID>" }'
```

## Back up and restore

Backups run nightly at 02:30 UTC to `s3://<bucket>/backups/YYYY/MM/DD/` and expire after 14 days.

```bash
# on the server: take one now
dc exec -T worker python -m docflow.cli backup
# list them
aws s3 ls s3://<bucket>/backups/ --recursive --profile docflow | tail
```

**Restore drill** (do this regularly; an untested backup is not a backup), on the server:

```bash
dc exec -T db psql -U docflow -d docflow -c "CREATE DATABASE restore_check"
dc exec -T worker python -m docflow.cli restore --key backups/... \
  --database-url "postgresql+psycopg://docflow:$(cat postgres_password)@db:5432/restore_check"
# compare counts, then: DROP DATABASE restore_check
```

**Real restore** (data loss or corruption): stop `api`, `worker` and `scheduler`, restore into
`docflow` with `--i-mean-production`, start them again. Data written after the backup is lost
(recovery point: up to 24 hours).

## Users

- First admin: `deploy/create-admin.sh` (see the deployment guide).
- Everyone else: `POST /users` as an admin. New users must change their temporary password.
- Deactivate instead of deleting (keeps the audit trail): set `is_active` in the database or
  create a deactivation endpoint when needed.
- Locked out by the login rate limit: wait 15 minutes, or on the server
  `dc exec redis redis-cli --scan --pattern 'login-failures:*'` and delete the key.

## Rotate secrets

| Secret | How | Effect |
|---|---|---|
| OpenAI key | `aws ssm put-parameter --overwrite ...openai-api-key`, then redeploy | None for users |
| JWT secret | `put-parameter --overwrite ...jwt-secret`, redeploy | Every user must log in again |
| Webhook signing secret | `POST /webhooks/{id}/rotate-secret` | The receiver must switch to the new secret |
| Database password | Change it in Postgres (`ALTER USER`) and in SSM together, then redeploy | Brief restart |

Rendering happens on every deploy (`render-env.sh`), so a redeploy is how new values reach the
containers: re-run the latest workflow, or `deploy/rollback.sh` twice.

## Common problems

| Symptom | Check | Fix |
|---|---|---|
| Submissions stuck in `uploaded` | worker and redis containers running; worker logs | After fixing, `POST /submissions/{id}/retry-extraction` (admin; failed or stuck > 10 min) |
| `extraction_failed: OpenAI billing or quota limit reached` | OpenAI project credits and spend limit | Raise the limit, then retry the submission |
| Webhook deliveries `pending` or `failed` | `GET /webhooks/{id}/deliveries` (`last_error`) | Receiver must be public HTTPS with an IPv4 address and answer 2xx within 10 s; then redeliver |
| Certificate errors | `caddy` logs | Ports 80 and 443 must be open; Let's Encrypt rate limits apply if certificates are re-issued often (keep the `caddy_data` volume) |
| Out of memory or disk | `free -m`, `df -h`, `docker system df` on the server | `docker image prune -a`; move to a larger instance type in Terraform |

## Costs

About USD 0.80 per day. The budget alert emails at the threshold set in AWS Budgets. To stop all
charges, follow *Tear down* in the deployment guide.
