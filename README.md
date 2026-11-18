# DocFlow

AI document intake and approval API. Upload a commercial invoice; an LLM extracts the fields,
deterministic checks flag anything suspicious, reviewers approve in stages with field-level queries,
and the customer's system is notified through signed webhooks.

Status: **M2 (upload and AI extraction)** done; review workflow (M3) next. See [docs/PLAN.md](docs/PLAN.md) for the full plan.

## Run with Docker

```bash
cp .env.example .env
# set DOCFLOW_JWT_SECRET in .env:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up --build
```

| Service | Role |
|---|---|
| `db` | Postgres |
| `redis` | Celery broker |
| `storage` | S3-compatible object storage (SeaweedFS) for uploaded PDFs |
| `migrate` | Runs migrations and creates the bucket once, then exits |
| `api` | FastAPI on http://localhost:8000 |
| `worker` | Celery worker that processes uploads |

The API and worker start only after `migrate` succeeds.

The worker reads `OPENAI_API_KEY` from `.env`. Without it, extraction fails with a clear reason.

`scripts/smoke_test.sh` runs an end-to-end check against a fresh stack (CI runs it too). It expects
no API key, so start the stack with `OPENAI_API_KEY= docker compose up -d --build` when running it
locally.

Create the first admin (prompts for a password):

```bash
docker compose run --rm api python -m docflow.cli create-admin --email admin@example.com
```

Open http://localhost:8000/docs, click **Authorize**, and log in with the admin's email as the
username.

## Develop locally

Python tooling runs in Linux (WSL on Windows). Native Windows is not supported: Windows
Application Control blocks the compiled extensions that SQLAlchemy and other libraries ship.
On WSL, keep the virtualenv on the Linux filesystem: reading thousands of package files through
`/mnt/<drive>` is slow enough to stall mypy.

```bash
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/docflow
docker compose up -d db redis storage       # Postgres (incl. docflow_test), Redis, object storage
uv sync
uv run alembic upgrade head
uv run uvicorn docflow.main:app --reload
```

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests alembic
uv run alembic check          # models and migrations agree
uv run pytest                 # needs the db service running; wipes only docflow_test
```

## Authentication and roles

| Role | Purpose |
|---|---|
| `submitter` | Uploads invoices, answers field queries |
| `reviewer` | Stage 1 review |
| `approver` | Stage 2, final decision |
| `admin` | Manages users |

- `POST /auth/login` takes an OAuth2 password form (`username` = email) and returns a 30-minute JWT.
- The token holds only the user id. Role and active status are read from the database on every
  request, so role changes and deactivations apply immediately.
- Admin-created users get a temporary password and can only call `/auth/me` and
  `/auth/change-password` until they change it.

See [docs/decisions/0001-production-readiness-auth.md](docs/decisions/0001-production-readiness-auth.md)
for what is production-grade and what is deliberately left out.

## Submissions

- `POST /submissions` (submitter, admin): multipart upload of one PDF, up to 10 MB. Returns
  `202` with status `uploaded`; extraction runs in the background. An optional
  `Idempotency-Key` header makes retries safe.
- `GET /submissions/{id}`: submitters see their own; reviewers, approvers and admins see all.

- `GET /submissions/{id}/attempts` (admin): every LLM call with tokens, estimated cost and latency.

Lifecycle so far: `uploaded -> extracting -> in_review (stage 1)` or `extraction_failed` with a
`failure_reason`. See [docs/decisions/0002-background-extraction.md](docs/decisions/0002-background-extraction.md)
for how duplicate deliveries, crashed workers and retries are handled.

## AI extraction

The worker sends the PDF to OpenAI (`gpt-6-luna` by default, `DOCFLOW_LLM_MODEL`) and gets back an
`InvoiceExtraction`: every field has a normalised `value` and the exact `source_quote` it came
from. Deterministic checks then add `flags` for a reviewer:

| Code | Meaning |
|---|---|
| `not_grounded` | The quoted text is not in the PDF, so the value may be invented |
| `grounding_unverifiable` | Scanned PDF without a text layer |
| `arithmetic_mismatch` | Line amount, subtotal or total does not add up |
| `invalid_format` | HS code, currency or date is invalid |
| `unparseable_number` | An amount is not a number |
| `missing_required` | A required field is missing |

See [docs/decisions/0003-llm-extraction.md](docs/decisions/0003-llm-extraction.md).

### Sample invoices and live test

`samples/` holds three synthetic invoices with their expected values and flags
(`uv run python scripts/make_sample_invoices.py` regenerates them). With a real key in `.env` and
a stack on which `scripts/smoke_test.sh` has run:

```bash
uv run python scripts/live_test.py   # calls OpenAI: costs real money (fractions of a cent per invoice on gpt-6-luna)
```
