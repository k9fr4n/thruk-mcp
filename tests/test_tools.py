"""End-to-end tool routing tests.

For every registered tool we assert that calling it through the FastMCP API
produces the expected HTTP request (method + path) against Thruk.
This is the primary regression guard against URL / param mistakes.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from tests.conftest import agg_rows, flap_side_effect, ok


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


# ---------------------------------------------------------------- issue #179
# thruk_get_host / thruk_get_service must unpack Thruk's per-backend list
# into a single object. Before the fix the raw list was forwarded, breaking
# the "single object" contract advertised by both tools.


@pytest.mark.asyncio
async def test_get_host_unpacks_single_element_list(mocked_server) -> None:
    """Pre-fix: returned [{"name": "srv01"}]. Post-fix: returns the dict."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/srv01").mock(
        return_value=ok([{"name": "srv01", "state": 0}])
    )
    result = await mcp.call_tool("thruk_get_host", {"host": "srv01"})
    payload = json.loads(result[0].text)
    assert isinstance(payload, dict), "single-backend response must be unpacked to a dict"
    assert payload == {"name": "srv01", "state": 0}


@pytest.mark.asyncio
async def test_get_host_empty_list_returns_not_found(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/missing").mock(return_value=ok([]))
    result = await mcp.call_tool("thruk_get_host", {"host": "missing"})
    payload = json.loads(result[0].text)
    assert payload == {"error": "Host 'missing' not found"}


@pytest.mark.asyncio
async def test_get_host_multi_backend_returns_list_with_warning(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/dup").mock(
        return_value=ok(
            [
                {"name": "dup", "peer_name": "A"},
                {"name": "dup", "peer_name": "B"},
            ]
        )
    )
    result = await mcp.call_tool("thruk_get_host", {"host": "dup"})
    payload = json.loads(result[0].text)
    assert isinstance(payload, dict)
    assert payload["data"] == [
        {"name": "dup", "peer_name": "A"},
        {"name": "dup", "peer_name": "B"},
    ]
    assert payload["_warnings"] and "2 backends" in payload["_warnings"][0]


@pytest.mark.asyncio
async def test_get_service_unpacks_single_element_list(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/srv01/ssh").mock(
        return_value=ok([{"description": "ssh", "state": 2}])
    )
    result = await mcp.call_tool("thruk_get_service", {"host": "srv01", "service": "ssh"})
    payload = json.loads(result[0].text)
    assert isinstance(payload, dict)
    assert payload == {"description": "ssh", "state": 2}


@pytest.mark.asyncio
async def test_get_service_empty_list_returns_not_found(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/srv01/nope").mock(return_value=ok([]))
    result = await mcp.call_tool("thruk_get_service", {"host": "srv01", "service": "nope"})
    payload = json.loads(result[0].text)
    assert payload == {"error": "Service 'srv01'/'nope' not found"}


@pytest.mark.asyncio
async def test_get_service_multi_backend_returns_list_with_warning(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/dup/svc").mock(
        return_value=ok(
            [
                {"description": "svc", "peer_name": "A"},
                {"description": "svc", "peer_name": "B"},
            ]
        )
    )
    result = await mcp.call_tool("thruk_get_service", {"host": "dup", "service": "svc"})
    payload = json.loads(result[0].text)
    assert isinstance(payload, dict)
    assert len(payload["data"]) == 2
    assert payload["_warnings"] and "2 backends" in payload["_warnings"][0]


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
async def test_list_contacts(mocked_server) -> None:
    """Issue #172: thruk_list_contacts hits GET /contacts with default columns."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/contacts").mock(
        return_value=ok([{"name": "alice", "email": "alice@example.com"}])
    )
    await mcp.call_tool("thruk_list_contacts", {})
    assert route.called
    p = route.calls.last.request.url.params
    # Default columns must be forwarded
    assert "name" in p["columns"]
    assert "email" in p["columns"]
    assert p["limit"] == "100"
    assert p["sort"] == "name"


@pytest.mark.asyncio
async def test_list_contacts_pagination_and_columns(mocked_server) -> None:
    """Issue #172: pagination + custom columns are forwarded."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/contacts").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_contacts",
        {"limit": 25, "offset": 50, "sort": "-name", "columns": "name,email"},
    )
    assert route.called
    p = route.calls.last.request.url.params
    assert p["limit"] == "25"
    assert p["offset"] == "50"
    assert p["columns"] == "name,email"
    assert p["sort"] == "-name"


@pytest.mark.asyncio
async def test_get_contact(mocked_server) -> None:
    """Issue #173: thruk_get_contact hits GET /contacts/{name} and returns the record."""
    mcp, router = mocked_server
    payload = {
        "name": "alice",
        "alias": "Alice Operator",
        "email": "alice@example.com",
        "pager": "+33...",
        "host_notifications_enabled": 1,
        "service_notifications_enabled": 1,
    }
    route = router.get("https://thruk.test/r/contacts/alice").mock(return_value=ok(payload))
    raw = await mcp.call_tool("thruk_get_contact", {"contact": "alice"})
    assert route.called
    body = json.loads(raw[0].text)
    assert body["name"] == "alice"
    assert body["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_contact_404_raises_thruk_error(mocked_server) -> None:
    """Issue #173: a 404 on an unknown contact surfaces as ThrukError."""
    from thruk_mcp.client import ThrukError

    mcp, router = mocked_server
    router.get("https://thruk.test/r/contacts/ghost").mock(
        return_value=httpx.Response(404, text="contact not found")
    )
    with pytest.raises(ThrukError):
        await mcp.call_tool("thruk_get_contact", {"contact": "ghost"})


@pytest.mark.asyncio
async def test_get_contact_url_escapes_name(mocked_server) -> None:
    """Issue #173: contact names with special chars must be URL-escaped via _seg."""
    mcp, router = mocked_server
    # Space → %20 in the path segment.
    route = router.get("https://thruk.test/r/contacts/alice%20smith").mock(
        return_value=ok({"name": "alice smith"})
    )
    await mcp.call_tool("thruk_get_contact", {"contact": "alice smith"})
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
    """Case 1 (issue #229): no filter, active_only defaults to True.

    Pre-fix, the tool accepted ``host: str | None``; post-fix the bare
    ``host`` param is gone and callers must pass a structured ``filter``.
    Behaviour with no filter is unchanged: ``start_time[lte]`` /
    ``end_time[gte]`` are populated from ``active_only=True``.
    """
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_downtimes", {})
    p = route.calls.last.request.url.params
    assert "host_name" not in p
    assert "host_name[regex]" not in p
    assert "start_time[lte]" in p  # active_only=True default
    assert "end_time[gte]" in p


@pytest.mark.asyncio
async def test_list_downtimes_host_filter_eq(mocked_server) -> None:
    """Case 2 (issue #229): ``host`` leaf is forwarded as ``host_name``.

    Pre-fix, this was the bare ``host="srv01"`` kwarg. Post-fix the same
    intent is expressed as a filter leaf and produces the same query param.
    """
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_downtimes",
        {
            "filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"},
            "active_only": False,
        },
    )
    p = route.calls.last.request.url.params
    assert p["host_name"] == "srv01"
    assert "start_time[lte]" not in p  # active_only=False removes time filter


@pytest.mark.asyncio
async def test_list_downtimes_hostgroup_filter_resolves_via_hosts(mocked_server) -> None:
    """Case 3 (issue #229): ``hostgroup`` leaf triggers a /hosts lookup.

    The ``/downtimes`` endpoint exposes neither ``host_groups`` nor
    custom-variable columns, so hostgroup filters must be resolved by
    fetching the matching host names from ``/hosts`` and applying them
    as ``host_name[regex]=...`` on the downtimes query.
    """
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "srv01"}, {"name": "srv02"}])
    )
    r_dt = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_downtimes",
        {
            "filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "HG_AGILE"},
            "active_only": False,
        },
    )
    # /hosts called with the compiled hostgroup filter
    assert r_hosts.called
    hp = r_hosts.calls.last.request.url.params
    assert hp["groups[gte]"] == "HG_AGILE"
    # /downtimes invoked with the resulting host_name[regex] intersection
    dp = r_dt.calls.last.request.url.params
    assert "host_name[regex]" in dp
    regex = dp["host_name[regex]"]
    assert "srv01" in regex and "srv02" in regex


@pytest.mark.asyncio
async def test_list_downtimes_custom_var_filter_resolves_via_hosts(mocked_server) -> None:
    """Case 4 (issue #229): ``custom_var`` leaf uses the same two-step lookup.

    The ``custom_var`` leaf compiles to the ``_VARNAME=`` syntax on the
    ``/hosts`` query; the resolved host names are then applied on the
    downtimes query as ``host_name[regex]=...``.
    """
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "win01"}]))
    r_dt = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_downtimes",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            },
            "active_only": False,
        },
    )
    assert r_hosts.called
    hp = r_hosts.calls.last.request.url.params
    assert hp["_KERNEL"] == "windows"
    dp = r_dt.calls.last.request.url.params
    assert dp["host_name[regex]"] == "^(win01)$"


