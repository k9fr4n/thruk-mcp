"""End-to-end tool routing tests.

For every registered tool we assert that calling it through the FastMCP API
produces the expected HTTP request (method + path) against Thruk.
This is the primary regression guard against URL / param mistakes.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from tests.conftest import ok


def post_params(call) -> dict[str, str]:
    """Parse a form-encoded POST body into a flat {key: value} dict."""
    body = call.request.content.decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


# ---------------------------------------------------------------- Read tools


@pytest.mark.asyncio
async def test_list_hosts(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "a"}]))
    await mcp.call_tool(
        "thruk_list_hosts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "down"}, "limit": 10},
    )
    assert route.called
    params = route.calls.last.request.url.params
    assert params["state"] == "1"  # down
    assert params["limit"] == "10"
    assert "columns" in params  # default columns applied


@pytest.mark.asyncio
async def test_list_hosts_state_numeric_string(mocked_server) -> None:
    """state='1' (numeric string) must be accepted inside filter."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "b"}]))
    await mcp.call_tool(
        "thruk_list_hosts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "1"}},
    )
    params = route.calls.last.request.url.params
    assert params["state"] == "1", "numeric state string must be forwarded to Thruk REST"


@pytest.mark.asyncio
async def test_list_services_state_numeric_string(mocked_server) -> None:
    """state='2' (numeric string) must be accepted for services too."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_services",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "2"}},
    )
    params = route.calls.last.request.url.params
    assert params["state"] == "2", "numeric state string must be forwarded to Thruk REST"


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
    await mcp.call_tool(
        "thruk_list_services",
        {
            "filter": {
                "type": "group",
                "operator": "and",
                "conditions": [
                    {"type": "leaf", "field": "servicegroup", "op": "eq", "value": "db"},
                    {"type": "leaf", "field": "state", "op": "eq", "value": "critical"},
                ],
            }
        },
    )
    params = route.calls.last.request.url.params
    assert params["groups[gte]"] == "db"
    assert params["state"] == "2"


# ---------------------------------------------------------------- filter: custom-var


@pytest.mark.asyncio
async def test_list_hosts_custom_var(mocked_server) -> None:
    """custom_var leaf translates to _VARNAME=value (uppercase)."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "w01"}]))
    await mcp.call_tool(
        "thruk_list_hosts",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            },
            "limit": 5,
        },
    )
    params = route.calls.last.request.url.params
    assert params["_KERNEL"] == "windows"


@pytest.mark.asyncio
async def test_list_hosts_custom_var_uppercased(mocked_server) -> None:
    """custom_var 'var' name is auto-uppercased."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_hosts",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "kernel", "val": "linux"},
            }
        },
    )
    params = route.calls.last.request.url.params
    assert params["_KERNEL"] == "linux"
    assert "_kernel" not in params


@pytest.mark.asyncio
async def test_list_services_custom_var(mocked_server) -> None:
    """custom_var leaf on services translates to _VARNAME=value."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_services",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "CRITICALITY", "val": "prod"},
            }
        },
    )
    params = route.calls.last.request.url.params
    assert params["_CRITICALITY"] == "prod"


@pytest.mark.asyncio
async def test_list_services_host_custom_var(mocked_server) -> None:
    """host_custom_var leaf translates to _HOSTVARNAME=value."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_services",
        {
            "filter": {
                "type": "leaf",
                "field": "host_custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            }
        },
    )
    params = route.calls.last.request.url.params
    assert params["_HOSTKERNEL"] == "windows"
    assert "_KERNEL" not in params


@pytest.mark.asyncio
async def test_list_services_custom_var_combined(mocked_server) -> None:
    """custom_var and host_custom_var can be combined in one AND group."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_services",
        {
            "filter": {
                "type": "group",
                "operator": "and",
                "conditions": [
                    {
                        "type": "leaf",
                        "field": "custom_var",
                        "op": "eq",
                        "value": {"var": "CRITICALITY", "val": "prod"},
                    },
                    {
                        "type": "leaf",
                        "field": "host_custom_var",
                        "op": "eq",
                        "value": {"var": "KERNEL", "val": "windows"},
                    },
                ],
            }
        },
    )
    params = route.calls.last.request.url.params
    assert params["_CRITICALITY"] == "prod"
    assert params["_HOSTKERNEL"] == "windows"


