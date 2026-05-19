"""End-to-end tool routing tests.

For every registered tool we assert that calling it through the FastMCP API
produces the expected HTTP request (method + path) against Thruk.
This is the primary regression guard against URL / param mistakes.
"""

from __future__ import annotations

import httpx
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


# ---------------------------------------------------------------- custom-var filtering


@pytest.mark.asyncio
async def test_list_hosts_custom_vars(mocked_server) -> None:
    """custom_vars dict is translated to _VARNAME=value top-level params (uppercase)."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "w01"}]))
    await mcp.call_tool("thruk_list_hosts", {"custom_vars": {"KERNEL": "windows"}, "limit": 5})
    params = route.calls.last.request.url.params
    assert params["_KERNEL"] == "windows"


@pytest.mark.asyncio
async def test_list_hosts_custom_vars_uppercased(mocked_server) -> None:
    """Varnames are auto-uppercased regardless of input case."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_hosts", {"custom_vars": {"kernel": "linux"}})
    params = route.calls.last.request.url.params
    assert params["_KERNEL"] == "linux"
    assert "_kernel" not in params


@pytest.mark.asyncio
async def test_list_services_custom_vars(mocked_server) -> None:
    """custom_vars filters on service-level custom variables."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_services", {"custom_vars": {"CRITICALITY": "prod"}})
    params = route.calls.last.request.url.params
    assert params["_CRITICALITY"] == "prod"


@pytest.mark.asyncio
async def test_list_services_host_custom_vars(mocked_server) -> None:
    """host_custom_vars filters services by *host*-level vars via _HOST prefix."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_services", {"host_custom_vars": {"KERNEL": "windows"}})
    params = route.calls.last.request.url.params
    assert params["_HOSTKERNEL"] == "windows"
    assert "_KERNEL" not in params


@pytest.mark.asyncio
async def test_list_services_custom_vars_combined(mocked_server) -> None:
    """custom_vars and host_custom_vars can be used simultaneously."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_services",
        {"custom_vars": {"CRITICALITY": "prod"}, "host_custom_vars": {"KERNEL": "windows"}},
    )
    params = route.calls.last.request.url.params
    assert params["_CRITICALITY"] == "prod"
    assert params["_HOSTKERNEL"] == "windows"


@pytest.mark.asyncio
async def test_problems_host_custom_vars(mocked_server) -> None:
    """host_custom_vars injected into service query only (with _HOST prefix)."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_problems", {"host_custom_vars": {"KERNEL": "windows"}})
    svc_params = r_svc.calls.last.request.url.params
    host_params = r_hosts.calls.last.request.url.params
    assert svc_params["_HOSTKERNEL"] == "windows"
    assert "_HOSTKERNEL" not in host_params


@pytest.mark.asyncio
async def test_problems_custom_vars_both_queries(mocked_server) -> None:
    """custom_vars injected into both host and service queries."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_problems", {"custom_vars": {"ENV": "prod"}})
    assert r_hosts.calls.last.request.url.params["_ENV"] == "prod"
    assert r_svc.calls.last.request.url.params["_ENV"] == "prod"


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
async def test_delete_downtime_host_explicit_service_none(mocked_server) -> None:
    """When service=None, tool auto-detects downtime type via GET /downtimes/{id}.
    A host downtime (empty service_description) routes to the host endpoint."""

    mcp, router = mocked_server
    # Auto-detection GET
    router.get("https://thruk.test/r/downtimes/42").mock(
        return_value=ok({"id": 42, "service_description": "", "host_name": "srv01"})
    )
    del_route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_delete_downtime", {"downtime_id": 42, "host": "srv01"})
    body = del_route.calls.last.request.content.decode()
    assert "downtime_id=42" in body


@pytest.mark.asyncio
async def test_delete_downtime_service_autodetect(mocked_server) -> None:
    """When service=None, tool auto-detects a service downtime and routes
    to the service endpoint — avoids the silent no-op of issue #35."""

    mcp, router = mocked_server
    # Auto-detection GET reveals a service downtime
    router.get("https://thruk.test/r/downtimes/446436").mock(
        return_value=ok(
            {
                "id": 446436,
                "service_description": "SERVICE_ACTIVE-DIRECTORY_HEALTH",
                "host_name": "srv01",
            }
        )
    )
    del_route = router.post(
        "https://thruk.test/r/services/srv01/SERVICE_ACTIVE-DIRECTORY_HEALTH/cmd/del_downtime"
    ).mock(return_value=ok({"rc": 0}))
    await mcp.call_tool(
        "thruk_delete_downtime",
        {"downtime_id": 446436, "host": "srv01"},  # no service arg
    )
    assert del_route.call_count == 1
    body = del_route.calls.last.request.content.decode()
    assert "downtime_id=446436" in body


@pytest.mark.asyncio
async def test_delete_downtime_service_explicit(mocked_server) -> None:
    """When service is provided explicitly, skip the GET round-trip."""
    mcp, router = mocked_server
    del_route = router.post("https://thruk.test/r/services/srv01/CPU/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_delete_downtime", {"downtime_id": 42, "host": "srv01", "service": "CPU"}
    )
    assert del_route.call_count == 1
    body = del_route.calls.last.request.content.decode()
    assert "downtime_id=42" in body


