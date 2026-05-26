"""Tests for asyncio.gather() concurrent-request refactor (issue #75).

Before the fix, five functions issued two HTTP GET requests sequentially:

    # Before (sequential - one extra round-trip of latency)
    hosts = await _get_client().get("/hosts/stats", backends=be)
    services = await _get_client().get("/services/stats", backends=be)

After the fix they run concurrently:

    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/stats", backends=be),
        _get_client().get("/services/stats", backends=be),
    )

These tests verify that both HTTP routes are hit and that the gathered
results are correctly unpacked into the response payload for each of the
five refactored functions.
"""

from __future__ import annotations

import json
import time

import pytest
from pydantic import AnyUrl

from tests.conftest import ok

# ---------------------------------------------------------------------------
# thruk_stats - gather refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thruk_stats_gather_both_routes_called(mocked_server) -> None:
    """Both /hosts/stats and /services/stats are fetched; results merged correctly.

    Regression guard: before #75 a single await chain would still call both
    routes but only the second response would be available if they raced.
    With gather() both coroutines run and results are tuple-unpacked in order.
    """
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts/stats").mock(
        return_value=ok({"up": 10, "down": 2})
    )
    r_services = router.get("https://thruk.test/r/services/stats").mock(
        return_value=ok({"ok": 80, "critical": 3})
    )

    result = await mcp.call_tool("thruk_stats", {})

    # Both routes must have been called exactly once
    assert r_hosts.call_count == 1, "hosts/stats route must be called"
    assert r_services.call_count == 1, "services/stats route must be called"

    payload = json.loads(result[0].text)
    assert payload["hosts"] == {"up": 10, "down": 2}
    assert payload["services"] == {"ok": 80, "critical": 3}


@pytest.mark.asyncio
async def test_thruk_stats_gather_response_order(mocked_server) -> None:
    """Hosts data lands in 'hosts' key and services data in 'services' key.

    With gather(hosts_coro, services_coro) the tuple is (hosts, services)
    in declaration order, regardless of which coroutine resolves first.
    """
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({"source": "hosts"}))
    router.get("https://thruk.test/r/services/stats").mock(return_value=ok({"source": "services"}))

    result = await mcp.call_tool("thruk_stats", {})
    payload = json.loads(result[0].text)
    assert payload["hosts"]["source"] == "hosts"
    assert payload["services"]["source"] == "services"


# ---------------------------------------------------------------------------
# thruk_oldest_problems - gather refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oldest_problems_gather_both_routes_called(mocked_server) -> None:
    """Both /hosts and /services are requested concurrently; results merged.

    Before #75 the second await would not start until the first completed.
    With gather() both fire immediately, halving the round-trip latency.
    """
    mcp, router = mocked_server
    now = int(time.time())
    r_hosts = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [
                {
                    "name": "web01",
                    "state": 1,
                    "last_state_change": now - 3600,
                    "peer_name": "local",
                }
            ]
        )
    )
    r_services = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "db01",
                    "description": "mysql",
                    "state": 2,
                    "last_state_change": now - 600,
                    "peer_name": "local",
                }
            ]
        )
    )

    result = await mcp.call_tool("thruk_oldest_problems", {"limit": 10})

    assert r_hosts.call_count == 1, "/hosts route must be called once"
    assert r_services.call_count == 1, "/services route must be called once"

    payload = json.loads(result[0].text)
    assert len(payload) == 2
    # oldest first: web01 (1h ago) before db01/mysql (10m ago)
    assert payload[0]["host"] == "web01"
    assert payload[0]["service"] is None
    assert payload[1]["host"] == "db01"
    assert payload[1]["service"] == "mysql"


# ---------------------------------------------------------------------------
# thruk_unacked_critical - gather refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unacked_critical_gather_both_routes_called(mocked_server) -> None:
    """Both /hosts and /services are requested concurrently.

    Sequential implementation: second request starts only after first finishes.
    Gather implementation: both fire simultaneously.  respx intercepts both.
    """
    mcp, router = mocked_server
    now = int(time.time())
    long_ago = now - 7200  # 120 min

    r_hosts = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [{"name": "db02", "state": 1, "last_state_change": long_ago, "peer_name": "local"}]
        )
    )
    r_services = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "web02",
                    "description": "nginx",
                    "state": 2,
                    "last_state_change": long_ago,
                    "peer_name": "local",
                }
            ]
        )
    )

    result = await mcp.call_tool("thruk_unacked_critical", {"threshold_minutes": 60})

    assert r_hosts.call_count == 1, "/hosts route must be called once"
    assert r_services.call_count == 1, "/services route must be called once"

    payload = json.loads(result[0].text)
    assert len(payload) == 2
    hosts_in_result = [r for r in payload if r["service"] is None]
    services_in_result = [r for r in payload if r["service"] is not None]
    assert any(r["host"] == "db02" for r in hosts_in_result)
    assert any(r["service"] == "nginx" for r in services_in_result)


