"""MCP server tests: a fake DocFlow API behind httpx2.MockTransport, driven through the MCP SDK's
in-memory client, so tool schemas, results and errors are checked as an agent would see them."""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent
from pydantic import SecretStr

from docflow_mcp.client import DocFlowClient
from docflow_mcp.server import UNTRUSTED_NOTICE, build_server

PASSWORD = "agent-password-123"
SUBMISSION_ID = uuid.UUID("0b8a4f3e-5d7c-4b9a-9e1f-2c3d4e5f6a7b")


@dataclass
class FakeDocFlow:
    """Answers like the DocFlow API and records what the MCP server sent."""

    logins: int = 0
    valid_tokens: set[str] = field(default_factory=set)
    requests: list[httpx2.Request] = field(default_factory=list)
    # Optional override: (method, path) -> response, for error cases.
    responses: dict[tuple[str, str], httpx2.Response] = field(default_factory=dict)
    login_response: httpx2.Response | None = None

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/auth/login":
            return self.login(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return httpx2.Response(401, json={"detail": "Not authenticated"})
        override = self.responses.get((request.method, request.url.path))
        if override is not None:
            return override
        if request.method == "GET" and request.url.path == "/submissions":
            return httpx2.Response(200, json=[{"id": str(SUBMISSION_ID), "status": "in_review"}])
        if request.method == "GET" and request.url.path == f"/submissions/{SUBMISSION_ID}":
            return httpx2.Response(
                200,
                json={
                    "id": str(SUBMISSION_ID),
                    "status": "in_review",
                    "fields": {"total": {"value": "100.00", "quote": "Total: 100.00"}},
                    "flags": [{"code": "TOTAL_MISMATCH", "message": "Lines do not add up"}],
                },
            )
        if request.method == "GET" and request.url.path == f"/submissions/{SUBMISSION_ID}/audit":
            return httpx2.Response(200, json=[{"action": "uploaded", "stage": None}])
        if request.method == "POST" and request.url.path == "/submissions":
            return httpx2.Response(202, json={"id": str(SUBMISSION_ID), "status": "uploaded"})
        if request.method == "POST" and request.url.path.startswith(
            f"/submissions/{SUBMISSION_ID}/"
        ):
            return httpx2.Response(200, json={"id": str(SUBMISSION_ID), "status": "query_raised"})
        return httpx2.Response(404, json={"detail": "Not found"})

    def login(self, request: httpx2.Request) -> httpx2.Response:
        self.logins += 1
        if self.login_response is not None:
            return self.login_response
        form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        if form.get("password") != PASSWORD:
            return httpx2.Response(401, json={"detail": "Invalid email or password"})
        token = f"token-{self.logins}"
        self.valid_tokens.add(token)
        return httpx2.Response(
            200, json={"access_token": token, "token_type": "bearer", "must_change_password": False}
        )

    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    def api_requests(self) -> list[httpx2.Request]:
        return [r for r in self.requests if r.url.path != "/auth/login"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_api() -> FakeDocFlow:
    return FakeDocFlow()


@pytest.fixture
async def agent(fake_api: FakeDocFlow) -> AsyncIterator[Client]:
    async with connect(fake_api) as client:
        yield client


def connect(fake_api: FakeDocFlow, password: str = PASSWORD, **options: Any) -> Client:
    docflow = DocFlowClient(
        "http://docflow.test",
        "agent@example.com",
        SecretStr(password),
        transport=httpx2.MockTransport(fake_api.handle),
    )
    return Client(build_server(docflow, **options))


def text_of(result: CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


pytestmark = pytest.mark.anyio


async def tool_map(agent: Client) -> dict[str, Any]:
    return {tool.name: tool for tool in (await agent.list_tools()).tools}


async def test_by_default_uploads_and_decisions_are_off(agent: Client) -> None:
    tools = await tool_map(agent)

    assert set(tools) == {"list_submissions", "get_submission", "get_audit_log", "raise_query"}
    for name in ("list_submissions", "get_submission", "get_audit_log"):
        assert tools[name].annotations.read_only_hint is True
    assert tools["raise_query"].annotations.read_only_hint is False
    assert tools["raise_query"].annotations.destructive_hint is False


async def test_options_add_upload_and_decision_tools(fake_api: FakeDocFlow, tmp_path: Path) -> None:
    async with connect(fake_api, upload_dir=tmp_path, allow_decisions=True) as agent:
        tools = await tool_map(agent)

    assert {"list_invoice_files", "upload_invoice", "record_decision"} <= set(tools)
    assert tools["upload_invoice"].annotations.idempotent_hint is True
    assert tools["record_decision"].annotations.destructive_hint is True


async def test_list_submissions_sends_filters_with_the_token(
    agent: Client, fake_api: FakeDocFlow
) -> None:
    result = await agent.call_tool(
        "list_submissions", {"status": "in_review", "awaiting_me": True, "limit": 5}
    )

    assert not result.is_error
    assert str(SUBMISSION_ID) in text_of(result)
    (request,) = fake_api.api_requests()
    assert request.url.params["status"] == "in_review"
    assert request.url.params["awaiting_me"] == "true"
    assert request.url.params["limit"] == "5"
    assert request.headers["Authorization"] == "Bearer token-1"


async def test_logs_in_once_for_several_calls(agent: Client, fake_api: FakeDocFlow) -> None:
    await agent.call_tool("list_submissions", {})
    await agent.call_tool("list_submissions", {})

    assert fake_api.logins == 1


async def test_get_submission_labels_document_content_as_untrusted(agent: Client) -> None:
    result = await agent.call_tool("get_submission", {"submission_id": str(SUBMISSION_ID)})

    assert not result.is_error
    body = result.structured_content
    assert body is not None
    assert body["notice"] == UNTRUSTED_NOTICE
    assert body["submission"]["flags"][0]["code"] == "TOTAL_MISMATCH"
    assert body["submission"]["fields"]["total"]["quote"] == "Total: 100.00"


async def test_expired_token_logs_in_again_and_retries_once(
    agent: Client, fake_api: FakeDocFlow
) -> None:
    await agent.call_tool("list_submissions", {})
    fake_api.expire_tokens()

    result = await agent.call_tool("list_submissions", {})

    assert not result.is_error
    assert fake_api.logins == 2
    assert len(fake_api.api_requests()) == 3  # first call, rejected retry, successful retry


async def test_wrong_credentials_give_a_clear_error_without_the_password(
    fake_api: FakeDocFlow,
) -> None:
    async with connect(fake_api, password="wrong-password-000") as agent:
        result = await agent.call_tool("list_submissions", {})

    assert result.is_error
    assert "DOCFLOW_MCP_EMAIL" in text_of(result)
    assert "wrong-password-000" not in text_of(result)
    assert fake_api.api_requests() == []


async def test_account_that_must_change_password_is_refused(fake_api: FakeDocFlow) -> None:
    fake_api.login_response = httpx2.Response(
        200, json={"access_token": "t", "token_type": "bearer", "must_change_password": True}
    )
    async with connect(fake_api) as agent:
        result = await agent.call_tool("list_submissions", {})

    assert result.is_error
    assert "change its password" in text_of(result)


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (403, {}, "not allowed to do this"),
        (404, {}, "Not found"),
        (429, {"Retry-After": "30"}, "Wait 30 seconds"),
        (500, {}, "unexpected error (HTTP 500)"),
    ],
)
async def test_api_errors_become_instructions_for_the_model(
    agent: Client,
    fake_api: FakeDocFlow,
    status: int,
    headers: dict[str, str],
    expected: str,
) -> None:
    fake_api.responses[("GET", f"/submissions/{SUBMISSION_ID}")] = httpx2.Response(
        status, headers=headers, json={"detail": "internal detail"}
    )

    result = await agent.call_tool("get_submission", {"submission_id": str(SUBMISSION_ID)})

    assert result.is_error
    assert expected in text_of(result)


async def test_validation_errors_pass_on_the_api_message(
    agent: Client, fake_api: FakeDocFlow
) -> None:
    fake_api.responses[("GET", "/submissions")] = httpx2.Response(
        422,
        json={"detail": [{"loc": ["query", "status"], "msg": "Input should be 'approved'"}]},
    )

    result = await agent.call_tool("list_submissions", {})

    assert result.is_error
    assert "status: Input should be 'approved'" in text_of(result)


async def test_unreachable_api_gives_a_clear_error() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    docflow = DocFlowClient(
        "http://docflow.test",
        "agent@example.com",
        SecretStr(PASSWORD),
        transport=httpx2.MockTransport(refuse),
    )
    async with Client(build_server(docflow)) as agent:
        result = await agent.call_tool("list_submissions", {})

    assert result.is_error
    assert "Could not reach DocFlow" in text_of(result)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_submissions", {"limit": 51}),
        ("list_submissions", {"status": "paid"}),
        ("get_submission", {"submission_id": "not-a-uuid"}),
    ],
)
async def test_bad_arguments_are_rejected_before_calling_the_api(
    agent: Client, fake_api: FakeDocFlow, tool: str, arguments: dict[str, Any]
) -> None:
    result = await agent.call_tool(tool, arguments)

    assert result.is_error
    assert fake_api.api_requests() == []


