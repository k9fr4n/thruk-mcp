"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest_asyncio
import respx

from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import build_server

BASE = "https://thruk.test"
CFG = ThrukConfig(base_url=BASE, api_key="k")


@pytest_asyncio.fixture
async def mocked_server():
    """FastMCP instance + respx router intercepting all outbound traffic.

    Yields a tuple ``(mcp, respx_router)``. Tests register HTTP expectations
    on the router and invoke tools via ``await mcp.call_tool(name, args)``.
    """
    with respx.mock(assert_all_called=False) as router:
        mcp = build_server(CFG)
        try:
            yield mcp, router
        finally:
            client: ThrukClient = mcp._thruk_client  # type: ignore[attr-defined]
            await client.aclose()


def ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)
