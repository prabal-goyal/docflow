# DocFlow: Project Plan

AI document intake and approval API. Confirmed 2026-11-17.

## Customer story

An import compliance team receives commercial invoices from vendors claiming duty exemption on imported
equipment. Reviewers re-type fields by hand, and a single wrong number forces a full rejection and
re-submission. DocFlow extracts the fields with an LLM, checks them with deterministic rules, flags
anything suspicious for a human, runs staged approval with field-level queries, and notifies the
customer's system through signed webhooks.

All data is synthetic.

## Scope

| In scope | Out of scope |
|---|---|
| REST API (FastAPI); Swagger UI as the demo | Frontend UI (stretch only) |
| One document type: commercial invoice | Other document types |
| Single-tenant: one deployment per customer | Multi-tenant SaaS |
| Email/password login, JWT, roles | SSO, refresh tokens, password reset |
| Text and scanned PDFs up to 10 MB | Non-PDF formats |

## Roles

| Role | Can do |
|---|---|
| Submitter | Upload invoices, view own submissions, answer field queries |
| Reviewer (stage 1) | Review/edit fields, raise field queries, pass to next stage or reject |
| Approver (stage 2) | Same as reviewer; final approve or reject |
| Admin | Manage users and webhook endpoints, view audit log, retry failed extractions |

Approval stages are an ordered list in config (default: 2).

## Submission lifecycle

```
UPLOADED -> EXTRACTING -> EXTRACTION_FAILED (admin retry)
                       -> IN_REVIEW(stage n) -> QUERY_RAISED -> (submitter corrects) -> IN_REVIEW(same stage)
                                             -> IN_REVIEW(stage n+1) ... -> APPROVED | REJECTED
```

- All transitions go through one pure state-machine function.
- Every change is written to an append-only audit log.

## Extraction

1. PDF is sent to OpenAI (Responses API, structured outputs); output must match a Pydantic schema (invoice number/date, supplier, buyer,
   countries, currency, line items with description, HS code, quantity, unit price, amount; subtotal, total).
2. Every field includes a verbatim source quote.
3. Invalid output: retry once with the validation error; then `EXTRACTION_FAILED`.
4. Deterministic checks decide flags (model self-confidence is not used):
   - grounding: source quote appears in the PDF text
   - arithmetic: qty x unit price = amount; line items sum to total (with tolerance)
   - format: HS code 6-10 digits, ISO 4217 currency, date not in the future
   - completeness: required fields present
5. Each LLM call logs model, tokens, cost, latency, attempt.
6. Model choice is decided by the eval.

## Reliability

- `Idempotency-Key` on upload.
- Extraction and webhook delivery run in a background worker (Celery + Redis).
- Retries with exponential backoff on LLM 429/5xx.
- Webhooks: HMAC-SHA256 signature, event ID for dedupe, up to 5 retries with backoff, delivery status stored.
- Uploads: verify PDF magic bytes, enforce size limit, store SHA-256.

## Data model

`users`, `submissions` (status, stage, fields JSONB, idempotency key), `documents`, `extractions`
(one row per LLM attempt), `field_queries`, `stage_decisions`, `audit_events` (append-only),
`webhook_endpoints`, `webhook_deliveries`.

## API

```
POST   /auth/login
POST   /submissions                         (upload, Idempotency-Key)
GET    /submissions                         (filters, pagination)
GET    /submissions/{id}
PATCH  /submissions/{id}/fields
POST   /submissions/{id}/queries
POST   /submissions/{id}/decision
POST   /submissions/{id}/retry-extraction
GET    /submissions/{id}/audit
CRUD   /webhooks
GET    /health, /ready
```

## Evaluation

- Generator for 30 synthetic invoices with ground truth: 3 layouts, text and scanned-style, some with planted errors.
- One command writes `evals/results.md`: field-level accuracy (overall and per field), flag
  precision/recall, cost and latency per document, two-model comparison.

## Stack

Python 3.12+, FastAPI, Pydantic v2, uv, ruff, mypy (strict), pytest, PostgreSQL, SQLAlchemy 2, Alembic,
Celery + Redis, S3 (SeaweedFS locally; MinIO stopped publishing free images in 2025),
OpenAI API (starting model `gpt-6.1-sol`; final choice decided by the M5 eval), Docker + docker compose,
GitHub Actions.

## Deployment (AWS)

