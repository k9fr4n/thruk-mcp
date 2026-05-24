"""Tests for semantic problem-management tools (issue #52):
thruk_oldest_problems, thruk_unacked_critical, thruk_stale_acks, thruk_problems_by_hostgroup.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import ok

# ---------------------------------------------------------------------------
# _duration_human unit tests
# ---------------------------------------------------------------------------


def test_duration_human_minutes_only() -> None:
    from thruk_mcp.server import _duration_human

    assert _duration_human(300) == "5m"


def test_duration_human_hours_and_minutes() -> None:
    from thruk_mcp.server import _duration_human

    assert _duration_human(3600 + 900) == "1h 15m"


def test_duration_human_days() -> None:
    from thruk_mcp.server import _duration_human

    assert _duration_human(3 * 86400 + 2 * 3600 + 15 * 60) == "3d 2h 15m"


def test_duration_human_zero() -> None:
    from thruk_mcp.server import _duration_human

    assert _duration_human(0) == "0m"


def test_duration_human_negative() -> None:
    from thruk_mcp.server import _duration_human

    assert _duration_human(-100) == "0m"


# ---------------------------------------------------------------------------
# thruk_oldest_problems
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oldest_problems_basic(mocked_server) -> None:
    """Both host and service endpoints are queried; results sorted oldest first."""
    mcp, router = mocked_server
    now = int(time.time())
    old_lsc = now - 7200  # 2h ago
    new_lsc = now - 300  # 5m ago

    host_route = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [{"name": "h1", "state": 1, "last_state_change": old_lsc, "peer_name": "local"}]
        )
    )
    svc_route = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "h2",
                    "description": "svc1",
                    "state": 2,
                    "last_state_change": new_lsc,
                    "peer_name": "local",
                }
            ]
        )
    )

    result = await mcp.call_tool("thruk_oldest_problems", {"limit": 10})
    assert host_route.called
    assert svc_route.called

    payload = json.loads(result[0].text)
    assert len(payload) == 2
    # oldest first: h1 (2h) before h2/svc1 (5m)
    assert payload[0]["host"] == "h1"
    assert payload[0]["service"] is None
    assert payload[0]["state"] == "DOWN"
    assert payload[0]["duration_human"]
    assert "_lsc" not in payload[0]
    assert payload[1]["host"] == "h2"
    assert payload[1]["service"] == "svc1"
    assert payload[1]["state"] == "CRITICAL"


@pytest.mark.asyncio
async def test_oldest_problems_query_params(mocked_server) -> None:
    """Correct Thruk params: acknowledged=0, scheduled_downtime_depth=0, state[gte]=1."""
    mcp, router = mocked_server
    host_route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))

    await mcp.call_tool("thruk_oldest_problems", {"limit": 5})
    params = host_route.calls.last.request.url.params
    assert params["acknowledged"] == "0"
    assert params["scheduled_downtime_depth"] == "0"
    assert params["state[gte]"] == "1"
    assert params["limit"] == "5"


# ---------------------------------------------------------------------------
# thruk_unacked_critical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unacked_critical_basic(mocked_server) -> None:
    """Returns DOWN hosts + CRITICAL services merged, sorted by duration desc."""
    mcp, router = mocked_server
    now = int(time.time())
    long_ago = now - 7200  # 120 min
    recent = now - 1800  # 30 min

    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [{"name": "h1", "state": 1, "last_state_change": long_ago, "peer_name": "local"}]
        )
    )
    router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "h2",
                    "description": "disk",
                    "state": 2,
                    "last_state_change": recent,
                    "peer_name": "local",
                }
            ]
        )
    )

    result = await mcp.call_tool("thruk_unacked_critical", {"threshold_minutes": 60})
    payload = json.loads(result[0].text)
    assert len(payload) == 2
    # sorted desc by duration_minutes
    assert payload[0]["duration_minutes"] >= payload[1]["duration_minutes"]
    assert payload[0]["host"] == "h1"
    assert payload[0]["service"] is None
    assert payload[0]["state"] == "DOWN"
    assert payload[1]["service"] == "disk"
    assert payload[1]["state"] == "CRITICAL"


@pytest.mark.asyncio
async def test_unacked_critical_query_params(mocked_server) -> None:
    """last_state_change[lte] is now - threshold_minutes * 60."""
    mcp, router = mocked_server
    host_route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))

    before = int(time.time())
    await mcp.call_tool("thruk_unacked_critical", {"threshold_minutes": 30})
    after = int(time.time())

    params = host_route.calls.last.request.url.params
    lte = int(params["last_state_change[lte]"])
    assert before - 30 * 60 - 2 <= lte <= after - 30 * 60 + 2
    assert params["acknowledged"] == "0"
    assert params["state[gte]"] == "1"


# ---------------------------------------------------------------------------
# thruk_stale_acks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_acks_basic(mocked_server) -> None:
    """entry_type=4 queried; results sorted stalest first; empty service_description → None."""
    mcp, router = mocked_server
    now = int(time.time())
    old = now - 20 * 86400
    less_old = now - 8 * 86400

    route = router.get("https://thruk.test/r/comments").mock(
        return_value=ok(
            [
                {
                    "host_name": "h1",
                    "service_description": "",
                    "author": "alice",
                    "comment": "known issue",
                    "entry_time": old,
                    "peer_name": "local",
                },
                {
                    "host_name": "h2",
                    "service_description": "svc",
                    "author": "bob",
                    "comment": "tbd",
                    "entry_time": less_old,
                    "peer_name": "local",
                },
            ]
        )
    )

    result = await mcp.call_tool("thruk_stale_acks", {"min_days": 7})
    assert route.called
    assert route.calls.last.request.url.params["entry_type"] == "4"

    payload = json.loads(result[0].text)
    assert len(payload) == 2
    assert payload[0]["ack_since_days"] >= payload[1]["ack_since_days"]
    assert payload[0]["host"] == "h1"
    assert payload[0]["service"] is None
    assert payload[0]["ack_author"] == "alice"
    assert payload[1]["service"] == "svc"


@pytest.mark.asyncio
async def test_stale_acks_threshold_param(mocked_server) -> None:
    """entry_time[lte] = now - min_days * 86400."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/comments").mock(return_value=ok([]))

    before = int(time.time())
    await mcp.call_tool("thruk_stale_acks", {"min_days": 14})
    after = int(time.time())

    lte = int(route.calls.last.request.url.params["entry_time[lte]"])
    assert before - 14 * 86400 - 2 <= lte <= after - 14 * 86400 + 2


