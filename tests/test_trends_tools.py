"""Tests for trends & history tools (issue #57):
thruk_alert_heatmap, thruk_recurring_problems.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from tests.conftest import ok
from thruk_mcp.server import _parse_thruk_time

# ---------------------------------------------------------------------------
# _parse_thruk_time unit tests
# ---------------------------------------------------------------------------


def test_parse_thruk_time_relative_hours() -> None:
    now = int(datetime.now().timestamp())
    result = _parse_thruk_time("-2h")
    assert result is not None
    assert abs(result - (now - 7200)) <= 2


def test_parse_thruk_time_relative_minutes() -> None:
    now = int(datetime.now().timestamp())
    result = _parse_thruk_time("-30m")
    assert result is not None
    assert abs(result - (now - 1800)) <= 2


def test_parse_thruk_time_relative_days() -> None:
    now = int(datetime.now().timestamp())
    result = _parse_thruk_time("-7d")
    assert result is not None
    assert abs(result - (now - 7 * 86400)) <= 2


def test_parse_thruk_time_epoch_string() -> None:
    assert _parse_thruk_time("1700000000") == 1700000000


def test_parse_thruk_time_iso_datetime() -> None:
    result = _parse_thruk_time("2026-05-21 14:00:00")
    assert result is not None
    assert result == int(datetime(2026, 5, 21, 14, 0, 0).timestamp())


def test_parse_thruk_time_none() -> None:
    assert _parse_thruk_time(None) is None


def test_parse_thruk_time_unparseable() -> None:
    assert _parse_thruk_time("not-a-time") is None


# ---------------------------------------------------------------------------
# thruk_alert_heatmap
# ---------------------------------------------------------------------------

BASE_TS = 1_748_822_400  # deterministic epoch (hour-aligned)


def _log(t: int) -> dict:
    return {"time": t}


@pytest.mark.asyncio
async def test_heatmap_invalid_bucket(mocked_server) -> None:
    mcp, _ = mocked_server
    result = await mcp.call_tool("thruk_alert_heatmap", {"bucket": "2h"})
    payload = json.loads(result[0].text)
    assert "error" in payload
    assert "2h" in payload["error"]


@pytest.mark.asyncio
async def test_heatmap_basic_bucketing(mocked_server) -> None:
    """3 alerts in first bucket, 1 in second, 0 in third (empty filled)."""
    mcp, router = mocked_server

    hour = 3600
    entries = [
        _log(BASE_TS + 0),
        _log(BASE_TS + 100),
        _log(BASE_TS + 200),
        _log(BASE_TS + hour + 10),
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    since = str(BASE_TS)
    until = str(BASE_TS + 2 * hour)

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"since": since, "until": until, "bucket": "1h"},
    )
    payload = json.loads(result[0].text)

    assert payload["bucket"] == "1h"
    assert payload["total_alerts"] == 4
    assert len(payload["results"]) == 3

    assert payload["results"][0]["count"] == 3
    assert payload["results"][1]["count"] == 1
    assert payload["results"][2]["count"] == 0  # empty bucket filled


@pytest.mark.asyncio
async def test_heatmap_empty_window(mocked_server) -> None:
    """No alerts => all buckets zero."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    since = str(BASE_TS)
    until = str(BASE_TS + 3600)

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"since": since, "until": until, "bucket": "1h"},
    )
    payload = json.loads(result[0].text)
    assert payload["total_alerts"] == 0
    assert all(b["count"] == 0 for b in payload["results"])
    assert len(payload["results"]) >= 1


@pytest.mark.asyncio
async def test_heatmap_request_params(mocked_server) -> None:
    """type[~] ALERT regex and time[gte] are sent to /logs via POST."""
    mcp, router = mocked_server
    log_route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    await mcp.call_tool("thruk_alert_heatmap", {"since": "-6h", "bucket": "30m"})

    body = log_route.calls.last.request.content.decode()
    assert "ALERT" in body
    assert "time" in body


