"""Issue #259 — triage/analytics tools relocated to ``thruk_mcp.tools.triage``.

This is a *refactor* (no behaviour change): the five semantic triage tools
(``thruk_oldest_problems`` ... ``thruk_concurrent_failures``), their
``_HOST_PROBLEM_KEYS`` / ``_SVC_PROBLEM_KEYS`` key tuples and the
``_project_problem_counts`` helper moved out of ``server.py`` into
``tools/triage.py`` with a co-located ``TRIAGE_REGISTRY``.

The tests below guard the invariants the refactor must preserve:

1. structure   - ``TRIAGE_REGISTRY`` holds exactly the expected tools in the
                 original order, all read-only (``is_write=False``);
2. re-exports  - every moved symbol is still importable from
                 ``thruk_mcp.server`` and is the *same object* as in
                 ``tools.triage``; ``_NOISY_CAP_HINT`` now lives in
                 ``constants`` (cycle break) and is re-exported by ``server``;
3. behaviour   - the moved tools still issue the same HTTP requests / produce
                 the same shape after the move, including the
                 ``deque``/``Counter`` sliding-window logic of
                 ``thruk_concurrent_failures`` and the cross-module
                 ``_strip_filter_field`` dependency of ``thruk_problem_counts``.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ok
from thruk_mcp import constants, server
from thruk_mcp.tools import inventory, triage
from thruk_mcp.tools.base import ToolSpec

# Expected names in the exact order they appeared in the original TOOL_REGISTRY.
_EXPECTED_TRIAGE_ORDER = [
    "thruk_oldest_problems",
    "thruk_unacked_critical",
    "thruk_stale_acks",
    "thruk_problem_counts",
    "thruk_concurrent_failures",
    "thruk_stale_checks",
    "thruk_worker_health",
]

BASE_TS = 1_700_000_000


def _evt(host: str, offset_secs: int = 0, state: int = 1) -> dict:
    return {"host_name": host, "state": state, "time": BASE_TS + offset_secs}


class TestTriageRegistryStructure:
    def test_all_entries_are_toolspec(self) -> None:
        assert all(isinstance(s, ToolSpec) for s in triage.TRIAGE_REGISTRY)

    def test_order_preserved(self) -> None:
        names = [s.name for s in triage.TRIAGE_REGISTRY]
        assert names == _EXPECTED_TRIAGE_ORDER

    def test_all_triage_tools_are_read_only(self) -> None:
        # None of the triage/analytics tools mutate monitoring state.
        assert all(s.is_write is False for s in triage.TRIAGE_REGISTRY)
        assert not (set(_EXPECTED_TRIAGE_ORDER) & server.WRITE_TOOLS)


class TestServerSplicingAndReExports:
    def test_triage_registry_spliced_once(self) -> None:
        registry_names = [s.name for s in server.TOOL_REGISTRY]
        for name in _EXPECTED_TRIAGE_ORDER:
            assert registry_names.count(name) == 1, f"{name} not spliced exactly once"

    def test_triage_tools_dispatchable(self) -> None:
        for name in _EXPECTED_TRIAGE_ORDER:
            assert name in server._TOOL_DISPATCH
            assert name in server._TOOL_SCHEMAS

    def test_server_reexports_are_same_object(self) -> None:
        for name in _EXPECTED_TRIAGE_ORDER:
            assert getattr(server, name) is getattr(triage, name), (
                f"server.{name} must re-export the triage implementation"
            )

    def test_private_helper_reexported(self) -> None:
        assert server._project_problem_counts is triage._project_problem_counts

    def test_noisy_cap_hint_moved_to_constants(self) -> None:
        # Cycle break (issue #259): the warning suffix shared by the staying
        # noisy tools and the moved concurrent_failures now lives in constants.
        assert server._NOISY_CAP_HINT is constants._NOISY_CAP_HINT
        assert triage._NOISY_CAP_HINT is constants._NOISY_CAP_HINT

    def test_strip_filter_field_sourced_from_inventory(self) -> None:
        # thruk_problem_counts depends on _strip_filter_field which lives in
        # tools/inventory.py — proving the cross-module import (no cycle).
        assert triage._strip_filter_field is inventory._strip_filter_field


class TestBehaviourEquivalence:
    @pytest.mark.asyncio
    async def test_problem_counts_hits_both_totals_endpoints(self, mocked_server) -> None:
        mcp, router = mocked_server
        hosts = router.get("https://thruk.test/r/hosts/totals").mock(
            return_value=ok({"down": 2, "unreachable": 0})
        )
        svcs = router.get("https://thruk.test/r/services/totals").mock(
            return_value=ok({"critical": 3, "warning": 1})
        )
        result = await mcp.call_tool("thruk_problem_counts", {})
        assert hosts.called and svcs.called
        body = json.loads(result[0].text)
        # Projection keeps the stable problem-state shape (defaults to 0).
        assert body["hosts"]["down"] == 2
        assert body["hosts"]["down_and_unhandled"] == 0
        assert body["services"]["critical"] == 3

    @pytest.mark.asyncio
    async def test_oldest_problems_queries_hosts_and_services(self, mocked_server) -> None:
        mcp, router = mocked_server
        hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
        svcs = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
        await mcp.call_tool("thruk_oldest_problems", {"limit": 5})
        assert hosts.called and svcs.called

    @pytest.mark.asyncio
    async def test_concurrent_failures_sliding_window_still_works(self, mocked_server) -> None:
        """The relocated deque/Counter sliding-window scan still detects bursts."""
        mcp, router = mocked_server
        route = router.post("https://thruk.test/r/logs").mock(
            return_value=ok([_evt("h1", 0), _evt("h2", 60), _evt("h3", 120)])
        )
        result = await mcp.call_tool(
            "thruk_concurrent_failures",
            {"since": "-1h", "window_minutes": 5, "min_hosts": 3},
        )
        assert route.called
        payload = json.loads(result[0].text)
        assert len(payload["results"]) == 1
        assert payload["results"][0]["count"] == 3
        assert sorted(payload["results"][0]["hosts"]) == ["h1", "h2", "h3"]

    @pytest.mark.asyncio
    async def test_concurrent_failures_absolute_since_normalised_to_epoch(
        self, mocked_server
    ) -> None:
        """Issue #317: absolute ISO since/until reach /logs as epoch, not raw ISO.

        Thruk's /logs time filter silently matches nothing for a bare ISO
        datetime, so the absolute window previously returned 0 events while the
        equivalent relative window worked. The payload still echoes the
        operator's original input.
        """
        from urllib.parse import parse_qs

        mcp, router = mocked_server
        route = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
        result = await mcp.call_tool(
            "thruk_concurrent_failures",
            {"since": "2026-05-20 00:00:00", "until": "2026-05-20 23:59:59"},
        )
        assert route.called
        body = parse_qs(route.calls.last.request.content.decode())
        params = {k: v[0] for k, v in body.items()}
        assert params["time[gte]"] == "1779235200"  # 2026-05-20 00:00:00 UTC
        assert params["time[lte]"] == "1779321599"  # 2026-05-20 23:59:59 UTC
        payload = json.loads(result[0].text)
        assert payload["since"] == "2026-05-20 00:00:00"
        assert payload["until"] == "2026-05-20 23:59:59"