async def test_list_result_is_structured(agent: Client) -> None:
    result = await agent.call_tool("list_submissions", {})

    assert result.structured_content is not None
    assert json.dumps(result.structured_content)  # plain JSON, not SDK objects


# --- Write tools ---------------------------------------------------------------------------

PDF = b"%PDF-1.7\n% test invoice\n"


def make_pdf(folder: Path, name: str = "invoice.pdf", data: bytes = PDF) -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


async def test_get_audit_log_returns_the_history(agent: Client, fake_api: FakeDocFlow) -> None:
    result = await agent.call_tool("get_audit_log", {"submission_id": str(SUBMISSION_ID)})

    assert not result.is_error
    (request,) = fake_api.api_requests()
    assert request.url.path == f"/submissions/{SUBMISSION_ID}/audit"


async def test_raise_query_sends_one_field_question(agent: Client, fake_api: FakeDocFlow) -> None:
    result = await agent.call_tool(
        "raise_query",
        {
            "submission_id": str(SUBMISSION_ID),
            "field": "line_items[1].hs_code",
            "question": "Please confirm the HS code; '90-26-B' is not valid.",
        },
    )

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["notice"] == UNTRUSTED_NOTICE
    (request,) = fake_api.api_requests()
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "queries": [
            {
                "field": "line_items[1].hs_code",
                "question": "Please confirm the HS code; '90-26-B' is not valid.",
            }
        ]
    }


