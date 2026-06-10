"""Prompt template tests — direct function tests and MCP server integration."""

from __future__ import annotations

import pytest
from mcp.types import GetPromptRequest, ListPromptsRequest

from thruk_mcp.server import (
    capacity_review,
    daily_health_report,
    diagnose_flapping,
    incident_triage,
    investigate_alert,
    noise_review,
    schedule_maintenance,
    sla_report,
)

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


def test_daily_health_report_estate_and_hostgroup() -> None:
    text = daily_health_report()
    assert "thruk_totals" in text
    assert "thruk_unacked_critical" in text
    assert "thruk_stale_checks" in text
    scoped = daily_health_report(hostgroup="HG_PROD")
    assert "HG_PROD" in scoped


def test_incident_triage_orchestrates_triage_tools() -> None:
    text = incident_triage(hostgroup="HG_PROD")
    assert "thruk_problem_counts" in text
    assert "thruk_concurrent_failures" in text
    assert "HG_PROD" in text


def test_capacity_review_uses_perfdata_tools_and_percent() -> None:
    text = capacity_review(within_percent=15)
    assert "thruk_perfdata_near_threshold" in text
    assert "15" in text


def test_sla_report_picks_correct_tool_per_kind() -> None:
    assert "thruk_host_availability" in sla_report(target="srv01", kind="host")
    assert "thruk_service_availability" in sla_report(target="srv01/ssh", kind="service")
    assert "thruk_hostgroup_availability" in sla_report(target="HG_PROD", kind="hostgroup")
    # unknown kind falls back to host
    assert "thruk_host_availability" in sla_report(target="x", kind="bogus")


def test_noise_review_window_and_tools() -> None:
    text = noise_review(since="-7d")
    assert "-7d" in text
    assert "thruk_top_noisy_hosts" in text
    assert "thruk_recurring_problems" in text


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
    """ThrukMCPServer.list_prompts() must return all prompt definitions."""
    mcp, _ = mocked_server
    prompts = await mcp._server.list_prompts()
    names = {p.name for p in prompts}
    assert names == {
        "investigate_alert",
        "schedule_maintenance",
        "diagnose_flapping",
        "daily_health_report",
        "incident_triage",
        "capacity_review",
        "sla_report",
        "noise_review",
    }


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
async def test_get_prompt_capacity_review_via_server(mocked_server) -> None:
    """get_prompt delegates to capacity_review() and coerces within_percent."""
    mcp, _ = mocked_server
    result = await mcp._server.get_prompt(
        "capacity_review", {"hostgroup": "HG_PROD", "within_percent": "20"}
    )
    text = result.messages[0].content.text
    assert "thruk_perfdata_near_threshold" in text
    assert "HG_PROD" in text
    assert "20" in text


@pytest.mark.asyncio
async def test_get_prompt_sla_report_via_server(mocked_server) -> None:
    """get_prompt delegates to sla_report() with the right availability tool."""
    mcp, _ = mocked_server
    result = await mcp._server.get_prompt(
        "sla_report", {"target": "HG_PROD", "kind": "hostgroup", "timeperiod": "lastmonth"}
    )
    text = result.messages[0].content.text
    assert "thruk_hostgroup_availability" in text
    assert "lastmonth" in text


@pytest.mark.asyncio
async def test_get_prompt_unknown_raises(mocked_server) -> None:
    """get_prompt must raise ValueError for unknown prompt names."""
    mcp, _ = mocked_server
    with pytest.raises(ValueError, match="Unknown prompt"):
        await mcp._server.get_prompt("no_such_prompt", {})
