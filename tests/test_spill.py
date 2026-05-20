"""Tests for the large-response spill mechanism (issue #49).

Covers:
- _spill_if_needed: no spill when workdir is None
- _spill_if_needed: no spill when payload is below threshold
- _spill_if_needed: spill when payload exceeds threshold
- Handle JSON structure validation
- Atomic write: file content integrity (sha256 match)
- thruk_problems integration: spill triggered on large response
- thruk_recent_events integration: spill triggered on large response
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import respx

import thruk_mcp.server as srv_mod
from tests.conftest import BASE, ok
from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import _TOOL_DISPATCH, _spill_if_needed, build_server


def _big_payload(n: int = 500) -> str:
    """Generate a JSON list with n rows (each ~250 bytes → ~125 KB for n=500)."""
    rows = [
        {
            "host_name": f"host-{i:04d}",
            "description": "Ping",
            "state": 2,
            "plugin_output": "CRITICAL - packet loss 100%",
            "last_check": 1716194400,
            "last_state_change": 1716190000,
        }
        for i in range(n)
    ]
    return json.dumps(rows, indent=2)


# ---------------------------------------------------------------------------
# Unit tests for _spill_if_needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spill_no_workdir() -> None:
    """No spill when workdir is not configured."""
    build_server(ThrukConfig(base_url=BASE, api_key="k"))
    payload = _big_payload(500)
    result = await _spill_if_needed(payload, "test_tool", {"rows": 500, "filters": {}})
    assert result == payload


@pytest.mark.asyncio
async def test_spill_below_threshold(tmp_path: Path) -> None:
    """No spill when payload is below the threshold."""
    build_server(ThrukConfig(base_url=BASE, api_key="k", workdir=tmp_path, spill_threshold_kb=1000))
    payload = json.dumps([{"host_name": "h", "state": 0}])
    result = await _spill_if_needed(payload, "test_tool", {"rows": 1, "filters": {}})
    assert result == payload


@pytest.mark.asyncio
async def test_spill_writes_file(tmp_path: Path) -> None:
    """Spill writes the full payload to disk and returns a compact handle."""
    build_server(ThrukConfig(base_url=BASE, api_key="k", workdir=tmp_path, spill_threshold_kb=10))
    payload = _big_payload(300)
    result = await _spill_if_needed(
        payload, "my_tool", {"rows": 300, "filters": {"hostgroup": "HG_X"}}
    )
    handle = json.loads(result)
    assert handle["mode"] == "file"
    assert handle["rows"] == 300
    assert handle["filters"] == {"hostgroup": "HG_X"}
    assert "saved_to" in handle
    assert "sha256" in handle
    assert "bytes" in handle
    dest = Path(handle["saved_to"])
    assert dest.exists()
    written = dest.read_text(encoding="utf-8")
    assert written == payload
    expected_sha = hashlib.sha256(payload.encode()).hexdigest()
    assert handle["sha256"] == expected_sha


@pytest.mark.asyncio
async def test_spill_creates_workdir(tmp_path: Path) -> None:
    """workdir is created automatically if it does not exist."""
    nested = tmp_path / "deep" / "nested"
    build_server(ThrukConfig(base_url=BASE, api_key="k", workdir=nested, spill_threshold_kb=1))
    payload = "x" * 2048  # 2 KB > 1 KB threshold
    result = await _spill_if_needed(payload, "t", {"rows": 0, "filters": {}})
    handle = json.loads(result)
    assert Path(handle["saved_to"]).parent == nested
    assert nested.exists()


@pytest.mark.asyncio
async def test_spill_handle_is_small(tmp_path: Path) -> None:
    """The returned handle JSON must be well below 1 KB."""
    build_server(ThrukConfig(base_url=BASE, api_key="k", workdir=tmp_path, spill_threshold_kb=10))
    payload = _big_payload(500)
    result = await _spill_if_needed(payload, "my_tool", {"rows": 500, "filters": {}})
    assert len(result.encode()) < 1024, f"Handle too large: {len(result.encode())} bytes"


# ---------------------------------------------------------------------------
# Integration: thruk_problems
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thruk_problems_spills(tmp_path: Path) -> None:
    """thruk_problems spills to workdir when the combined response is large."""
    new_cfg = ThrukConfig(base_url=BASE, api_key="k", workdir=tmp_path, spill_threshold_kb=10)
    new_client = ThrukClient(new_cfg)
    original = srv_mod._client
    srv_mod._client = new_client

    big_hosts = [{"name": f"h{i}", "state": 1, "plugin_output": "DOWN"} for i in range(300)]
    big_svcs = [{"host_name": f"h{i}", "description": "Ping", "state": 2} for i in range(300)]

    try:
        with respx.mock(assert_all_called=False) as r:
            r.get(f"{BASE}/r/hosts").mock(return_value=ok(big_hosts))
            r.get(f"{BASE}/r/services").mock(return_value=ok(big_svcs))
            result_str = await _TOOL_DISPATCH["thruk_problems"]()
    finally:
        srv_mod._client = original
        await new_client.aclose()

    handle = json.loads(result_str)
    assert handle["mode"] == "file"
    assert handle["rows"] == 600
    dest = Path(handle["saved_to"])
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert len(data["hosts"]) == 300
    assert len(data["services"]) == 300


# ---------------------------------------------------------------------------
# Integration: thruk_recent_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thruk_recent_events_spills(tmp_path: Path) -> None:
    """thruk_recent_events spills to workdir when log response is large."""
    new_cfg = ThrukConfig(base_url=BASE, api_key="k", workdir=tmp_path, spill_threshold_kb=10)
    new_client = ThrukClient(new_cfg)
    original = srv_mod._client
    srv_mod._client = new_client

    big_logs = [
        {
            "time": 1716194400 + i,
            "type": "SERVICE ALERT",
            "host_name": f"h{i}",
            "service_description": "Ping",
            "state": 2,
            "message": "CRITICAL - packet loss",
        }
        for i in range(500)
    ]

    try:
        with respx.mock(assert_all_called=False) as r:
            r.post(f"{BASE}/r/logs").mock(return_value=ok(big_logs))
            result_str = await _TOOL_DISPATCH["thruk_recent_events"](hours=1, only_alerts=True)
    finally:
        srv_mod._client = original
        await new_client.aclose()

    handle = json.loads(result_str)
    assert handle["mode"] == "file"
    assert handle["rows"] == 500
    assert handle["filters"]["hours"] == 1
    assert handle["filters"]["only_alerts"] is True
    dest = Path(handle["saved_to"])
    assert dest.exists()
