"""End-to-end tool routing tests.

For every registered tool we assert that calling it through the FastMCP API
produces the expected HTTP request (method + path) against Thruk.
This is the primary regression guard against URL / param mistakes.
"""

from __future__ import annotations

import pytest

from tests.conftest import ok

# ---------------------------------------------------------------- Read tools


@pytest.mark.asyncio
async def test_list_hosts(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "a"}]))
    await mcp.call_tool("thruk_list_hosts", {"state": "down", "limit": 10})
    assert route.called
    params = route.calls.last.request.url.params
    assert params["state"] == "1"  # down
    assert params["limit"] == "10"
    assert "columns" in params  # default columns applied


@pytest.mark.asyncio
async def test_get_host(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts/srv01").mock(return_value=ok({"name": "srv01"}))
    await mcp.call_tool("thruk_get_host", {"host": "srv01"})
    assert route.called


@pytest.mark.asyncio
async def test_list_services_with_servicegroup(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_services", {"servicegroup": "db", "state": "critical"})
    params = route.calls.last.request.url.params
    assert params["groups[gte]"] == "db"
    assert params["state"] == "2"


@pytest.mark.asyncio
async def test_get_service(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services/srv01/ssh").mock(return_value=ok({}))
    await mcp.call_tool("thruk_get_service", {"host": "srv01", "service": "ssh"})
    assert route.called


@pytest.mark.asyncio
async def test_list_hostgroups(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hostgroups").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_hostgroups", {})
    assert route.called


@pytest.mark.asyncio
async def test_list_servicegroups(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/servicegroups").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_servicegroups", {})
    assert route.called


@pytest.mark.asyncio
async def test_problems(mocked_server) -> None:
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_problems", {})
    assert r_hosts.called and r_svc.called
    # Hosts query filters on acknowledged=0 etc.
    p = r_hosts.calls.last.request.url.params
    assert p["acknowledged"] == "0"
    assert p["scheduled_downtime_depth"] == "0"


@pytest.mark.asyncio
async def test_stats(mocked_server) -> None:
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/stats").mock(return_value=ok({}))
    await mcp.call_tool("thruk_stats", {})
    assert r_h.called and r_s.called


@pytest.mark.asyncio
async def test_list_downtimes(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_downtimes", {"host": "srv01", "active_only": False})
    p = route.calls.last.request.url.params
    assert p["host_name"] == "srv01"
    assert "start_time[lte]" not in p  # active_only=False removes time filter


@pytest.mark.asyncio
async def test_get_downtime(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/downtimes/42").mock(return_value=ok({}))
    await mcp.call_tool("thruk_get_downtime", {"downtime_id": 42})
    assert route.called


@pytest.mark.asyncio
async def test_list_comments(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/comments").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_comments", {})
    assert route.called


@pytest.mark.asyncio
async def test_sites(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/sites").mock(return_value=ok([]))
    await mcp.call_tool("thruk_sites", {})
    assert route.called


# ----------------------------------------------------- Logs / history tools


@pytest.mark.asyncio
async def test_list_logs(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_logs", {"host": "srv01", "message_regex": "timeout"})
    p = route.calls.last.request.url.params
    assert p["host_name"] == "srv01"
    assert p["message[regex]"] == "timeout"
    assert p["time[gte]"] == "-24h"


@pytest.mark.asyncio
async def test_list_alerts_with_state(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/alerts").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_alerts", {"state": "warning"})
    p = route.calls.last.request.url.params
    assert p["state"] == "1"  # service warning


@pytest.mark.asyncio
async def test_list_notifications_with_contact(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/notifications").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_notifications", {"contact": "oncall"})
    p = route.calls.last.request.url.params
    assert p["contact_name"] == "oncall"


@pytest.mark.asyncio
async def test_recent_events(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_recent_events", {"hours": 2})
    p = route.calls.last.request.url.params
    assert p["time[gte]"] == "-2h"


@pytest.mark.asyncio
async def test_recent_events_only_alerts(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/alerts").mock(return_value=ok([]))
    await mcp.call_tool("thruk_recent_events", {"hours": 1, "only_alerts": True})
    assert route.called


# ---------------------------------------------------------- Downtime writes


@pytest.mark.asyncio
async def test_schedule_downtime_host(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_host_downtime").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_schedule_downtime", {"host": "srv01", "duration_minutes": 30})
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "end_time=%2B30m" in body  # URL-encoded +30m


@pytest.mark.asyncio
async def test_schedule_downtime_service(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/schedule_svc_downtime").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_schedule_downtime", {"host": "srv01", "service": "ssh"})
    assert route.called


@pytest.mark.asyncio
async def test_schedule_host_services_downtime(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_host_svc_downtime").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_schedule_host_services_downtime", {"host": "srv01"})
    assert route.called


@pytest.mark.asyncio
async def test_schedule_propagated_downtime(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post(
        "https://thruk.test/r/hosts/srv01/cmd/schedule_and_propagate_triggered_host_downtime"
    ).mock(return_value=ok({"rc": 0}))
    await mcp.call_tool(
        "thruk_schedule_propagated_host_downtime",
        {"host": "srv01", "triggered": True},
    )
    assert route.called


@pytest.mark.asyncio
async def test_schedule_hostgroup_downtime_services(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post(
        "https://thruk.test/r/hostgroups/db/cmd/schedule_hostgroup_svc_downtime"
    ).mock(return_value=ok({"rc": 0}))
    await mcp.call_tool(
        "thruk_schedule_hostgroup_downtime",
        {"hostgroup": "db", "target": "services"},
    )
    assert route.called


@pytest.mark.asyncio
async def test_schedule_servicegroup_downtime(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post(
        "https://thruk.test/r/servicegroups/web/cmd/schedule_servicegroup_svc_downtime"
    ).mock(return_value=ok({"rc": 0}))
    await mcp.call_tool("thruk_schedule_servicegroup_downtime", {"servicegroup": "web"})
    assert route.called


@pytest.mark.asyncio
async def test_delete_downtime(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_delete_downtime", {"downtime_id": 42, "host": "srv01"})
    body = route.calls.last.request.content.decode()
    assert "downtime_id=42" in body


@pytest.mark.asyncio
async def test_delete_active_downtimes(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_active_host_downtimes").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    assert route.called


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_picks_hostgroup_cmd(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/system/cmd/del_downtime_by_hostgroup_name").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_delete_downtimes_by_filter", {"hostgroup": "db"})
    assert route.called


# ---------------------------------------------------------------- Ack / recheck


@pytest.mark.asyncio
async def test_acknowledge_uses_correct_payload_keys(mocked_server) -> None:
    """Regression for the v0.1 bug: payload keys must be sticky_ack,
    send_notification, persistent_comment — not sticky/notify/persistent."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/acknowledge_host_problem").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_acknowledge",
        {"host": "srv01", "sticky": False, "notify": False, "persistent": True},
    )
    body = route.calls.last.request.content.decode()
    assert "sticky_ack=0" in body
    assert "send_notification=0" in body
    assert "persistent_comment=1" in body


@pytest.mark.asyncio
async def test_remove_acknowledgement(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/remove_host_acknowledgement").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_remove_acknowledgement", {"host": "srv01"})
    assert route.called


@pytest.mark.asyncio
async def test_recheck_forced(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_forced_host_check").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_recheck", {"host": "srv01"})
    assert route.called


@pytest.mark.asyncio
async def test_recheck_service_unforced(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/schedule_svc_check").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_recheck",
        {"host": "srv01", "service": "ssh", "forced": False},
    )
    assert route.called


# ---------------------------------------------------- Query escape hatches


@pytest.mark.asyncio
async def test_query_forwards_path_and_params(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/processinfo").mock(return_value=ok({"ver": "3.x"}))
    await mcp.call_tool(
        "thruk_query",
        {"path": "/processinfo", "params": {"q": "x"}},
    )
    assert route.called
    assert route.calls.last.request.url.params["q"] == "x"


@pytest.mark.asyncio
async def test_query_with_backends(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/sites/prod,dr/hosts").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_query",
        {"path": "/hosts", "backends": "prod,dr"},
    )
    assert route.called
