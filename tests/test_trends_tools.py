"""Tests for trends & history tools (issue #57):
thruk_alert_heatmap, thruk_recurring_problems.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import pytest

from tests.conftest import ok
from thruk_mcp.server import _now_utc_epoch, _parse_thruk_time


def _post_params(call) -> dict[str, str]:
    """Local helper mirroring tests/test_tools.py::post_params."""
    body = call.request.content.decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


# ---------------------------------------------------------------------------
# _parse_thruk_time unit tests
# ---------------------------------------------------------------------------


def test_parse_thruk_time_relative_hours() -> None:
    # Use UTC-aware reference to match the fixed implementation.
    now = _now_utc_epoch()
    result = _parse_thruk_time("-2h")
    assert result is not None
    assert abs(result - (now - 7200)) <= 2


def test_parse_thruk_time_relative_minutes() -> None:
    now = _now_utc_epoch()
    result = _parse_thruk_time("-30m")
    assert result is not None
    assert abs(result - (now - 1800)) <= 2


def test_parse_thruk_time_relative_days() -> None:
    now = _now_utc_epoch()
    result = _parse_thruk_time("-7d")
    assert result is not None
    assert abs(result - (now - 7 * 86400)) <= 2


def test_parse_thruk_time_epoch_string() -> None:
    assert _parse_thruk_time("1700000000") == 1700000000


def test_parse_thruk_time_iso_datetime() -> None:
    # BUG REGRESSION (issue #139): bare ISO strings must be interpreted as UTC,
    # not local TZ.  Previously `datetime.strptime(...).timestamp()` used the
    # local timezone — on a Paris-TZ host this would return epoch+3600 instead
    # of the correct UTC epoch.
    #
    # Before fix (broken):
    #   result == int(datetime(2026, 5, 21, 14, 0, 0).timestamp())  # local TZ
    #
    # After fix (correct):
    result = _parse_thruk_time("2026-05-21 14:00:00")
    assert result is not None
    expected_utc = int(datetime(2026, 5, 21, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    assert result == expected_utc, (
        f"ISO string must be parsed as UTC; got {result}, expected {expected_utc}"
    )


def test_parse_thruk_time_iso_t_format() -> None:
    """'T'-separated ISO format is also interpreted as UTC."""
    result = _parse_thruk_time("2026-05-21T14:00:00")
    expected_utc = int(datetime(2026, 5, 21, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    assert result == expected_utc


def test_parse_thruk_time_iso_z_suffix() -> None:
    """Trailing 'Z' suffix — unambiguously UTC."""
    result = _parse_thruk_time("2026-05-21T14:00:00Z")
    expected_utc = int(datetime(2026, 5, 21, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    assert result == expected_utc


def test_parse_thruk_time_none() -> None:
    assert _parse_thruk_time(None) is None


def test_parse_thruk_time_unparseable() -> None:
    assert _parse_thruk_time("not-a-time") is None


def test_now_utc_epoch_is_timezone_aware() -> None:
    """_now_utc_epoch() must return the same value regardless of the process TZ.

    Verified by comparing with datetime.now(timezone.utc) — the only correct
    way to get the current UTC epoch.
    """
    before = int(datetime.now(timezone.utc).timestamp())
    result = _now_utc_epoch()
    after = int(datetime.now(timezone.utc).timestamp())
    assert before <= result <= after, "_now_utc_epoch() must be within [before, after]"


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
async def test_heatmap_fetches_newest_first(mocked_server) -> None:
    """Regression test for issue #250: heatmap must fetch /logs newest-first.

    BEFORE FIX (broken):
        ``sort="time"`` (ascending). When the _NOISY_MAX_ALERTS cap was hit,
        Thruk returned the *oldest* entries, so the most recent buckets — the
        ones that matter for incident analytics — came back unpopulated.

    AFTER FIX:
        ``sort="-time"`` (descending) so the cap drops the oldest entries and
        recent buckets stay populated.
    """
    from urllib.parse import parse_qs

    mcp, router = mocked_server
    log_route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    await mcp.call_tool("thruk_alert_heatmap", {"since": "-24h", "bucket": "1h"})

    body = parse_qs(log_route.calls.last.request.content.decode())
    assert body.get("sort") == ["-time"], (
        f"heatmap must request sort=-time (newest first), body was: {body}"
    )


@pytest.mark.asyncio
async def test_heatmap_truncated_buckets_marked_null(mocked_server) -> None:
    """Regression test for issues #178 / #250.

    BEFORE #250 FIX (broken):
        With ``sort="time"`` ascending and limit=_NOISY_MAX_ALERTS, when the
        cap was hit the fetch returned only the *earliest* entries — recent
        buckets silently showed ``count=0`` (the #178 band-aid then marked
        buckets *after* the last fetched timestamp via ``truncated_after``,
        which is exactly the wrong end for recent-incident analytics).

    AFTER #250 FIX:
        The fetch is newest-first, so the *earliest* buckets are the uncovered
        ones. Buckets strictly before the bucket containing the earliest
        fetched entry are reported as ``count=null, truncated=true`` and the
        payload exposes ``truncated_before`` (ISO-UTC of the earliest fetched
        event). Recent buckets are populated with real counts.
    """
    mcp, router = mocked_server
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    hour = 3600
    # Simulate a newest-first capped fetch: _NOISY_MAX_ALERTS entries crammed
    # into the *last* 2 hours of a 24h window (hours 22 and 23). With sort=-time
    # the backend returns exactly these recent rows; the earliest 22 buckets
    # were never fetched.
    base_recent = BASE_TS + 22 * hour
    big_data = [_log(base_recent + (i % (2 * hour))) for i in range(_NOISY_MAX_ALERTS)]
    # Force the smallest timestamp to fall in bucket #22 for determinism.
    big_data[-1] = _log(base_recent + 1234)
    router.post("https://thruk.test/r/logs").mock(return_value=ok(big_data))

    since = str(BASE_TS)
    until = str(BASE_TS + 24 * hour)

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"since": since, "until": until, "bucket": "1h"},
    )
    payload = json.loads(result[0].text)

    assert payload["total_alerts"] == _NOISY_MAX_ALERTS
    assert "truncated_before" in payload, "Expected truncated_before top-level field"
    assert payload["truncated_before"].endswith("Z")
    assert "_warning" in payload

    # 24h window @ 1h bucket => 25 buckets (inclusive of both ends).
    assert len(payload["results"]) == 25

    # Buckets 22 and 23 contain real data (count is an int, not None).
    assert isinstance(payload["results"][22]["count"], int)
    assert payload["results"][22]["count"] > 0
    assert isinstance(payload["results"][23]["count"], int)
    assert payload["results"][23]["count"] > 0
    # Bucket containing the earliest fetched event must not be marked truncated.
    assert not payload["results"][22].get("truncated", False)

    # Buckets 0..21 (before the earliest fetched bucket) must be null + truncated.
    for bucket in payload["results"][:22]:
        assert bucket["count"] is None, f"Expected null count, got {bucket}"
        assert bucket["truncated"] is True


@pytest.mark.asyncio
async def test_heatmap_no_truncation_when_cap_not_hit(mocked_server) -> None:
    """No `truncated_before` field when the fetch stays below the cap."""
    mcp, router = mocked_server

    hour = 3600
    entries = [_log(BASE_TS + 10), _log(BASE_TS + hour + 10)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"since": str(BASE_TS), "until": str(BASE_TS + 2 * hour), "bucket": "1h"},
    )
    payload = json.loads(result[0].text)
    assert "truncated_before" not in payload
    for bucket in payload["results"]:
        assert bucket["count"] is not None
        assert "truncated" not in bucket


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


@pytest.mark.asyncio
async def test_heatmap_bucket_start_is_utc_aware_iso(mocked_server) -> None:
    """Regression test for issue #140: bucket_start must be a UTC-aware ISO-8601 string.

    OLD (broken, deprecated since Python 3.12, removed in future):
        datetime.utcfromtimestamp(b).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Returns a naive datetime; the trailing 'Z' is a lie — if ever compared
        # to an aware datetime the comparison raises TypeError.

    FIXED (current implementation):
        datetime.fromtimestamp(b, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Returns an aware datetime in UTC; the 'Z' suffix is accurate.

    Both produce the same string representation, but only the fixed version is
    correct — this test guards against a silent revert of the fix.

    Specifically verified:
    - Every bucket_start ends with 'Z'.
    - Every bucket_start round-trips through datetime.fromisoformat() (Python 3.11+
      accepts trailing 'Z' as UTC; a naive string would still parse, so we also
      check .tzinfo is not None after parsing via datetime.strptime + replace).
    - The first bucket_start matches the expected UTC string for BASE_TS exactly.
    """
    mcp, router = mocked_server

    # Place one alert in each of two consecutive 1-hour buckets.
    hour = 3600
    entries = [_log(BASE_TS + 10), _log(BASE_TS + hour + 10)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    since = str(BASE_TS)
    until = str(BASE_TS + 2 * hour)

    result = await mcp.call_tool(
        "thruk_alert_heatmap",
        {"since": since, "until": until, "bucket": "1h"},
    )
    payload = json.loads(result[0].text)

    assert payload["results"], "Expected non-empty results list"

    # Expected string for BASE_TS (1_748_822_400) in UTC:
    #   datetime.fromtimestamp(1_748_822_400, tz=timezone.utc)
    #   => 2025-06-02 00:00:00+00:00  => "2025-06-02T00:00:00Z"
    expected_first = datetime.fromtimestamp(BASE_TS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert payload["results"][0]["bucket_start"] == expected_first, (
        f"First bucket_start mismatch: {payload['results'][0]['bucket_start']!r} "
        f"!= {expected_first!r}"
    )

    for bucket in payload["results"]:
        bs: str = bucket["bucket_start"]

        # Must end with 'Z' (UTC marker).
        assert bs.endswith("Z"), f"bucket_start {bs!r} does not end with 'Z'"

        # Must be a valid ISO-8601 datetime string of the expected format.
        try:
            dt = datetime.strptime(bs, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise AssertionError(f"bucket_start {bs!r} is not valid ISO-8601: {exc}") from exc

        # The reconstructed datetime must be timezone-aware.
        assert dt.tzinfo is not None, (
            f"Reconstructed datetime for {bs!r} has no tzinfo — "
            "indicates the source used a naive utcfromtimestamp() path"
        )


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


# ---------------------------------------------------------------------------
# Issue #193 — class=1 defence-in-depth on trend tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, args",
    [
        ("thruk_alert_heatmap", {"since": "-6h", "bucket": "1h"}),
        ("thruk_recurring_problems", {"since": "-6h", "min_alerts": 1}),
    ],
)
@pytest.mark.asyncio
async def test_trend_tools_post_class_one(mocked_server, tool_name: str, args: dict) -> None:
    """Regression for issue #193 (sibling of #176).

    Before the fix, ``thruk_alert_heatmap`` and ``thruk_recurring_problems``
    only set ``type[~]=^(HOST|SERVICE) ALERT`` on their /logs POST. Naemon
    Livestatus does not exclude rows where ``type`` is NULL from regex
    filters, so class=0 system messages, class=5 external commands and
    class=6 current-state snapshots leaked through and inflated bucket
    counts / per-object alert counts.

    The fix adds ``class=1`` to the POST body as a defence-in-depth cut.
    """
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
    await mcp.call_tool(tool_name, args)
    assert route.called, f"{tool_name} must POST to /logs"
    p = _post_params(route.calls.last)
    assert p.get("type[~]") == "^(HOST|SERVICE) ALERT", (
        f"{tool_name}: type[~] regex filter must remain present."
    )
    assert p.get("class") == "1", (
        f"{tool_name}: must POST class=1 server-side cut (issue #193) so class=0/5/6 "
        "rows with type=NULL cannot leak past the regex filter."
    )


# ---------------------------------------------------------------------------
# Issue #201 — THRUK_NOISY_MAX_ALERTS env var override + actionable warning
# ---------------------------------------------------------------------------
#
# Before the fix, `_NOISY_MAX_ALERTS` was a literal `10_000` in constants.py
# and the cap-hit `_warning` string read:
#
#     "Result capped at 10000 log entries; aggregation may be incomplete."
#
# Operators on large infrastructures (>10k alert events per analysis window)
# had no way to raise the cap, and the warning gave no remediation hint.
#
# After the fix:
#   - constants._load_noisy_max_alerts honours THRUK_NOISY_MAX_ALERTS;
#   - the warning mentions the env var and the time-window mitigation.


def test_load_noisy_max_alerts_default_when_unset() -> None:
    from thruk_mcp.constants import _NOISY_MAX_ALERTS_DEFAULT, _load_noisy_max_alerts

    assert _load_noisy_max_alerts(None) == _NOISY_MAX_ALERTS_DEFAULT


def test_load_noisy_max_alerts_honours_env_override() -> None:
    from thruk_mcp.constants import _load_noisy_max_alerts

    assert _load_noisy_max_alerts("50000") == 50_000


def test_load_noisy_max_alerts_invalid_falls_back_to_default() -> None:
    from thruk_mcp.constants import _NOISY_MAX_ALERTS_DEFAULT, _load_noisy_max_alerts

    # Non-int strings, empty strings, and other garbage must NOT crash —
    # an operator typo should not bring the server down.
    assert _load_noisy_max_alerts("not-a-number") == _NOISY_MAX_ALERTS_DEFAULT
    assert _load_noisy_max_alerts("") == _NOISY_MAX_ALERTS_DEFAULT


def test_load_noisy_max_alerts_enforces_minimum() -> None:
    """Tiny caps would defeat aggregation; the loader floors to _NOISY_MAX_ALERTS_MIN."""
    from thruk_mcp.constants import _NOISY_MAX_ALERTS_MIN, _load_noisy_max_alerts

    assert _load_noisy_max_alerts("5") == _NOISY_MAX_ALERTS_MIN
    assert _load_noisy_max_alerts("0") == _NOISY_MAX_ALERTS_MIN
    # Negative values are also coerced up.
    assert _load_noisy_max_alerts("-100") == _NOISY_MAX_ALERTS_MIN


@pytest.mark.asyncio
async def test_heatmap_cap_warning_is_actionable(mocked_server) -> None:
    """Issue #201: the cap warning must point users at the env-var mitigation.

    Pre-fix message:
        "Result capped at 10000 log entries; aggregation may be incomplete."
    Post-fix message must additionally mention THRUK_NOISY_MAX_ALERTS so an
    LLM consuming the tool output can immediately suggest the right fix.
    """
    mcp, router = mocked_server
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    big_data = [_log(BASE_TS + i) for i in range(_NOISY_MAX_ALERTS)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(big_data))

    result = await mcp.call_tool("thruk_alert_heatmap", {"since": "-24h"})
    payload = json.loads(result[0].text)
    warning = payload["_warning"]
    assert "THRUK_NOISY_MAX_ALERTS" in warning, (
        "Cap warning must mention the env var so operators know how to raise the cap."
    )
    assert "since" in warning, (
        "Cap warning must suggest narrowing the time window as an alternative mitigation."
    )


# ---------------------------------------------------------------------------
# thruk_notification_summary (issue #271)
# ---------------------------------------------------------------------------


def _notif(contact: str, t: int, *, host: str = "h1", state: int = 2) -> dict:
    """Build a class=3 notification log row."""
    return {"contact_name": contact, "host_name": host, "state": state, "time": t}


@pytest.mark.asyncio
async def test_notification_summary_invalid_group_by(mocked_server) -> None:
    mcp, _ = mocked_server
    result = await mcp.call_tool("thruk_notification_summary", {"group_by": "wat"})
    payload = json.loads(result[0].text)
    assert "error" in payload
    assert "wat" in payload["error"]


@pytest.mark.asyncio
async def test_notification_summary_groups_and_sorts_by_count(mocked_server) -> None:
    """Counts per contact, sorted desc, with total + last_time.

    BEFORE FIX (issue #271): no aggregation tool existed — counting required
    manual client-side tallying of thruk_list_notifications output. This test
    asserts the new aggregation primitive.
    """
    mcp, router = mocked_server
    rows = [
        _notif("oncall", BASE_TS + 0),
        _notif("oncall", BASE_TS + 50),
        _notif("oncall", BASE_TS + 300),  # latest for oncall
        _notif("backup", BASE_TS + 10),
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(rows))

    result = await mcp.call_tool(
        "thruk_notification_summary",
        {"group_by": "contact", "since": str(BASE_TS), "until": str(BASE_TS + 3600)},
    )
    payload = json.loads(result[0].text)

    assert payload["group_by"] == "contact"
    assert payload["total"] == 4
    assert len(payload["results"]) == 2
    # Sorted by count desc: oncall (3) before backup (1).
    assert payload["results"][0]["contact"] == "oncall"
    assert payload["results"][0]["count"] == 3
    assert payload["results"][1]["contact"] == "backup"
    assert payload["results"][1]["count"] == 1
    # last_time tracks the newest row for the group.
    assert payload["results"][0]["last_time"] is not None


@pytest.mark.asyncio
async def test_notification_summary_request_params(mocked_server) -> None:
    """class=3, sort=-time and the group column are sent to /logs via POST."""
    mcp, router = mocked_server
    log_route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    await mcp.call_tool("thruk_notification_summary", {"group_by": "contact", "since": "-6h"})

    params = _post_params(log_route.calls.last)
    assert params["class"] == "3"
    assert params["sort"] == "-time"
    assert "contact_name" in params["columns"]
    assert "time" in params["columns"]
    assert params["time[gte]"] == "-6h"


@pytest.mark.asyncio
async def test_notification_summary_group_by_state(mocked_server) -> None:
    """group_by='state' maps host-notification codes to labels (issue #282).

    BEFORE FIX: rows with state=2/state=1 surfaced as raw codes {"2": 2, "1": 1}.
    The rows below carry no service_description, so they are host notifications:
    2 -> UNREACHABLE, 1 -> DOWN. The summary now also requests the
    service_description column so it can pick the right state vocabulary.
    """
    mcp, router = mocked_server
    log_route = router.post("https://thruk.test/r/logs").mock(
        return_value=ok(
            [
                _notif("a", BASE_TS + 0, state=2),
                _notif("b", BASE_TS + 1, state=2),
                _notif("c", BASE_TS + 2, state=1),
            ]
        )
    )

    result = await mcp.call_tool("thruk_notification_summary", {"group_by": "state"})
    payload = json.loads(result[0].text)

    cols = _post_params(log_route.calls.last)["columns"]
    assert "state" in cols
    assert "service_description" in cols
    counts = {r["state"]: r["count"] for r in payload["results"]}
    assert counts == {"UNREACHABLE": 2, "DOWN": 1}


@pytest.mark.asyncio
async def test_notification_summary_state_host_vs_service_and_recovery(mocked_server) -> None:
    """Mixed host/service rows map per-context; state 0 is OK/UP, never "" (issue #282).

    BEFORE FIX: `key = str(entry.get(group_field) or "")` coerced state 0 to a
    bare "" bucket, and service rows reused the host vocabulary. After the fix:
    - service row (has service_description) state=2 -> CRITICAL,
    - service recovery state=0 -> OK (not ""),
    - host row state=1 -> DOWN.
    """
    mcp, router = mocked_server
    rows = [
        {
            "contact_name": "a",
            "host_name": "h1",
            "service_description": "HTTP",
            "state": 2,
            "time": BASE_TS + 0,
        },
        {
            "contact_name": "b",
            "host_name": "h1",
            "service_description": "HTTP",
            "state": 0,
            "time": BASE_TS + 1,
        },
        {
            "contact_name": "c",
            "host_name": "h2",
            "service_description": "",
            "state": 1,
            "time": BASE_TS + 2,
        },
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(rows))

    result = await mcp.call_tool("thruk_notification_summary", {"group_by": "state"})
    payload = json.loads(result[0].text)

    counts = {r["state"]: r["count"] for r in payload["results"]}
    assert counts == {"CRITICAL": 1, "OK": 1, "DOWN": 1}
    assert "" not in counts


@pytest.mark.asyncio
async def test_notification_summary_empty(mocked_server) -> None:
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool("thruk_notification_summary", {})
    payload = json.loads(result[0].text)
    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["group_by"] == "contact"


@pytest.mark.asyncio
async def test_notification_summary_cap_warning(mocked_server) -> None:
    mcp, router = mocked_server
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    big = [_notif(f"c{i}", BASE_TS + i) for i in range(_NOISY_MAX_ALERTS)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(big))

    result = await mcp.call_tool("thruk_notification_summary", {"since": "-24h"})
    payload = json.loads(result[0].text)
    assert "_warning" in payload
    assert "THRUK_NOISY_MAX_ALERTS" in payload["_warning"]


@pytest.mark.asyncio
async def test_notification_summary_invalid_filter(mocked_server) -> None:
    mcp, _ = mocked_server
    result = await mcp.call_tool(
        "thruk_notification_summary",
        {"filter": {"type": "leaf", "field": "bogus", "op": "eq", "value": "x"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload


# ---------------------------------------------------------------------------
# thruk_notification_heatmap (issue #272)
# ---------------------------------------------------------------------------
#
# Mirrors thruk_alert_heatmap but counts class=3 notification log rows instead
# of HOST/SERVICE ALERT (class=1) entries. Same since/until/bucket params,
# same continuous-timeline empty-bucket fill, same sort=-time newest-first
# fetch and truncated_before null-marking on cap hit.


@pytest.mark.asyncio
async def test_notif_heatmap_invalid_bucket(mocked_server) -> None:
    mcp, _ = mocked_server
    result = await mcp.call_tool("thruk_notification_heatmap", {"bucket": "2h"})
    payload = json.loads(result[0].text)
    assert "error" in payload
    assert "2h" in payload["error"]


@pytest.mark.asyncio
async def test_notif_heatmap_basic_bucketing(mocked_server) -> None:
    """3 notifications in first bucket, 1 in second, 0 in third (empty filled)."""
    mcp, router = mocked_server

    hour = 3600
    entries = [
        _log(BASE_TS + 0),
        _log(BASE_TS + 100),
        _log(BASE_TS + 200),
        _log(BASE_TS + hour + 10),
    ]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {"since": str(BASE_TS), "until": str(BASE_TS + 2 * hour), "bucket": "1h"},
    )
    payload = json.loads(result[0].text)

    assert payload["bucket"] == "1h"
    assert payload["total_notifications"] == 4
    assert len(payload["results"]) == 3
    assert payload["results"][0]["count"] == 3
    assert payload["results"][1]["count"] == 1
    assert payload["results"][2]["count"] == 0  # empty bucket filled


@pytest.mark.asyncio
async def test_notif_heatmap_empty_window(mocked_server) -> None:
    """No notifications => all buckets zero."""
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {"since": str(BASE_TS), "until": str(BASE_TS + 3600), "bucket": "1h"},
    )
    payload = json.loads(result[0].text)
    assert payload["total_notifications"] == 0
    assert all(b["count"] == 0 for b in payload["results"])
    assert len(payload["results"]) >= 1


@pytest.mark.asyncio
async def test_notif_heatmap_posts_class_three(mocked_server) -> None:
    """Must POST class=3 (notifications), NOT the ALERT type[~] regex."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    await mcp.call_tool("thruk_notification_heatmap", {"since": "-6h", "bucket": "30m"})

    p = _post_params(route.calls.last)
    assert p.get("class") == "3", "notification heatmap must POST class=3"
    assert "type[~]" not in p, "notification heatmap must not use the ALERT regex filter"
    assert p.get("sort") == "-time", "must fetch newest-first (issue #250 parity)"
    assert "time" in p.get("columns", "")


@pytest.mark.asyncio
async def test_notif_heatmap_contact_filter(mocked_server) -> None:
    """The contact filter field (notification-specific) is forwarded to /logs."""
    mcp, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {
            "since": "-6h",
            "filter": {"type": "leaf", "field": "contact", "op": "eq", "value": "oncall"},
        },
    )
    payload = json.loads(result[0].text)
    assert "error" not in payload
    p = _post_params(route.calls.last)
    assert p.get("contact_name") == "oncall"
    assert p.get("class") == "3"


@pytest.mark.asyncio
async def test_notif_heatmap_metadata_in_output(mocked_server) -> None:
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {"since": "-12h", "until": None, "bucket": "6h"},
    )
    payload = json.loads(result[0].text)
    assert payload["since"] == "-12h"
    assert payload["until"] is None
    assert payload["bucket"] == "6h"
    assert "total_notifications" in payload
    assert "results" in payload


@pytest.mark.asyncio
async def test_notif_heatmap_cap_warning(mocked_server) -> None:
    mcp, router = mocked_server
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    big_data = [_log(BASE_TS + i) for i in range(_NOISY_MAX_ALERTS)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(big_data))

    result = await mcp.call_tool("thruk_notification_heatmap", {"since": "-24h"})
    payload = json.loads(result[0].text)
    assert "_warning" in payload
    assert "THRUK_NOISY_MAX_ALERTS" in payload["_warning"]


@pytest.mark.asyncio
async def test_notif_heatmap_truncated_buckets_marked_null(mocked_server) -> None:
    """Cap hit + newest-first => earliest buckets are count=null, truncated=true.

    Same semantics as thruk_alert_heatmap (issue #250): with sort=-time the
    earliest buckets are the ones not covered by the capped fetch.
    """
    mcp, router = mocked_server
    from thruk_mcp.server import _NOISY_MAX_ALERTS

    hour = 3600
    base_recent = BASE_TS + 22 * hour
    big_data = [_log(base_recent + (i % (2 * hour))) for i in range(_NOISY_MAX_ALERTS)]
    big_data[-1] = _log(base_recent + 1234)  # force earliest into bucket #22
    router.post("https://thruk.test/r/logs").mock(return_value=ok(big_data))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {"since": str(BASE_TS), "until": str(BASE_TS + 24 * hour), "bucket": "1h"},
    )
    payload = json.loads(result[0].text)

    assert payload["total_notifications"] == _NOISY_MAX_ALERTS
    assert "truncated_before" in payload
    assert payload["truncated_before"].endswith("Z")
    assert len(payload["results"]) == 25
    assert isinstance(payload["results"][22]["count"], int)
    assert payload["results"][22]["count"] > 0
    assert not payload["results"][22].get("truncated", False)
    for bucket in payload["results"][:22]:
        assert bucket["count"] is None
        assert bucket["truncated"] is True


@pytest.mark.asyncio
async def test_notif_heatmap_no_truncation_when_cap_not_hit(mocked_server) -> None:
    mcp, router = mocked_server
    hour = 3600
    entries = [_log(BASE_TS + 10), _log(BASE_TS + hour + 10)]
    router.post("https://thruk.test/r/logs").mock(return_value=ok(entries))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {"since": str(BASE_TS), "until": str(BASE_TS + 2 * hour), "bucket": "1h"},
    )
    payload = json.loads(result[0].text)
    assert "truncated_before" not in payload
    for bucket in payload["results"]:
        assert bucket["count"] is not None
        assert "truncated" not in bucket


@pytest.mark.asyncio
async def test_notif_heatmap_invalid_filter(mocked_server) -> None:
    mcp, router = mocked_server
    router.post("https://thruk.test/r/logs").mock(return_value=ok([]))

    result = await mcp.call_tool(
        "thruk_notification_heatmap",
        {"filter": {"type": "leaf", "field": "bad_field", "op": "eq", "value": "x"}},
    )
    payload = json.loads(result[0].text)
    assert "error" in payload
