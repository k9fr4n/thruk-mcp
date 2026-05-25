"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest_asyncio
import respx
from mcp.types import TextContent

from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import _TOOL_DISPATCH, build_server

BASE = "https://thruk.test"
CFG = ThrukConfig(base_url=BASE, api_key="k")


class _ServerProxy:
    """Thin wrapper around the low-level Server that provides the same
    ``call_tool(name, args)`` interface the tests expect.

    Also exposes ``read_resource`` as a direct call to the module-level
    resource functions (resources are not part of the low-level Server
    in the same way as FastMCP).
    """

    def __init__(self, server, client: ThrukClient) -> None:
        self._server = server
        self._thruk_client = client

    async def call_tool(self, name: str, args: dict) -> list[TextContent]:
        fn = _TOOL_DISPATCH.get(name)
        if fn is None:
            raise ValueError(f"Unknown tool: {name!r}")
        result = await fn(**args)
        return [TextContent(type="text", text=result)]

    async def read_resource(self, uri) -> list:
        """Delegate to ThrukMCPServer.read_resource (issue #145: tests the real path)."""
        return await self._server.read_resource(uri)


@pytest_asyncio.fixture
async def mocked_server():
    """Low-level MCP Server + respx router intercepting all outbound traffic.

    Yields a tuple ``(proxy, respx_router)``. Tests register HTTP expectations
    on the router and invoke tools via ``await proxy.call_tool(name, args)``.
    """
    with respx.mock(assert_all_called=False) as router:
        server = build_server(CFG)
        client: ThrukClient = server._thruk_client  # type: ignore[attr-defined]
        proxy = _ServerProxy(server, client)
        try:
            yield proxy, router
        finally:
            await client.aclose()


def ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)