@pytest.mark.asyncio
async def test_heatmap_metadata_in_output(mocked_server) -> None:
    """since/until/bucket/total_alerts always present in payload."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"since": "-12h", "until": None, "bucket": "6h"},
    )
    payload = json.loads(result[0].text)
    assert payload["since"] == "-12h"
    assert payload["until"] is None
    assert payload["bucket"] == "6h"
    assert "total_alerts" in payload
    assert "results" in payload


@pytest.mark.asyncio
async def test_heatmap_cap_warning(mocked_server) -> None:
    """Hitting _NOISY_MAX_ALERTS cap => _warning key present."""
    mcp, router = mocked_server
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    big_data = [_log(BASE_TS + i) for i in range(_NOISY_MAX_ALERTS)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(big_data))

    result = await mcp.call_tool("thruk_alert_heatmap", {"since": "-24h"})
    payload = json.loads(result[0].text)
    assert "_warning" in payload


@pytest.mark.asyncio
async def test_heatmap_invalid_filter(mocked_server) -> None:
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"filter": {"type": "leaf", "field": "bad_field", "op": "eq", "value": "x"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


# ---------------------------------------------------------------------------
# thruk_recurring_problems
# ---------------------------------------------------------------------------


def _alert(host: str, svc: str, state: int, t: int) -> dict:
    return {"host_name": host, "service_description": svc, "state": state, "time": t}


@pytest.mark.asyncio
async def test_recurring_min_alerts_zero(mocked_server) -> None:
    mcp, _ = mocked_server
    result = await mcp.call_tool("thruk_recurring_problems", {"min_alerts": 0})
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_recurring_basic(mocked_server) -> None:
    """h1/Disk fires 6x, h2/CPU fires 3x, min_alerts=5 => only h1/Disk returned."""
    mcp, router = mocked_server

    entries = [_alert("h1", "Disk", 2, BASE_TS + i * 300) for i in range(6)] + [
        _alert("h2", "CPU", 1, BASE_TS + i * 600) for i in range(3)
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-24h", "min_alerts": 5, "limit": 10},
    )
    payload = json.loads(result[0].text)
    assert payload["min_alerts"] == 5
    assert payload["total_objects_above_threshold"] == 1
    assert len(payload["results"]) == 1
    r = payload["results"][0]
    assert r["host"] == "h1"
    assert r["service"] == "Disk"
    assert r["alert_count"] == 6
    assert r["last_state"] == "CRITICAL"


@pytest.mark.asyncio
async def test_recurring_recovery_excluded(mocked_server) -> None:
    """state=0 (RECOVERY) entries are NOT counted toward min_alerts."""
    mcp, router = mocked_server

    entries = [_alert("h1", "", 1, BASE_TS + i * 100) for i in range(4)] + [
        _alert("h1", "", 0, BASE_TS + 500),  # RECOVERY — must be ignored
        _alert("h1", "", 0, BASE_TS + 600),  # RECOVERY — must be ignored
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-1h", "min_alerts": 3},
    )
    payload = json.loads(result[0].text)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["alert_count"] == 4  # not 6


@pytest.mark.asyncio
async def test_recurring_host_alert_no_service(mocked_server) -> None:
    """Host-level alert (service_description='') => service=None, last_state=DOWN."""
    mcp, router = mocked_server

    entries = [_alert("router1", "", 1, BASE_TS + i * 60) for i in range(7)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-24h", "min_alerts": 5},
    )
    payload = json.loads(result[0].text)
    assert payload["results"][0]["service"] is None
    assert payload["results"][0]["last_state"] == "DOWN"


@pytest.mark.asyncio
async def test_recurring_sorted_by_count_desc(mocked_server) -> None:
    """Results sorted by alert_count descending."""
    mcp, router = mocked_server

    entries = [_alert("h-low", "svc", 2, BASE_TS + i * 100) for i in range(5)] + [
        _alert("h-high", "svc", 1, BASE_TS + i * 50) for i in range(9)
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-24h", "min_alerts": 3, "limit": 10},
    )
    payload = json.loads(result[0].text)
    counts = [r["alert_count"] for r in payload["results"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 9


@pytest.mark.asyncio
async def test_recurring_limit_respected(mocked_server) -> None:
    """limit=1 returns only the top entry even when 2 exceed min_alerts."""
    mcp, router = mocked_server

    entries = [_alert("h1", "svc", 2, BASE_TS + i * 100) for i in range(8)] + [
        _alert("h2", "svc", 1, BASE_TS + i * 200) for i in range(6)
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-24h", "min_alerts": 5, "limit": 1},
    )
    payload = json.loads(result[0].text)
    assert payload["total_objects_above_threshold"] == 2
    assert len(payload["results"]) == 1


@pytest.mark.asyncio
async def test_recurring_first_last_seen(mocked_server) -> None:
    """first_seen is earliest entry, last_seen is latest, last_state from last entry."""
    mcp, router = mocked_server

    entries = [
        _alert("h1", "svc", 2, BASE_TS),
        _alert("h1", "svc", 1, BASE_TS + 1000),
        _alert("h1", "svc", 2, BASE_TS + 2000),
        _alert("h1", "svc", 1, BASE_TS + 3000),
        _alert("h1", "svc", 2, BASE_TS + 4000),
        _alert("h1", "svc", 1, BASE_TS + 5000),  # last entry: state=1 = WARNING
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-24h", "min_alerts": 5},
    )
    payload = json.loads(result[0].text)
    r = payload["results"][0]
    assert r["first_seen"] != "N/A"
    assert r["last_seen"] != "N/A"
    assert r["first_seen"] != r["last_seen"]
    assert r["last_state"] == "WARNING"


@pytest.mark.asyncio
async def test_recurring_empty_result(mocked_server) -> None:
    """No entries above threshold => empty results, no error key."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-24h", "min_alerts": 5},
    )
    payload = json.loads(result[0].text)
    assert payload["results"] == []
    assert payload["total_objects_above_threshold"] == 0
    assert "error" not in payload


@pytest.mark.asyncio
async def test_recurring_metadata(mocked_server) -> None:
    """since/until/min_alerts always present in payload."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"since": "-48h", "until": "2026-05-21 00:00:00", "min_alerts": 3},
    )
    payload = json.loads(result[0].text)
    assert payload["since"] == "-48h"
    assert payload["until"] == "2026-05-21 00:00:00"
    assert payload["min_alerts"] == 3


@pytest.mark.asyncio
async def test_recurring_invalid_filter(mocked_server) -> None:
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_recurring_problems",
        {"filter": {"type": "leaf", "field": "unknown", "op": "eq", "value": "x"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload
