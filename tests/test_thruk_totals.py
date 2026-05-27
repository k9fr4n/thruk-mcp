"""Tests for issue #222: ``thruk_totals`` — compact host+service counts with filter.

Before the fix:
    No compact dashboard-overview tool existed. ``thruk_stats`` returned the full
    ~100-field raw dump from ``/hosts/stats`` + ``/services/stats``, which is
    token-heavy for the common 'how is everything?' question.

After the fix:
    ``thruk_totals`` calls ``/hosts/totals`` (7 fields) and ``/services/totals``
    (9 fields) concurrently and merges them into a 16-field response. Supports
    a structured AND/OR ``filter`` tree on ``hostgroup``, ``servicegroup``
    (services-only) and ``custom_var``.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ok


@pytest.mark.asyncio
async def test_totals_no_filter(mocked_server) -> None:
    """No filter -> both endpoints hit with no scope params, output merged."""
    mcp, router = mocked_server
    host_payload = {
        "total": 120,
        "up": 115,
        "down": 3,
        "unreachable": 1,
        "pending": 1,
        "down_and_unhandled": 2,
        "unreachable_and_unhandled": 0,
    }
    svc_payload = {
        "total": 850,
        "ok": 820,
        "warning": 10,
        "critical": 5,
        "unknown": 3,
        "pending": 12,
        "warning_and_unhandled": 4,
        "critical_and_unhandled": 2,
        "unknown_and_unhandled": 1,
    }
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok(host_payload))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok(svc_payload))

    result = await mcp.call_tool("thruk_totals", {})

    assert r_h.called and r_s.called
    assert "groups[gte]" not in r_h.calls.last.request.url.params
    assert "host_groups[gte]" not in r_s.calls.last.request.url.params
    body = json.loads(result[0].text)
    assert body == {"hosts": host_payload, "services": svc_payload}


@pytest.mark.asyncio
async def test_totals_hostgroup_filter(mocked_server) -> None:
    """hostgroup -> groups[gte] on /hosts/totals, host_groups[gte] on /services/totals."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_totals",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "prod"}},
    )
    assert r_h.calls.last.request.url.params["groups[gte]"] == "prod"
    assert r_s.calls.last.request.url.params["host_groups[gte]"] == "prod"
    assert "host_groups[gte]" not in r_h.calls.last.request.url.params
    assert "groups[gte]" not in r_s.calls.last.request.url.params


@pytest.mark.asyncio
async def test_totals_servicegroup_filter_services_only(mocked_server) -> None:
    """servicegroup -> groups[gte] on /services/totals only; /hosts/totals unscoped."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_totals",
        {"filter": {"type": "leaf", "field": "servicegroup", "op": "eq", "value": "db"}},
    )
    assert r_s.calls.last.request.url.params["groups[gte]"] == "db"
    # /hosts/totals must NOT carry any servicegroup-derived param (it would otherwise
    # produce a stray groups[gte]=db colliding with a hostgroup leaf).
    assert "groups[gte]" not in r_h.calls.last.request.url.params


@pytest.mark.asyncio
async def test_totals_custom_var_filter(mocked_server) -> None:
    """custom_var -> _VARNAME=value forwarded to both endpoints."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_totals",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "ENV", "val": "prod"},
            }
        },
    )
    assert r_h.calls.last.request.url.params["_ENV"] == "prod"
    assert r_s.calls.last.request.url.params["_ENV"] == "prod"


@pytest.mark.asyncio
async def test_totals_combined_hostgroup_and_servicegroup(mocked_server) -> None:
    """AND(hostgroup, servicegroup) -> hostgroup on both; servicegroup only on services."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_totals",
        {
            "filter": {
                "type": "group",
                "operator": "and",
                "conditions": [
                    {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "prod"},
                    {"type": "leaf", "field": "servicegroup", "op": "eq", "value": "db"},
                ],
            }
        },
    )
    hp = r_h.calls.last.request.url.params
    sp = r_s.calls.last.request.url.params
    # /hosts/totals: only hostgroup scope (servicegroup stripped).
    assert hp["groups[gte]"] == "prod"
    # /services/totals: hostgroup -> host_groups, servicegroup -> groups.
    assert sp["host_groups[gte]"] == "prod"
    assert sp["groups[gte]"] == "db"


@pytest.mark.asyncio
async def test_totals_invalid_field_rejected(mocked_server) -> None:
    """Fields outside FIELDS_TOTALS (e.g. ``state``) surface a FilterError to the caller."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    result = await mcp.call_tool(
        "thruk_totals",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "down"}},
    )
    assert "error" in result[0].text
    assert "state" in result[0].text
