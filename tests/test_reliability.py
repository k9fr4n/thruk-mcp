"""Tests for the reliability report reducer + tool (issue #286).

The pure reducer (:mod:`thruk_mcp.reliability`) is exercised on a synthetic
HARD/SOFT log fixture covering every acceptance criterion: SOFT noise, downtime
alerts, WARN->CRIT collapse, an ongoing incident, a pre-window start, and the
empty / single-event safety cases. The tool is covered by a respx-backed
routing + output test.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from tests.conftest import ok
from thruk_mcp.reliability import extract_incidents, summarize_reliability

# Fixed window: 30 days starting at a stable epoch so assertions are exact.
T0 = 1_700_000_000
WINDOW_END = T0 + 30 * 86400


def _alert(t: int, state: int, *, service: str = "", state_type: str = "HARD") -> dict:
    """Build a synthetic /logs HOST/SERVICE ALERT row."""
    return {
        "time": t,
        "state": state,
        "state_type": state_type,
        "type": "SERVICE ALERT" if service else "HOST ALERT",
        "host_name": "h1",
        "service_description": service,
    }


def _summary(entries: list[dict]) -> dict:
    return summarize_reliability(entries, window_start=T0, window_end=WINDOW_END)


# ---------------------------------------------------------------------------
# Criterion 1 + 2: SOFT rows and non-alert types are ignored
# ---------------------------------------------------------------------------


def test_soft_and_downtime_rows_are_ignored() -> None:
    entries = [
        _alert(T0 + 10, 2, state_type="SOFT"),  # SOFT noise -> ignored
        {"time": T0 + 20, "state": 1, "state_type": "HARD", "type": "HOST DOWNTIME ALERT"},
        {"time": T0 + 30, "state": 1, "state_type": "HARD", "type": "HOST FLAPPING ALERT"},
        _alert(T0 + 100, 2),  # real HARD problem
        _alert(T0 + 160, 0),  # HARD recovery
    ]
    m = _summary(entries)
    assert m["incidents"] == 1
    assert m["mttr_seconds"] == 60
    assert m["total_downtime_seconds"] == 60
    assert m["longest_incident_seconds"] == 60
    assert m["ongoing"] is False
    assert m["mtbf_seconds"] is None  # < 2 incidents (criterion 7)


# ---------------------------------------------------------------------------
# Criterion 4: consecutive non-OK HARD states collapse into one incident
# ---------------------------------------------------------------------------


def test_warn_to_crit_is_a_single_incident() -> None:
    entries = [
        _alert(T0 + 100, 1, service="svc"),  # WARNING
        _alert(T0 + 150, 2, service="svc"),  # CRITICAL (not a new incident)
        _alert(T0 + 300, 0, service="svc"),  # OK recovery
    ]
    m = _summary(entries)
    assert m["incidents"] == 1
    assert m["total_downtime_seconds"] == 200
    assert m["mttr_seconds"] == 200


# ---------------------------------------------------------------------------
# Criterion 5: ongoing incident excluded from MTTR, counted in downtime
# ---------------------------------------------------------------------------


def test_ongoing_incident() -> None:
    start = WINDOW_END - 100
    entries = [_alert(start, 2)]  # problem, never recovers
    incidents = extract_incidents(entries, window_start=T0, window_end=WINDOW_END)
    assert len(incidents) == 1
    assert incidents[0]["ongoing"] is True
    assert incidents[0]["duration_seconds"] == 100  # clamped at window_end
    m = _summary(entries)
    assert m["incidents"] == 1
    assert m["ongoing"] is True
    assert m["mttr_seconds"] is None  # no recovery -> excluded from MTTR
    assert m["total_downtime_seconds"] == 100


# ---------------------------------------------------------------------------
# Criterion 6: a leading recovery clamps a pre-window incident to window_start
# ---------------------------------------------------------------------------


def test_pre_window_incident_clamped_to_window_start() -> None:
    entries = [_alert(T0 + 50, 0)]  # first row is a HARD recovery
    incidents = extract_incidents(entries, window_start=T0, window_end=WINDOW_END)
    assert len(incidents) == 1
    assert incidents[0]["start"] == T0  # clamped
    assert incidents[0]["duration_seconds"] == 50
    assert incidents[0]["ongoing"] is False


def test_pre_window_incident_skipped_without_window_start() -> None:
    entries = [_alert(T0 + 50, 0)]
    incidents = extract_incidents(entries, window_start=None, window_end=WINDOW_END)
    assert incidents == []  # cannot date the start -> dropped


# ---------------------------------------------------------------------------
# MTBF: mean gap between consecutive incident starts (>= 2 incidents)
# ---------------------------------------------------------------------------


def test_mtbf_and_mttr_over_multiple_incidents() -> None:
    entries = [
        _alert(T0 + 100, 2),
        _alert(T0 + 200, 0),  # incident A: dur 100
        _alert(T0 + 1100, 2),
        _alert(T0 + 1300, 0),  # incident B: dur 200
    ]
    m = _summary(entries)
    assert m["incidents"] == 2
    assert m["mttr_seconds"] == 150  # (100 + 200) / 2
    assert m["mtbf_seconds"] == 1000  # start gap 1100 - 100
    assert m["total_downtime_seconds"] == 300
    assert m["longest_incident_seconds"] == 200


# ---------------------------------------------------------------------------
# Criterion 7: empty / single-event safety
# ---------------------------------------------------------------------------


def test_empty_log_yields_zeros_not_error() -> None:
    m = _summary([])
    assert m == {
        "incidents": 0,
        "mttr_seconds": None,
        "mtbf_seconds": None,
        "total_downtime_seconds": 0,
        "longest_incident_seconds": 0,
        "ongoing": False,
    }


def test_numeric_state_type_is_accepted() -> None:
    # Some exports surface state_type as 1 (HARD) / 0 (SOFT) instead of strings.
    entries = [
        _alert(T0 + 100, 2, state_type=0),  # numeric SOFT -> ignored
        {"time": T0 + 200, "state": 2, "state_type": 1, "type": "HOST ALERT"},
        {"time": T0 + 260, "state": 0, "state_type": 1, "type": "HOST ALERT"},
    ]
    m = _summary(entries)
    assert m["incidents"] == 1
    assert m["total_downtime_seconds"] == 60


# ---------------------------------------------------------------------------
# Tool: respx-backed routing + output
# ---------------------------------------------------------------------------


def _post_params(call) -> dict:
    return {k: v[0] for k, v in parse_qs(call.request.content.decode()).items()}


@pytest.mark.asyncio
async def test_reliability_report_tool(mocked_server) -> None:
    mcp, router = mocked_server
    since = str(T0)
    until = str(T0 + 100_000)
    rows = [
        # downtime alert must be ignored by the reducer even if returned
        {"time": T0 + 100, "state": 1, "state_type": "HARD", "type": "HOST DOWNTIME ALERT"},
        {
            "time": T0 + 500,
            "state": 2,
            "state_type": "HARD",
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
        },
        {
            "time": T0 + 800,
            "state": 0,
            "state_type": "HARD",
            "type": "SERVICE ALERT",
            "host_name": "srv01",
            "service_description": "CPU",
        },
    ]
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok(rows))

    result = await mcp.call_tool("thruk_reliability_report", {"since": since, "until": until})
    payload = json.loads(result[0].text)

    assert payload["total_objects"] == 1
    entry = payload["results"][0]
    assert entry["host"] == "srv01"
    assert entry["service"] == "CPU"
    assert entry["incidents"] == 1
    assert entry["mttr_seconds"] == 300
    assert entry["mttr_human"] == "5m"
    assert entry["total_downtime_seconds"] == 300
    assert entry["ongoing"] is False

    params = _post_params(route.calls.last)
    assert params["type[~]"] == "^(HOST|SERVICE) ALERT"
    assert params["class"] == "1"
    assert params["sort"] == "time"
    assert "state_type" in params["columns"]
    assert params["time[gte]"] == since