async def test_role_refusals_from_docflow_reach_the_model(
    agent: Client, fake_api: FakeDocFlow
) -> None:
    fake_api.responses[("POST", f"/submissions/{SUBMISSION_ID}/queries")] = httpx2.Response(
        403, json={"detail": "Not allowed"}
    )

    result = await agent.call_tool(
        "raise_query",
        {"submission_id": str(SUBMISSION_ID), "field": "total", "question": "Why?"},
    )

    assert result.is_error
    assert "not allowed to do this" in text_of(result)


async def test_upload_sends_the_pdf_with_a_content_hash_key(
    fake_api: FakeDocFlow, tmp_path: Path
) -> None:
    make_pdf(tmp_path)
    async with connect(fake_api, upload_dir=tmp_path) as agent:
        listing = await agent.call_tool("list_invoice_files", {})
        result = await agent.call_tool("upload_invoice", {"file_name": "invoice.pdf"})

    assert (listing.structured_content or {}).get("result") == ["invoice.pdf"]
    assert not result.is_error
    (request,) = fake_api.api_requests()
    assert request.method == "POST"
    assert request.url.path == "/submissions"
    assert request.headers["Idempotency-Key"] == f"mcp-sha256-{hashlib.sha256(PDF).hexdigest()}"
    assert PDF in request.content
    assert b'filename="invoice.pdf"' in request.content


