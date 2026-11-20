# 0008: MCP server for AI agents (M8)

Date: 2026-11-20. Status: accepted.

## Context

Customers increasingly want their own AI agents (Claude Code, Claude Desktop, Cursor, in-house
agents) to work with the systems they buy: "which invoices need my review, and what is wrong with
each?". The Model Context Protocol (MCP) is the common way to give an agent tools. The risk is
that an agent is driven by a model that can be wrong or manipulated, including by text inside an
uploaded invoice.

## Decision: a separate package that is only an API client

`src/docflow_mcp/` is a small MCP server that calls DocFlow's public HTTP API with a normal user
login. It never imports DocFlow's server code and never touches the database.

- Every agent action goes through the same login, role checks, rate limits, validation and
  append-only audit log as a person's. An agent can never do more than the account it runs as.
- One place enforces access: the API. The MCP server registers every tool for every account and
  passes DocFlow's refusals back to the model ("not allowed for this account"), instead of
  re-implementing role rules that could drift from the API's.
- Same repository (it changes together with the API and shares CI), separate package and
  dependency group (`uv sync --group mcp`), so the API's Docker image does not ship the MCP SDK.
- SDK: the official `mcp` package v2 (`MCPServer`), pinned `>=2,<3`, with its `httpx2` client.

## Transport and credentials

- **stdio only.** The agent's client starts the server as a child process on the user's machine.
  stdout carries the protocol, so all logging goes to stderr.
- The server logs in as a DocFlow user from `DOCFLOW_MCP_EMAIL` / `DOCFLOW_MCP_PASSWORD` (a
  gitignored `.env.mcp` locally). Use a dedicated least-privilege account, never an admin. An
  expired token triggers one fresh login and one resend; a request rejected with 401 was never
  processed, so resending is safe.
- Errors become short instructions for the model (403: a person with the right role must do
  it; 429: wait N seconds; 4xx: DocFlow's own message). Unexpected errors are logged and returned
  as a generic message. Credentials never appear in tool output.

## Tools and their guards

| Tool | Kind | Guard |
|---|---|---|
| `list_submissions`, `get_submission`, `get_audit_log` | read-only | API role checks |
| `raise_query` | write, reversible | API role and stage checks |
| `list_invoice_files`, `upload_invoice` | write | Off unless `DOCFLOW_MCP_UPLOAD_DIR` is set; see below |
| `record_decision` | write, destructive | Off unless `DOCFLOW_MCP_ALLOW_DECISIONS=true`; marked destructive so the client asks the person; API decides who may approve |

Tool annotations (`read_only_hint`, `destructive_hint`, `idempotent_hint`) tell clients which
calls need the person's confirmation. They are hints, so nothing depends on the client honouring
them: the switches above and the API are the real controls.

**Uploads read local files chosen by the model**, which is the most dangerous capability here.
`files.py` resolves the requested name (including `..` and symlinks) and refuses anything outside
the configured folder, anything that is not a `.pdf` starting with `%PDF-`, and anything over
10 MB. The `Idempotency-Key` is the file's SHA-256, so a retried or repeated upload returns the
existing submission instead of a duplicate.

## Prompt injection

Invoice text is written by whoever uploaded the PDF. Every result that carries extracted fields
or quotes is wrapped with a notice that the content is data, not instructions, and the decision
tool's description says to act only on the person's request. These reduce the risk; they do not
remove it. The controls that hold even if the model is fooled are structural: no tool acts on
document content by itself, approve/reject is off by default and confirmed by the person, and the
API enforces roles and logs everything.

## Testing

`tests/test_mcp_server.py` (33 tests) runs the server in memory through the SDK's `Client`
against a fake DocFlow API (`httpx2.MockTransport`): tool schemas and annotations, filters,
re-login, every error mapping, argument validation, upload confinement (traversal, absolute path,
symlink, wrong type, fake PDF, size), and the opt-in tools. `scripts/mcp_smoke.py` runs the real
stdio transport against a running DocFlow.

## Known limitations and next steps

- No remote (Streamable HTTP) transport. Exposing the server to a customer's hosted agents would
  need OAuth 2.1 on the MCP endpoint and per-user tokens, not one shared service account.
- One DocFlow account per server process; the agent acts with that account's role.
- No `answer_query` or `edit_fields` tools yet; submitters answer queries through the API.