@pytest.mark.asyncio
async def test_get_downtime(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/downtimes/42").mock(return_value=ok({}))
    await mcp.call_tool("thruk_get_downtime", {"downtime_id": 42})
    assert route.called


# ---------------------------------------------------------------- issue #199
# thruk_get_downtime must unpack Thruk's per-backend list into a single
# object, matching thruk_get_host / thruk_get_service. Pre-fix the raw list
# was forwarded verbatim, so a caller asking for one downtime received
# ``[{...}]`` instead of ``{...}``.


@pytest.mark.asyncio
async def test_get_downtime_unpacks_single_element_list(mocked_server) -> None:
    """Pre-fix: returned [{"id": 42}]. Post-fix: returns the dict."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes/42").mock(
        return_value=ok([{"id": 42, "host_name": "srv01", "comment": "maint"}])
    )
    result = await mcp.call_tool("thruk_get_downtime", {"downtime_id": 42})
    payload = json.loads(result[0].text)
    assert isinstance(payload, dict), "single-backend response must be unpacked to a dict"
    assert payload == {"id": 42, "host_name": "srv01", "comment": "maint"}


@pytest.mark.asyncio
async def test_get_downtime_empty_list_returns_not_found(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes/99999").mock(return_value=ok([]))
    result = await mcp.call_tool("thruk_get_downtime", {"downtime_id": 99999})
    payload = json.loads(result[0].text)
    assert payload == {"error": "Downtime 99999 not found"}


@pytest.mark.asyncio
async def test_get_downtime_multi_backend_returns_list_with_warning(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes/42").mock(
        return_value=ok(
            [
                {"id": 42, "peer_name": "A"},
                {"id": 42, "peer_name": "B"},
            ]
        )
    )
    result = await mcp.call_tool("thruk_get_downtime", {"downtime_id": 42})
    payload = json.loads(result[0].text)
    assert isinstance(payload, dict)
    assert payload["data"] == [
        {"id": 42, "peer_name": "A"},
        {"id": 42, "peer_name": "B"},
    ]
    assert payload["_warnings"] and "2 backends" in payload["_warnings"][0]


@pytest.mark.asyncio
async def test_list_comments(mocked_server) -> None:
    """Case 1 (issue #230): no filter — bare ``/comments`` call.

    Pre-fix, the tool accepted ``host: str | None``; post-fix the bare
    ``host`` param is gone and callers must pass a structured ``filter``.
    With no filter the request must contain no ``host_name`` param.
    """
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/comments").mock(return_value=ok([]))
    await mcp.call_tool("thruk_list_comments", {})
    assert route.called
    p = route.calls.last.request.url.params
    assert "host_name" not in p
    assert "host_name[regex]" not in p


@pytest.mark.asyncio
async def test_list_comments_host_filter_eq(mocked_server) -> None:
    """Case 2 (issue #230): ``host`` leaf is forwarded as ``host_name``.

    Pre-fix, this was the bare ``host="srv01"`` kwarg. Post-fix the same
    intent is expressed as a filter leaf and produces the same query param.
    """
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/comments").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_comments",
        {"filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"}},
    )
    p = route.calls.last.request.url.params
    assert p["host_name"] == "srv01"


@pytest.mark.asyncio
async def test_list_comments_hostgroup_filter_resolves_via_hosts(mocked_server) -> None:
    """Case 3 (issue #230): ``hostgroup`` leaf triggers a /hosts lookup.

    The ``/comments`` endpoint exposes neither ``host_groups`` nor
    custom-variable columns, so hostgroup filters must be resolved by
    fetching the matching host names from ``/hosts`` and applying them
    as ``host_name[regex]=...`` on the comments query.
    """
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "srv01"}, {"name": "srv02"}])
    )
    r_cm = router.get("https://thruk.test/r/comments").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_comments",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "HG_AGILE"}},
    )
    assert r_hosts.called
    hp = r_hosts.calls.last.request.url.params
    assert hp["groups[gte]"] == "HG_AGILE"
    cp = r_cm.calls.last.request.url.params
    assert "host_name[regex]" in cp
    regex = cp["host_name[regex]"]
    assert "srv01" in regex and "srv02" in regex


@pytest.mark.asyncio
async def test_list_comments_custom_var_filter_resolves_via_hosts(mocked_server) -> None:
    """Case 4 (issue #230): ``custom_var`` leaf uses the same two-step lookup.

    The ``custom_var`` leaf compiles to the ``_VARNAME=`` syntax on the
    ``/hosts`` query; the resolved host names are then applied on the
    comments query as ``host_name[regex]=...``.
    """
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "win01"}]))
    r_cm = router.get("https://thruk.test/r/comments").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_comments",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            },
        },
    )
    assert r_hosts.called
    hp = r_hosts.calls.last.request.url.params
    assert hp["_KERNEL"] == "windows"
    cp = r_cm.calls.last.request.url.params
    assert cp["host_name[regex]"] == "^(win01)$"


@pytest.mark.asyncio
async def test_sites(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/sites").mock(return_value=ok([]))
    await mcp.call_tool("thruk_sites", {})
    assert route.called


# ----------------------------------------------------- Logs / history tools


@pytest.mark.parametrize(
    "tool_name, extra_params",
    [
        ("thruk_list_logs", {}),
        ("thruk_list_alerts", {"type[~]": "^(HOST|SERVICE) ALERT", "class": "1"}),
        ("thruk_list_notifications", {"class": "3"}),
        ("thruk_recent_events", {}),
    ],
)
@pytest.mark.asyncio
async def test_log_family_tool_posts_to_logs(
    mocked_server, tool_name: str, extra_params: dict
) -> None:
    """Each log-family tool must POST to /logs, forward the limit param, and include
    its tool-specific fixed params (e.g. type[~] for alerts, class for notifications).

    Regression guard: a change to log-family routing or fixed-param injection only needs
    to be updated in this single parametrized test rather than in four separate copies.
    """
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(tool_name, {"limit": 10})
    assert route.called, f"{tool_name} must POST to /logs"
    body = parse_qs(route.calls.last.request.content.decode())
    assert body.get("limit") == ["10"], f"{tool_name}: limit param not forwarded"
    for key, val in extra_params.items():
        assert body.get(key) == [val], f"{tool_name}: expected {key!r}={val!r} in POST body"


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
    """state='warning' filter must be translated to state=1 on the /logs POST.
    The type[~]=ALERT fixed param is covered by test_log_family_tool_posts_to_logs."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "warning"}},
    )
    p = post_params(route.calls.last)
    assert p["state"] == "1"  # service warning → numeric 1


@pytest.mark.asyncio
async def test_list_alerts_state_down_narrows_type_to_host_alert(mocked_server) -> None:
    """Regression for issue #198.

    Before the fix, ``state=down`` mapped to integer ``1`` while
    ``type[~]`` remained ``^(HOST|SERVICE) ALERT``.  SERVICE ALERT WARNING
    rows (also ``state=1``) leaked through.  After the fix, the server-side
    ``type[~]`` regex is narrowed to ``^HOST ALERT`` whenever every state
    filter uses host-only names.
    """
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "down"}},
    )
    p = post_params(route.calls.last)
    assert p["state"] == "1"
    assert p["type[~]"] == "^HOST ALERT", (
        "state=down must narrow type[~] to ^HOST ALERT to exclude SERVICE "
        "ALERT WARNING (issue #198)"
    )


@pytest.mark.asyncio
async def test_list_alerts_state_warning_narrows_type_to_service_alert(mocked_server) -> None:
    """state=warning must narrow type[~] to ^SERVICE ALERT (issue #198)."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "warning"}},
    )
    p = post_params(route.calls.last)
    assert p["state"] == "1"
    assert p["type[~]"] == "^SERVICE ALERT"


@pytest.mark.asyncio
async def test_list_alerts_no_state_keeps_combined_type_regex(mocked_server) -> None:
    """No state filter → keep the default ^(HOST|SERVICE) ALERT regex."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {"filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"}},
    )
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"


@pytest.mark.asyncio
async def test_list_alerts_numeric_state_keeps_combined_type_regex(mocked_server) -> None:
    """Integer state value is ambiguous → keep the default regex."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": 1}},
    )
    p = post_params(route.calls.last)
    assert p["state"] == "1"
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"


@pytest.mark.asyncio
async def test_list_alerts_state_in_host_states_narrows_to_host_alert(mocked_server) -> None:
    """op=in with only host-state names must still narrow to ^HOST ALERT."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_alerts",
        {
            "filter": {
                "type": "leaf",
                "field": "state",
                "op": "in",
                "value": ["down", "unreachable"],
            }
        },
    )
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^HOST ALERT"


@pytest.mark.asyncio
async def test_list_alerts_filters_class_zero_system_entries(mocked_server) -> None:
    """Regression for issue #176.

    Before the fix, ``thruk_list_alerts`` only set ``type[~]=^(HOST|SERVICE) ALERT``.
    Naemon Livestatus does not exclude rows where ``type`` is NULL/empty from regex
    filters, so class=0 system messages (e.g. retention auto-save) leaked through
    and were returned to the caller, polluting the alert stream and consuming the
    user-provided ``limit``.

    The fix adds a server-side ``class=1`` filter to the POST body. This test asserts
    the POST contains ``class=1`` so the upstream Thruk/Livestatus query already drops
    the system rows before they reach the tool's pagination window.
    """
    mcp, router = mocked_server
    # Simulate what Thruk would return *if* the server-side filter wasn't applied:
    # a mix of class=0 system rows (type=null) and proper class=1 ALERT rows.
    # With the fix, our POST asks for class=1 so Thruk wouldn't actually return
    # the class=0 rows — but mounting them here proves the assertion bites on the
    # request side regardless of what the mock returns.
    mixed_payload = [
        {"class": 0, "type": None, "host_name": "", "message": "Auto-save completed."},
        {"class": 1, "type": "HOST ALERT", "host_name": "srv01", "state": 1},
        {"class": 1, "type": "SERVICE ALERT", "host_name": "srv01", "state": 2},
    ]
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(mixed_payload))
    await mcp.call_tool("thruk_list_alerts", {"limit": 50})
    assert route.called
    p = post_params(route.calls.last)
    assert p.get("class") == "1", (
        "thruk_list_alerts must POST class=1 to drop class=0 system messages "
        "server-side (issue #176)."
    )
    assert p.get("type[~]") == "^(HOST|SERVICE) ALERT", (
        "Existing type regex filter must still be present as defence-in-depth."
    )


@pytest.mark.asyncio
async def test_list_notifications_with_contact(mocked_server) -> None:
    """contact filter must translate to contact_name= on the /logs POST.
    The class=3 fixed param is covered by test_log_family_tool_posts_to_logs."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_list_notifications",
        {"filter": {"type": "leaf", "field": "contact", "op": "eq", "value": "oncall"}},
    )
    p = post_params(route.calls.last)
    assert p["contact_name"] == "oncall"


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


# ---------------------------------------------- hostgroup defense-in-depth (issue #200)
# Before the fix: a backend that returns a host/service not actually in the
# requested hostgroup would leak straight through the merged response.
# After the fix: such rows are dropped client-side and a _warnings entry is added.


@pytest.mark.asyncio
async def test_problems_hostgroup_leak_is_filtered_out(mocked_server) -> None:
    """A row whose ``groups`` does not contain the requested hostgroup must be dropped."""
    mcp, router = mocked_server
    # h1 legitimately belongs to HG_X; h2 is the leak from a misbehaving backend.
    r_hosts = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [
                {"name": "h1", "state": 1, "groups": ["HG_X", "OTHER"]},
                {"name": "h2", "state": 1, "groups": ["UNRELATED"]},
            ]
        )
    )
    # svc on host-in-group is kept; svc on host-not-in-group is dropped.
    r_svc = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {"host_name": "h1", "description": "cpu", "state": 2, "host_groups": ["HG_X"]},
                {
                    "host_name": "leaked",
                    "description": "disk",
                    "state": 2,
                    "host_groups": ["OTHER"],
                },
            ]
        )
    )

    raw = await mcp.call_tool(
        "thruk_problems",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "HG_X"}},
    )
    payload = json.loads(raw[0].text)

    # Server-side filter still requested (defense-in-depth, not a replacement).
    assert r_hosts.calls.last.request.url.params["groups[gte]"] == "HG_X"
    assert r_svc.calls.last.request.url.params["host_groups[gte]"] == "HG_X"
    # The ``groups`` / ``host_groups`` columns were appended so we can re-validate.
    assert "groups" in r_hosts.calls.last.request.url.params["columns"]
    assert "host_groups" in r_svc.calls.last.request.url.params["columns"]
    # Leaked rows are gone.
    assert [h["name"] for h in payload["hosts"]] == ["h1"]
    assert [s["host_name"] for s in payload["services"]] == ["h1"]
    # Warning surfaced (one host + one service dropped).
    assert any("hostgroup_filter_leak" in w for w in payload["_warnings"])


@pytest.mark.asyncio
async def test_problems_hostgroup_no_leak_no_warning(mocked_server) -> None:
    """Clean response: no row is dropped and no leak warning is appended."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "h1", "state": 1, "groups": ["HG_X"]}])
    )
    router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [{"host_name": "h1", "description": "cpu", "state": 2, "host_groups": ["HG_X"]}]
        )
    )
    raw = await mcp.call_tool(
        "thruk_problems",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "HG_X"}},
    )
    payload = json.loads(raw[0].text)
    assert len(payload["hosts"]) == 1
    assert len(payload["services"]) == 1
    assert "_warnings" not in payload or not any(
        "hostgroup_filter_leak" in w for w in payload.get("_warnings", [])
    )


