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
