import json
import logging

import pytest
from fastapi.testclient import TestClient

from docflow.logs import JsonFormatter, correlation_id


def format_record(message: str, **extra: object) -> dict[str, object]:
    record = logging.LogRecord("docflow.test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    entry: dict[str, object] = json.loads(JsonFormatter().format(record))
    return entry


def test_log_lines_are_json_with_extra_fields() -> None:
    entry = format_record("uploaded", submission_id="abc", size_bytes=10)

    assert entry["message"] == "uploaded"
    assert entry["level"] == "INFO"
    assert entry["submission_id"] == "abc"
    assert entry["size_bytes"] == 10


def test_log_lines_carry_the_current_correlation_id() -> None:
    token = correlation_id.set("req-123")
    try:
        assert format_record("x")["correlation_id"] == "req-123"
    finally:
        correlation_id.reset(token)
    assert "correlation_id" not in format_record("x")


def test_each_request_gets_an_id_in_the_response(client: TestClient) -> None:
    response = client.get("/health")

    assert len(response.headers["X-Request-ID"]) == 32


def test_a_safe_caller_request_id_is_reused(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-abc.123"})

    assert response.headers["X-Request-ID"] == "trace-abc.123"


@pytest.mark.parametrize("unsafe", ["x" * 65, "bad id with spaces", "evil\nINFO fake log line"])
def test_unsafe_caller_request_ids_are_replaced(client: TestClient, unsafe: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": unsafe.replace("\n", "%0A")})

    assert response.headers["X-Request-ID"] != unsafe
    assert len(response.headers["X-Request-ID"]) == 32


def test_requests_are_logged_without_query_strings(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="docflow.request"):
        client.get("/health?token=secret")

    [record] = [r for r in caplog.records if r.name == "docflow.request"]
    assert record.__dict__["path"] == "/health"
    assert record.__dict__["status"] == 200
    assert "secret" not in record.getMessage() + str(record.__dict__)