# ---------------------------------------------------------------------------
# thruk_problems_by_hostgroup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_problems_by_hostgroup_basic(mocked_server) -> None:
    """Groups with 0 problems excluded; remaining sorted by severity (DOWN > CRIT > WARN)."""
    mcp, router = mocked_server

    route = router.get("https://thruk.test/r/hostgroups").mock(
        return_value=ok(
            [
                {
                    "name": "hg-web",
                    "alias": "Web",
                    "num_hosts_down": 2,
                    "num_hosts_unreachable": 0,
                    "num_services_warn": 1,
                    "num_services_crit": 3,
                    "num_services_unknown": 0,
                },
                {
                    "name": "hg-db",
                    "alias": "DB",
                    "num_hosts_down": 0,
                    "num_hosts_unreachable": 0,
                    "num_services_warn": 0,
                    "num_services_crit": 0,
                    "num_services_unknown": 0,
                },
                {
                    "name": "hg-app",
                    "alias": "App",
                    "num_hosts_down": 0,
                    "num_hosts_unreachable": 0,
                    "num_services_warn": 5,
                    "num_services_crit": 1,
                    "num_services_unknown": 0,
                },
            ]
        )
    )

    result = await mcp.call_tool("thruk_problems_by_hostgroup", {})
    assert route.called
    assert "num_hosts_down" in route.calls.last.request.url.params["columns"]

    payload = json.loads(result[0].text)
    names = [r["hostgroup"] for r in payload]
    assert "hg-db" not in names
    assert len(payload) == 2
    assert payload[0]["hostgroup"] == "hg-web"
    assert payload[0]["hosts_down"] == 2
    assert payload[0]["services_crit"] == 3
    assert payload[1]["hostgroup"] == "hg-app"


