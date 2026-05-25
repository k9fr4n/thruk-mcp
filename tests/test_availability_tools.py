"""Tests for thruk_host_availability and thruk_service_availability (issue #171)."""

from __future__ import annotations

import json

import pytest

from tests.conftest import ok
from thruk_mcp.server import _parse_thruk_time


# ---------------------------------------------------------------------------
# thruk_host_availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_availability_basic(mocked_server) -> None:
    """Basic call: since/until converted to epoch start/end."""
    mcp, router = mocked_server
    payload = {
        "time_up": 600000,
        "time_up_percent": 99.5,
        "time_down": 3000,
        "time_down_percent": 0.5,
        "time_unreachable": 0,
        "time_unreachable_percent": 0.0,
    }
    route = router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok(payload)
    )
    await mcp.call_tool("thruk_host_availability", {"host": "web01", "since": "-7d"})
    assert route.called
    params = route.calls.last.request.url.params
    assert "start" in params
    assert int(params["start"]) > 0
    assert "end" in params
    assert "timeperiod" not in params


@pytest.mark.asyncio
async def test_host_availability_response_structure(mocked_server) -> None:
    """Response must include host + since/until metadata merged with Thruk data."""
    mcp, router = mocked_server
    thruk_data = {"time_up_percent": 99.87, "time_down_percent": 0.13}
    router.get("https://thruk.test/r/hosts/db01/availability").mock(return_value=ok(thruk_data))

    result = await mcp.call_tool(
        "thruk_host_availability",
        {"host": "db01", "since": "-30d", "until": None},
    )
    data = json.loads(result[0].text)
    assert data["host"] == "db01"
    assert data["since"] == "-30d"
    assert data["until"] is None
    assert data["time_up_percent"] == 99.87
    assert data["time_down_percent"] == 0.13


@pytest.mark.asyncio
async def test_host_availability_timeperiod_overrides_since_until(mocked_server) -> None:
    """When timeperiod is set, start/end are NOT sent; timeperiod is sent instead."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok({"time_up_percent": 100.0})
    )
    await mcp.call_tool(
        "thruk_host_availability",
        {"host": "web01", "since": "-7d", "timeperiod": "lastmonth"},
    )
    assert route.called
    params = route.calls.last.request.url.params
    assert params["timeperiod"] == "lastmonth"
    assert "start" not in params
    assert "end" not in params


@pytest.mark.asyncio
async def test_host_availability_timeperiod_in_response(mocked_server) -> None:
    """timeperiod must appear in response when used (no since/until keys)."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/db01/availability").mock(
        return_value=ok({"time_up_percent": 98.0})
    )
    result = await mcp.call_tool(
        "thruk_host_availability",
        {"host": "db01", "timeperiod": "thismonth"},
    )
    data = json.loads(result[0].text)
    assert data["timeperiod"] == "thismonth"
    assert "since" not in data
    assert "until" not in data


@pytest.mark.asyncio
async def test_host_availability_with_downtimes(mocked_server) -> None:
    """with_downtimes=True must send withdowntimes=1."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok({})
    )
    await mcp.call_tool(
        "thruk_host_availability",
        {"host": "web01", "with_downtimes": True},
    )
    params = route.calls.last.request.url.params
    assert params["withdowntimes"] == "1"


@pytest.mark.asyncio
async def test_host_availability_without_downtimes(mocked_server) -> None:
    """with_downtimes=False (default) must NOT send withdowntimes param."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok({})
    )
    await mcp.call_tool("thruk_host_availability", {"host": "web01"})
    params = route.calls.last.request.url.params
    assert "withdowntimes" not in params


@pytest.mark.asyncio
async def test_host_availability_include_soft_states(mocked_server) -> None:
    """include_soft_states=True must send includesoftstates=1."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok({})
    )
    await mcp.call_tool(
        "thruk_host_availability",
        {"host": "web01", "include_soft_states": True},
    )
    params = route.calls.last.request.url.params
    assert params["includesoftstates"] == "1"


@pytest.mark.asyncio
async def test_host_availability_iso_since(mocked_server) -> None:
    """ISO datetime since/until are converted to epoch integers."""
    mcp, router = mocked_server
    route = router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok({})
    )
    await mcp.call_tool(
        "thruk_host_availability",
        {"host": "web01", "since": "2026-05-01 00:00:00", "until": "2026-05-25 00:00:00"},
    )
    params = route.calls.last.request.url.params
    expected_start = _parse_thruk_time("2026-05-01 00:00:00")
    expected_end = _parse_thruk_time("2026-05-25 00:00:00")
    assert int(params["start"]) == expected_start
    assert int(params["end"]) == expected_end


@pytest.mark.asyncio
async def test_host_availability_list_response(mocked_server) -> None:
    """Thruk returning a single-element list is handled (uses data[0])."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/web01/availability").mock(
        return_value=ok([{"time_up_percent": 99.1}])
    )
    result = await mcp.call_tool("thruk_host_availability", {"host": "web01"})
    data = json.loads(result[0].text)
    assert data["time_up_percent"] == 99.1