@pytest.mark.asyncio
async def test_problems_hostgroup_in_op_keeps_any_match(mocked_server) -> None:
    """``op=in`` accepts a row whose groups intersects the requested list."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [
                {"name": "h1", "state": 1, "groups": ["HG_A"]},
                {"name": "h2", "state": 1, "groups": ["HG_B"]},
                {"name": "h3", "state": 1, "groups": ["HG_C"]},  # leak
            ]
        )
    )
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    raw = await mcp.call_tool(
        "thruk_problems",
        {
            "filter": {
                "type": "leaf",
                "field": "hostgroup",
                "op": "in",
                "value": ["HG_A", "HG_B"],
            }
        },
    )
    payload = json.loads(raw[0].text)
    assert sorted(h["name"] for h in payload["hosts"]) == ["h1", "h2"]
    assert any("hostgroup_filter_leak" in w for w in payload["_warnings"])


@pytest.mark.asyncio
async def test_problems_hostgroup_missing_groups_column_treated_as_leak(mocked_server) -> None:
    """If a backend strips the ``groups`` column we conservatively drop the row.

    This protects against silent leaks even when the column is absent —
    matches the "fail closed" intent of the issue #200 fix.
    """
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "h-stripped", "state": 1}])  # no groups key
    )
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    raw = await mcp.call_tool(
        "thruk_problems",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "HG_X"}},
    )
    payload = json.loads(raw[0].text)
    assert payload["hosts"] == []
    assert any("hostgroup_filter_leak" in w for w in payload["_warnings"])


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
    """Returns count=0 and a message when no active downtimes exist.

    ``retry_on_empty=False`` disables the issue #194 Naemon-lag retry so this
    test stays fast and exercises the original control path."""
    import json

    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
    result_raw = await mcp.call_tool(
        "thruk_delete_active_downtimes",
        {"host": "srv01", "retry_on_empty": False},
    )
    result = json.loads(result_raw[0].text)
    assert result["count"] == 0
    assert "No active downtimes found" in result["message"]


@pytest.mark.asyncio
async def test_delete_active_downtimes_retries_when_naemon_lags(mocked_server, monkeypatch) -> None:
    """Regression test for issue #194.

    Naemon processes scheduling commands asynchronously: a downtime created
    via thruk_schedule_downtime may not yet be visible to Livestatus when
    thruk_delete_active_downtimes immediately queries ``/downtimes``. Before
    the fix the first empty result was returned as ``count: 0`` and the
    delete was silently skipped. After the fix the tool retries once with a
    short backoff and then deletes the downtime that has become visible.
    """
    import json as _json

    from thruk_mcp.tools import commands

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    # thruk_delete_active_downtimes moved to tools/commands.py (issue #261);
    # patch the asyncio it actually calls instead of the (now removed) server.asyncio.
    monkeypatch.setattr(commands.asyncio, "sleep", _fake_sleep)

    mcp, router = mocked_server
    # 1st GET -> empty (Naemon hasn't processed the schedule yet).
    # 2nd GET -> downtime now visible.
    router.get("https://thruk.test/r/downtimes").mock(
        side_effect=[
            ok([]),
            ok(
                [
                    {
                        "id": 454995,
                        "service_description": "",
                        "author": "thruk-mcp",
                        "comment": "TEST",
                    }
                ]
            ),
        ]
    )
    del_route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )

    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    result = _json.loads(result_raw[0].text)

    assert sleep_calls == [2.0], "exactly one backoff sleep at the default delay"
    assert del_route.call_count == 1, "the downtime found on retry must be deleted"
    assert result["count"] == 1
    assert result["deleted"][0]["downtime_id"] == 454995
    assert "_warning" not in result


@pytest.mark.asyncio
async def test_delete_active_downtimes_warns_when_still_empty(mocked_server, monkeypatch) -> None:
    """Issue #194 - when the retry also returns empty, surface a structured warning."""
    import json as _json

    from thruk_mcp.tools import commands

    async def _fake_sleep(_delay: float) -> None:
        return None

    # thruk_delete_active_downtimes moved to tools/commands.py (issue #261);
    # patch the asyncio it actually calls instead of the (now removed) server.asyncio.
    monkeypatch.setattr(commands.asyncio, "sleep", _fake_sleep)

    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))

    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    result = _json.loads(result_raw[0].text)

    assert result["count"] == 0
    assert result["deleted"] == []
    assert "_warning" in result
    assert "asynchronously" in result["_warning"]


@pytest.mark.asyncio
async def test_delete_active_downtimes_opt_out_no_retry(mocked_server, monkeypatch) -> None:
    """retry_on_empty=False short-circuits without sleeping / re-querying."""
    import json as _json

    from thruk_mcp.tools import commands

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    # thruk_delete_active_downtimes moved to tools/commands.py (issue #261);
    # patch the asyncio it actually calls instead of the (now removed) server.asyncio.
    monkeypatch.setattr(commands.asyncio, "sleep", _fake_sleep)

    mcp, router = mocked_server
    dt_route = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))

    result_raw = await mcp.call_tool(
        "thruk_delete_active_downtimes",
        {"host": "srv01", "retry_on_empty": False},
    )
    result = _json.loads(result_raw[0].text)

    assert dt_route.call_count == 1, "no retry when retry_on_empty=False"
    assert sleep_calls == []
    assert result["count"] == 0
    # Warning still helpful so callers learn about the lag.
    assert "_warning" in result


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
    """Issue #197: when filtering by host + comment, both host- and
    service-level downtimes whose comment **substring-matches** are deleted
    individually. The exact-match ``DEL_DOWNTIME_BY_HOST_NAME`` system command
    is NOT invoked (it would silently no-op on partial matches)."""
    import json

    mcp, router = mocked_server
    # Issue #196: peer resolver — ambiguous → broadcast fallback.
    router.get("https://thruk.test/r/hosts/srv01").mock(return_value=ok([]))
    # The exact-match system command must NOT be called on the substring path.
    sys_cmd = router.post("https://thruk.test/r/system/cmd/del_downtime_by_host_name").mock(
        return_value=ok({"rc": 0})
    )
    # Two downtimes: one host-level, one service-level — both match "maint".
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 1050, "service_description": "", "comment": "scheduled maint window"},
                {"id": 1051, "service_description": "CPU", "comment": "scheduled maint window"},
            ]
        )
    )
    del_host_route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    del_svc_route = router.post("https://thruk.test/r/services/srv01/CPU/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool(
        "thruk_delete_downtimes_by_filter", {"host": "srv01", "comment": "maint"}
    )
    result = json.loads(result_raw[0].text)
    assert sys_cmd.call_count == 0, "exact-match system cmd must be skipped on substring path"
    assert del_host_route.call_count == 1
    assert del_svc_route.call_count == 1
    assert result["match_mode"] == "substring"
    assert result["host_downtimes_deleted"][0]["downtime_id"] == 1050
    assert result["service_downtimes_deleted"][0]["downtime_id"] == 1051
    assert result["host_downtimes_errors"] == []
    assert result["service_downtimes_errors"] == []


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_resolves_peer_for_host(mocked_server) -> None:
    """Regression test for issue #196.

    Before the fix, calling ``thruk_delete_downtimes_by_filter`` with only a
    ``host=`` argument (no ``backends=`` override) broadcast
    ``DEL_DOWNTIME_BY_HOST_NAME`` to every configured backend — generating
    N-1 useless commands in a federated setup.

    After the fix, the tool first resolves the owning backend via
    ``GET /hosts/{name}?columns=peer_key`` and routes both the system command
    and the host-level enumeration to that peer only.
    """
    import json

    mcp, router = mocked_server
    # Peer resolution: host lives on backend 'wopr-node-01'.
    peer_route = router.get("https://thruk.test/r/hosts/srv01").mock(
        return_value=ok([{"peer_key": "wopr-node-01"}])
    )
    # Issue #197: substring path no longer issues the system command, but
    # peer routing must still apply to all per-id deletes.
    broadcast_cmd = router.post("https://thruk.test/r/system/cmd/del_downtime_by_host_name").mock(
        return_value=ok({"rc": 0})
    )
    scoped_cmd = router.post(
        "https://thruk.test/r/sites/wopr-node-01/system/cmd/del_downtime_by_host_name"
    ).mock(return_value=ok({"rc": 0}))
    # Downtime enumeration targets the resolved peer only.
    router.get("https://thruk.test/r/sites/wopr-node-01/downtimes").mock(
        return_value=ok([{"id": 2042, "service_description": "", "comment": "scheduled maint"}])
    )
    scoped_del_host = router.post(
        "https://thruk.test/r/sites/wopr-node-01/hosts/srv01/cmd/del_downtime"
    ).mock(return_value=ok({"rc": 0}))

    result_raw = await mcp.call_tool(
        "thruk_delete_downtimes_by_filter", {"host": "srv01", "comment": "maint"}
    )
    result = json.loads(result_raw[0].text)

    assert peer_route.called
    assert scoped_del_host.call_count == 1
    # The critical assertion: the broadcast endpoint was NOT hit.
    assert broadcast_cmd.call_count == 0
    # And: on the substring path, the system command must not be issued at all.
    assert scoped_cmd.call_count == 0
    assert result["host_downtimes_deleted"][0]["downtime_id"] == 2042


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_respects_explicit_backends(mocked_server) -> None:
    """Issue #196: when the caller passes ``backends=`` explicitly, the
    tool must honour it verbatim and skip peer resolution. On the substring
    path (issue #197), this means the ``/downtimes`` enumeration is scoped
    to that backend."""
    import json

    mcp, router = mocked_server
    # If the resolver fires, this route would log a call — we assert it does not.
    peer_route = router.get("https://thruk.test/r/hosts/srv01").mock(
        return_value=ok([{"peer_key": "some-other-peer"}])
    )
    scoped_list = router.get("https://thruk.test/r/sites/explicit-peer/downtimes").mock(
        return_value=ok([{"id": 9001, "service_description": "", "comment": "maint window"}])
    )
    scoped_del = router.post(
        "https://thruk.test/r/sites/explicit-peer/hosts/srv01/cmd/del_downtime"
    ).mock(return_value=ok({"rc": 0}))

    result_raw = await mcp.call_tool(
        "thruk_delete_downtimes_by_filter",
        {"host": "srv01", "comment": "maint", "backends": "explicit-peer"},
    )
    result = json.loads(result_raw[0].text)
    assert scoped_list.called
    assert scoped_del.call_count == 1
    assert peer_route.call_count == 0
    assert result["host_downtimes_deleted"][0]["downtime_id"] == 9001


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_substring_comment_match(mocked_server) -> None:
    """Regression test for issue #197.

    Before the fix:
        thruk_delete_downtimes_by_filter(host="X", comment="TEST MCP")
        → sent DEL_DOWNTIME_BY_HOST_NAME with comment="TEST MCP" (exact match)
        → Naemon's external command does exact-string compare on the comment
        → if stored comment was "TEST MCP host_services_downtime",
          ZERO downtimes were deleted, yet the response said
          {"message": "Command successfully submitted"} (silent no-op).

    After the fix: the tool client-side substring-matches on ``comment``
    (case-insensitive) and issues per-id DEL_*_DOWNTIME against the right
    endpoint — so partial matches actually delete the matching downtimes.
    """
    import json

    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/ecrmut-ad-01").mock(return_value=ok([]))
    # Realistic mix: two match (one host-, one service-level), one doesn't.
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {
                    "id": 5001,
                    "service_description": "",
                    "comment": "TEST MCP host_services_downtime",
                },
                {
                    "id": 5002,
                    "service_description": "Disk /",
                    "comment": "test mcp service downtime",  # different case
                },
                {
                    "id": 5003,
                    "service_description": "RAM",
                    "comment": "unrelated maintenance",
                },
            ]
        )
    )
    del_host = router.post("https://thruk.test/r/hosts/ecrmut-ad-01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )
    del_svc = router.post(
        "https://thruk.test/r/services/ecrmut-ad-01/Disk%20%2F/cmd/del_downtime"
    ).mock(return_value=ok({"rc": 0}))
    del_ram = router.post("https://thruk.test/r/services/ecrmut-ad-01/RAM/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )

    result_raw = await mcp.call_tool(
        "thruk_delete_downtimes_by_filter",
        {"host": "ecrmut-ad-01", "comment": "TEST MCP"},
    )
    result = json.loads(result_raw[0].text)

    assert result["match_mode"] == "substring"
    assert result["matched"] == 2
    assert del_host.call_count == 1
    assert del_svc.call_count == 1, "case-insensitive substring match must apply"
    assert del_ram.call_count == 0, "non-matching downtime must NOT be deleted"
    assert {d["downtime_id"] for d in result["host_downtimes_deleted"]} == {5001}
    assert {d["downtime_id"] for d in result["service_downtimes_deleted"]} == {5002}


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_comment_only_keeps_exact_path(mocked_server) -> None:
    """Issue #197: when ``host`` is not provided, the tool keeps the
    ``del_downtime_by_start_time_comment`` system command (no client-side
    fallback available without scanning every downtime). The docstring
    documents this exact-match limitation."""
    mcp, router = mocked_server
    cmd_route = router.post(
        "https://thruk.test/r/system/cmd/del_downtime_by_start_time_comment"
    ).mock(return_value=ok({"rc": 0}))
    await mcp.call_tool(
        "thruk_delete_downtimes_by_filter", {"comment": "ticket-123", "start_time": "1700000000"}
    )
    assert cmd_route.called
    body = cmd_route.calls.last.request.content.decode()
    assert "comment=ticket-123" in body


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
async def test_add_comment_host(mocked_server) -> None:
    """Host comment must POST to add_host_comment with comment_data/author/persistent=1."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/add_host_comment").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_add_comment",
        {"host": "srv01", "comment": "Investigating high load, ETA 30 min"},
    )
    assert route.called, "thruk_add_comment must POST to /hosts/{host}/cmd/add_host_comment"
    body = post_params(route.calls.last)
    assert body["comment_data"] == "Investigating high load, ETA 30 min"
    assert body["comment_author"] == "thruk-mcp"  # default author
    assert body["persistent"] == "1"  # default persistent=True


@pytest.mark.asyncio
async def test_add_comment_service(mocked_server) -> None:
    """Service comment must POST to add_svc_comment under /services/{host}/{svc}/cmd/."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/add_svc_comment").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_add_comment",
        {"host": "srv01", "service": "ssh", "comment": "False positive — upstream degraded"},
    )
    assert route.called, "service comment must POST to /services/{host}/{svc}/cmd/add_svc_comment"
    body = post_params(route.calls.last)
    assert body["comment_data"] == "False positive — upstream degraded"