@pytest.mark.asyncio
async def test_problems_by_hostgroup_empty(mocked_server) -> None:
    """All-green infra returns empty list."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hostgroups").mock(
        return_value=ok(
            [
                {
                    "name": "hg-ok",
                    "alias": "All good",
                    "num_hosts_down": 0,
                    "num_hosts_unreachable": 0,
                    "num_services_warn": 0,
                    "num_services_crit": 0,
                    "num_services_unknown": 0,
                }
            ]
        )
    )
    result = await mcp.call_tool("thruk_problems_by_hostgroup", {})
    assert json.loads(result[0].text) == []


# ---------------------------------------------------------------------------
# thruk_concurrent_failures
# ---------------------------------------------------------------------------

BASE_TS = 1_700_000_000  # arbitrary fixed epoch for deterministic tests


# Helpers to build fake log entries
def _evt(host: str, offset_secs: int = 0, state: int = 1) -> dict:
    return {"host_name": host, "state": state, "time": BASE_TS + offset_secs}


@pytest.mark.asyncio
async def test_concurrent_failures_no_events(mocked_server) -> None:
    """Empty log response → empty results."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    payload = json.loads(result[0].text)
    assert payload["results"] == []
    assert payload["total_down_events"] == 0


@pytest.mark.asyncio
async def test_concurrent_failures_below_threshold(mocked_server) -> None:
    """Only 2 distinct hosts in window; min_hosts=3 → no burst reported."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([_evt("h1", 0), _evt("h2", 30)]))

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    payload = json.loads(result[0].text)
    assert payload["results"] == []
    assert payload["total_down_events"] == 2


@pytest.mark.asyncio
async def test_concurrent_failures_basic_burst(mocked_server) -> None:
    """3 distinct hosts within a 5-min window → 1 burst returned."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(
        return_value=ok(
            [
                _evt("h1", 0),
                _evt("h2", 60),
                _evt("h3", 120),
            ]
        )
    )

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    payload = json.loads(result[0].text)
    assert len(payload["results"]) == 1
    burst = payload["results"][0]
    assert burst["count"] == 3
    assert sorted(burst["hosts"]) == ["h1", "h2", "h3"]


@pytest.mark.asyncio
async def test_concurrent_failures_dedup_same_host(mocked_server) -> None:
    """Same host DOWN twice in one window counts as 1 distinct host."""
    mcp, router = mocked_server
    # h1 appears twice, h2 and h3 once → 3 distinct but h1 deduped
    router.post("https://thruk.test/r/logs").mock(
        return_value=ok(
            [
                _evt("h1", 0),
                _evt("h1", 30),  # duplicate
                _evt("h2", 60),
                _evt("h3", 90),
            ]
        )
    )

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    payload = json.loads(result[0].text)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["count"] == 3
    assert sorted(payload["results"][0]["hosts"]) == ["h1", "h2", "h3"]


@pytest.mark.asyncio
async def test_concurrent_failures_merge_overlapping(mocked_server) -> None:
    """Two overlapping hit windows are merged into a single burst."""
    mcp, router = mocked_server
    # Window anchored at T+0: h1,h2,h3 (ok)
    # Window anchored at T+60: h2,h3,h4 (ok) — overlaps with previous (T+0+300 > T+60)
    # Expected: 1 merged burst containing h1,h2,h3,h4
    router.post("https://thruk.test/r/logs").mock(
        return_value=ok(
            [
                _evt("h1", 0),
                _evt("h2", 60),
                _evt("h3", 120),
                _evt("h4", 180),
            ]
        )
    )

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    payload = json.loads(result[0].text)
    assert len(payload["results"]) == 1
    burst = payload["results"][0]
    assert burst["count"] == 4
    assert sorted(burst["hosts"]) == ["h1", "h2", "h3", "h4"]


