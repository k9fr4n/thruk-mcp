"""Prompt template tests — direct function tests and MCP server integration."""

from __future__ import annotations

import pytest
from mcp.types import GetPromptRequest, ListPromptsRequest

from thruk_mcp.server import diagnose_flapping, investigate_alert, schedule_maintenance

# ---------------------------------------------------------------------------
# Direct function tests (unchanged behaviour)
# ---------------------------------------------------------------------------


def test_investigate_alert_renders_for_host_only() -> None:
    text = investigate_alert(host="srv01")
    assert "srv01" in text
    assert "thruk_get_host" in text
    assert "thruk_get_service" not in text  # service branch absent


def test_investigate_alert_renders_for_service() -> None:
    text = investigate_alert(host="srv01", service="ssh")
    assert "thruk_get_service" in text


def test_schedule_maintenance_picks_correct_tool() -> None:
    text = schedule_maintenance(target="db", duration_minutes=60, kind="hostgroup")
    assert "thruk_schedule_hostgroup_downtime" in text
    assert "60" in text


def test_diagnose_flapping_mentions_flapping_and_tools() -> None:
    text = diagnose_flapping(host="srv01", service="http")
    assert "flapp" in text.lower()
    assert "thruk_list_alerts" in text


# ---------------------------------------------------------------------------
# Regression tests for issue #145 — handlers must be registered on the
# low-level mcp.server.Server, not just defined as orphan module-level code.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_registers_prompt_handlers(mocked_server) -> None:
    """Verify that list_prompts and get_prompt handlers are wired on the
    low-level Server after build_server() (issue #145 regression guard)."""
    mcp, _ = mocked_server
    low_level = mcp._server._server  # mcp.server.Server (low-level SDK)
    assert ListPromptsRequest in low_level.request_handlers, (
        "list_prompts handler not registered — issue #145 regression"
    )
    assert GetPromptRequest in low_level.request_handlers, (
        "get_prompt handler not registered — issue #145 regression"
    )


@pytest.mark.asyncio
async def test_list_prompts_via_server(mocked_server) -> None:
    """ThrukMCPServer.list_prompts() must return the three prompt definitions."""
    mcp, _ = mocked_server
    prompts = await mcp._server.list_prompts()
    names = {p.name for p in prompts}
    assert names == {"investigate_alert", "schedule_maintenance", "diagnose_flapping"}


@pytest.mark.asyncio
async def test_get_prompt_investigate_alert_via_server(mocked_server) -> None:
    """get_prompt delegates to investigate_alert() with correct arguments."""
    mcp, _ = mocked_server
    result = await mcp._server.get_prompt("investigate_alert", {"host": "srv01", "service": "ssh"})
    text = result.messages[0].content.text
    assert "srv01" in text
    assert "ssh" in text
    assert "thruk_get_service" in text


@pytest.mark.asyncio
async def test_get_prompt_diagnose_flapping_via_server(mocked_server) -> None:
    """get_prompt delegates to diagnose_flapping() with correct arguments."""
    mcp, _ = mocked_server
    result = await mcp._server.get_prompt("diagnose_flapping", {"host": "srv01", "service": "http"})
    text = result.messages[0].content.text
    assert "flapp" in text.lower()
    assert "thruk_list_alerts" in text


@pytest.mark.asyncio
async def test_get_prompt_schedule_maintenance_via_server(mocked_server) -> None:
    """get_prompt delegates to schedule_maintenance() with correct arguments."""
    mcp, _ = mocked_server
    result = await mcp._server.get_prompt(
        "schedule_maintenance",
        {"target": "prod-db", "duration_minutes": "60", "kind": "hostgroup"},
    )
    text = result.messages[0].content.text
    assert "prod-db" in text
    assert "thruk_schedule_hostgroup_downtime" in text


@pytest.mark.asyncio
async def test_get_prompt_unknown_raises(mocked_server) -> None:
    """get_prompt must raise ValueError for unknown prompt names."""
    mcp, _ = mocked_server
    with pytest.raises(ValueError, match="Unknown prompt"):
        await mcp._server.get_prompt("no_such_prompt", {})