@pytest.mark.asyncio
async def test_add_comment_author_and_persistent_forwarded(mocked_server) -> None:
    """Custom author and persistent=False must be forwarded verbatim to Thruk payload."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/add_host_comment").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_add_comment",
        {
            "host": "srv01",
            "comment": "transient note",
            "author": "incident-bot",
            "persistent": False,
        },
    )
    body = post_params(route.calls.last)
    assert body["comment_author"] == "incident-bot"
    assert body["persistent"] == "0"


@pytest.mark.asyncio
async def test_delete_comment_host(mocked_server) -> None:
    """Host comment must POST to del_comment with comment_id payload (issue #169)."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_comment").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_delete_comment",
        {"comment_id": 4242, "host": "srv01"},
    )
    assert route.called, "thruk_delete_comment must POST to /hosts/{host}/cmd/del_comment"
    body = post_params(route.calls.last)
    assert body["comment_id"] == "4242"


@pytest.mark.asyncio
async def test_delete_comment_service(mocked_server) -> None:
    """Service comment must POST to del_comment under /services/{host}/{svc}/cmd/."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/del_comment").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_delete_comment",
        {"comment_id": 99, "host": "srv01", "service": "ssh"},
    )
    assert route.called, "service comment must POST to /services/{host}/{svc}/cmd/del_comment"
    body = post_params(route.calls.last)
    assert body["comment_id"] == "99"


@pytest.mark.asyncio
async def test_delete_comment_id_forwarded_as_string(mocked_server) -> None:
    """comment_id arrives as int from MCP and must be serialised as string for Thruk."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_comment").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool(
        "thruk_delete_comment",
        {"comment_id": 1, "host": "srv01"},
    )
    body = post_params(route.calls.last)
    assert body["comment_id"] == "1"
    # Pre-fix reproducer: with no thruk_delete_comment tool, mcp.call_tool would
    # have raised an UnknownToolError instead of reaching this assertion.


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


# ----------------------------------------- Notifications enable/disable


@pytest.mark.asyncio
async def test_notifications_disable_host(mocked_server) -> None:
    """Disabling notifications on a host POSTs to the correct command endpoint."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/disable_host_notifications").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool("thruk_notifications", {"host": "srv01", "enabled": False})
    payload = json.loads(result_raw[0].text)
    assert payload["action"] == "disabled"
    assert payload["target"] == "srv01"
    assert route.called


@pytest.mark.asyncio
async def test_notifications_enable_host(mocked_server) -> None:
    """Enabling notifications on a host POSTs to enable_host_notifications."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/enable_host_notifications").mock(
        return_value=ok({"rc": 0})
    )
    await mcp.call_tool("thruk_notifications", {"host": "srv01", "enabled": True})
    assert route.called


@pytest.mark.asyncio
async def test_notifications_disable_service(mocked_server) -> None:
    """Service-level command; host command must NOT be called."""
    mcp, router = mocked_server
    svc_route = router.post(
        "https://thruk.test/r/services/srv01/ssh/cmd/disable_svc_notifications"
    ).mock(return_value=ok({"rc": 0}))
    host_route = router.post(
        "https://thruk.test/r/hosts/srv01/cmd/disable_host_notifications"
    ).mock(return_value=ok({"rc": 0}))
    result_raw = await mcp.call_tool(
        "thruk_notifications", {"host": "srv01", "service": "ssh", "enabled": False}
    )
    payload = json.loads(result_raw[0].text)
    assert payload["target"] == "srv01/ssh"
    assert svc_route.called
    assert not host_route.called


@pytest.mark.asyncio
async def test_notifications_cascade(mocked_server) -> None:
    """cascade=True triggers host cmd + one cmd per service returned by /hosts/{host}/services."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/srv01/services").mock(
        return_value=ok([{"description": "ssh"}, {"description": "http"}])
    )
    host_route = router.post(
        "https://thruk.test/r/hosts/srv01/cmd/disable_host_notifications"
    ).mock(return_value=ok({"rc": 0}))
    ssh_route = router.post(
        "https://thruk.test/r/services/srv01/ssh/cmd/disable_svc_notifications"
    ).mock(return_value=ok({"rc": 0}))
    http_route = router.post(
        "https://thruk.test/r/services/srv01/http/cmd/disable_svc_notifications"
    ).mock(return_value=ok({"rc": 0}))
    result_raw = await mcp.call_tool(
        "thruk_notifications", {"host": "srv01", "enabled": False, "cascade": True}
    )
    payload = json.loads(result_raw[0].text)
    assert payload["target"] == "srv01 (host + all services)"
    assert host_route.called
    assert ssh_route.called
    assert http_route.called


@pytest.mark.asyncio
async def test_notifications_cascade_ignored_when_service_given(mocked_server) -> None:
    """cascade=True is silently ignored when a service is explicitly specified."""
    mcp, router = mocked_server
    svc_route = router.post(
        "https://thruk.test/r/services/srv01/ssh/cmd/enable_svc_notifications"
    ).mock(return_value=ok({"rc": 0}))
    # /hosts/{host}/services must NOT be called
    svc_list_route = router.get("https://thruk.test/r/hosts/srv01/services").mock(
        return_value=ok([])
    )
    await mcp.call_tool(
        "thruk_notifications",
        {"host": "srv01", "service": "ssh", "enabled": True, "cascade": True},
    )
    assert svc_route.called
    assert not svc_list_route.called


# ---------------------------------------------------- thruk_checks (issue #167)


@pytest.mark.asyncio
async def test_checks_disable_host(mocked_server) -> None:
    """Disabling active checks on a host POSTs to disable_host_checks."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/disable_host_checks").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool("thruk_checks", {"host": "srv01", "enabled": False})
    payload = json.loads(result_raw[0].text)
    assert payload["action"] == "disabled"
    assert payload["target"] == "srv01"
    assert route.called


@pytest.mark.asyncio
async def test_checks_enable_host(mocked_server) -> None:
    """Enabling active checks on a host POSTs to enable_host_checks."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/hosts/srv01/cmd/enable_host_checks").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool("thruk_checks", {"host": "srv01", "enabled": True})
    payload = json.loads(result_raw[0].text)
    assert payload["action"] == "enabled"
    assert route.called


@pytest.mark.asyncio
async def test_checks_disable_service(mocked_server) -> None:
    """Service-level command; host command must NOT be called."""
    mcp, router = mocked_server
    svc_route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/disable_svc_checks").mock(
        return_value=ok({"rc": 0})
    )
    host_route = router.post("https://thruk.test/r/hosts/srv01/cmd/disable_host_checks").mock(
        return_value=ok({"rc": 0})
    )
    result_raw = await mcp.call_tool(
        "thruk_checks", {"host": "srv01", "service": "ssh", "enabled": False}
    )
    payload = json.loads(result_raw[0].text)
    assert payload["target"] == "srv01/ssh"
    assert svc_route.called
    assert not host_route.called


@pytest.mark.asyncio
async def test_checks_cascade(mocked_server) -> None:
    """cascade=True triggers host cmd + one cmd per service returned by /hosts/{host}/services."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/srv01/services").mock(
        return_value=ok([{"description": "ssh"}, {"description": "http"}])
    )
    host_route = router.post("https://thruk.test/r/hosts/srv01/cmd/disable_host_checks").mock(
        return_value=ok({"rc": 0})
    )
    ssh_route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/disable_svc_checks").mock(
        return_value=ok({"rc": 0})
    )
    http_route = router.post(
        "https://thruk.test/r/services/srv01/http/cmd/disable_svc_checks"
    ).mock(return_value=ok({"rc": 0}))
    result_raw = await mcp.call_tool(
        "thruk_checks", {"host": "srv01", "enabled": False, "cascade": True}
    )
    payload = json.loads(result_raw[0].text)
    assert payload["target"] == "srv01 (host + all services)"
    assert host_route.called
    assert ssh_route.called
    assert http_route.called