@pytest.mark.asyncio
async def test_concurrent_failures_two_separate_bursts(mocked_server) -> None:
    """Two bursts separated by more than window_minutes → 2 distinct results."""
    mcp, router = mocked_server
    gap = 15 * 60  # 15 min gap between bursts
    router.post("https://thruk.test/r/logs").mock(
        return_value=ok(
            [
                # Burst 1
                _evt("h1", 0),
                _evt("h2", 30),
                _evt("h3", 60),
                # Burst 2 (15 min later, outside any 5-min window from burst 1)
                _evt("h4", gap),
                _evt("h5", gap + 30),
                _evt("h6", gap + 60),
            ]
        )
    )

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    payload = json.loads(result[0].text)
    assert len(payload["results"]) == 2
    assert sorted(payload["results"][0]["hosts"]) == ["h1", "h2", "h3"]
    assert sorted(payload["results"][1]["hosts"]) == ["h4", "h5", "h6"]


@pytest.mark.asyncio
async def test_concurrent_failures_recovery_excluded(mocked_server) -> None:
    """state=0 (UP/recovery) entries are filtered out by state[gte]=1 in the query."""
    mcp, router = mocked_server
    # Verify the HTTP params contain state[gte]=1 and type regex
    log_route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-2h", "until": "2026-05-20 16:00:00", "window_minutes": 10, "min_hosts": 2},
    )
    # respx captures the POST body as form-encoded
    body = log_route.calls.last.request.content.decode()
    assert "state%5Bgte%5D=1" in body or "state[gte]=1" in body
    assert "HOST+ALERT" in body or "HOST%20ALERT" in body or "HOST ALERT" in body


@pytest.mark.asyncio
async def test_concurrent_failures_invalid_filter(mocked_server) -> None:
    """Invalid filter tree returns an error JSON, not a crash."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {
            "filter": {"type": "leaf", "field": "nonexistent_field", "op": "eq", "value": "x"},
        },
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_concurrent_failures_metadata(mocked_server) -> None:
    """Returned payload always includes since/until/window_minutes/min_hosts metadata."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-6h", "until": None, "window_minutes": 10, "min_hosts": 5},
    )
    payload = json.loads(result[0].text)
    assert payload["since"] == "-6h"
    assert payload["until"] is None
    assert payload["window_minutes"] == 10
    assert payload["min_hosts"] == 5


@pytest.mark.asyncio
async def test_concurrent_failures_large_dataset_perf(mocked_server) -> None:
    """Regression test for O(n²) sliding window (issue #86).

    BEFORE the fix the inner ``for j in range(i, n)`` loop re-scanned every
    event inside the window for each anchor ``i``.  With n = _NOISY_MAX_ALERTS
    (10 000) events all packed into the same 5-minute window this yields up to
    10^8 Python iterations, blocking the event loop for several seconds.

    AFTER the fix (deque two-pointer) each event is appended to and popped from
    the deque at most once, giving O(n) work after the O(n log n) sort.  This
    test asserts that 10 000 tightly-packed events complete well within a
    generous wall-clock budget that would be impossible with the O(n²) loop.
    """
    import time

    from thruk_mcp.server import _NOISY_MAX_ALERTS

    mcp, router = mocked_server

    n = _NOISY_MAX_ALERTS  # 10 000
    # All events within a 60-second span — worst case for the old O(n²) loop
    events = [_evt(f"h{i % 50}", offset_secs=i // 10) for i in range(n)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(events))

    t0 = time.perf_counter()
    result = await mcp.call_tool(
        "thruk_concurrent_failures",
        {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
    )
    elapsed = time.perf_counter() - t0

    payload = json.loads(result[0].text)
    assert "error" not in payload
    assert payload["total_down_events"] == n
    assert len(payload["results"]) >= 1  # all events fall in one burst
    # O(n) should complete well under 5 s even inside Docker with n=10_000.
    # The O(n²) implementation would take 100+ s for the same input.
    assert elapsed < 5.0, f"concurrent_failures took {elapsed:.2f}s for {n} events (too slow)"