@pytest.mark.asyncio
async def test_problems_host_custom_var(mocked_server) -> None:
    """host_custom_var in problems filter → _HOSTVAR on services only."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_problems",
        {
            "filter": {
                "type": "leaf",
                "field": "host_custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            }
        },
    )
    svc_params = r_svc.calls.last.request.url.params
    host_params = r_hosts.calls.last.request.url.params
    assert svc_params["_HOSTKERNEL"] == "windows"
    assert "_HOSTKERNEL" not in host_params


@pytest.mark.asyncio
async def test_problems_custom_var_both_queries(mocked_server) -> None:
    """custom_var in problems → _VAR on hosts, _HOSTVAR on services."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_problems",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "ENV", "val": "prod"},
            }
        },
    )
    assert r_hosts.calls.last.request.url.params["_ENV"] == "prod"
    assert r_svc.calls.last.request.url.params["_HOSTENV"] == "prod"
    assert "_ENV" not in r_svc.calls.last.request.url.params


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
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_logs",
        {
            "filter": {
                "type": "group",
                "operator": "and",
                "conditions": [
                    {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"},
                    {"type": "leaf", "field": "message", "op": "regex", "value": "timeout"},
                ],
            }
        },
    )
    p = post_params(route.calls.last)
    assert p["host_name"] == "srv01"
    assert p["message[regex]"] == "timeout"
    assert p["time[gte]"] == "-24h"  # since default still applied


@pytest.mark.asyncio
async def test_list_alerts_with_state(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "warning"}},
    )
    p = post_params(route.calls.last)
    assert p["state"] == "1"  # service warning
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"


@pytest.mark.asyncio
async def test_list_notifications_with_contact(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_notifications",
        {"filter": {"type": "leaf", "field": "contact", "op": "eq", "value": "oncall"}},
    )
    p = post_params(route.calls.last)
    assert p["contact_name"] == "oncall"
    assert p["class"] == "3"


@pytest.mark.asyncio
async def test_list_notifications_default_columns(mocked_server) -> None:
    """Default columns must include contact_name and command_name; state_type must be absent."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_notifications",
        {"filter": {"type": "leaf", "field": "host", "op": "eq", "value": "REDONLINE006"}},
    )
    p = post_params(route.calls.last)
    cols = p.get("columns", "")
    assert "contact_name" in cols, f"contact_name missing from columns: {cols}"
    assert "command_name" in cols, f"command_name missing from columns: {cols}"
    assert "state_type" not in cols, f"state_type should not be in notification columns: {cols}"


@pytest.mark.asyncio
async def test_recent_events(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_recent_events", {"hours": 2})
    p = post_params(route.calls.last)
    assert p["time[gte]"] == "-2h"


@pytest.mark.asyncio
async def test_recent_events_only_alerts(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_recent_events", {"hours": 1, "only_alerts": True})
    assert route.called
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"


# ---------------------------------------------- hostgroup filter (issue #43)


@pytest.mark.asyncio
async def test_problems_hostgroup_applied_to_both_queries(mocked_server) -> None:
    """hostgroup in filter → groups[gte] on hosts, host_groups[gte] on services."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_problems",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "linux-servers"}},
    )
    assert r_hosts.called and r_svc.called
    assert r_hosts.calls.last.request.url.params["groups[gte]"] == "linux-servers"
    assert r_svc.calls.last.request.url.params["host_groups[gte]"] == "linux-servers"


@pytest.mark.asyncio
async def test_problems_no_hostgroup_no_group_param(mocked_server) -> None:
    """Without hostgroup, neither groups[gte] nor host_groups[gte] appear."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool("thruk_problems", {})
    assert "groups[gte]" not in r_hosts.calls.last.request.url.params
    assert "host_groups[gte]" not in r_svc.calls.last.request.url.params


@pytest.mark.asyncio
async def test_list_notifications_hostgroup(mocked_server) -> None:
    """hostgroup resolved to host_name[regex] on /logs (two-step approach)."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "db01"}, {"name": "db02"}])
    )
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_notifications",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "db-servers"}},
    )
    p = post_params(route.calls.last)
    assert "host_name[regex]" in p, "host_name[regex] must be set on /logs"
    assert "db01" in p["host_name[regex]"] and "db02" in p["host_name[regex]"]
    assert "current_host_groups" not in str(p), "current_host_groups must not appear on /logs"
    assert p["class"] == "3"


