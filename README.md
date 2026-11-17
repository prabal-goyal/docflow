# DocFlow

AI document intake and approval API. Upload a commercial invoice; an LLM extracts the fields,
deterministic checks flag anything suspicious, reviewers approve in stages with field-level queries,
and the customer's system is notified through signed webhooks.

Status: **M0 (setup)**. See [docs/PLAN.md](docs/PLAN.md) for the full plan.

## Run locally

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m uvicorn docflow.main:app --reload
# open http://localhost:8000/docs
```

## Run with Docker

```bash
docker compose up --build
# open http://localhost:8000/health
```

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m mypy src tests
uv run python -m pytest
```
