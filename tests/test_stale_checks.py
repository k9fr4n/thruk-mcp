"""Tests for ``thruk_stale_checks`` — stale/overdue check-execution detector (issue #287).

Two layers:

1. **Classifier unit tests** — ``_classify_check`` is a pure function (no I/O),
   so the full reason matrix (active stale, passive stale, disabled,
   never_checked, healthy, high-latency, minutes→seconds conversion) is exercised
   in isolation with an injected ``now``.
2. **respx-backed behaviour tests** — drive the live column shape through the
   real MCP dispatch path: ``/services`` (+ ``/hosts``) routing, ``include_hosts``
   toggle, per-reason ``counts`` and spurious-latency sanitization (issue #202).
"""

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import ok
from thruk_mcp.tools.triage import _classify_check

# Default classifier knobs mirroring the tool defaults.
_KW = {
    "staleness_factor": 2.0,
    "latency_threshold_s": 30.0,
    "grace_seconds": 60,
    "passive_max_age_s": 3600,
    "include_disabled": True,
}


def _svc(
    *,
    last_check_age: int,
    check_interval: float = 5.0,  # MINUTES (Livestatus unit, issue #287)
    latency: float = 0.5,
    active_checks_enabled: int = 1,
    has_been_checked: int = 1,
    check_type: int = 0,
    now: int = 1_700_000_000,
) -> dict:
    return {
        "host_name": "h1",
        "description": "svc",
        "last_check": now - last_check_age,
        "check_interval": check_interval,
        "latency": latency,
        "execution_time": 0.1,
        "active_checks_enabled": active_checks_enabled,
        "has_been_checked": has_been_checked,
        "check_type": check_type,
    }


# ---------------------------------------------------------------------------
# _classify_check — unit matrix
# ---------------------------------------------------------------------------

NOW = 1_700_000_000


def test_classify_healthy_active_returns_none() -> None:
    # interval 5 min = 300 s; threshold = 300*2 + 60 = 660 s. age 100 s ⇒ healthy.
    row = _svc(last_check_age=100, now=NOW)
    assert _classify_check(row, now=NOW, **_KW) is None


def test_classify_stale_active() -> None:
    # age 700 s > 660 s threshold ⇒ stale.
    row = _svc(last_check_age=700, now=NOW)
    res = _classify_check(row, now=NOW, **_KW)
    assert res is not None
    assert res["reason"] == "stale"
    assert res["check_interval_s"] == 300  # minutes → seconds conversion
    assert res["service"] == "svc"
    assert res["check_type"] == "active"


def test_classify_interval_minutes_boundary() -> None:
    # Just under the 660 s threshold stays healthy; just over flips to stale.
    assert _classify_check(_svc(last_check_age=659, now=NOW), now=NOW, **_KW) is None
    assert _classify_check(_svc(last_check_age=661, now=NOW), now=NOW, **_KW)["reason"] == "stale"


def test_classify_never_checked() -> None:
    row = _svc(last_check_age=999999, has_been_checked=0, now=NOW)
    res = _classify_check(row, now=NOW, **_KW)
    assert res is not None and res["reason"] == "never_checked"


def test_classify_disabled_active() -> None:
    # Active check with active_checks_enabled=0 ⇒ disabled (separate category).
    row = _svc(last_check_age=700, active_checks_enabled=0, now=NOW)
    res = _classify_check(row, now=NOW, **_KW)
    assert res is not None and res["reason"] == "disabled"


def test_classify_disabled_suppressed_when_flag_off() -> None:
    row = _svc(last_check_age=700, active_checks_enabled=0, now=NOW)
    kw = {**_KW, "include_disabled": False}
    assert _classify_check(row, now=NOW, **kw) is None


def test_classify_high_latency_active() -> None:
    row = _svc(last_check_age=100, latency=42.0, now=NOW)
    res = _classify_check(row, now=NOW, **_KW)
    assert res is not None and res["reason"] == "high_latency"
    assert res["latency_s"] == 42.0


def test_classify_passive_stale_uses_freshness_not_interval() -> None:
    # Passive: interval is meaningless. age 4000 s > passive_max_age_s(3600)+grace(60).
    row = _svc(
        last_check_age=4000, check_interval=0, active_checks_enabled=0, check_type=1, now=NOW
    )
    res = _classify_check(row, now=NOW, **_KW)
    assert res is not None
    assert res["reason"] == "stale_passive"
    assert res["check_type"] == "passive"