@pytest.mark.asyncio
async def test_recent_events_hostgroup(mocked_server) -> None:
    """hostgroup resolved to host_name[regex] on /logs (two-step approach)."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "sw01"}, {"name": "sw02"}])
    )
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_recent_events",
        {
            "filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "network"},
            "hours": 2,
        },
    )
    p = post_params(route.calls.last)
    assert "host_name[regex]" in p
    assert "sw01" in p["host_name[regex]"] and "sw02" in p["host_name[regex]"]
    assert p["time[gte]"] == "-2h"


@pytest.mark.asyncio
async def test_recent_events_hostgroup_and_only_alerts(mocked_server) -> None:
    """hostgroup and only_alerts can be combined."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "sw01"}]))
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_recent_events",
        {
            "filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "network"},
            "only_alerts": True,
            "hours": 1,
        },
    )
    p = post_params(route.calls.last)
    assert "host_name[regex]" in p
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"


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
async def test_delete_downtimes_by_filter_no_args_returns_graceful_error(
    mocked_server,
) -> None:
    """Regression test for issue #71.

    Before the fix, calling thruk_delete_downtimes_by_filter with no filter
    arguments raised a bare ValueError that escaped call_tool and reached the
    MCP SDK as a -32603 Internal Error.

    Fix part 1: the tool function now raises ThrukError (not ValueError).
    Fix part 2: ThrukMCPServer.call_tool catches (ThrukError, ValueError) and
    returns a graceful 'Error: …' TextContent instead of re-raising.

    The _ServerProxy used by most tests bypasses ThrukMCPServer.call_tool, so
    this test calls wrapper.call_tool() directly to exercise the real handler.
    """
    from thruk_mcp.client import ThrukError

    _proxy, _router = mocked_server
    # Access the real ThrukMCPServer (not the _ServerProxy shim).
    wrapper = _proxy._server

    # Part 1: the tool itself must raise ThrukError, not ValueError.
    import pytest

    with pytest.raises(ThrukError, match="Provide at least one"):
        await _proxy.call_tool("thruk_delete_downtimes_by_filter", {})

    # Part 2: ThrukMCPServer.call_tool must catch ThrukError and return
    # a graceful error TextContent — NOT raise to the MCP layer.
    result = await wrapper.call_tool("thruk_delete_downtimes_by_filter", {})
    assert len(result) == 1
    text = result[0].text
    assert text.startswith("Error:")
    assert "provide" in text.lower()


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
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "h1"}, {"name": "h2"}]))
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


# ---------------------------------------------------------------- Noisy tools


def _make_log_entry(
    host: str,
    state: int,
    time_offset: int = 0,
    service: str | None = None,
) -> dict:
    """Build a minimal log entry for noisy-* tests."""
    entry: dict = {"host_name": host, "state": state, "time": 1_700_000_000 + time_offset}
    if service is not None:
        entry["service_description"] = service
    return entry


@pytest.mark.asyncio
async def test_top_noisy_hosts_basic(mocked_server) -> None:
    """Top-noisy-hosts aggregates by host and excludes RECOVERY (state=0)."""
    import json as _json

    mcp, router = mocked_server
    raw = [
        _make_log_entry("alpha", 1, 10),  # DOWN
        _make_log_entry("alpha", 1, 20),  # DOWN
        _make_log_entry("alpha", 0, 30),  # UP = recovery, excluded
        _make_log_entry("beta", 1, 40),  # DOWN
    ]
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {"since": "-6h", "limit": 5})
    assert route.called
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^HOST ALERT"
    assert p["time[gte]"] == "-6h"
    assert p["columns"] == "host_name,state,time"

    payload = _json.loads(result[0].text)
    assert payload["since"] == "-6h"
    assert payload["until"] is None
    assert payload["total_alerts_in_window"] == 3  # alpha x2 + beta x1 (recovery excluded)
    results = payload["results"]
    assert results[0]["host"] == "alpha"
    assert results[0]["alert_count"] == 2
    assert results[0]["last_state"] == "DOWN"
    assert results[1]["host"] == "beta"
    assert results[1]["alert_count"] == 1


