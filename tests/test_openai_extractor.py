"""OpenAIExtractor against a fake client. Responses and errors are the SDK's real types, so the
mapping is tested against what the SDK actually raises and returns. No network calls."""

from typing import Any

import httpx2
import openai
import pytest
from openai.types.responses import ParsedResponse

from docflow.config import get_settings
from docflow.extraction import PermanentExtractionError, RetryableExtractionError
from docflow.invoice import InvoiceExtraction
from docflow.openai_extractor import OpenAIExtractor, get_extractor
from tests.invoices import make_invoice

REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


class FakeResponses:
    def __init__(self, result: ParsedResponse[InvoiceExtraction] | Exception) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> ParsedResponse[InvoiceExtraction]:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: ParsedResponse[InvoiceExtraction] | Exception) -> None:
        self.responses = FakeResponses(result)


def parsed_response(
    content: list[dict[str, Any]],
    status: str = "completed",
    incomplete_reason: str | None = None,
) -> ParsedResponse[InvoiceExtraction]:
    return ParsedResponse[InvoiceExtraction].model_validate(
        {
            "id": "resp_123",
            "object": "response",
            "created_at": 0,
            "model": "gpt-6-luna",
            "status": status,
            "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": content,
                }
            ],
            "usage": {
                "input_tokens": 3000,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 900,
                "output_tokens_details": {"reasoning_tokens": 500},
                "total_tokens": 3900,
            },
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
        }
    )


def text_part(invoice: InvoiceExtraction) -> dict[str, Any]:
    return {
        "type": "output_text",
        "text": invoice.model_dump_json(),
        "annotations": [],
        "parsed": invoice.model_dump(),
    }


def status_error(
    error_class: type[openai.APIStatusError], status: int, body: dict[str, Any]
) -> openai.APIStatusError:
    response = httpx2.Response(status, request=REQUEST, json={"error": body})
    return error_class(body.get("message", "error"), response=response, body=body)


def extract_with(result: ParsedResponse[InvoiceExtraction] | Exception) -> tuple[Any, FakeClient]:
    client = FakeClient(result)
    extractor = OpenAIExtractor(client, get_settings())  # type: ignore[arg-type]  # duck-typed fake
    return extractor.extract(b"%PDF-1.7 fake"), client


# --- success -----------------------------------------------------------------------------------


def test_success_returns_invoice_and_usage() -> None:
    invoice = make_invoice()

    result, _ = extract_with(parsed_response([text_part(invoice)]))

    assert result.invoice == invoice
    assert result.usage.model == "gpt-6-luna"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (3000, 900)
    assert result.usage.reasoning_tokens == 500
    assert result.usage.response_id == "resp_123"


def test_request_sends_pdf_schema_effort_and_disables_storage() -> None:
    _, client = extract_with(parsed_response([text_part(make_invoice())]))
    kwargs = client.responses.kwargs

    file_part = kwargs["input"][0]["content"][0]
    assert file_part["type"] == "input_file"
    assert file_part["file_data"].startswith("data:application/pdf;base64,")
    assert kwargs["text_format"] is InvoiceExtraction
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["reasoning"] == {"effort": "medium"}
    assert kwargs["store"] is False
    assert "Copy, never correct" in kwargs["instructions"]


# --- model-level failures (billed, so usage is attached) ---------------------------------------


def test_refusal_is_permanent_and_keeps_usage() -> None:
    response = parsed_response([{"type": "refusal", "refusal": "I can't help with that."}])

    with pytest.raises(PermanentExtractionError, match="refused") as caught:
        extract_with(response)

    assert caught.value.usage is not None
    assert caught.value.usage.output_tokens == 900


def test_incomplete_response_is_permanent_and_keeps_usage() -> None:
    response = parsed_response([], status="incomplete", incomplete_reason="max_output_tokens")

    with pytest.raises(PermanentExtractionError, match="max_output_tokens") as caught:
        extract_with(response)

    assert caught.value.usage is not None


# --- API errors --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        status_error(
            openai.RateLimitError,
            429,
            {"message": "Rate limit reached", "type": "rate_limit_error"},
        ),
        status_error(openai.InternalServerError, 500, {"message": "Server error"}),
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
    ],
)
def test_transient_api_errors_are_retryable(error: Exception) -> None:
    with pytest.raises(RetryableExtractionError):
        extract_with(error)


@pytest.mark.parametrize(
    "body",
    [
        {
            "message": "Out of credits",
            "code": "credit_balance_exhausted",
            "type": "insufficient_quota",
        },
        {"message": "Spend limit", "code": "project_spend_limit_exceeded", "type": "requests"},
        {"message": "Quota", "code": None, "type": "insufficient_quota"},
    ],
)
def test_billing_429s_are_permanent_and_point_to_billing(body: dict[str, Any]) -> None:
    with pytest.raises(PermanentExtractionError, match="billing or quota"):
        extract_with(status_error(openai.RateLimitError, 429, body))


@pytest.mark.parametrize(
    ("error_class", "status"),
    [
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.BadRequestError, 400),
        (openai.NotFoundError, 404),
        (openai.UnprocessableEntityError, 422),
    ],
)
def test_client_errors_are_permanent(error_class: type[openai.APIStatusError], status: int) -> None:
    with pytest.raises(PermanentExtractionError, match=str(status)):
        extract_with(status_error(error_class, status, {"message": "nope"}))


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, ""])
def test_missing_api_key_fails_clearly(monkeypatch: pytest.MonkeyPatch, key: str | None) -> None:
    from pydantic import SecretStr

    settings = get_settings().model_copy(
        update={"openai_api_key": None if key is None else SecretStr(key)}
    )
    monkeypatch.setattr("docflow.openai_extractor.get_settings", lambda: settings)

    with pytest.raises(PermanentExtractionError, match="OPENAI_API_KEY is not set"):
        get_extractor()
