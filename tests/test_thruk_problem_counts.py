"""Tests for issue #223: ``thruk_problem_counts`` — generic, filterable problem aggregate.

Before the fix:
    ``thruk_problems_by_hostgroup`` was the only tool returning "how many
    problems by group?" data. It always called ``/hostgroups``, had no filter
    support, silently dropped groups with zero problems, and encoded the
    grouping strategy in the tool name.

After the fix:
    ``thruk_problems_by_hostgroup`` is removed. ``thruk_problem_counts``
    replaces it: ``/hosts/totals`` + ``/services/totals`` called concurrently,
    projected down to the non-OK / non-pending fields only, with an optional
    structured ``filter`` (same contract as ``thruk_totals``).

    Repro of the old bug (now removed):
        # await mcp.call_tool("thruk_problems_by_hostgroup", {})  # -> KeyError now
        # could not be scoped to a single hostgroup, custom_var, or servicegroup.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ok

_HOST_PROBLEM_KEYS = (
    "down",
    "unreachable",
    "down_and_unhandled",
    "unreachable_and_unhandled",
)
_SVC_PROBLEM_KEYS = (
    "warning",
    "critical",
    "unknown",
    "warning_and_unhandled",
    "critical_and_unhandled",
    "unknown_and_unhandled",
)


@pytest.mark.asyncio
async def test_problem_counts_no_filter(mocked_server) -> None:
    """No filter -> both endpoints hit unscoped, problem-state subset returned."""
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

    result = await mcp.call_tool("thruk_problem_counts", {})

    assert r_h.called and r_s.called
    assert "groups[gte]" not in r_h.calls.last.request.url.params
    assert "host_groups[gte]" not in r_s.calls.last.request.url.params

    body = json.loads(result[0].text)
    # Only problem-state fields are surfaced — `total`, `up`, `ok`, `pending`
    # must be filtered out so the response is a stable flat aggregate.
    assert set(body) == {"hosts", "services"}
    assert set(body["hosts"]) == set(_HOST_PROBLEM_KEYS)
    assert set(body["services"]) == set(_SVC_PROBLEM_KEYS)
    assert body["hosts"]["down"] == 3
    assert body["hosts"]["unreachable"] == 1
    assert body["hosts"]["down_and_unhandled"] == 2
    assert body["services"]["critical"] == 5
    assert body["services"]["warning_and_unhandled"] == 4


@pytest.mark.asyncio
async def test_problem_counts_missing_keys_default_to_zero(mocked_server) -> None:
    """Thruk omits zero-valued fields — the projection must still include them as 0."""
    mcp, router = mocked_server
    # Only `down` reported; the other three host problem keys are absent.
    router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({"down": 2}))
    router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))

    result = await mcp.call_tool("thruk_problem_counts", {})
    body = json.loads(result[0].text)

    assert body["hosts"]["down"] == 2
    assert body["hosts"]["unreachable"] == 0
    assert body["hosts"]["down_and_unhandled"] == 0
    assert body["hosts"]["unreachable_and_unhandled"] == 0
    assert all(body["services"][k] == 0 for k in _SVC_PROBLEM_KEYS)


@pytest.mark.asyncio
async def test_problem_counts_hostgroup_filter(mocked_server) -> None:
    """hostgroup -> groups[gte] on /hosts/totals, host_groups[gte] on /services/totals."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_problem_counts",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "prod"}},
    )
    assert r_h.calls.last.request.url.params["groups[gte]"] == "prod"
    assert r_s.calls.last.request.url.params["host_groups[gte]"] == "prod"
    assert "host_groups[gte]" not in r_h.calls.last.request.url.params
    assert "groups[gte]" not in r_s.calls.last.request.url.params


@pytest.mark.asyncio
async def test_problem_counts_custom_var_filter(mocked_server) -> None:
    """custom_var -> _VARNAME=value forwarded to BOTH endpoints."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_problem_counts",
        {
            "filter": {
                "type": "leaf",
                "field": "custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            }
        },
    )
    assert r_h.calls.last.request.url.params["_KERNEL"] == "windows"
    assert r_s.calls.last.request.url.params["_KERNEL"] == "windows"


@pytest.mark.asyncio
async def test_problem_counts_servicegroup_filter_services_only(mocked_server) -> None:
    """servicegroup -> only /services/totals scoped; /hosts/totals unscoped (stripped)."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_problem_counts",
        {"filter": {"type": "leaf", "field": "servicegroup", "op": "eq", "value": "db"}},
    )
    assert r_s.calls.last.request.url.params["groups[gte]"] == "db"
    assert "groups[gte]" not in r_h.calls.last.request.url.params


@pytest.mark.asyncio
async def test_problem_counts_invalid_field_rejected(mocked_server) -> None:
    """Filter fields outside FIELDS_PROBLEM_COUNTS surface a FilterError."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/totals").mock(return_value=ok({}))
    router.get("https://thruk.test/r/services/totals").mock(return_value=ok({}))
    result = await mcp.call_tool(
        "thruk_problem_counts",
        {"filter": {"type": "leaf", "field": "state", "op": "eq", "value": "down"}},
    )
    assert "error" in result[0].text
    assert "state" in result[0].text
