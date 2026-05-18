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
        """Call the appropriate resource function directly."""
        from thruk_mcp.server import (
            _host_resource,
            _hostgroup_resource,
            _problems_resource,
            _service_resource,
            _stats_resource,
        )

        s = str(uri)
        if s.startswith("thruk://hosts/"):
            name = s.split("/hosts/", 1)[1]
            content = await _host_resource(name)
        elif s.startswith("thruk://services/"):
            parts = s.split("/services/", 1)[1].split("/", 1)
            content = await _service_resource(parts[0], parts[1])
        elif s.startswith("thruk://hostgroups/"):
            name = s.split("/hostgroups/", 1)[1]
            content = await _hostgroup_resource(name)
        elif s == "thruk://problems":
            content = await _problems_resource()
        elif s == "thruk://stats":
            content = await _stats_resource()
        else:
            raise ValueError(f"Unknown resource URI: {uri}")

        class _Content:
            def __init__(self, text: str) -> None:
                self.content = text

        return [_Content(content)]


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
