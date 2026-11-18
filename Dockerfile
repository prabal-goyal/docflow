# Build stage: install dependencies into a virtualenv with uv.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, so code changes don't invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md alembic.ini ./
COPY alembic ./alembic
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: only the virtualenv and source, running as a non-root user.
FROM python:3.13-slim
# pg_dump/pg_restore for database backups; must match the server's major version (17).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
# --proxy-headers: behind Caddy, use X-Forwarded-For for the client address (rate limiting).
# Which proxies are trusted comes from FORWARDED_ALLOW_IPS (default: only 127.0.0.1).
CMD ["uvicorn", "docflow.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
