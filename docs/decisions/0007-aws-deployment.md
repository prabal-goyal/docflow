# 0007: AWS deployment (M6)

Date: 2026-11-18. Status: accepted.

## What was built

- **Infrastructure as code** (`infra/`, Terraform, `ap-south-1`): one EC2 t3.small running the
  whole stack with Docker Compose, an Elastic IP, S3 (documents, backups, deploy bundles), ECR,
  a KMS key, SSM parameters, a CloudWatch log group, and a GitHub OIDC deploy role. 28 resources.
- **No standing credentials anywhere:** people use IAM Identity Center (temporary, MFA); the
  server uses its instance role (IMDSv2 only); GitHub Actions uses OIDC. No access keys exist.
- **No SSH:** port 22 is closed; shell access and deploys go through SSM.
- **Deploys** on every merge to `main` after all checks pass, health-checked, with one-command
  rollback (`deploy/rollback.sh`, drilled in both directions).
- **HTTPS** from Caddy with Let's Encrypt on a free `sslip.io` hostname; HTTP redirects to HTTPS.

## Production-grade

| Area | Measure |
|---|---|
| Least privilege | Instance role limited to its bucket, its SSM prefix, its KMS key, its ECR repository, its log group. Deploy role limited to pushing that repository, writing `deploy/*` and running a command on that one instance |
| Deploy identity | OIDC trust bound to the repository's immutable subject (owner and repository ids) and the `production` environment, which only accepts `main` |
| Secrets | SSM SecureString; rendered on the server into root-only files; never in git, images, Terraform state or logs. Webhook signing secrets encrypted with KMS (checked in the production database: stored as `kms:v1:...`) |
| Network | Only 80/443 open; Postgres, Redis and the API are reachable only inside Docker |
| Data | Encrypted EBS and S3, S3 public access blocked, TLS-only bucket policy, nightly backups with a tested restore |
| Startup guards | Production refuses to start without a KMS key or with private webhook targets allowed |
| Observability | JSON logs with request and task ids in CloudWatch |

## Deliberately simplified (and the upgrade path)

| Simplification | Risk | Upgrade |
|---|---|---|
| One instance runs everything | Instance failure means downtime until it is replaced | ECS Fargate behind an ALB, at least two tasks |
| Postgres on the instance, nightly dumps | Up to 24 h of data loss in a disaster; restore is manual | RDS with point-in-time recovery and Multi-AZ |
| **No alarms** | Errors and outages are noticed only by looking | CloudWatch alarms on `/ready`, ERROR log lines, disk and CPU, to email or Slack |
| `sslip.io` hostname | Depends on a free third-party DNS service | Own domain; ACM certificate on an ALB |
| Local Terraform state | One operator at a time; state lives on one machine | S3 backend with `use_lockfile` |
| No staging environment | Changes reach production after CI only | A second environment from the same Terraform (workspaces; proven by the drill below) |
| No WAF | Application-level abuse is handled only by rate limiting | AWS WAF on an ALB |
| Rollback swaps images, not schemas | A breaking migration cannot be rolled back by image alone | Expand/contract migrations (see the runbook) |

## Found in production, not in CI

1. **OIDC subject format.** A job that uses a GitHub *environment* gets an environment-based
   subject, and this repository uses *immutable* subjects with numeric ids. The trust policy now
   matches exactly; found by reading the repository's OIDC settings rather than guessing.
2. **IPv6 on an IPv4-only network.** Webhook deliveries to hosts with an IPv6 address failed.
   Fixed by connecting to the vetted IPv4 address with the real hostname as SNI and `Host`, which
   also closed the DNS-rebinding gap in decision 0005.
3. **A third-party test receiver (webhook.site) is unreachable from AWS addresses.** Not a
   DocFlow fault; the production delivery check used httpbin.org instead.

## Deployment drill

A second, complete environment (`drill`, `ap-southeast-1`) was built by following only the
deployment guide, verified (HTTPS, health, admin login, closed ports, logs, a deploy and a
rollback), and destroyed, with production's plan showing no changes before and after. The drill
found five problems, all fixed:

1. The guide and scripts assumed `/docflow/prod`; a non-production rollback would have read the
   wrong secrets. The server now records its SSM prefix at each deploy.
2. The account's GitHub OIDC provider is a singleton; a second environment now reuses it.
3. Nothing stopped one environment's variables being applied to another's state; a precondition
   now ties the Terraform workspace to the environment.
4. The scripts were not executable in git, so they failed from a fresh Linux clone.
5. Teardown left untagged child manifests in ECR (from multi-platform image indexes); the guide
   now deletes in passes until the repository is empty.

Environments other than production are deployed with `deploy/manual-deploy.sh`, which also
serves as the deploy path when GitHub Actions is unavailable.