@pytest.mark.asyncio
async def test_top_noisy_hosts_only_recovery_returns_empty(mocked_server) -> None:
    """When all entries are RECOVERY the results list should be empty."""
    import json as _json

    mcp, router = mocked_server
    raw = [_make_log_entry("alpha", 0), _make_log_entry("beta", 0)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {})
    payload = _json.loads(result[0].text)
    assert payload["total_alerts_in_window"] == 0
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_top_noisy_hosts_limit_respected(mocked_server) -> None:
    """Only ``limit`` hosts are returned even when more are present."""
    import json as _json

    mcp, router = mocked_server
    raw = [_make_log_entry(f"host{i}", 1, i) for i in range(20)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {"limit": 3})
    payload = _json.loads(result[0].text)
    assert len(payload["results"]) == 3


@pytest.mark.asyncio
async def test_top_noisy_hosts_filter_error(mocked_server) -> None:
    """Invalid filter field must return an error key."""
    import json as _json

    mcp, _router = mocked_server
    result = await mcp.call_tool(
        "thruk_top_noisy_hosts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "down"}},
    )
    payload = _json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_top_noisy_hosts_since_until(mocked_server) -> None:
    """since/until are forwarded as time[gte]/time[lte] and reflected in payload."""
    import json as _json

    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_top_noisy_hosts",
        {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59"},
    )
    p = post_params(router.calls.last)
    assert p["time[gte]"] == "2026-05-20 00:00:00"
    assert p["time[lte]"] == "2026-05-20 23:59:59"
    payload = _json.loads(result[0].text)
    assert payload["since"] == "2026-05-20 00:00:00"
    assert payload["until"] == "2026-05-20 23:59:59"


@pytest.mark.asyncio
async def test_top_noisy_services_basic(mocked_server) -> None:
    """Top-noisy-services aggregates by (host, service) and excludes RECOVERY (state=0)."""
    import json as _json

    mcp, router = mocked_server
    raw = [
        _make_log_entry("alpha", 2, 10, service="HTTP"),  # CRITICAL
        _make_log_entry("alpha", 1, 20, service="HTTP"),  # WARNING
        _make_log_entry("alpha", 0, 30, service="HTTP"),  # OK = recovery, excluded
        _make_log_entry("alpha", 2, 40, service="DISK"),  # CRITICAL
        _make_log_entry("beta", 1, 50, service="CPU"),  # WARNING
    ]
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_services", {"since": "-12h", "limit": 5})
    assert route.called
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^SERVICE ALERT"
    assert p["time[gte]"] == "-12h"
    assert p["columns"] == "host_name,service_description,state,time"

    payload = _json.loads(result[0].text)
    assert payload["since"] == "-12h"
    assert payload["until"] is None
    assert payload["total_alerts_in_window"] == 4  # recovery excluded
    results = payload["results"]
    # alpha/HTTP has 2 alerts → ranked first
    assert results[0]["host"] == "alpha"
    assert results[0]["service"] == "HTTP"
    assert results[0]["alert_count"] == 2
    assert results[0]["last_state"] == "WARNING"  # last non-recovery state
    assert results[0]["last_alert_time"] is not None


