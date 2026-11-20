#!/usr/bin/env bash
# Starts the DocFlow MCP server over stdio, for MCP clients such as Claude Code:
#
#   claude mcp add docflow -- /path/to/docflow/scripts/docflow-mcp.sh
#
# Credentials come from .env.mcp in the repo root (DOCFLOW_MCP_URL, DOCFLOW_MCP_EMAIL,
# DOCFLOW_MCP_PASSWORD); it is gitignored. stdout carries the MCP protocol, so this script must
# not print anything to it.
set -euo pipefail
cd "$(dirname "$0")/.."
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
exec "$UV" run --quiet --frozen --group mcp --env-file .env.mcp docflow-mcp
