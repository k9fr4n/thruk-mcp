"""Tests for issue #221: filter support on ``thruk_stats``.

Before the fix:
    ``thruk_stats`` had only a ``backends`` arg. There was no way to scope
    ``/hosts/stats`` and ``/services/stats`` to a hostgroup or custom
    variable, so the global ~50-field dump was always returned.

After the fix:
    A structured AND/OR ``filter`` tree (same shape as on ``thruk_list_hosts``)
    is compiled twice -- ``context='hosts'`` for ``/hosts/stats`` (yields
    ``groups[gte]=``) and ``context='services'`` for ``/services/stats``
    (yields ``host_groups[gte]=``). ``custom_var`` translates to ``_VARNAME``
    on both endpoints. Output shape is unchanged.
"""

from __future__ import annotations

import pytest

from tests.conftest import ok


@pytest.mark.asyncio
async def test_stats_no_filter_unchanged(mocked_server) -> None:
    """No filter -> both endpoints called with no scope params (back-compat)."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/stats").mock(return_value=ok({}))
    await mcp.call_tool("thruk_stats", {})
    assert r_h.called and r_s.called
    assert "groups[gte]" not in r_h.calls.last.request.url.params
    assert "host_groups[gte]" not in r_s.calls.last.request.url.params


@pytest.mark.asyncio
async def test_stats_hostgroup_filter(mocked_server) -> None:
    """hostgroup leaf -> groups[gte] on /hosts/stats, host_groups[gte] on /services/stats."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/stats").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_stats",
        {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "linux"}},
    )
    assert r_h.calls.last.request.url.params["groups[gte]"] == "linux"
    assert r_s.calls.last.request.url.params["host_groups[gte]"] == "linux"
    assert "host_groups[gte]" not in r_h.calls.last.request.url.params
    assert "groups[gte]" not in r_s.calls.last.request.url.params


@pytest.mark.asyncio
async def test_stats_custom_var_filter(mocked_server) -> None:
    """custom_var leaf -> _VARNAME on /hosts/stats, _HOSTVARNAME on /services/stats.

    Issue #244 regression: pre-fix the services sub-query was sent
    ``_ENV=prod`` and silently matched nothing (services have host-level
    cvs under the ``host_custom_variables`` column = ``_HOST{VAR}``).
    """
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/stats").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_stats",
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
    assert r_s.calls.last.request.url.params["_HOSTENV"] == "prod"
    # Regression guard for issue #244: the buggy _ENV must NOT leak through
    # to /services/stats (it would silently match the empty set there).
    assert "_ENV" not in r_s.calls.last.request.url.params


@pytest.mark.asyncio
async def test_stats_servicegroup_field_rejected(mocked_server) -> None:
    """servicegroup is intentionally excluded from FIELDS_HOST_STATS."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({}))
    router.get("https://thruk.test/r/services/stats").mock(return_value=ok({}))
    result = await mcp.call_tool(
        "thruk_stats",
        {"filter": {"type": "leaf", "field": "servicegroup", "op": "eq", "value": "db"}},
    )
    assert "error" in result[0].text
    assert "servicegroup" in result[0].text


@pytest.mark.asyncio
async def test_stats_combined_and_filter(mocked_server) -> None:
    """AND combination of hostgroup + custom_var compiles for both contexts."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({}))
    r_s = router.get("https://thruk.test/r/services/stats").mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_stats",
        {
            "filter": {
                "type": "group",
                "operator": "and",
                "conditions": [
                    {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "linux"},
                    {
                        "type": "leaf",
                        "field": "custom_var",
                        "op": "eq",
                        "value": {"var": "ENV", "val": "prod"},
                    },
                ],
            }
        },
    )
    hp = r_h.calls.last.request.url.params
    sp = r_s.calls.last.request.url.params
    assert hp["groups[gte]"] == "linux" and hp["_ENV"] == "prod"
    # Issue #244: host-level cv → _HOST{VAR} on /services/stats.
    assert sp["host_groups[gte]"] == "linux" and sp["_HOSTENV"] == "prod"
    assert "_ENV" not in sp
