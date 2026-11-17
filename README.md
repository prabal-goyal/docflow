# DocFlow

AI document intake and approval API. Upload a commercial invoice; an LLM extracts the fields,
deterministic checks flag anything suspicious, reviewers approve in stages with field-level queries,
and the customer's system is notified through signed webhooks.

Status: **M1 (data and auth)** done. See [docs/PLAN.md](docs/PLAN.md) for the full plan.

## Run with Docker

```bash
cp .env.example .env
# set DOCFLOW_JWT_SECRET in .env:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up --build
```

Compose starts Postgres, runs migrations once in the `migrate` service, and only then starts the API.

Create the first admin (prompts for a password):

```bash
docker compose run --rm api python -m docflow.cli create-admin --email admin@example.com
```

Open http://localhost:8000/docs, click **Authorize**, and log in with the admin's email as the
username.

## Develop locally

Python tooling runs in Linux (WSL on Windows). Native Windows is not supported: Windows
Application Control blocks the compiled extensions that SQLAlchemy and other libraries ship.

```bash
export UV_PROJECT_ENVIRONMENT=.venv-linux   # keeps the Linux venv separate from any Windows one
docker compose up -d db                     # Postgres, including the docflow_test database
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