- CI builds images and pushes to ECR.
- One EC2 instance runs docker compose (API, worker, Redis, Postgres) behind Caddy (HTTPS).
- Files in S3, accessed through an IAM instance role (no stored keys).
- Secrets in SSM Parameter Store; logs in CloudWatch; AWS budget alert.
- ECS Fargate + RDS documented as the production upgrade path, not built.
- **Second deployment from scratch using only the deployment guide**, to test the handover docs.

## Handover docs

`README.md`, `docs/deployment-guide.md`, `docs/integration-guide.md` (incl. webhook signature
verification sample), `docs/runbook.md`, `docs/decisions/`.

## Milestones (about 3 weeks at 3-4 h/day)

| # | Milestone | Days | Done when |
|---|---|---|---|
| M0 | Setup | 1 | `/health` works locally and in Docker; lint, types, tests, CI green |
| M1 | Data and auth | 2-4 | Migrations run; login works; role checks tested |
| M2 | Upload and extraction | 4-7 | PDF upload gives validated, flagged fields with logged cost/latency |
| M3 | Workflow | 8-10 | Approve/reject/query loop works and is audited; state machine fully tested |
| M4 | Webhooks | 11-12 | Mock receiver gets signed events; retries work when it is down |
| M5 | Eval | 12-14 | `evals/results.md` with accuracy, flag precision/recall, 2-model comparison |
| M6 | AWS deploy | 15-18 | Live on HTTPS; merge to main auto-deploys; second clean deploy from the guide |
| M7 | Docs and demo | 19-21 | Handover docs, 3-minute demo video, resume bullet |
| M8 | MCP server | 22-24 | An agent in Claude Code lists, reads, uploads and queries invoices through the API; tests and CI green |
| Stretch | Small review UI | after | Only when M0-M8 are done |

Infrastructure is added in the milestone that first uses it (Postgres in M1, Redis/SeaweedFS in M2).
M2 is split into M2a (upload, storage, worker plumbing with a fake extractor) and M2b (OpenAI
extraction and checks).

## M8: MCP server

Lets an AI agent (Claude Code, Claude Desktop, Cursor) work with DocFlow through MCP tools, for
example "which invoices are flagged this week, and why?".

**Shape.** A separate package, `src/docflow_mcp/`, in this repo. It is a client of DocFlow's HTTP
API and never touches the database, so login, role checks, rate limits and the audit log apply to
every agent action exactly as they do to a person. Same repo because it changes together with the
API it calls and shares CI; separate package and optional dependency group so the API's Docker
image does not ship the MCP SDK. SDK: the official `mcp` package, v2 (`MCPServer`), pinned
`>=2,<3`.

**Tools (v1).**

| Tool | API call | Kind |
|---|---|---|
| `list_submissions(status?, limit?)` | `GET /submissions` | read |
| `get_submission(id)` | `GET /submissions/{id}` (fields, source quotes, flags) | read |
| `get_audit_log(id)` | `GET /submissions/{id}/audit` | read |
| `upload_invoice(file_path)` | `POST /submissions` | write |
| `raise_query(id, field, question)` | `POST /submissions/{id}/queries` | write |
| `record_decision(id, decision, reason)` | `POST /submissions/{id}/decision` | write, destructive |

**Decisions (record in 0008).**

- Transport: stdio only in v1 (runs on the user's machine next to the agent). Remote HTTP needs
  OAuth and is out of scope.
- Auth: the server logs in as a DocFlow user from environment variables. Use a dedicated
  least-privilege user, never the admin. Tokens never appear in tool output or logs.
- `record_decision` is marked destructive so clients ask the human first; the API still decides
  who may approve.
- Invoice text is untrusted: tool results label document content as data, and no tool acts on
  instructions found in a document.
- Errors: HTTP 401/403/404/409/422/429 become short messages that tell the model what to do next;
  anything else is logged to stderr and returned as a generic failure.

**Done when.** Each tool has unit tests against a mocked HTTP layer; one end-to-end test runs
against the local Docker stack; CI green; README section, decision 0008, a short demo recording,
and a resume bullet.

## Testing

Unit tests for the state machine, validation rules and webhook signing. Integration tests against a
real Postgres. A fake LLM client in tests, so CI never calls the real API.

## Prerequisites

1. Docker Desktop (WSL2)
2. OpenAI API key in a dedicated project with a budget limit (M2b)
3. AWS account with a budget alert (M6)
4. Public GitHub repo `docflow`

## Risks

- Timeline is tight while learning Python, Docker and AWS. Cut order: stretch, second eval model, scanned PDFs.
- AWS cost: one small instance, budget alert, tear down after demo if needed.
- Every line must be explainable in interviews: build step by step with explanations.
