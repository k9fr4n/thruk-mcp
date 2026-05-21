"""v0.6: read-only mode, allowlist, audit log, rate limit."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import respx

from thruk_mcp import audit
from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import WRITE_TOOLS, build_server

BASE = "https://thruk.test"


async def _close(mcp) -> None:
    await mcp._thruk_client.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_only_strips_write_tools() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=True)
    mcp = build_server(cfg)
    try:
        tools = {t.name for t in await mcp.list_tools()}
        # No write tool should remain
        assert tools.isdisjoint(WRITE_TOOLS)
        # Read tools still present
        assert "thruk_list_hosts" in tools
        assert "thruk_sites" in tools
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_allowlist_filters_tools_with_wildcard() -> None:
    cfg = ThrukConfig(
        base_url=BASE,
        api_key="k",
        enabled_tools=("thruk_list_*", "thruk_problems"),
    )
    mcp = build_server(cfg)
    try:
        tools = {t.name for t in await mcp.list_tools()}
        assert "thruk_list_hosts" in tools
        assert "thruk_list_alerts" in tools
        assert "thruk_problems" in tools
        assert "thruk_acknowledge" not in tools
        assert "thruk_get_host" not in tools  # not in allowlist
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_read_only_overrides_allowlist_for_writes() -> None:
    """Even if a write tool matches the allowlist, read_only strips it."""
    cfg = ThrukConfig(
        base_url=BASE,
        api_key="k",
        read_only=True,
        enabled_tools=("thruk_*",),
    )
    mcp = build_server(cfg)
    try:
        tools = {t.name for t in await mcp.list_tools()}
        assert tools.isdisjoint(WRITE_TOOLS)
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_emits_json_line_on_write(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", auth_user="alice")
    mcp = build_server(cfg)
    try:
        # caplog captures via the root logger; we need to attach to thruk_mcp.audit
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post("https://thruk.test/r/hosts/srv01/cmd/acknowledge_host_problem").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            await mcp.call_tool("thruk_acknowledge", {"host": "srv01"})
        audit_records = [r for r in caplog.records if r.name == "thruk_mcp.audit"]
        assert len(audit_records) == 1
        payload = json.loads(audit_records[0].message)
        assert payload["tool"] == "thruk_acknowledge"
        assert payload["user"] == "alice"
        assert payload["target"] == "srv01"
        assert payload["status"] == "ok"
        assert "ts" in payload
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_disabled_emits_nothing(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", audit_log=False)
    mcp = build_server(cfg)
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_forced_host_check").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            await mcp.call_tool("thruk_recheck", {"host": "srv01"})
        assert not any(r.name == "thruk_mcp.audit" for r in caplog.records)
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_records_error_status(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    # Disable retries so a single 500 mock is enough
    mcp._thruk_client.max_retries = 0  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post("https://thruk.test/r/hosts/srv01/cmd/acknowledge_host_problem").mock(
                return_value=httpx.Response(500, text="boom")
            )
            result = await mcp.call_tool("thruk_acknowledge", {"host": "srv01"})
        # ThrukError is now returned as tool-level error content (not raised),
        # so the MCP client sees the actual Thruk message instead of a generic
        # "tool execution failed" protocol error.
        assert len(result) == 1
        assert result[0].text.startswith("Error:")
        rec = next(r for r in caplog.records if r.name == "thruk_mcp.audit")
        payload = json.loads(rec.message)
        assert payload["status"] == "error"
        assert payload["error"]  # any non-empty error message
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_empty_filter_returns_tool_error() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        result = await mcp.call_tool("thruk_delete_downtimes_by_filter", {})
        assert len(result) == 1
        assert result[0].text == (
            "Error: Provide at least one of host, hostgroup, service, start_time, comment."
        )
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_max_concurrent_creates_semaphore() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", max_concurrent=4)
    client = ThrukClient(cfg)
    try:
        assert client._sem is not None
        assert isinstance(client._sem, asyncio.Semaphore)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_max_concurrent_zero_means_no_semaphore() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", max_concurrent=0)
    client = ThrukClient(cfg)
    try:
        assert client._sem is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_audit_redacts_sensitive_keys() -> None:
    """Direct unit test on the redactor."""
    out = audit._redact({"host": "srv01", "api_key": "secret", "nested": {"token": "x"}})
    assert out == {"host": "srv01", "api_key": "***", "nested": {"token": "***"}}
