from __future__ import annotations

import json

import pytest
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
