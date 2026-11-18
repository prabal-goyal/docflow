import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from docflow.logs import correlation_id

logger = logging.getLogger("docflow.request")

REQUEST_ID_HEADER = "X-Request-ID"
# Accept a caller's id only if it is short and plain, so it cannot be used to inject into logs.
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Gives every request an id (the caller's X-Request-ID if safe, else a new one), returns it in
    the response, attaches it to every log record, and logs one line per request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if SAFE_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = correlation_id.set(request_id)
        started = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            # Path only, never the query string: it can carry tokens or personal data.
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            correlation_id.reset(token)
