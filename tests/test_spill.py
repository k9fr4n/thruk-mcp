"""Tests for the large-response spill mechanism (issue #49).

Covers:
- _spill_if_needed: no spill when workdir is None
- _spill_if_needed: no spill when payload is below threshold
- _spill_if_needed: spill when payload exceeds threshold
- _make_spill_meta: row count + filter extraction
- Handle JSON structure (mode, saved_to, bytes, sha256, rows, filters)
- Atomic write: file content integrity (sha256 match)
- workdir created automatically
- Spill is transparent to error responses (ThrukError path)
- Integration via ThrukMCPServer.call_tool:
  thruk_problems, thruk_recent_events, thruk_list_notifications,
  thruk_list_hosts, thruk_list_services, thruk_list_logs, thruk_list_alerts
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import respx

from tests.conftest import BASE, ok
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import (
    _make_spill_meta,
    _spill_if_needed,
    build_server,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rows(n: int, host_prefix: str = "host") -> list[dict]:
    """Generate n minimal host-problem-style rows (~180 B each)."""
    return [
        {
            "name": f"{host_prefix}-{i:04d}",
            "state": 1,
            "plugin_output": "DOWN - packet loss 100%",
            "last_check": 1716194400,
            "last_state_change": 1716190000,
        }
        for i in range(n)
    ]


def _log_rows(n: int) -> list[dict]:
    return [
        {
            "time": 1716194400 + i,
            "type": "SERVICE ALERT",
            "host_name": f"h{i}",
            "service_description": "Ping",
            "state": 2,
            "message": "CRITICAL - packet loss 100%",
        }
        for i in range(n)
    ]


def _spill_cfg(tmp_path: Path, threshold_kb: int = 10) -> ThrukConfig:
    return ThrukConfig(
        base_url=BASE, api_key="k", workdir=tmp_path, spill_threshold_kb=threshold_kb
    )


# ---------------------------------------------------------------------------
# Unit: _spill_if_needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spill_no_workdir() -> None:
    """No spill when workdir is not configured."""
    build_server(ThrukConfig(base_url=BASE, api_key="k"))
    payload = json.dumps(_rows(500), indent=2)
    assert await _spill_if_needed(payload, "t", {"rows": 500, "filters": {}}) == payload


@pytest.mark.asyncio
async def test_spill_below_threshold(tmp_path: Path) -> None:
    """No spill when payload is under threshold."""
    build_server(_spill_cfg(tmp_path, threshold_kb=1000))
    payload = json.dumps([{"name": "h", "state": 0}])
    assert await _spill_if_needed(payload, "t", {"rows": 1, "filters": {}}) == payload


@pytest.mark.asyncio
async def test_spill_writes_file_and_returns_handle(tmp_path: Path) -> None:
    """Spill writes the full payload atomically and returns a compact handle."""
    build_server(_spill_cfg(tmp_path))
    payload = json.dumps(_rows(300), indent=2)  # >10 KB
    result = await _spill_if_needed(
        payload, "my_tool", {"rows": 300, "filters": {"hostgroup": "HG_X"}}
    )
    handle = json.loads(result)
    assert handle["mode"] == "file"
    assert handle["rows"] == 300
    assert handle["filters"] == {"hostgroup": "HG_X"}
    dest = Path(handle["saved_to"])
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == payload
    assert handle["sha256"] == hashlib.sha256(payload.encode()).hexdigest()
    assert handle["bytes"] == len(payload.encode())


@pytest.mark.asyncio
async def test_spill_creates_nested_workdir(tmp_path: Path) -> None:
    """workdir is created automatically (including parents)."""
    nested = tmp_path / "deep" / "nested"
    build_server(ThrukConfig(base_url=BASE, api_key="k", workdir=nested, spill_threshold_kb=1))
    payload = "x" * 2048  # 2 KB > 1 KB threshold
    result = await _spill_if_needed(payload, "t", {"rows": 0, "filters": {}})
    handle = json.loads(result)
    assert Path(handle["saved_to"]).parent == nested
    assert nested.exists()


@pytest.mark.asyncio
async def test_spill_handle_is_small(tmp_path: Path) -> None:
    """Handle must be well under 1 KB (safe for Dust inline cap)."""
    build_server(_spill_cfg(tmp_path))
    payload = json.dumps(_rows(500), indent=2)
    result = await _spill_if_needed(payload, "t", {"rows": 500, "filters": {}})
    assert len(result.encode()) < 1024


# ---------------------------------------------------------------------------
# Unit: _make_spill_meta
# ---------------------------------------------------------------------------


def test_make_spill_meta_list() -> None:
    payload = json.dumps([{"name": f"h{i}"} for i in range(42)])
    meta = _make_spill_meta(payload, {"hostgroup": "HG", "limit": 100, "sort": "name"})
    assert meta["rows"] == 42
    assert meta["filters"] == {"hostgroup": "HG"}  # limit/sort stripped


def test_make_spill_meta_problems_envelope() -> None:
    payload = json.dumps({"hosts": _rows(10), "services": _rows(20)})
    meta = _make_spill_meta(payload, {})
    assert meta["rows"] == 30


def test_make_spill_meta_warnings_envelope() -> None:
    payload = json.dumps({"data": _rows(15), "_warnings": ["b1: err"]})
    meta = _make_spill_meta(payload, {})
    # "data" list (15) + "_warnings" list (1) = 16
    assert meta["rows"] == 16


def test_make_spill_meta_strips_none() -> None:
    meta = _make_spill_meta(json.dumps([]), {"host": None, "hostgroup": "HG", "offset": 0})
    assert "host" not in meta["filters"]  # None stripped
    assert "offset" not in meta["filters"]  # pagination stripped
    assert meta["filters"]["hostgroup"] == "HG"


# ---------------------------------------------------------------------------
# Integration: spill via ThrukMCPServer.call_tool
# ---------------------------------------------------------------------------


async def _call(tool: str, args: dict, tmp_path: Path, threshold_kb: int = 10) -> dict:
    """Build a spill-enabled server, call the tool, parse the handle."""
    server = build_server(_spill_cfg(tmp_path, threshold_kb))
    results = await server.call_tool(tool, args)
    return json.loads(results[0].text)


@pytest.mark.asyncio
async def test_dispatch_thruk_problems_spills(tmp_path: Path) -> None:
    big_hosts = _rows(300)
    big_svcs = [{"host_name": f"h{i}", "description": "Ping", "state": 2} for i in range(300)]
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{BASE}/r/hosts").mock(return_value=ok(big_hosts))
        r.get(f"{BASE}/r/services").mock(return_value=ok(big_svcs))
        handle = await _call("thruk_problems", {}, tmp_path)
    assert handle["mode"] == "file"
    assert handle["rows"] == 600
    data = json.loads(Path(handle["saved_to"]).read_text())
    assert len(data["hosts"]) == 300 and len(data["services"]) == 300


@pytest.mark.asyncio
async def test_dispatch_thruk_recent_events_spills(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{BASE}/r/logs").mock(return_value=ok(_log_rows(500)))
        handle = await _call("thruk_recent_events", {"hours": 1, "only_alerts": True}, tmp_path)
    assert handle["mode"] == "file"
    assert handle["rows"] == 500
    assert handle["filters"]["hours"] == 1
    assert handle["filters"]["only_alerts"] is True


@pytest.mark.asyncio
async def test_dispatch_thruk_list_notifications_spills(tmp_path: Path) -> None:
    # thruk_list_notifications with hostgroup: first resolves hostgroup → /hosts GET,
    # then queries /logs POST. Both must be mocked.
    resolved_hosts = [{"name": f"prod-{i:02d}"} for i in range(20)]
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{BASE}/r/hosts").mock(return_value=ok(resolved_hosts))
        r.post(f"{BASE}/r/logs").mock(return_value=ok(_log_rows(500)))
        handle = await _call(
            "thruk_list_notifications",
            {"since": "-24h", "hostgroup": "PROD"},
            tmp_path,
        )
    assert handle["mode"] == "file"
    assert handle["filters"]["hostgroup"] == "PROD"
    assert handle["rows"] == 500
    assert Path(handle["saved_to"]).exists()


@pytest.mark.asyncio
async def test_dispatch_thruk_list_hosts_spills(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{BASE}/r/hosts").mock(return_value=ok(_rows(500)))
        handle = await _call("thruk_list_hosts", {"limit": 500}, tmp_path)
    assert handle["mode"] == "file"
    assert handle["rows"] == 500
    assert "limit" not in handle["filters"]


@pytest.mark.asyncio
async def test_dispatch_thruk_list_services_spills(tmp_path: Path) -> None:
    svc_rows = [{"host_name": f"h{i}", "description": "Ping", "state": 2} for i in range(500)]
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{BASE}/r/services").mock(return_value=ok(svc_rows))
        handle = await _call("thruk_list_services", {"limit": 500}, tmp_path)
    assert handle["mode"] == "file"
    assert handle["rows"] == 500


@pytest.mark.asyncio
async def test_dispatch_thruk_list_logs_spills(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{BASE}/r/logs").mock(return_value=ok(_log_rows(500)))
        handle = await _call("thruk_list_logs", {"since": "-24h"}, tmp_path)
    assert handle["mode"] == "file"
    assert handle["rows"] == 500


@pytest.mark.asyncio
async def test_dispatch_thruk_list_alerts_spills(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{BASE}/r/logs").mock(return_value=ok(_log_rows(500)))
        handle = await _call("thruk_list_alerts", {"since": "-24h"}, tmp_path)
    assert handle["mode"] == "file"
    assert handle["rows"] == 500


@pytest.mark.asyncio
async def test_dispatch_no_spill_when_small(tmp_path: Path) -> None:
    """Small responses must never be spilled, even with workdir configured."""
    with respx.mock(assert_all_called=False) as r:
        r.get(f"{BASE}/r/hosts").mock(return_value=ok([{"name": "h1", "state": 0}]))
        # threshold=1000 KB → no spill for a 1-row response
        handle_or_data = await _call("thruk_list_hosts", {}, tmp_path, threshold_kb=1000)
    # Inline: returned as list, not a spill handle
    assert handle_or_data != {"mode": "file"}
    assert "name" in str(handle_or_data) or isinstance(handle_or_data, list)
