"""Issue #202 — sanitise spurious latency values surfaced by Naemon.

Background: Naemon/Livestatus occasionally writes a Unix-timestamp-shaped
value (~1.7e9) into the host ``latency`` column.  Surfacing that verbatim
misleads LLM clients into reporting decades of latency.  The fix nullifies
any ``latency`` / ``host_latency`` value above the sanity cap (3600 s by
default) and emits a single aggregated ``_warnings`` entry.

The respx-mocked HTTP fixtures below reproduce the bug payload that the
upstream Thruk REST API returns; before the fix, the raw value would leak
straight through ``_tool_response``.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ok
from thruk_mcp.helpers import _sanitize_latency

# ---------------------------------------------------------------- pure helper


def test_sanitize_latency_nullifies_above_cap() -> None:
    payload = {"name": "ecrmut-ad-01", "latency": 1779764194.9, "state": 0}
    sanitized, warns = _sanitize_latency(payload, cap_seconds=3600.0)
    assert sanitized["latency"] is None
    assert sanitized["state"] == 0
    assert len(warns) == 1
    assert "ecrmut-ad-01" in warns[0]
    assert "issue #202" in warns[0]


def test_sanitize_latency_preserves_legitimate_values() -> None:
    payload = {"name": "srv01", "latency": 0.42, "host_latency": 12.5}
    sanitized, warns = _sanitize_latency(payload, cap_seconds=3600.0)
    assert sanitized["latency"] == 0.42
    assert sanitized["host_latency"] == 12.5
    assert warns == []


def test_sanitize_latency_walks_list_and_dedupes_hosts() -> None:
    payload = [
        {"name": "hostA", "latency": 1.0},
        {"name": "hostB", "latency": 1_700_000_000.0},
        {"name": "hostB", "latency": 1_700_000_001.0},  # same host twice
        {"name": "hostC", "latency": 2_000_000_000.0},
    ]
    sanitized, warns = _sanitize_latency(payload, cap_seconds=3600.0)
    assert sanitized[1]["latency"] is None
    assert sanitized[2]["latency"] is None
    assert sanitized[3]["latency"] is None
    assert sanitized[0]["latency"] == 1.0
    assert len(warns) == 1
    # hostB appears once in the deduplicated sample.
    assert warns[0].count("hostB") == 1
    assert "hostC" in warns[0]


def test_sanitize_latency_handles_service_row_host_latency() -> None:
    payload = {
        "host_name": "ecrmut-ad-01",
        "description": "SYSTEM_CPU_USAGE",
        "host_latency": 1779764723.1,
        "latency": 0.98,  # service-level latency is fine
    }
    sanitized, warns = _sanitize_latency(payload, cap_seconds=3600.0)
    assert sanitized["host_latency"] is None
    assert sanitized["latency"] == 0.98
    assert "ecrmut-ad-01" in warns[0]


def test_sanitize_latency_boolean_is_not_a_number() -> None:
    # Defensive: ``isinstance(True, int)`` is True in Python.  We must not
    # interpret a stray boolean as a latency reading.
    payload = {"name": "x", "latency": True}
    sanitized, warns = _sanitize_latency(payload, cap_seconds=3600.0)
    assert sanitized["latency"] is True
    assert warns == []


def test_sanitize_latency_passthrough_non_container() -> None:
    sanitized, warns = _sanitize_latency("not a dict", cap_seconds=3600.0)
    assert sanitized == "not a dict"
    assert warns == []


# ---------------------------------------------------------------- end-to-end via MCP tools


@pytest.mark.asyncio
async def test_get_host_sanitises_spurious_latency(mocked_server) -> None:
    """Reproduces issue #202 end-to-end.

    Before the fix, the raw 1.78e9 latency would leak through verbatim:
        >>> json.loads(out[0].text)["latency"] == 1779764194.9
    After the fix, the value is nullified and a warning is appended.
    """
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts/ecrmut-ad-01").mock(
        return_value=ok([{"name": "ecrmut-ad-01", "state": 0, "latency": 1779764194.9}])
    )
    result = await mcp.call_tool("thruk_get_host", {"host": "ecrmut-ad-01"})
    payload = json.loads(result[0].text)
    assert payload["latency"] is None
    assert payload["name"] == "ecrmut-ad-01"
    assert any("ecrmut-ad-01" in w for w in payload["_warnings"])


@pytest.mark.asyncio
async def test_list_hosts_sanitises_spurious_latency(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok(
            [
                {"name": "good", "latency": 0.5},
                {"name": "bad", "latency": 1_779_764_194.9},
            ]
        )
    )
    result = await mcp.call_tool("thruk_list_hosts", {"limit": 10})
    payload = json.loads(result[0].text)
    # list payload becomes {"data": [...], "_warnings": [...]} when warnings exist.
    rows = payload["data"]
    assert rows[0]["latency"] == 0.5
    assert rows[1]["latency"] is None
    assert any("bad" in w for w in payload["_warnings"])


@pytest.mark.asyncio
async def test_list_hosts_no_warning_when_clean(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(
        return_value=ok([{"name": "good", "latency": 1.2}])
    )
    result = await mcp.call_tool("thruk_list_hosts", {"limit": 10})
    payload = json.loads(result[0].text)
    # No warnings => bare list, byte-identical to pre-fix behaviour.
    assert isinstance(payload, list)
    assert payload[0]["latency"] == 1.2


@pytest.mark.asyncio
async def test_get_service_sanitises_host_latency(mocked_server) -> None:
    mcp, router = mocked_server
    router.get("https://thruk.test/r/services/ecrmut-ad-01/SYSTEM_CPU_USAGE").mock(
        return_value=ok(
            [
                {
                    "host_name": "ecrmut-ad-01",
                    "description": "SYSTEM_CPU_USAGE",
                    "host_latency": 1779764723.1,
                    "latency": 0.98,
                }
            ]
        )
    )
    result = await mcp.call_tool(
        "thruk_get_service",
        {"host": "ecrmut-ad-01", "service": "SYSTEM_CPU_USAGE"},
    )
    payload = json.loads(result[0].text)
    assert payload["host_latency"] is None
    assert payload["latency"] == 0.98  # service-level latency preserved
    assert any("ecrmut-ad-01" in w for w in payload["_warnings"])
