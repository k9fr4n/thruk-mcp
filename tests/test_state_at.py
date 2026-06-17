"""Tests for thruk_state_at / thruk_state_diff (issue #324).

Unit-tests the pure reducers (``_reconstruct_state_at`` / ``_state_snapshot`` /
``_diff_states``) and respx-mocked routing tests asserting the ``/logs`` query
shape and the assembled snapshot / diff.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from tests.conftest import ok
from thruk_mcp.tools.history import (
    _diff_states,
    _reconstruct_state_at,
    _state_snapshot,
)

T0 = 1_700_000_000


def _post_params(call) -> dict[str, str]:
    body = call.request.content.decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


# ---------------------------------------------------------------------------
# _reconstruct_state_at
# ---------------------------------------------------------------------------


def _cpu_rows() -> list[dict]:
    """srv01/CPU: OK at T0, CRITICAL at T0+100 (soft then hard), recovers at T0+400."""
    return [
        {
            "time": T0,
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 0,
            "state_type": "HARD",
            "plugin_output": "OK - load 0.2",
        },
        {
            "time": T0 + 100,
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 2,
            "state_type": "SOFT",
            "plugin_output": "CRITICAL - load 9.9",
        },
        {
            "time": T0 + 120,
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 2,
            "state_type": "HARD",
            "plugin_output": "CRITICAL - load 9.9",
        },
        {
            "time": T0 + 400,
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 0,
            "state_type": "HARD",
            "plugin_output": "OK - load 0.3",
        },
    ]


def test_reconstruct_picks_state_at_cutoff() -> None:
    # At T0+200 the service is CRITICAL; "since" is the *first* CRITICAL row
    # (the SOFT entry at T0+100), since the same code persisted.
    state = _reconstruct_state_at(_cpu_rows(), T0 + 200)
    rec = state[("srv01", "CPU")]
    assert rec["state"] == 2
    assert rec["state_type"] == "HARD"
    assert rec["since"] == T0 + 100


def test_reconstruct_ignores_rows_after_cutoff() -> None:
    # At T0+50 only the first OK row is visible — recovery row is in the future.
    state = _reconstruct_state_at(_cpu_rows(), T0 + 50)
    assert state[("srv01", "CPU")]["state"] == 0


def test_reconstruct_after_recovery() -> None:
    state = _reconstruct_state_at(_cpu_rows(), T0 + 1000)
    rec = state[("srv01", "CPU")]
    assert rec["state"] == 0
    assert rec["since"] == T0 + 400  # entered OK at recovery


def test_reconstruct_downtime_toggle() -> None:
    rows = [
        {
            "time": T0,
            "type": "HOST DOWNTIME ALERT",
            "host_name": "srv01",
            "service_description": "",
            "state": "STARTED",
        },
        {
            "time": T0 + 500,
            "type": "HOST DOWNTIME ALERT",
            "host_name": "srv01",
            "service_description": "",
            "state": "STOPPED",
        },
    ]
    assert _reconstruct_state_at(rows, T0 + 100)[("srv01", "")]["in_downtime"] is True
    assert _reconstruct_state_at(rows, T0 + 600)[("srv01", "")]["in_downtime"] is False


def test_reconstruct_ack_set_and_cleared_on_recovery() -> None:
    rows = [
        {
            "time": T0,
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 2,
            "state_type": "HARD",
        },
        {
            "time": T0 + 50,
            "type": "SERVICE ACKNOWLEDGE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": "STARTED",
        },
        {
            "time": T0 + 200,
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 0,
            "state_type": "HARD",
        },
    ]
    # Acked while still CRITICAL.
    assert _reconstruct_state_at(rows, T0 + 100)[("srv01", "CPU")]["acknowledged"] is True
    # Recovery (state 0) clears the ack.
    after = _reconstruct_state_at(rows, T0 + 300)[("srv01", "CPU")]
    assert after["state"] == 0
    assert after["acknowledged"] is False


# ---------------------------------------------------------------------------
# _state_snapshot
# ---------------------------------------------------------------------------


def _mixed_state() -> dict:
    return {
        ("srv01", "CPU"): {
            "state": 2,
            "state_type": "HARD",
            "since": T0,
            "in_downtime": False,
            "acknowledged": False,
            "plugin_output": "CRIT",
        },
        ("srv02", ""): {
            "state": 0,
            "state_type": "HARD",
            "since": T0,
            "in_downtime": False,
            "acknowledged": False,
            "plugin_output": "UP",
        },
    }


def test_snapshot_summary_and_problems_first() -> None:
    objects, summary = _state_snapshot(_mixed_state(), problems_only=False)
    assert summary == {
        "total": 2,
        "ok": 1,
        "problems": 1,
        "by_state": {"CRITICAL": 1, "UP": 1},
    }
    # Problem (srv01/CPU CRITICAL) sorts before the OK host.
    assert objects[0]["host"] == "srv01"
    assert objects[0]["state"] == "CRITICAL"
    assert objects[0]["service"] == "CPU"


def test_snapshot_problems_only_filters_but_summary_is_full() -> None:
    objects, summary = _state_snapshot(_mixed_state(), problems_only=True)
    assert [o["host"] for o in objects] == ["srv01"]
    assert summary["total"] == 2  # summary still covers the OK host


# ---------------------------------------------------------------------------
# _diff_states
# ---------------------------------------------------------------------------


def test_diff_categories() -> None:
    base = {
        "state": 0,
        "state_type": "HARD",
        "in_downtime": False,
        "acknowledged": False,
    }
    before = {
        ("h", "ok2crit"): {**base, "state": 0},
        ("h", "crit2ok"): {**base, "state": 2},
        ("h", "warn2crit"): {**base, "state": 1},
        ("h", "stable"): {**base, "state": 2},
        ("h", "dt"): {**base, "state": 2, "in_downtime": False},
    }
    after = {
        ("h", "ok2crit"): {**base, "state": 2},
        ("h", "crit2ok"): {**base, "state": 0},
        ("h", "warn2crit"): {**base, "state": 2},
        ("h", "stable"): {**base, "state": 2},
        ("h", "dt"): {**base, "state": 2, "in_downtime": True},
    }
    changes, summary = _diff_states(before, after)
    by_cat = {(c["host"], c["service"]): c["category"] for c in changes}
    assert by_cat[("h", "ok2crit")] == "new_problem"
    assert by_cat[("h", "crit2ok")] == "recovered"
    assert by_cat[("h", "warn2crit")] == "state_changed"
    assert by_cat[("h", "dt")] == "downtime_changed"
    assert ("h", "stable") not in by_cat  # unchanged → omitted
    assert summary["changed"] == 4
    assert summary["by_category"] == {
        "downtime_changed": 1,
        "new_problem": 1,
        "recovered": 1,
        "state_changed": 1,
    }


def test_diff_object_present_in_one_snapshot_only() -> None:
    base = {"state": 2, "state_type": "HARD", "in_downtime": False, "acknowledged": False}
    changes, _summary = _diff_states({}, {("h", "new"): base})
    assert changes[0]["category"] == "new_problem"
    assert changes[0]["from_state"] == "UNKNOWN(None)"
    assert changes[0]["to_state"] == "CRITICAL"


# ---------------------------------------------------------------------------
# End-to-end (respx-mocked) routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_at_end_to_end(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(_cpu_rows()))

    result = await mcp.call_tool(
        "thruk_state_at",
        {
            "timestamp": str(T0 + 200),
            "filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"},
        },
    )
    payload = json.loads(result[0].text)

    assert payload["summary"]["problems"] == 1
    obj = payload["objects"][0]
    assert obj["host"] == "srv01"
    assert obj["state"] == "CRITICAL"
    assert obj["state_type"] == "HARD"

    # Data fetch is the last /logs call: cut-off at the timestamp + spanning regex.
    params = _post_params(route.calls.last)
    assert params["time[lte]"] == str(T0 + 200)
    assert params["type[~]"].startswith("^(HOST|SERVICE) (ALERT|DOWNTIME ALERT")
    assert "state_type" in params["columns"]
    assert params["host_name"] == "srv01"
    assert params["sort"] == "-time"


@pytest.mark.asyncio
async def test_state_at_requires_timestamp_and_filter(mocked_server) -> None:
    mcp, _router = mocked_server
    missing_ts = await mcp.call_tool(
        "thruk_state_at",
        {"filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"}},
    )
    assert "timestamp" in json.loads(missing_ts[0].text)["error"]

    missing_filter = await mcp.call_tool("thruk_state_at", {"timestamp": "-1h"})
    assert "filter" in json.loads(missing_filter[0].text)["error"]


@pytest.mark.asyncio
async def test_state_diff_end_to_end(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(_cpu_rows()))

    # t1 before the outage (srv01/CPU OK), t2 during it (CRITICAL).
    result = await mcp.call_tool(
        "thruk_state_diff",
        {
            "t1": str(T0 + 50),
            "t2": str(T0 + 200),
            "filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"},
        },
    )
    payload = json.loads(result[0].text)

    assert payload["summary"]["by_category"] == {"new_problem": 1}
    change = payload["changes"][0]
    assert change["from_state"] == "OK"
    assert change["to_state"] == "CRITICAL"

    # One /logs fetch up to the later instant.
    params = _post_params(route.calls.last)
    assert params["time[lte]"] == str(T0 + 200)


@pytest.mark.asyncio
async def test_state_diff_normalises_order(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(_cpu_rows()))

    # Pass the instants reversed: result must still read earlier→later.
    result = await mcp.call_tool(
        "thruk_state_diff",
        {
            "t1": str(T0 + 200),
            "t2": str(T0 + 50),
            "filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"},
        },
    )
    payload = json.loads(result[0].text)
    assert payload["changes"][0]["category"] == "new_problem"
    params = _post_params(route.calls.last)
    assert params["time[lte]"] == str(T0 + 200)