def test_classify_passive_disabled_active_is_not_a_fault() -> None:
    # Passive check, fresh, active_checks_enabled=0 (normal) ⇒ healthy, NOT disabled.
    row = _svc(last_check_age=100, check_interval=0, active_checks_enabled=0, check_type=1, now=NOW)
    assert _classify_check(row, now=NOW, **_KW) is None


def test_classify_host_row_has_no_service() -> None:
    # Host rows carry ``name`` and no ``description``.
    host = {
        "name": "router-1",
        "last_check": NOW - 5000,
        "check_interval": 1.0,
        "latency": 0.0,
        "execution_time": 0.0,
        "active_checks_enabled": 1,
        "has_been_checked": 1,
        "check_type": 0,
    }
    res = _classify_check(host, now=NOW, **_KW)
    assert res is not None
    assert res["host"] == "router-1"
    assert res["service"] is None
    assert res["reason"] == "stale"


def test_classify_zero_interval_active_not_stale() -> None:
    # interval 0 ⇒ no schedule to compare against ⇒ not flagged stale.
    row = _svc(last_check_age=999999, check_interval=0, now=NOW)
    assert _classify_check(row, now=NOW, **_KW) is None


# ---------------------------------------------------------------------------
# thruk_stale_checks — behaviour (respx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_checks_queries_services_and_hosts(mocked_server) -> None:
    mcp, router = mocked_server
    now = int(time.time())
    svc = router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {  # stale active service
                    "host_name": "h1",
                    "description": "disk",
                    "last_check": now - 100000,
                    "check_interval": 5.0,
                    "latency": 0.4,
                    "execution_time": 0.1,
                    "active_checks_enabled": 1,
                    "has_been_checked": 1,
                    "check_type": 0,
                },
                {  # healthy active service
                    "host_name": "h1",
                    "description": "load",
                    "last_check": now - 10,
                    "check_interval": 5.0,
                    "latency": 0.3,
                    "execution_time": 0.1,
                    "active_checks_enabled": 1,
                    "has_been_checked": 1,
                    "check_type": 0,
                },
            ]
        )
    )
    hosts = router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [
                {  # never-checked host
                    "name": "newbox",
                    "last_check": 0,
                    "check_interval": 1.0,
                    "latency": 0.0,
                    "execution_time": 0.0,
                    "active_checks_enabled": 1,
                    "has_been_checked": 0,
                    "check_type": 0,
                }
            ]
        )
    )

    result = await mcp.call_tool("thruk_stale_checks", {})
    assert svc.called and hosts.called
    payload = json.loads(result[0].text)
    reasons = {r["reason"] for r in payload["results"]}
    assert reasons == {"stale", "never_checked"}
    assert payload["counts"]["stale"] == 1
    assert payload["counts"]["never_checked"] == 1
    # Never-checked (age None) floats to the top of the stalest-first ordering.
    assert payload["results"][0]["reason"] == "never_checked"
    # The requested columns must include the execution-state fields.
    requested = svc.calls[0].request.url.params.get("columns")
    assert "check_interval" in requested and "has_been_checked" in requested


@pytest.mark.asyncio
async def test_stale_checks_include_hosts_false_skips_hosts(mocked_server) -> None:
    mcp, router = mocked_server
    svc = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    await mcp.call_tool("thruk_stale_checks", {"include_hosts": False})
    assert svc.called
    assert not hosts.called


@pytest.mark.asyncio
async def test_stale_checks_sanitizes_spurious_latency(mocked_server) -> None:
    """A Unix-timestamp-shaped latency (issue #202) must not masquerade as high latency."""
    mcp, router = mocked_server
    now = int(time.time())
    router.get("https://thruk.test/r/services").mock(
        return_value=ok(
            [
                {
                    "host_name": "h1",
                    "description": "ntp",
                    "last_check": now - 10,
                    "check_interval": 5.0,
                    "latency": float(now),  # spurious — ~1.7e9
                    "execution_time": 0.1,
                    "active_checks_enabled": 1,
                    "has_been_checked": 1,
                    "check_type": 0,
                }
            ]
        )
    )
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    result = await mcp.call_tool("thruk_stale_checks", {})
    payload = json.loads(result[0].text)
    # Sanitized to None ⇒ not flagged high_latency ⇒ healthy ⇒ empty results.
    assert payload["results"] == []
    assert "_warnings" in payload  # sanitizer surfaced its aggregated warning


@pytest.mark.asyncio
async def test_stale_checks_rejects_bad_filter_field(mocked_server) -> None:
    mcp, _router = mocked_server
    bad = {"type": "leaf", "field": "state", "op": "eq", "value": "down"}
    result = await mcp.call_tool("thruk_stale_checks", {"filter": bad})
    payload = json.loads(result[0].text)
    assert "error" in payload
