from __future__ import annotations

import pytest

from tests.conftest import CFG
from thruk_mcp.server import build_server


@pytest.mark.asyncio
async def test_prompts_registered() -> None:
    mcp = build_server(CFG)
    prompts = await mcp.list_prompts()
    names = {p.name for p in prompts}
    assert names == {"investigate_alert", "schedule_maintenance", "diagnose_flapping"}


@pytest.mark.asyncio
async def test_investigate_alert_renders_for_host_only() -> None:
    mcp = build_server(CFG)
    result = await mcp.get_prompt("investigate_alert", {"host": "srv01"})
    text = " ".join(getattr(m.content, "text", "") for m in result.messages)
    assert "srv01" in text
    assert "thruk_get_host" in text
    assert "thruk_get_service" not in text  # service-only branch should be absent


@pytest.mark.asyncio
async def test_investigate_alert_renders_for_service() -> None:
    mcp = build_server(CFG)
    result = await mcp.get_prompt(
        "investigate_alert",
        {"host": "srv01", "service": "ssh"},
    )
    text = " ".join(getattr(m.content, "text", "") for m in result.messages)
    assert "thruk_get_service" in text


@pytest.mark.asyncio
async def test_schedule_maintenance_picks_correct_tool() -> None:
    mcp = build_server(CFG)
    result = await mcp.get_prompt(
        "schedule_maintenance",
        {"target": "db", "duration_minutes": 60, "kind": "hostgroup"},
    )
    text = " ".join(getattr(m.content, "text", "") for m in result.messages)
    assert "thruk_schedule_hostgroup_downtime" in text
    assert "60" in text


@pytest.mark.asyncio
async def test_diagnose_flapping_mentions_flapping_and_tools() -> None:
    mcp = build_server(CFG)
    result = await mcp.get_prompt(
        "diagnose_flapping",
        {"host": "srv01", "service": "http"},
    )
    text = " ".join(getattr(m.content, "text", "") for m in result.messages)
    assert "flapp" in text.lower()
    assert "thruk_list_alerts" in text