@pytest.mark.asyncio
async def test_top_noisy_services_default_since(mocked_server) -> None:
    """Default window should be since=-24h."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_top_noisy_services", {})
    p = post_params(router.calls.last)
    assert p["time[gte]"] == "-24h"
    assert "time[lte]" not in p


@pytest.mark.asyncio
async def test_top_noisy_services_filter_error(mocked_server) -> None:
    """Invalid filter field (e.g. 'state') must return an error key."""
    import json as _json

    mcp, _router = mocked_server
    result = await mcp.call_tool(
        "thruk_top_noisy_services",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "warning"}},
    )
    payload = _json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_top_noisy_services_since_until(mocked_server) -> None:
    """since/until are forwarded as time[gte]/time[lte] and reflected in payload."""
    import json as _json

    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_top_noisy_services",
        {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59"},
    )
    p = post_params(router.calls.last)
    assert p["time[gte]"] == "2026-05-20 00:00:00"
    assert p["time[lte]"] == "2026-05-20 23:59:59"
    payload = _json.loads(result[0].text)
    assert payload["since"] == "2026-05-20 00:00:00"
    assert payload["until"] == "2026-05-20 23:59:59"


# ---------------------------------------------------------------- Flap summary


def _make_flap_sequence(
    host: str,
    states: list[int],
    service: str | None = None,
    base_time: int = 1_700_000_000,
) -> list[dict]:
    """Build a chronological list of log entries with alternating states."""
    return [
        {
            "host_name": host,
            "service_description": service or "",
            "state": s,
            "time": base_time + i * 60,
        }
        for i, s in enumerate(states)
    ]


@pytest.mark.asyncio
async def test_flap_summary_basic_service(mocked_server) -> None:
    """Service with 4 transitions is returned; one with only 1 is excluded."""
    import json as _json

    mcp, router = mocked_server
    # alpha/HTTP: OK->CRIT->OK->CRIT->OK = 4 transitions (included)
    # beta/CPU:   OK->CRIT = 1 transition (excluded with min_transitions=3)
    raw = _make_flap_sequence("alpha", [0, 2, 0, 2, 0], service="HTTP") + _make_flap_sequence(
        "beta", [0, 2], service="CPU"
    )
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"since": "-6h", "min_transitions": 3})
    assert route.called
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"
    assert p["time[gte]"] == "-6h"
    assert p["sort"] == "time"

    payload = _json.loads(result[0].text)
    assert payload["since"] == "-6h"
    assert payload["until"] is None
    assert payload["min_transitions"] == 3
    assert payload["total_flapping_objects"] == 1
    r = payload["results"][0]
    assert r["host"] == "alpha"
    assert r["service"] == "HTTP"
    assert r["transition_count"] == 4
    assert "OK" in r["states_seen"]
    assert "CRITICAL" in r["states_seen"]


@pytest.mark.asyncio
async def test_flap_summary_host_level(mocked_server) -> None:
    """Host-level flapping has service=null in the result."""
    import json as _json

    mcp, router = mocked_server
    # Host flapping: UP(0)->DOWN(1)->UP(0)->DOWN(1) = 3 transitions
    raw = _make_flap_sequence("router-01", [0, 1, 0, 1])
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"min_transitions": 3})
    payload = _json.loads(result[0].text)
    assert payload["total_flapping_objects"] == 1
    r = payload["results"][0]
    assert r["service"] is None
    assert r["transition_count"] == 3
    assert "DOWN" in r["states_seen"]
    assert "UP" in r["states_seen"]


@pytest.mark.asyncio
async def test_flap_summary_ranked_by_transitions(mocked_server) -> None:
    """Results are sorted by transition_count descending."""
    import json as _json

    mcp, router = mocked_server
    # svc-A: 4 transitions, svc-B: 6 transitions -> B must be first
    raw = _make_flap_sequence("h", [0, 1, 0, 1, 0], service="svc-A") + _make_flap_sequence(
        "h", [0, 2, 0, 2, 0, 2, 0], service="svc-B"
    )
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"min_transitions": 3})
    payload = _json.loads(result[0].text)
    results = payload["results"]
    assert results[0]["service"] == "svc-B"
    assert results[0]["transition_count"] == 6
    assert results[1]["service"] == "svc-A"


@pytest.mark.asyncio
async def test_flap_summary_no_flapping(mocked_server) -> None:
    """All objects below min_transitions yields empty results."""
    import json as _json

    mcp, router = mocked_server
    raw = _make_flap_sequence("h", [0, 1], service="svc")  # 1 transition only
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"min_transitions": 3})
    payload = _json.loads(result[0].text)
    assert payload["total_flapping_objects"] == 0
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_flap_summary_filter_error(mocked_server) -> None:
    """Invalid filter field returns an error key."""
    import json as _json

    mcp, _router = mocked_server
    result = await mcp.call_tool(
        "thruk_flap_summary",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "ok"}},
    )
    payload = _json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_flap_summary_since_until(mocked_server) -> None:
    """since/until are forwarded as time[gte]/time[lte] and reflected in payload."""
    import json as _json

    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_flap_summary",
        {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59", "min_transitions": 2},
    )
    p = post_params(router.calls.last)
    assert p["time[gte]"] == "2026-05-20 00:00:00"
    assert p["time[lte]"] == "2026-05-20 23:59:59"
    payload = _json.loads(result[0].text)
    assert payload["since"] == "2026-05-20 00:00:00"
    assert payload["until"] == "2026-05-20 23:59:59"
    assert payload["min_transitions"] == 2