@pytest.mark.asyncio
async def test_checks_cascade_ignored_when_service_given(mocked_server) -> None:
    """cascade=True is silently ignored when a service is explicitly specified."""
    mcp, router = mocked_server
    svc_route = router.post("https://thruk.test/r/services/srv01/ssh/cmd/enable_svc_checks").mock(
        return_value=ok({"rc": 0})
    )
    # /hosts/{host}/services must NOT be called
    svc_list_route = router.get("https://thruk.test/r/hosts/srv01/services").mock(
        return_value=ok([])
    )
    await mcp.call_tool(
        "thruk_checks",
        {"host": "srv01", "service": "ssh", "enabled": True, "cascade": True},
    )
    assert svc_route.called
    assert not svc_list_route.called


# ---------------------------------------------------- Query escape hatches


@pytest.mark.asyncio
async def test_query_cv_warning_injected(mocked_server) -> None:
    """thruk_query wraps the response in a _warning envelope when q= contains custom_variables."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "h1"}, {"name": "h2"}]))
    result = await mcp.call_tool(
        "thruk_query",
        {"path": "/hosts", "params": {"q": "custom_variables >= 'KERNEL windows'", "limit": 10}},
    )
    payload = json.loads(result[0].text)
    assert "_warning" in payload
    assert "custom_variables" in payload["_warning"]
    assert "data" in payload
    assert len(payload["data"]) == 2  # the actual result is still returned


@pytest.mark.asyncio
async def test_query_no_warning_without_cv(mocked_server) -> None:
    """thruk_query does NOT inject a warning when q= does not mention custom_variables."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "h1"}]))
    result = await mcp.call_tool(
        "thruk_query",
        {"path": "/hosts", "params": {"q": "state = 1", "limit": 5}},
    )
    payload = json.loads(result[0].text)
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
    mcp, router = mocked_server
    raw = [
        _make_log_entry("alpha", 1, 10),  # DOWN
        _make_log_entry("alpha", 1, 20),  # DOWN
        _make_log_entry("alpha", 0, 30),  # UP = recovery, excluded
        _make_log_entry("beta", 1, 40),  # DOWN
    ]
    route = router.post("https://thruk.test/r/logs").mock(
        return_value=ok(agg_rows(raw, ("host_name",)))
    )
    result = await mcp.call_tool("thruk_top_noisy_hosts", {"since": "-6h", "limit": 5})
    assert route.called
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^HOST ALERT"
    assert p["class"] == "1"
    assert p["state[!=]"] == "0"
    assert p["time[gte]"] == "-6h"
    assert p["sort"] == "-cnt"
    assert p["columns"] == "host_name,state,count(*):cnt,min(time):first_t,max(time):last_t"

    payload = json.loads(result[0].text)
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
    mcp, router = mocked_server
    raw = [_make_log_entry("alpha", 0), _make_log_entry("beta", 0)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {})
    payload = json.loads(result[0].text)
    assert payload["total_alerts_in_window"] == 0
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_top_noisy_hosts_limit_respected(mocked_server) -> None:
    """Only ``limit`` hosts are returned even when more are present."""
    mcp, router = mocked_server
    raw = [_make_log_entry(f"host{i}", 1, i) for i in range(20)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(agg_rows(raw, ("host_name",))))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {"limit": 3})
    payload = json.loads(result[0].text)
    assert len(payload["results"]) == 3


@pytest.mark.asyncio
async def test_top_noisy_hosts_filter_error(mocked_server) -> None:
    """Invalid filter field must return an error key."""
    mcp, _router = mocked_server
    result = await mcp.call_tool(
        "thruk_top_noisy_hosts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "down"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_top_noisy_hosts_since_until(mocked_server) -> None:
    """since/until are forwarded as time[gte]/time[lte] and reflected in payload."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_top_noisy_hosts",
        {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59"},
    )
    p = post_params(router.calls.last)
    # Issue #317: absolute ISO bounds are normalised to epoch on the wire so the
    # /logs time filter matches (relative bounds pass through verbatim).
    assert p["time[gte]"] == "1779235200"  # 2026-05-20 00:00:00 UTC
    assert p["time[lte]"] == "1779321599"  # 2026-05-20 23:59:59 UTC
    payload = json.loads(result[0].text)
    assert payload["since"] == "2026-05-20 00:00:00"
    assert payload["until"] == "2026-05-20 23:59:59"


@pytest.mark.asyncio
async def test_top_noisy_hosts_unknown_state_friendly_label(mocked_server) -> None:
    """Issue #245: HOST ALERT rows with state=3 (not in HOST_STATE_STR) must
    render as ``UNKNOWN(3)`` rather than a raw ``"3"`` string.

    Pre-fix behaviour (regression repro):
        ``state_map.get(3, str(3))`` -> ``"3"`` leaked into ``last_state``.
    Post-fix: ``_format_state_label(3, HOST_STATE_STR)`` -> ``"UNKNOWN(3)"``.
    """
    mcp, router = mocked_server
    raw = [
        _make_log_entry("wopr-naemon-05", 3, 10),  # stray host state 3
        _make_log_entry("wopr-naemon-05", 3, 20),
        _make_log_entry("ecrint-ad-03", 1, 30),  # legitimate DOWN
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(agg_rows(raw, ("host_name",))))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {"since": "-7d", "limit": 5})
    payload = json.loads(result[0].text)
    by_host = {r["host"]: r for r in payload["results"]}
    assert by_host["wopr-naemon-05"]["last_state"] == "UNKNOWN(3)"
    # Known host states must still resolve to their symbolic label.
    assert by_host["ecrint-ad-03"]["last_state"] == "DOWN"


@pytest.mark.asyncio
async def test_top_noisy_hosts_ignores_service_alert_leak(mocked_server) -> None:
    """Issue #248 / #312: a SERVICE ALERT row must NOT leak into the host
    aggregation and surface a service-vocabulary ``last_state``.

    The leak is now prevented **server-side**: the aggregation query scopes to
    ``type[~]=^HOST ALERT`` *and* ``class=1`` (genuine HOST ALERT rows only), so
    Thruk never returns the stray SERVICE ALERT row. We assert the request
    carries both scopes and that the simulated server response (host alerts
    only) yields the host vocabulary (DOWN), never the leaked UNKNOWN(3).
    """
    mcp, router = mocked_server
    host_rows = [
        _make_log_entry("fw-01", 1, 100),  # DOWN
        _make_log_entry("fw-01", 1, 200),  # DOWN, newest host alert
    ]
    # Thruk's type[~]=^HOST ALERT + class=1 query never returns the SERVICE
    # ALERT row, so the aggregated response is built from host alerts only.
    route = router.post("https://thruk.test/r/logs").mock(
        return_value=ok(agg_rows(host_rows, ("host_name",)))
    )
    result = await mcp.call_tool("thruk_top_noisy_hosts", {"since": "-7d", "limit": 5})
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^HOST ALERT"
    assert p["class"] == "1"
    payload = json.loads(result[0].text)
    by_host = {r["host"]: r for r in payload["results"]}
    assert by_host["fw-01"]["alert_count"] == 2
    # last_state reflects the host vocabulary (DOWN), never a leaked UNKNOWN(3).
    assert by_host["fw-01"]["last_state"] == "DOWN"
    assert payload["total_alerts_in_window"] == 2


@pytest.mark.asyncio
async def test_top_noisy_services_basic(mocked_server) -> None:
    """Top-noisy-services aggregates by (host, service) and excludes RECOVERY (state=0)."""
    mcp, router = mocked_server
    raw = [
        _make_log_entry("alpha", 2, 10, service="HTTP"),  # CRITICAL
        _make_log_entry("alpha", 1, 20, service="HTTP"),  # WARNING
        _make_log_entry("alpha", 0, 30, service="HTTP"),  # OK = recovery, excluded
        _make_log_entry("alpha", 2, 40, service="DISK"),  # CRITICAL
        _make_log_entry("beta", 1, 50, service="CPU"),  # WARNING
    ]
    route = router.post("https://thruk.test/r/logs").mock(
        return_value=ok(agg_rows(raw, ("host_name", "service_description")))
    )
    result = await mcp.call_tool("thruk_top_noisy_services", {"since": "-12h", "limit": 5})
    assert route.called
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^SERVICE ALERT"
    assert p["class"] == "1"
    assert p["state[!=]"] == "0"
    assert p["time[gte]"] == "-12h"
    assert p["sort"] == "-cnt"
    assert (
        p["columns"]
        == "host_name,service_description,state,count(*):cnt,min(time):first_t,max(time):last_t"
    )

    payload = json.loads(result[0].text)
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
    mcp, _router = mocked_server
    result = await mcp.call_tool(
        "thruk_top_noisy_services",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "warning"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_top_noisy_services_since_until(mocked_server) -> None:
    """since/until are forwarded as time[gte]/time[lte] and reflected in payload."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_top_noisy_services",
        {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59"},
    )
    p = post_params(router.calls.last)
    # Issue #317: absolute ISO bounds are normalised to epoch on the wire so the
    # /logs time filter matches (relative bounds pass through verbatim).
    assert p["time[gte]"] == "1779235200"  # 2026-05-20 00:00:00 UTC
    assert p["time[lte]"] == "1779321599"  # 2026-05-20 23:59:59 UTC
    payload = json.loads(result[0].text)
    assert payload["since"] == "2026-05-20 00:00:00"
    assert payload["until"] == "2026-05-20 23:59:59"


# ---- _aggregate_alerts helper (issue #84) ----
# Tests verifying that the shared aggregation helper used by both
# thruk_top_noisy_hosts and thruk_top_noisy_services produces equivalent
# behaviour to the former inline implementations.


@pytest.mark.asyncio
async def test_aggregate_alerts_helper_host_type_regex(mocked_server) -> None:
    """_aggregate_alerts (via noisy_hosts) must always send type[~]=^HOST ALERT.

    Before the refactor this was set inline; after extraction the helper
    enforces the type regex regardless of what extra_params contains.
    """
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_top_noisy_hosts", {"since": "-1h"})
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^HOST ALERT", "helper must enforce HOST ALERT type regex"


@pytest.mark.asyncio
async def test_aggregate_alerts_helper_service_type_regex(mocked_server) -> None:
    """_aggregate_alerts (via noisy_services) must always send type[~]=^SERVICE ALERT."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_top_noisy_services", {"since": "-1h"})
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^SERVICE ALERT", "helper must enforce SERVICE ALERT type regex"


@pytest.mark.asyncio
async def test_aggregate_alerts_helper_hit_limit_warning(mocked_server) -> None:
    """_aggregate_alerts sets _warning when data reaches _NOISY_MAX_ALERTS entries.

    Regression: before the refactor the cap check used ``len(data) >= _NOISY_MAX_ALERTS``
    inline. The helper must preserve this behaviour.
    """
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    mcp, router = mocked_server
    # Produce exactly _NOISY_MAX_ALERTS entries, all non-recovery (state=1).
    raw = [_make_log_entry(f"h{i % 10}", 1, i) for i in range(_NOISY_MAX_ALERTS)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {})
    payload = json.loads(result[0].text)
    assert "_warning" in payload, "_warning must appear when data hits the hard cap"
    assert str(_NOISY_MAX_ALERTS) in payload["_warning"]


@pytest.mark.asyncio
async def test_aggregate_alerts_helper_below_limit_no_warning(mocked_server) -> None:
    """No _warning key when data is below the hard cap."""
    mcp, router = mocked_server
    raw = [_make_log_entry("alpha", 1, i) for i in range(5)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_top_noisy_hosts", {})
    payload = json.loads(result[0].text)
    assert "_warning" not in payload, "_warning must not appear below the hard cap"


@pytest.mark.asyncio
async def test_aggregate_alerts_helper_state_map_host(mocked_server) -> None:
    """last_state uses HOST_STATES for noisy_hosts (DOWN/UNREACHABLE)."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(
        return_value=ok(agg_rows([_make_log_entry("srv", 1)], ("host_name",)))  # state 1 = DOWN
    )
    result = await mcp.call_tool("thruk_top_noisy_hosts", {})
    payload = json.loads(result[0].text)
    assert payload["results"][0]["last_state"] == "DOWN"