@pytest.mark.asyncio
async def test_delete_active_downtimes_host_deletes_all(mocked_server) -> None:
    """Enumerates active host-level downtimes and deletes each by ID."""
    import json

    mcp, router = mocked_server
    dt_route = router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 1087, "service_description": "", "author": "fjarry", "comment": "maint"},
                {"id": 1093, "service_description": "", "author": "fsallet", "comment": "test"},
            ]
        )
    )
    del_route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    result = json.loads(result_raw[0].text)
    assert dt_route.called
    assert del_route.call_count == 2
    assert result["count"] == 2
    assert result["errors"] == []
    deleted_ids = [d["downtime_id"] for d in result["deleted"]]
    assert 1087 in deleted_ids
    assert 1093 in deleted_ids


@pytest.mark.asyncio
async def test_delete_active_downtimes_service_filters_correctly(mocked_server) -> None:
    """Service downtimes are filtered by service_description; others are skipped."""
    import json

    mcp, router = mocked_server
    # Thruk returns one matching service downtime + one host downtime (should be ignored).
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 200, "service_description": "CPU", "author": "a", "comment": "c"},
                {"id": 201, "service_description": "", "author": "b", "comment": "x"},
            ]
        )
    )
    del_route = router.post("https://thruk.test/r/services/srv01/CPU/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool(
        "thruk_delete_active_downtimes", {"host": "srv01", "service": "CPU"}
    )
    result = json.loads(result_raw[0].text)
    assert del_route.call_count == 1
    assert result["count"] == 1
    assert result["deleted"][0]["downtime_id"] == 200


@pytest.mark.asyncio
async def test_delete_active_downtimes_none_found(mocked_server) -> None:
    """Returns count=0 and a message when no active downtimes exist."""
    import json

    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    result = json.loads(result_raw[0].text)
    assert result["count"] == 0
    assert "No active downtimes found" in result["message"]


@pytest.mark.asyncio
async def test_delete_active_downtimes_partial_failure(mocked_server) -> None:
    """Errors on individual IDs are collected in `errors`, not raised."""
    import json

    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 1087, "service_description": "", "author": "fjarry", "comment": "m"},
                {"id": 1093, "service_description": "", "author": "fsallet", "comment": "t"},
            ]
        )
    )
    # First call succeeds, second raises ThrukError via a 403.
    router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        side_effect=[
            ok({"rc": 0}),
            httpx.Response(403, text="Permission denied"),
        ]
    )
    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    result = json.loads(result_raw[0].text)
    assert result["count"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["downtime_id"] == 1093


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_picks_hostgroup_cmd(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/system/cmd/del_downtime_by_hostgroup_name").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_delete_downtimes_by_filter", {"hostgroup": "db"})
    assert route.called


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_host_also_deletes_host_level(mocked_server) -> None:
    """When filtering by host, host-level downtimes are deleted explicitly
    in addition to the DEL_DOWNTIME_BY_HOST_NAME system command."""
    import json

    mcp, router = mocked_server
    router.post("https://thruk.test/r/system/cmd/del_downtime_by_host_name").mock(
        return_value=ok({"rc": 0})
    )
    # Two downtimes: one host-level, one service-level.
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 1050, "service_description": "", "comment": "maint"},
                {"id": 1051, "service_description": "CPU", "comment": "maint"},
            ]
        )
    )
    del_host_route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool(
        "thruk_delete_downtimes_by_filter", {"host": "srv01", "comment": "maint"}
    )
    result = json.loads(result_raw[0].text)
    # Only host-level downtime (1050) should be deleted explicitly.
    assert del_host_route.call_count == 1
    assert result["host_downtimes_deleted"][0]["downtime_id"] == 1050
    assert result["host_downtimes_errors"] == []


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
async def test_query_cv_warning_injected(mocked_server) -> None:
    """thruk_query wraps the response in a _warning envelope when q= contains custom_variables."""
    import json as _json

    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "h1"}, {"name": "h2"}])
    )
    result = await mcp.call_tool(
        "thruk_query",
        {"path": "/hosts", "params": {"q": "custom_variables >= 'KERNEL windows'", "limit": 10}},
    )
    payload = _json.loads(result[0].text)
    assert "_warning" in payload
    assert "custom_variables" in payload["_warning"]
    assert "data" in payload
    assert len(payload["data"]) == 2  # the actual result is still returned


@pytest.mark.asyncio
async def test_query_no_warning_without_cv(mocked_server) -> None:
    """thruk_query does NOT inject a warning when q= does not mention custom_variables."""
    import json as _json

    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "h1"}]))
    result = await mcp.call_tool(
        "thruk_query",
        {"path": "/hosts", "params": {"q": "state = 1", "limit": 5}},
    )
    payload = _json.loads(result[0].text)
    # Plain list, no envelope
    assert isinstance(payload, list)


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
