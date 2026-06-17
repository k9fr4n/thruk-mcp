"""Tests for thruk_incident_timeline (issue #321).

Unit-tests the pure reducers (``_classify_timeline_type`` / ``_build_timeline``
/ ``_timeline_summary``) and an end-to-end respx-mocked routing test asserting
the ``/logs`` query shape and the assembled timeline + summary.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from tests.conftest import ok
from thruk_mcp.tools.history import (
    _build_timeline,
    _classify_timeline_type,
    _timeline_summary,
)

T0 = 1_700_000_000


def _post_params(call) -> dict[str, str]:
    body = call.request.content.decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


# ---------------------------------------------------------------------------
# _classify_timeline_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("HOST ALERT", "state_change"),
        ("SERVICE ALERT", "state_change"),
        ("HOST NOTIFICATION", "notification"),
        ("SERVICE NOTIFICATION", "notification"),
        ("HOST DOWNTIME ALERT", "downtime"),
        ("SERVICE DOWNTIME ALERT", "downtime"),
        ("HOST FLAPPING ALERT", "flap"),
        ("SERVICE FLAPPING ALERT", "flap"),
        ("SERVICE ACKNOWLEDGE ALERT", "ack"),
        ("EXTERNAL COMMAND", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify_timeline_type(type_str: object, expected: str) -> None:
    assert _classify_timeline_type(type_str) == expected


def test_classify_downtime_before_bare_alert() -> None:
    # "DOWNTIME ALERT" contains the "ALERT" substring; ordering must classify it
    # as downtime, not state_change.
    assert _classify_timeline_type("HOST DOWNTIME ALERT") == "downtime"


# ---------------------------------------------------------------------------
# _build_timeline
# ---------------------------------------------------------------------------


def _service_incident_rows() -> list[dict]:
    return [
        {
            "time": T0 + 400,  # deliberately out of order to prove sorting
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
            "state_type": "HARD",
            "plugin_output": "CRITICAL - load 9.9",
        },
        {
            "time": T0 + 160,
            "type": "SERVICE NOTIFICATION",
            "host_name": "srv01",
            "service_description": "CPU",
            "state": 2,
            "contact_name": "oncall",
            "plugin_output": "CRITICAL - load 9.9",
        },
    ]


def test_build_timeline_orders_and_computes_duration() -> None:
    events = _build_timeline(_service_incident_rows())

    # Sorted ascending by time regardless of input order.
    assert [e["epoch"] for e in events] == [T0 + 100, T0 + 160, T0 + 400]

    first, notif, recovery = events

    assert first["type"] == "state_change"
    assert first["from_state"] is None  # first transition seen for this object
    assert first["to_state"] == "CRITICAL"
    assert first["soft_hard"] == "HARD"
    assert first["duration_in_state"] is None
    assert first["service"] == "CPU"
    assert first["plugin_output"] == "CRITICAL - load 9.9"

    assert notif["type"] == "notification"
    assert notif["contact"] == "oncall"
    assert notif["state"] == "CRITICAL"

    assert recovery["type"] == "state_change"
    assert recovery["from_state"] == "CRITICAL"
    assert recovery["to_state"] == "OK"
    # In the CRITICAL state from T0+100 until the recovery at T0+400.
    assert recovery["duration_in_state"] == 300
    assert recovery["duration_in_state_human"] == "5m"


def test_build_timeline_host_state_vocabulary() -> None:
    rows = [
        {
            "time": T0,
            "type": "HOST ALERT",
            "host_name": "srv01",
            "service_description": "",
            "state": 1,
            "state_type": "HARD",
        }
    ]
    (event,) = _build_timeline(rows)
    assert event["service"] is None
    assert event["to_state"] == "DOWN"  # host vocab (1=DOWN), not service WARNING


def test_build_timeline_downtime_detail() -> None:
    rows = [
        {
            "time": T0,
            "type": "HOST DOWNTIME ALERT",
            "host_name": "srv01",
            "service_description": "",
            "state": "STARTED",
            "plugin_output": "Host has entered a period of scheduled downtime",
        }
    ]
    (event,) = _build_timeline(rows)
    assert event["type"] == "downtime"
    assert event["detail"] == "STARTED"


# ---------------------------------------------------------------------------
# _timeline_summary
# ---------------------------------------------------------------------------


def test_timeline_summary_reuses_incident_reducer() -> None:
    rows = _service_incident_rows()
    events = _build_timeline(rows)
    summary = _timeline_summary(rows, events, window_start=T0, window_end=T0 + 100_000)

    assert summary["events"] == 3
    assert summary["state_changes"] == 2
    assert summary["hard_transitions"] == 2
    assert summary["notifications"] == 1
    assert summary["incidents"] == 1
    assert summary["total_downtime_seconds"] == 300
    assert summary["mttr_seconds"] == 300
    assert summary["mttr_human"] == "5m"
    assert summary["ongoing"] is False
    assert summary["first_event"] is not None
    assert summary["last_event"] is not None


# ---------------------------------------------------------------------------
# End-to-end (respx-mocked) routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_timeline_tool_end_to_end(mocked_server) -> None:
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(_service_incident_rows()))

    result = await mcp.call_tool(
        "thruk_incident_timeline",
        {
            "filter": {"type": "leaf", "field": "host", "op": "eq", "value": "srv01"},
            "since": str(T0),
            "until": str(T0 + 100_000),
        },
    )
    payload = json.loads(result[0].text)

    assert payload["total_events"] >= 0
    assert [e["epoch"] for e in payload["timeline"]] == [T0 + 100, T0 + 160, T0 + 400]
    assert payload["summary"]["incidents"] == 1
    assert payload["summary"]["mttr_seconds"] == 300

    # Data fetch is the last /logs call: chronological sort + spanning type regex.
    params = _post_params(route.calls.last)
    assert params["sort"] == "time"
    assert params["type[~]"].startswith("^(HOST|SERVICE) (ALERT|NOTIFICATION")
    assert params["time[gte]"] == str(T0)
    assert "state_type" in params["columns"]
    assert params["host_name"] == "srv01"


@pytest.mark.asyncio
async def test_incident_timeline_requires_filter(mocked_server) -> None:
    mcp, _router = mocked_server
    result = await mcp.call_tool("thruk_incident_timeline", {"since": "-1h"})
    payload = json.loads(result[0].text)
    assert "error" in payload
    assert "filter" in payload["error"]
