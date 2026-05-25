from __future__ import annotations

import json

import pytest
from mcp.types import ListResourcesRequest, ListResourceTemplatesRequest, ReadResourceRequest
from pydantic import AnyUrl

from tests.conftest import ok


@pytest.mark.asyncio
async def test_resource_host(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/srv01").mock(return_value=ok({"name": "srv01"}))
    contents = await mcp.read_resource(AnyUrl("thruk://hosts/srv01"))
    payload = next(iter(contents))
    assert json.loads(payload.content)["name"] == "srv01"


@pytest.mark.asyncio
async def test_resource_service(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/srv01/ssh").mock(
        return_value=ok({"description": "ssh"})
    )
    contents = await mcp.read_resource(AnyUrl("thruk://services/srv01/ssh"))
    assert "ssh" in next(iter(contents)).content


@pytest.mark.asyncio
async def test_resource_hostgroup(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hostgroups/db").mock(return_value=ok({"name": "db"}))
    contents = await mcp.read_resource(AnyUrl("thruk://hostgroups/db"))
    assert "db" in next(iter(contents)).content


@pytest.mark.asyncio
async def test_resource_problems(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "h1"}]))
    router.get("https://thruk.test/r/services").mock(return_value=ok([{"description": "s1"}]))
    contents = await mcp.read_resource(AnyUrl("thruk://problems"))
    payload = json.loads(next(iter(contents)).content)
    assert payload["hosts"] == [{"name": "h1"}]
    assert payload["services"] == [{"description": "s1"}]


@pytest.mark.asyncio
async def test_resource_stats(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({"up": 5}))
    router.get("https://thruk.test/r/services/stats").mock(return_value=ok({"ok": 50}))
    contents = await mcp.read_resource(AnyUrl("thruk://stats"))
    payload = json.loads(next(iter(contents)).content)
    assert payload == {"hosts": {"up": 5}, "services": {"ok": 50}}


# ---------------------------------------------------------------------------
# Regression tests for issue #145 — handlers must be registered on the
# low-level mcp.server.Server, not just defined as orphan module-level code.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_registers_resource_handlers(mocked_server) -> None:
    """Verify that read_resource, list_resources and list_resource_templates
    handlers are actually wired to the low-level MCP Server after build_server().

    Before the fix for issue #145 this assertion failed: the handlers existed
    as module-level functions but were never registered via the @server.*()
    decorators, so MCP clients could not discover or read any resources.
    """
    mcp, _ = mocked_server
    low_level = mcp._server._server  # mcp.server.Server (low-level SDK)
    assert ReadResourceRequest in low_level.request_handlers, (
        "read_resource handler not registered — issue #145 regression"
    )
    assert ListResourcesRequest in low_level.request_handlers, (
        "list_resources handler not registered — issue #145 regression"
    )
    assert ListResourceTemplatesRequest in low_level.request_handlers, (
        "list_resource_templates handler not registered — issue #145 regression"
    )


@pytest.mark.asyncio
async def test_list_resources_via_server(mocked_server) -> None:
    """ThrukMCPServer.list_resources() must return problems and stats entries."""
    mcp, _ = mocked_server
    resources = await mcp._server.list_resources()
    names = {r.name for r in resources}
    assert "Current unhandled problems" in names
    assert "Aggregated host/service stats" in names


@pytest.mark.asyncio
async def test_list_resource_templates_via_server(mocked_server) -> None:
    """ThrukMCPServer.list_resource_templates() must return the three URI templates."""
    mcp, _ = mocked_server
    templates = await mcp._server.list_resource_templates()
    uris = {t.uriTemplate for t in templates}
    assert "thruk://hosts/{name}" in uris
    assert "thruk://services/{host}/{service}" in uris
    assert "thruk://hostgroups/{name}" in uris


@pytest.mark.asyncio
async def test_resource_unknown_uri_raises(mocked_server) -> None:
    """read_resource must raise ValueError for unrecognised URIs."""
    mcp, _ = mocked_server
    with pytest.raises(ValueError, match="Unknown resource URI"):
        await mcp._server.read_resource(AnyUrl("thruk://no_such_thing"))