@pytest.mark.asyncio
async def test_aggregate_alerts_helper_state_map_service(mocked_server) -> None:
    """last_state uses SERVICE_STATES for noisy_services (WARNING/CRITICAL/UNKNOWN)."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(
        return_value=ok(
            agg_rows(
                [_make_log_entry("srv", 2, service="HTTP")], ("host_name", "service_description")
            )
        )  # state 2 = CRITICAL
    )
    result = await mcp.call_tool("thruk_top_noisy_services", {})
    payload = json.loads(result[0].text)
    assert payload["results"][0]["last_state"] == "CRITICAL"


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
    mcp, router = mocked_server
    # alpha/HTTP: OK->CRIT->OK->CRIT->OK = 4 transitions (included)
    # beta/CPU:   OK->CRIT = 1 transition (excluded with min_transitions=3)
    raw = _make_flap_sequence("alpha", [0, 2, 0, 2, 0], service="HTTP") + _make_flap_sequence(
        "beta", [0, 2], service="CPU"
    )
    route = router.post("https://thruk.test/r/logs").mock(side_effect=flap_side_effect(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"since": "-6h", "min_transitions": 3})
    assert route.called
    # Two queries: candidate aggregation (sort=-cnt) then the scoped chronological
    # raw fetch (sort=time). The last call is the raw fetch.
    p = post_params(route.calls.last)
    assert p["type[~]"] == "^(HOST|SERVICE) ALERT"
    assert p["time[gte]"] == "-6h"
    assert p["sort"] == "time"
    assert p["columns"] == "host_name,service_description,state,time"

    payload = json.loads(result[0].text)
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
    mcp, router = mocked_server
    # Host flapping: UP(0)->DOWN(1)->UP(0)->DOWN(1) = 3 transitions
    raw = _make_flap_sequence("router-01", [0, 1, 0, 1])
    router.post("https://thruk.test/r/logs").mock(side_effect=flap_side_effect(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"min_transitions": 3})
    payload = json.loads(result[0].text)
    assert payload["total_flapping_objects"] == 1
    r = payload["results"][0]
    assert r["service"] is None
    assert r["transition_count"] == 3
    assert "DOWN" in r["states_seen"]
    assert "UP" in r["states_seen"]


@pytest.mark.asyncio
async def test_flap_summary_ranked_by_transitions(mocked_server) -> None:
    """Results are sorted by transition_count descending."""
    mcp, router = mocked_server
    # svc-A: 4 transitions, svc-B: 6 transitions -> B must be first
    raw = _make_flap_sequence("h", [0, 1, 0, 1, 0], service="svc-A") + _make_flap_sequence(
        "h", [0, 2, 0, 2, 0, 2, 0], service="svc-B"
    )
    router.post("https://thruk.test/r/logs").mock(side_effect=flap_side_effect(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"min_transitions": 3})
    payload = json.loads(result[0].text)
    results = payload["results"]
    assert results[0]["service"] == "svc-B"
    assert results[0]["transition_count"] == 6
    assert results[1]["service"] == "svc-A"


@pytest.mark.asyncio
async def test_flap_summary_no_flapping(mocked_server) -> None:
    """All objects below min_transitions yields empty results."""
    mcp, router = mocked_server
    raw = _make_flap_sequence("h", [0, 1], service="svc")  # 1 transition only
    router.post("https://thruk.test/r/logs").mock(return_value=ok(raw))
    result = await mcp.call_tool("thruk_flap_summary", {"min_transitions": 3})
    payload = json.loads(result[0].text)
    assert payload["total_flapping_objects"] == 0
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_flap_summary_filter_error(mocked_server) -> None:
    """Invalid filter field returns an error key."""
    mcp, _router = mocked_server
    result = await mcp.call_tool(
        "thruk_flap_summary",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "ok"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_flap_summary_since_until(mocked_server) -> None:
    """since/until are forwarded as time[gte]/time[lte] and reflected in payload."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_flap_summary",
        {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59", "min_transitions": 2},
    )
    p = post_params(router.calls.last)
    # Issue #317: absolute ISO bounds are normalised to epoch on the wire so the
    # /logs time filter matches (relative bounds pass through verbatim).
    assert p["time[gte]"] == "1779235200"  # 2026-05-20 00:00:00 UTC
    assert p["time[lte]"] == "1779321599"  # 2026-05-20 23:59:59 UTC
    payload = json.loads(result[0].text)
    assert payload["since"] == "2026-05-20 00:00:00"
    assert payload["until"] == "2026-05-20 23:59:59"
    assert payload["min_transitions"] == 2


# ---------------------------------------------------------------------------
# Issue #142 — paginated /hosts lookup + truncation warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_hosts_paginates_beyond_1000(mocked_server) -> None:
    """_resolve_hosts_to_regex must paginate through all pages, not stop at 1000.

    Bug (before fix): a hard-coded ``limit=1000`` silently truncated any
    hostgroup with >1000 members, so the resulting regex missed those hosts.

    After the fix: get_all() pages through all results and the regex includes
    every returned host name.
    """
    mcp, router = mocked_server

    # Simulate a hostgroup with 1500 hosts: first page returns 500 rows,
    # second page returns 500 rows, third page returns 500 rows (full → keep
    # paging), fourth page returns 0 rows → stop.
    page_a = [{"name": f"host{i:04d}"} for i in range(500)]
    page_b = [{"name": f"host{i:04d}"} for i in range(500, 1000)]
    page_c = [{"name": f"host{i:04d}"} for i in range(1000, 1500)]

    router.get("https://thruk.test/r/hosts").mock(
        side_effect=[
            ok(page_a),
            ok(page_b),
            ok(page_c),
            ok([]),  # final empty page → stop
        ]
    )
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_list_alerts",
        {
            "filter": {
                "type": "leaf",
                "field": "hostgroup",
                "op": "eq",
                "value": "big-group",
            }
        },
    )

    # The logs POST must have carried a regex that includes all 1500 hosts
    log_call = router.calls.last
    body_params = {k: v[0] for k, v in parse_qs(log_call.request.content.decode()).items()}
    regex = body_params.get("host_name[regex]", "")
    assert "host0000" in regex, "First host should appear in regex"
    assert "host1499" in regex, "Last host (page 3) should appear in regex"

    # No truncation warning expected at 1500 hosts (well below 20_000)
    payload = json.loads(result[0].text)
    if isinstance(payload, dict):
        assert "_warning" not in payload or "truncated" not in payload.get("_warning", "")


@pytest.mark.asyncio
async def test_resolve_hosts_truncation_warning_list_alerts(mocked_server) -> None:
    """When the host lookup is truncated a _warning is injected into the result.

    Bug (before fix): no warning was ever emitted because the single-shot
    ``limit=1000`` GET never signalled that results were cut off.

    After the fix: ``_resolve_hosts_to_regex_from_params`` returns
    ``(regex, True)`` when ``len(names) >= hard_limit``, and the caller wraps
    the response in ``{"data": ..., "_warnings": [...]}``.

    We mock ``_resolve_hosts_to_regex_from_params`` directly to return
    ``truncated=True`` without having to generate 20 000 mock host rows.
    """
    from unittest.mock import AsyncMock, patch

    mcp, router = mocked_server

    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    with patch(
        "thruk_mcp.helpers._resolve_hosts_to_regex_from_params",
        new=AsyncMock(return_value=("^(h0|h1|h2)$", True)),
    ):
        result = await mcp.call_tool(
            "thruk_list_alerts",
            {
                "filter": {
                    "type": "leaf",
                    "field": "hostgroup",
                    "op": "eq",
                    "value": "huge-group",
                }
            },
        )

    payload = json.loads(result[0].text)
    assert isinstance(payload, dict), "truncated result must be wrapped in a dict"
    assert "_warnings" in payload, "truncation warning must appear in _warnings"
    assert any("truncated" in w.lower() for w in payload["_warnings"]), (
        f"no truncation warning found in {payload['_warnings']}"
    )


@pytest.mark.asyncio
async def test_resolve_hosts_truncation_warning_top_noisy_hosts(mocked_server) -> None:
    """Truncation warning propagates through _resolve_log_filter into payload-dict tools."""
    from unittest.mock import AsyncMock, patch

    mcp, router = mocked_server

    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    with patch(
        "thruk_mcp.helpers._resolve_hosts_to_regex_from_params",
        new=AsyncMock(return_value=("^(h0|h1|h2)$", True)),
    ):
        result = await mcp.call_tool(
            "thruk_top_noisy_hosts",
            {
                "filter": {
                    "type": "leaf",
                    "field": "hostgroup",
                    "op": "eq",
                    "value": "massive-hg",
                },
                "since": "-1h",
            },
        )

    payload = json.loads(result[0].text)
    assert "_warning" in payload, "truncation warning must appear in _warning"
    assert "truncated" in payload["_warning"].lower()


# ---------------------------------------------------------------- Bulk ack (issue #170)


