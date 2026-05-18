"""Prompt template tests — test functions directly (no FastMCP dependency)."""

from __future__ import annotations

from thruk_mcp.server import diagnose_flapping, investigate_alert, schedule_maintenance


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
