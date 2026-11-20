"""Smoke test for the MCP server over the real stdio transport, against a running DocFlow API.

Starts `docflow-mcp` as a child process, exactly as an MCP client such as Claude Code does, then
lists its tools and calls the read-only ones. Reads DOCFLOW_MCP_* from the environment, for example:

    uv run --group mcp --env-file .env.mcp python scripts/mcp_smoke.py
"""

import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters
from mcp.types import TextContent


async def main() -> int:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "docflow_mcp.server"],
        env={k: v for k, v in os.environ.items() if k.startswith("DOCFLOW_MCP_") or k == "PATH"},
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        print("tools:", ", ".join(sorted(t.name for t in tools)))

        listing = await client.call_tool("list_submissions", {"limit": 5})
        if listing.is_error:
            print("list_submissions failed:", text(listing.content))
            return 1
        rows = (listing.structured_content or {}).get("result", [])
        print(f"list_submissions: {len(rows)} submission(s)")
        for row in rows:
            print(f"  {row['id']}  {row['status']:<18} flags={row['flag_count']}")

        if rows:
            detail = await client.call_tool("get_submission", {"submission_id": rows[0]["id"]})
            if detail.is_error:
                print("get_submission failed:", text(detail.content))
                return 1
            body = detail.structured_content or {}
            print("get_submission notice:", body.get("notice"))
            print(json.dumps(body.get("submission", {}).get("flags"), indent=2))
    return 0


def text(content: list[object]) -> str:
    return " ".join(c.text for c in content if isinstance(c, TextContent))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