@pytest.mark.asyncio
async def test_bulk_acknowledge_state_filter_critical(mocked_server) -> None:
    """state='critical' must skip /hosts entirely and ack only service problems.

    Regression for issue #170: a state in {critical,warning,unknown} is
    service-only; querying /hosts in that case would mis-report DOWN hosts
    as 'critical' targets.
    """
    mcp, router = mocked_server
    svc_route = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {"host_name": "srv01", "description": "http", "state": 2},
                {"host_name": "srv02", "description": "ssh", "state": 2},
            ]
        )
    )
    host_route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    ack1 = router.post("https://thruk.test/r/services/srv01/http/cmd/acknowledge_svc_problem").mock(
        return_value=ok({"rc": 0})
    )
    ack2 = router.post("https://thruk.test/r/services/srv02/ssh/cmd/acknowledge_svc_problem").mock(
        return_value=ok({"rc": 0})
    )

    raw = await mcp.call_tool(
        "thruk_bulk_acknowledge",
        {"state": "critical", "comment": "incident-42", "author": "oncall"},
    )
    payload = json.loads(raw[0].text)

    assert svc_route.called
    assert not host_route.called, "state=critical must NOT query /hosts"
    assert ack1.call_count == 1
    assert ack2.call_count == 1
    assert payload["acknowledged"] == 2
    assert payload["failed"] == 0
    assert {t["host"] for t in payload["targets"]} == {"srv01", "srv02"}
    # Verify state was forwarded to /services as the canonical int (2 = CRITICAL).
    assert svc_route.calls.last.request.url.params["state"] == "2"
    # Verify payload keys are the Thruk-canonical ones.
    body = post_params(ack1.calls.last)
    assert body["comment_data"] == "incident-42"
    assert body["comment_author"] == "oncall"
    assert body["sticky_ack"] == "1"
    assert body["send_notification"] == "1"
    assert body["persistent_comment"] == "0"


@pytest.mark.asyncio
async def test_bulk_acknowledge_hostgroup_filter(mocked_server) -> None:
    """hostgroup filter must be forwarded as Livestatus groups[gte] / host_groups[gte]."""
    mcp, router = mocked_server
    host_route = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "h1", "state": 1}])
    )
    svc_route = router.get("https://thruk.test/r/services").mock(
        return_value=ok([{"host_name": "h2", "description": "disk", "state": 2}])
    )
    router.post("https://thruk.test/r/hosts/h1/cmd/acknowledge_host_problem").mock(
        return_value=ok({"rc": 0})
    )
    router.post("https://thruk.test/r/services/h2/disk/cmd/acknowledge_svc_problem").mock(
        return_value=ok({"rc": 0})
    )

    raw = await mcp.call_tool("thruk_bulk_acknowledge", {"hostgroup": "HG_PROD"})
    payload = json.loads(raw[0].text)

    assert host_route.calls.last.request.url.params["groups[gte]"] == "HG_PROD"
    assert svc_route.calls.last.request.url.params["host_groups[gte]"] == "HG_PROD"
    # Default state=None must yield state[gte]=1 on both queries.
    assert host_route.calls.last.request.url.params["state[gte]"] == "1"
    assert svc_route.calls.last.request.url.params["state[gte]"] == "1"
    assert payload["acknowledged"] == 2


@pytest.mark.asyncio
async def test_bulk_acknowledge_hosts_only(mocked_server) -> None:
    """hosts_only=True must skip /services entirely."""
    mcp, router = mocked_server
    host_route = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "down01", "state": 1}])
    )
    svc_route = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    ack = router.post("https://thruk.test/r/hosts/down01/cmd/acknowledge_host_problem").mock(
        return_value=ok({"rc": 0})
    )

    raw = await mcp.call_tool("thruk_bulk_acknowledge", {"hosts_only": True})
    payload = json.loads(raw[0].text)

    assert host_route.called
    assert not svc_route.called, "hosts_only must NOT query /services"
    assert ack.call_count == 1
    assert payload["acknowledged"] == 1
    assert payload["targets"][0] == {"host": "down01", "service": None, "state": "DOWN"}


@pytest.mark.asyncio
async def test_bulk_acknowledge_empty_result_no_ack(mocked_server) -> None:
    """Zero matching problems is informational (not an error) and fires no POST."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    # If a POST happened respx would record it; we don't even register a route.

    raw = await mcp.call_tool("thruk_bulk_acknowledge", {})
    payload = json.loads(raw[0].text)

    assert payload["acknowledged"] == 0
    assert payload["failed"] == 0
    assert payload["targets"] == []
    assert "_warning" in payload
    assert "nothing to acknowledge" in payload["_warning"]


@pytest.mark.asyncio
async def test_bulk_acknowledge_invalid_state_returns_error(mocked_server) -> None:
    """Unknown state strings must produce an error payload, not a Thruk roundtrip."""
    mcp, _router = mocked_server
    # No routes registered: any HTTP call would raise.

    raw = await mcp.call_tool("thruk_bulk_acknowledge", {"state": "bogus"})
    payload = json.loads(raw[0].text)
    assert "error" in payload
    assert "bogus" in payload["error"]


@pytest.mark.asyncio
async def test_bulk_acknowledge_mutually_exclusive_flags(mocked_server) -> None:
    """hosts_only and services_only together is a guard-rail error."""
    mcp, _ = mocked_server
    raw = await mcp.call_tool("thruk_bulk_acknowledge", {"hosts_only": True, "services_only": True})
    payload = json.loads(raw[0].text)
    assert "error" in payload
    assert "mutually exclusive" in payload["error"]


# ---------------------------------------------------------------------------
# Regression: issue #191 — legacy `hours` parameter backward-compat shim
#
# Pre-fix reproduction (would raise TypeError):
#
#     await mcp.call_tool("thruk_top_noisy_hosts", {"hours": 24, "limit": 5})
#     # → TypeError: thruk_top_noisy_hosts() got an unexpected keyword
#     #   argument 'hours'
#
# Fix: each of the three trend tools now accepts `hours: int | None = None`
# and translates it to `since="-{hours}h"` (emitting a DeprecationWarning).
# Schema continues to advertise since/until (per issue #177).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_noisy_hosts_hours_shim_translates_to_since(mocked_server) -> None:
    """Legacy `hours=6` must be accepted and translated to time[gte]=-6h."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    with pytest.warns(DeprecationWarning, match="hours"):
        result = await mcp.call_tool("thruk_top_noisy_hosts", {"hours": 6, "limit": 5})
    assert route.called
    p = post_params(route.calls.last)
    assert p["time[gte]"] == "-6h"
    payload = json.loads(result[0].text)
    assert payload["since"] == "-6h"


@pytest.mark.asyncio
async def test_top_noisy_services_hours_shim_translates_to_since(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    with pytest.warns(DeprecationWarning, match="hours"):
        result = await mcp.call_tool("thruk_top_noisy_services", {"hours": 12})
    assert route.called
    p = post_params(route.calls.last)
    assert p["time[gte]"] == "-12h"
    payload = json.loads(result[0].text)
    assert payload["since"] == "-12h"


@pytest.mark.asyncio
async def test_flap_summary_hours_shim_translates_to_since(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    with pytest.warns(DeprecationWarning, match="hours"):
        result = await mcp.call_tool("thruk_flap_summary", {"hours": 3})
    assert route.called
    p = post_params(route.calls.last)
    assert p["time[gte]"] == "-3h"
    payload = json.loads(result[0].text)
    assert payload["since"] == "-3h"


@pytest.mark.asyncio
async def test_top_noisy_hosts_explicit_since_wins_over_hours(mocked_server) -> None:
    """Explicit non-default `since` must take precedence over deprecated `hours`."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    with pytest.warns(DeprecationWarning):
        await mcp.call_tool("thruk_top_noisy_hosts", {"hours": 6, "since": "-48h"})
    p = post_params(route.calls.last)
    assert p["time[gte]"] == "-48h", "explicit since must win over legacy hours"


@pytest.mark.asyncio
async def test_top_noisy_hosts_invalid_hours_raises_thruk_error(mocked_server) -> None:
    """Non-positive `hours` must raise ThrukError (mapped to tool error by SDK)."""
    from thruk_mcp.client import ThrukError

    mcp, _ = mocked_server
    with pytest.raises(ThrukError, match="positive integer"):
        await mcp.call_tool("thruk_top_noisy_hosts", {"hours": 0})


# ---------------------------------------------------------------------------
# Issue #193 — defence-in-depth ``class=1`` filter on every ALERT-restricted
# /logs query. The issue #176 fix was originally only applied to
# ``thruk_list_alerts``; this regression suite asserts the same server-side
# cut is now POSTed by every sibling tool that filters by
# ``type[~]=^(HOST|SERVICE) ALERT`` (or ``^HOST ALERT``), so class=0 system
# messages, class=5 external commands and class=6 current-state snapshots
# can no longer leak past the regex via Naemon Livestatus' NULL-row quirk.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, args, expected_type_regex",
    [
        ("thruk_flap_summary", {"since": "-6h", "min_transitions": 3}, "^(HOST|SERVICE) ALERT"),
        (
            "thruk_recent_events",
            {"only_alerts": True, "hours": 2},
            "^(HOST|SERVICE) ALERT",
        ),
    ],
)
@pytest.mark.asyncio
async def test_alert_restricted_tools_post_class_one(
    mocked_server, tool_name: str, args: dict, expected_type_regex: str
) -> None:
    """Regression for issue #193 (sibling of #176).

    Before the fix, only ``thruk_list_alerts`` paired ``type[~]`` with a
    server-side ``class=1`` cut. Every other ALERT-restricted tool was
    vulnerable to the same Naemon Livestatus leak (rows with ``type=NULL``
    pass through ``type[~]`` regex filters), inflating downstream counts.
    """
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(tool_name, args)
    assert route.called, f"{tool_name} must POST to /logs"
    p = post_params(route.calls.last)
    assert p.get("type[~]") == expected_type_regex, (
        f"{tool_name}: expected type[~]={expected_type_regex!r}, got {p.get('type[~]')!r}"
    )
    assert p.get("class") == "1", (
        f"{tool_name}: must POST class=1 server-side cut (issue #193, sibling of #176) "
        "to drop class=0 system messages that leak past type[~]."
    )


@pytest.mark.asyncio
async def test_recent_events_without_only_alerts_does_not_force_class(mocked_server) -> None:
    """``thruk_recent_events`` without ``only_alerts`` is a generic log feed
    and must NOT inject ``class=1`` — that would hide notifications, downtimes
    and external commands the user explicitly asked to see."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool("thruk_recent_events", {"hours": 1})
    p = post_params(route.calls.last)
    assert p.get("class") is None, (
        "recent_events(only_alerts=False) must not force class=1 — it is a generic feed."
    )
    assert p.get("type[~]") is None


# ---------------------------------------------------------------------------
# thruk_worker_health — mod-gearman supervision-artefact scan (issue #320)
# ---------------------------------------------------------------------------
from thruk_mcp.tools.triage import _classify_worker_artefact  # noqa: E402


def test_classify_worker_artefact_orphaned_extracts_queue() -> None:
    sig, queue = _classify_worker_artefact(
        "(service check orphaned, is the mod-gearman worker on queue 'service' running?)"
    )
    assert sig == "orphaned"
    assert queue == "service"


def test_classify_worker_artefact_host_queue() -> None:
    sig, queue = _classify_worker_artefact(
        "(host check orphaned, is the mod-gearman worker on queue 'host' running?)"
    )
    assert sig == "orphaned"
    assert queue == "host"


def test_classify_worker_artefact_worker_timeout() -> None:
    sig, queue = _classify_worker_artefact("Host Check Timed Out On Worker host-01")
    assert sig == "worker_timeout"
    assert queue is None


def test_classify_worker_artefact_address_undef() -> None:
    sig, queue = _classify_worker_artefact("check_ping: Invalid hostname/address - undef")
    assert sig == "address_undef"
    assert queue is None


def test_classify_worker_artefact_no_match() -> None:
    sig, queue = _classify_worker_artefact("OK - all good")
    assert sig is None
    assert queue is None


def test_classify_worker_artefact_empty() -> None:
    assert _classify_worker_artefact("") == (None, None)


@pytest.mark.asyncio
async def test_worker_health_routing(mocked_server) -> None:
    """The plugin_output signature filter must be pushed server-side as a single
    quoted q-language regex on both /services and /hosts, and /sites must be read
    for backend connectivity."""
    mcp, router = mocked_server
    r_sites = router.get("https://thruk.test/r/sites").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    r_host = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))

    await mcp.call_tool("thruk_worker_health", {"limit": 123})

    assert r_sites.called and r_svc.called and r_host.called
    expected_q = 'plugin_output ~~ "orphaned|Timed Out On Worker|Invalid hostname/address - undef"'
    for route in (r_svc, r_host):
        params = route.calls.last.request.url.params
        assert params["q"] == expected_q
        assert params["limit"] == "123"
        assert "peer_name" in params["columns"]
        assert "plugin_output" in params["columns"]