@pytest.mark.asyncio
async def test_host_availability_backends(mocked_server) -> None:
    """backends param routes through /r/sites/<backend>/..."""
    mcp, router = mocked_server
    route = router.get(
        "https://thruk.test/r/sites/prod/hosts/web01/availability"
    ).mock(return_value=ok({"time_up_percent": 100.0}))
    await mcp.call_tool(
        "thruk_host_availability",
        {"host": "web01", "backends": "prod"},
    )
    assert route.called


# ---------------------------------------------------------------------------
# thruk_service_availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_availability_basic(mocked_server) -> None:
    """Basic call: URL contains host and service, start/end params present."""
    mcp, router = mocked_server
    payload = {
        "time_ok_percent": 99.9,
        "time_warning_percent": 0.05,
        "time_critical_percent": 0.05,
        "time_unknown_percent": 0.0,
    }
    route = router.get(
        "https://thruk.test/r/services/web01/HTTP/availability"
    ).mock(return_value=ok(payload))
    await mcp.call_tool(
        "thruk_service_availability",
        {"host": "web01", "service": "HTTP", "since": "-7d"},
    )
    assert route.called
    params = route.calls.last.request.url.params
    assert "start" in params
    assert int(params["start"]) > 0


@pytest.mark.asyncio
async def test_service_availability_response_structure(mocked_server) -> None:
    """Response must include host + service + since/until metadata."""
    mcp, router = mocked_server
    thruk_data = {"time_ok_percent": 99.5, "time_critical_percent": 0.5}
    router.get("https://thruk.test/r/services/db01/MySQL/availability").mock(
        return_value=ok(thruk_data)
    )
    result = await mcp.call_tool(
        "thruk_service_availability",
        {"host": "db01", "service": "MySQL", "since": "-7d", "until": None},
    )
    data = json.loads(result[0].text)
    assert data["host"] == "db01"
    assert data["service"] == "MySQL"
    assert data["since"] == "-7d"
    assert data["until"] is None
    assert data["time_ok_percent"] == 99.5


@pytest.mark.asyncio
async def test_service_availability_timeperiod(mocked_server) -> None:
    """timeperiod overrides since/until; only timeperiod param sent to Thruk."""
    mcp, router = mocked_server
    route = router.get(
        "https://thruk.test/r/services/web01/HTTP/availability"
    ).mock(return_value=ok({"time_ok_percent": 100.0}))
    await mcp.call_tool(
        "thruk_service_availability",
        {"host": "web01", "service": "HTTP", "timeperiod": "last24hours"},
    )
    params = route.calls.last.request.url.params
    assert params["timeperiod"] == "last24hours"
    assert "start" not in params
    assert "end" not in params


@pytest.mark.asyncio
async def test_service_availability_timeperiod_in_response(mocked_server) -> None:
    """timeperiod must appear in response; no since/until keys."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/db01/MySQL/availability").mock(
        return_value=ok({})
    )
    result = await mcp.call_tool(
        "thruk_service_availability",
        {"host": "db01", "service": "MySQL", "timeperiod": "thismonth"},
    )
    data = json.loads(result[0].text)
    assert data["timeperiod"] == "thismonth"
    assert "since" not in data
    assert "until" not in data


@pytest.mark.asyncio
async def test_service_availability_with_downtimes(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get(
        "https://thruk.test/r/services/web01/HTTP/availability"
    ).mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_service_availability",
        {"host": "web01", "service": "HTTP", "with_downtimes": True},
    )
    params = route.calls.last.request.url.params
    assert params["withdowntimes"] == "1"


@pytest.mark.asyncio
async def test_service_availability_include_soft_states(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.get(
        "https://thruk.test/r/services/web01/HTTP/availability"
    ).mock(return_value=ok({}))
    await mcp.call_tool(
        "thruk_service_availability",
        {"host": "web01", "service": "HTTP", "include_soft_states": True},
    )
    params = route.calls.last.request.url.params
    assert params["includesoftstates"] == "1"


@pytest.mark.asyncio
async def test_service_availability_backends(mocked_server) -> None:
    """backends param routes the request through /r/sites/<backend>/..."""
    mcp, router = mocked_server
    route = router.get(
        "https://thruk.test/r/sites/prod/services/web01/HTTP/availability"
    ).mock(return_value=ok({"time_ok_percent": 99.0}))
    await mcp.call_tool(
        "thruk_service_availability",
        {"host": "web01", "service": "HTTP", "backends": "prod"},
    )
    assert route.called


@pytest.mark.asyncio
async def test_service_availability_list_response(mocked_server) -> None:
    """Thruk returning a single-element list is handled (uses data[0])."""
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/web01/HTTP/availability").mock(
        return_value=ok([{"time_ok_percent": 98.5}])
    )
    result = await mcp.call_tool(
        "thruk_service_availability",
        {"host": "web01", "service": "HTTP"},
    )
    data = json.loads(result[0].text)
    assert data["time_ok_percent"] == 98.5