async def test_upload_retry_after_expired_token_keeps_the_idempotency_key(
    fake_api: FakeDocFlow, tmp_path: Path
) -> None:
    make_pdf(tmp_path)
    async with connect(fake_api, upload_dir=tmp_path) as agent:
        await agent.call_tool("list_submissions", {})
        fake_api.expire_tokens()
        result = await agent.call_tool("upload_invoice", {"file_name": "invoice.pdf"})

    assert not result.is_error
    rejected, resent = fake_api.api_requests()[1:]
    assert rejected.headers["Idempotency-Key"] == resent.headers["Idempotency-Key"]
    assert PDF in resent.content


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("../outside.pdf", "inside the upload folder"),
        ("/etc/passwd", "inside the upload folder"),
        ("notes.txt", "Only PDF files"),
        ("missing.pdf", "was not found"),
        ("fake.pdf", "not a valid PDF"),
        ("big.pdf", "larger than"),
    ],
)
async def test_upload_refuses_unsafe_or_invalid_files(
    fake_api: FakeDocFlow, tmp_path: Path, file_name: str, expected: str
) -> None:
    folder = tmp_path / "uploads"
    folder.mkdir()
    make_pdf(tmp_path, "outside.pdf")
    (folder / "notes.txt").write_text("not a pdf")
    make_pdf(folder, "fake.pdf", b"MZ not really a pdf")
    make_pdf(folder, "big.pdf", PDF + b"x" * 2048)

    async with connect(fake_api, upload_dir=folder, max_upload_bytes=1024) as agent:
        result = await agent.call_tool("upload_invoice", {"file_name": file_name})

    assert result.is_error
    assert expected in text_of(result)
    assert fake_api.api_requests() == []


async def test_upload_refuses_a_symlink_that_points_outside(
    fake_api: FakeDocFlow, tmp_path: Path
) -> None:
    folder = tmp_path / "uploads"
    folder.mkdir()
    secret = make_pdf(tmp_path, "secret.pdf")
    (folder / "link.pdf").symlink_to(secret)

    async with connect(fake_api, upload_dir=folder) as agent:
        listing = await agent.call_tool("list_invoice_files", {})
        result = await agent.call_tool("upload_invoice", {"file_name": "link.pdf"})

    assert (listing.structured_content or {}).get("result") == []
    assert result.is_error
    assert fake_api.api_requests() == []


async def test_record_decision_sends_the_decision_and_comment(fake_api: FakeDocFlow) -> None:
    async with connect(fake_api, allow_decisions=True) as agent:
        result = await agent.call_tool(
            "record_decision",
            {"submission_id": str(SUBMISSION_ID), "decision": "reject", "comment": "Wrong buyer"},
        )

    assert not result.is_error
    (request,) = fake_api.api_requests()
    assert request.url.path == f"/submissions/{SUBMISSION_ID}/decision"
    assert json.loads(request.content) == {"decision": "reject", "comment": "Wrong buyer"}


async def test_record_decision_passes_on_docflow_refusal(fake_api: FakeDocFlow) -> None:
    fake_api.responses[("POST", f"/submissions/{SUBMISSION_ID}/decision")] = httpx2.Response(
        400, json={"detail": "A rejection needs a comment"}
    )
    async with connect(fake_api, allow_decisions=True) as agent:
        result = await agent.call_tool(
            "record_decision", {"submission_id": str(SUBMISSION_ID), "decision": "reject"}
        )

    assert result.is_error
    assert "A rejection needs a comment" in text_of(result)


async def test_record_decision_is_not_callable_unless_enabled(agent: Client) -> None:
    result = await agent.call_tool(
        "record_decision", {"submission_id": str(SUBMISSION_ID), "decision": "approve"}
    )

    assert result.is_error