@pytest.mark.asyncio
async def test_worker_health_include_hosts_false_skips_hosts(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/sites").mock(return_value=ok([]))
    r_svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    r_host = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))

    await mcp.call_tool("thruk_worker_health", {"include_hosts": False})

    assert r_svc.called
    assert not r_host.called, "include_hosts=False must not query /hosts"


@pytest.mark.asyncio
async def test_worker_health_aggregation(mocked_server) -> None:
    """Artefacts are classified and aggregated per signature, queue and backend;
    /sites disconnection is surfaced; samples and assessment are populated."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/sites").mock(
        return_value=ok(
            [
                {"name": "wopr-node-02", "connected": 1, "status": 0, "last_error": ""},
                {"name": "wopr-dead-01", "connected": 0, "status": 2, "last_error": "timeout"},
            ]
        )
    )
    router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "aprt-rl10-02",
                    "description": "FS_BOOT_INODE",
                    "plugin_output": (
                        "(service check orphaned, is the mod-gearman worker on "
                        "queue 'service' running?)"
                    ),
                    "peer_name": "wopr-node-02",
                    "state": 3,
                },
                {
                    "host_name": "aprt-rl10-02",
                    "description": "PING",
                    "plugin_output": "check_ping: Invalid hostname/address - undef",
                    "peer_name": "wopr-node-02",
                    "state": 2,
                },
                {
                    "host_name": "noise-01",
                    "description": "OK_SVC",
                    "plugin_output": "OK - nothing to see",
                    "peer_name": "wopr-node-02",
                    "state": 0,
                },
            ]
        )
    )
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [
                {
                    "name": "cdsgroupe-brocade2",
                    "plugin_output": "Host Check Timed Out On Worker",
                    "peer_name": "wopr-node-01",
                    "state": 1,
                },
            ]
        )
    )

    result = await mcp.call_tool("thruk_worker_health", {})
    payload = json.loads(result[0].text)

    assert payload["total_artefacts"] == 3
    assert payload["artefact_counts"] == {
        "orphaned": 1,
        "address_undef": 1,
        "worker_timeout": 1,
    }
    assert payload["by_queue"] == {"service": 1}
    assert payload["by_backend"]["wopr-node-02"] == {"orphaned": 1, "address_undef": 1}
    assert payload["by_backend"]["wopr-node-01"] == {"worker_timeout": 1}
    assert payload["backends"]["connected"] == 1
    assert payload["backends"]["disconnected"] == 1
    assert payload["backends"]["disconnected_sites"][0]["name"] == "wopr-dead-01"
    assert len(payload["samples"]) == 3
    assert "supervision artefacts" in payload["assessment"]
    assert "blind spot" in payload["assessment"]


@pytest.mark.asyncio
async def test_worker_health_clean(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/sites").mock(
        return_value=ok([{"name": "s1", "connected": 1, "status": 0}])
    )
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))

    result = await mcp.call_tool("thruk_worker_health", {})
    payload = json.loads(result[0].text)

    assert payload["total_artefacts"] == 0
    assert payload["by_queue"] == {}
    assert payload["assessment"].startswith("No mod-gearman worker artefacts")


# ----------------------------------------------------- thruk_backend_health (#323)


@pytest.mark.asyncio
async def test_backend_health_merges_three_endpoints(mocked_server) -> None:
    """/sites + /lmd/sites + /processinfo are all queried and merged into one
    per-site report; a fresh, fast, connected backend is classified ok."""
    mcp, router = mocked_server
    r_sites = router.get("https://thruk.test/r/sites").mock(
        return_value=ok(
            [
                {
                    "name": "wopr-naemon-01",
                    "id": "abcd1",
                    "section": "Main",
                    "type": "livestatus",
                    "addr": "10.0.0.1:6557",
                    "connected": 1,
                    "status": 0,
                    "last_error": "",
                    "localtime": 1_000_000,
                }
            ]
        )
    )
    r_lmd = router.get("https://thruk.test/r/lmd/sites").mock(
        return_value=ok(
            [
                {
                    "peer_key": "abcd1",
                    "name": "wopr-naemon-01",
                    "response_time": 0.042,
                    "last_online": 999_995,
                    "last_update": 999_995,
                    "queries": 1234,
                    "bytes_send": 5000,
                    "bytes_received": 9000,
                    "idling": 0,
                    "last_error": "",
                }
            ]
        )
    )
    r_proc = router.get("https://thruk.test/r/processinfo").mock(
        return_value=ok(
            [
                {
                    "peer_key": "abcd1",
                    "peer_name": "wopr-naemon-01",
                    "program_start": 990_000,
                    "program_version": "1.4.3",
                    "accept_passive_host_checks": 1,
                    "accept_passive_service_checks": 1,
                    "cached": 0,
                }
            ]
        )
    )

    result = await mcp.call_tool("thruk_backend_health", {})
    payload = json.loads(result[0].text)

    assert r_sites.called and r_lmd.called and r_proc.called
    assert payload["lmd_available"] is True
    assert payload["processinfo_available"] is True
    assert payload["summary"] == {"total": 1, "ok": 1, "degraded": 0, "disconnected": 0}
    site = payload["sites"][0]
    assert site["name"] == "wopr-naemon-01"
    assert site["health"] == "ok"
    assert site["latency_seconds"] == 0.042
    assert site["data_age_seconds"] == 5  # localtime - last_update
    assert site["queries"] == 1234
    assert site["accept_passive_host_checks"] is True
    assert "program_start" in site
    assert payload["assessment"].startswith("All 1 backend(s)")


@pytest.mark.asyncio
async def test_backend_health_disconnected_is_blind_spot(mocked_server) -> None:
    """A backend with connected=0 / non-OK status is classified disconnected and
    carries its raw last_error; the assessment names the blind spot."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/sites").mock(
        return_value=ok(
            [
                {"name": "wopr-naemon-01", "id": "a1", "connected": 1, "status": 0},
                {
                    "name": "wopr-naemon-04",
                    "id": "a4",
                    "connected": 0,
                    "status": 2,
                    "last_error": "i/o timeout on 10.0.0.4:6557",
                },
            ]
        )
    )
    router.get("https://thruk.test/r/lmd/sites").mock(return_value=ok([]))
    router.get("https://thruk.test/r/processinfo").mock(return_value=ok([]))

    result = await mcp.call_tool("thruk_backend_health", {})
    payload = json.loads(result[0].text)

    assert payload["summary"]["disconnected"] == 1
    assert payload["disconnected_sites"][0]["name"] == "wopr-naemon-04"
    assert "i/o timeout" in payload["disconnected_sites"][0]["last_error"]
    # worst-first ordering: the disconnected site sorts ahead of the ok one.
    assert payload["sites"][0]["name"] == "wopr-naemon-04"
    assert payload["sites"][0]["health"] == "disconnected"
    assert "blind spot" in payload["assessment"]


@pytest.mark.asyncio
async def test_backend_health_degraded_on_latency_and_lag(mocked_server) -> None:
    """A connected backend over the latency / freshness thresholds is degraded
    with explanatory reasons; thresholds are honoured."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/sites").mock(
        return_value=ok(
            [{"name": "slow-01", "id": "s1", "connected": 1, "status": 0, "localtime": 2_000_000}]
        )
    )
    router.get("https://thruk.test/r/lmd/sites").mock(
        return_value=ok([{"peer_key": "s1", "response_time": 12.5, "last_update": 1_999_000}])
    )
    router.get("https://thruk.test/r/processinfo").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_backend_health", {"latency_warn_seconds": 5.0, "lag_warn_seconds": 120}
    )
    payload = json.loads(result[0].text)

    site = payload["sites"][0]
    assert site["health"] == "degraded"
    assert site["latency_seconds"] == 12.5
    assert site["data_age_seconds"] == 1000
    reasons = " ".join(site["reasons"])
    assert "latency" in reasons and "data age" in reasons
    assert payload["summary"]["degraded"] == 1
    assert "slow-01" in payload["degraded_sites"]


@pytest.mark.asyncio
async def test_backend_health_graceful_degradation_no_lmd(mocked_server) -> None:
    """When /lmd/sites and /processinfo are absent (404), the tool still returns a
    connectivity-only report instead of failing."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/sites").mock(
        return_value=ok([{"name": "n1", "id": "n1", "connected": 1, "status": 0}])
    )
    router.get("https://thruk.test/r/lmd/sites").mock(return_value=httpx.Response(404))
    router.get("https://thruk.test/r/processinfo").mock(return_value=httpx.Response(404))

    result = await mcp.call_tool("thruk_backend_health", {})
    payload = json.loads(result[0].text)

    assert payload["lmd_available"] is False
    assert payload["processinfo_available"] is False
    site = payload["sites"][0]
    assert site["health"] == "ok"
    assert "latency_seconds" not in site  # no metric source available
    assert "connectivity-only" in payload["assessment"]


@pytest.mark.asyncio
async def test_backend_health_surfaces_sites_error(mocked_server) -> None:
    """If the mandatory /sites call itself fails, the ThrukError surfaces (the
    tool does not swallow it)."""
    from thruk_mcp.client import ThrukError

    mcp, router = mocked_server
    # 404 (not a retry status) so the failure surfaces immediately, deterministically.
    router.get("https://thruk.test/r/sites").mock(return_value=httpx.Response(404))
    router.get("https://thruk.test/r/lmd/sites").mock(return_value=ok([]))
    router.get("https://thruk.test/r/processinfo").mock(return_value=ok([]))

    with pytest.raises(ThrukError):
        await mcp.call_tool("thruk_backend_health", {})