# ---------------------------------------------------------------------------
# _problems_resource - gather refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_problems_resource_gather_both_routes_called(mocked_server) -> None:
    """thruk://problems resource hits /hosts and /services concurrently."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "h1"}]))
    r_services = router.get("https://thruk.test/r/services").mock(
        return_value=ok([{"description": "svc1"}])
    )

    contents = await mcp.read_resource(AnyUrl("thruk://problems"))

    assert r_hosts.call_count == 1, "/hosts route must be called once"
    assert r_services.call_count == 1, "/services route must be called once"

    payload = json.loads(next(iter(contents)).content)
    assert payload["hosts"] == [{"name": "h1"}]
    assert payload["services"] == [{"description": "svc1"}]


# ---------------------------------------------------------------------------
# _stats_resource - gather refactor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_resource_gather_both_routes_called(mocked_server) -> None:
    """thruk://stats resource hits /hosts/stats and /services/stats concurrently."""
    mcp, router = mocked_server
    r_hosts = router.get("https://thruk.test/r/hosts/stats").mock(return_value=ok({"up": 7}))
    r_services = router.get("https://thruk.test/r/services/stats").mock(return_value=ok({"ok": 70}))

    contents = await mcp.read_resource(AnyUrl("thruk://stats"))

    assert r_hosts.call_count == 1, "/hosts/stats route must be called once"
    assert r_services.call_count == 1, "/services/stats route must be called once"

    payload = json.loads(next(iter(contents)).content)
    assert payload["hosts"] == {"up": 7}
    assert payload["services"] == {"ok": 70}


# ---------------------------------------------------------------------------
# thruk_delete_active_downtimes - gather refactor (#141)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_active_downtimes_gather_all_ids_fired(mocked_server) -> None:
    """All N per-id DEL requests fire concurrently via asyncio.gather (#141).

    Before #141: sequential ``for dt in downtimes:`` loop — N requests serialised.
    After #141:  ``asyncio.gather(*(_del_one(i) for i in ids))`` — all N start at once.

    Verified by asserting that 3 downtime IDs each trigger exactly one POST to
    the del_downtime endpoint (call_count == 3) and that partial errors on one
    id do not abort the remaining deletions (isolation guarantee).
    """
    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 10, "service_description": "", "author": "a", "comment": "c1"},
                {"id": 20, "service_description": "", "author": "b", "comment": "c2"},
                {"id": 30, "service_description": "", "author": "c", "comment": "c3"},
            ]
        )
    )
    del_route = router.post("https://thruk.test/r/hosts/srv01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )

    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "srv01"})
    result = json.loads(result_raw[0].text)

    # All 3 DEL requests must have been submitted (not just the first N-1).
    assert del_route.call_count == 3, "gather must fire all 3 DEL requests"
    assert result["count"] == 3
    assert result["errors"] == []
    deleted_ids = {d["downtime_id"] for d in result["deleted"]}
    assert deleted_ids == {10, 20, 30}


@pytest.mark.asyncio
async def test_delete_active_downtimes_gather_error_isolation(mocked_server) -> None:
    """A ThrukError on one id must not abort the others (gather isolation).

    Before #141: a raised exception inside the for-loop body would propagate
    and abort subsequent iterations.  With asyncio.gather each coroutine wraps
    the call in try/except ThrukError, so errors are collected as values and the
    remaining coroutines are unaffected.
    """
    import httpx

    mcp, router = mocked_server
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 11, "service_description": "", "author": "a", "comment": "ok"},
                {"id": 22, "service_description": "", "author": "b", "comment": "fail"},
                {"id": 33, "service_description": "", "author": "c", "comment": "ok"},
            ]
        )
    )
    # id=11 and id=33 succeed; id=22 returns 403 (ThrukError).
    router.post("https://thruk.test/r/hosts/h1/cmd/del_downtime").mock(
        side_effect=[
            ok({"rc": 0}),
            httpx.Response(403, text="Forbidden"),
            ok({"rc": 0}),
        ]
    )

    result_raw = await mcp.call_tool("thruk_delete_active_downtimes", {"host": "h1"})
    result = json.loads(result_raw[0].text)

    assert result["count"] == 2, "two ids must succeed despite one error"
    assert len(result["errors"]) == 1, "exactly one error expected"


# ---------------------------------------------------------------------------
# thruk_delete_downtimes_by_filter host-level loop - gather refactor (#141)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_downtimes_by_filter_host_gather_all_ids_fired(mocked_server) -> None:
    """Host-level DEL requests in thruk_delete_downtimes_by_filter are parallelised.

    Before #141: ``for dt in host_dts:`` was sequential.
    After #141:  ``asyncio.gather`` fires all host-level DEL requests at once.
    """
    mcp, router = mocked_server
    # Issue #196: peer resolver returns empty → broadcast fallback (preserves
    # the parallelism property this test asserts).
    router.get("https://thruk.test/r/hosts/web01").mock(return_value=ok([]))
    router.post("https://thruk.test/r/system/cmd/del_downtime_by_host_name").mock(
        return_value=ok({"rc": 0})
    )
    # Three host-level downtimes returned by the enumeration GET.
    router.get("https://thruk.test/r/downtimes").mock(
        return_value=ok(
            [
                {"id": 100, "service_description": "", "comment": "maint"},
                {"id": 200, "service_description": "", "comment": "maint"},
                {"id": 300, "service_description": "", "comment": "maint"},
            ]
        )
    )
    del_route = router.post("https://thruk.test/r/hosts/web01/cmd/del_downtime").mock(
        return_value=ok({"rc": 0})
    )

    result_raw = await mcp.call_tool(
        "thruk_delete_downtimes_by_filter", {"host": "web01", "comment": "maint"}
    )
    result = json.loads(result_raw[0].text)

    assert del_route.call_count == 3, "gather must fire all 3 host-level DEL requests"
    deleted_ids = {d["downtime_id"] for d in result["host_downtimes_deleted"]}
    assert deleted_ids == {100, 200, 300}
    assert result["host_downtimes_errors"] == []
