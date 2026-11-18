"""Structured JSON logs on stdout, one object per line, so CloudWatch (or any log tool) can search
and filter by field. Every record carries the current request or task id when there is one."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Set per HTTP request (middleware) or per Celery task (signal), read by every log record.
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Attributes every LogRecord has; anything else was passed with extra={...} and is logged as is.
STANDARD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (cid := correlation_id.get()) is not None:
            entry["correlation_id"] = cid
        for key, value in vars(record).items():
            if key not in STANDARD_ATTRIBUTES and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Replaces any existing handlers with one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Our middleware logs each request; uvicorn's own access log would duplicate it.
    logging.getLogger("uvicorn.access").disabled = True
    # One INFO line per outgoing HTTP call (OpenAI, webhooks) is noise; keep warnings and errors.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "celery"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
