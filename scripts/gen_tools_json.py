#!/usr/bin/env python3
"""Generate catalog/tools.json from the live tool registry.

Single source of truth is ``build_server().list_tools()`` — the exact contract
the MCP server advertises to clients — so the catalog can never drift from the
runtime schema (issue: catalog/tools.json was a hand-maintained artifact that
fell back to the abandoned FastMCP/Pydantic schema shape).

Mirrors scripts/gen_metadata.py: same async list_tools() source, same
first-line tool descriptions, the real explicit ``inputSchema`` verbatim.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("THRUK_BASE_URL", "x")
os.environ.setdefault("THRUK_API_KEY", "x")

from thruk_mcp.server import build_server


async def _collect_tools() -> list:
    """Async helper: builds the server and collects {name, description, inputSchema}."""
    server = build_server()
    tools_list = await server.list_tools()
    tools = []
    for t in tools_list:
        # t is mcp.types.Tool: .name, .description, .inputSchema
        tools.append(
            {
                "name": t.name,
                "description": " ".join((t.description or "").split()),
                "inputSchema": t.inputSchema,
            }
        )
    return tools


def main():
    tools = asyncio.run(_collect_tools())
    payload = json.dumps(tools, indent=2) + "\n"

    out = os.path.join(os.path.dirname(__file__), "..", "catalog", "tools.json")

    if "--check" in sys.argv:
        # CI drift guard: fail if the committed file is out of sync with the registry.
        with open(out) as fh:
            current = fh.read()
        if current != payload:
            print(
                "catalog/tools.json is out of sync with the tool registry.\n"
                "Regenerate it with: python scripts/gen_tools_json.py",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"catalog/tools.json is in sync ({len(tools)} tools).", file=sys.stderr)
        return

    with open(out, "w") as fh:
        fh.write(payload)
    print(f"Written {len(tools)} tools to catalog/tools.json", file=sys.stderr)
    print(payload, end="")


if __name__ == "__main__":
    main()
