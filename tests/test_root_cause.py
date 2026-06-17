"""Tests for the root-cause topology tools (issue #322).

``thruk_root_cause`` collapses a DOWN/UNREACHABLE storm into its common cause by
walking the host ``parents`` topology; ``thruk_unreachable_vs_down`` is the
lightweight companion that just splits the window into DOWN (cause) vs
UNREACHABLE (consequence).

Coverage:
  * the pure attribution kernel ``_attribute_root_causes`` (single root,
    multi-level cascade, multi-parent/diamond, flat estate, unattributed
    UNREACHABLE, cycle guard);
  * the confidence tiers;
  * routing — ``/logs`` POST carries ``class=1`` + ``type[~]=^HOST ALERT`` and
    ``/hosts`` is fetched unfiltered with the topology columns;
  * end-to-end payload shapes for both tools, including filter-error handling.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import body_params, ok
from thruk_mcp.tools.triage import _attribute_root_causes, _root_cause_confidence

BASE_TS = 1_700_000_000


def _evt(host: str, state: int, offset_secs: int = 0) -> dict:
    """A HOST ALERT log row (state 1=DOWN, 2=UNREACHABLE)."""
    return {"host_name": host, "state": state, "time": BASE_TS + offset_secs}


def _host(name: str, parents: list[str], state: int = 0, groups: list[str] | None = None) -> dict:
    return {"name": name, "parents": parents, "state": state, "groups": groups or []}


# ---------------------------------------------------------------------------
# _attribute_root_causes — pure kernel
# ---------------------------------------------------------------------------


class TestAttributeRootCauses:
    def test_single_root_with_cascade(self) -> None:
        # router DOWN; two leaves UNREACHABLE behind it.
        parents = {"router": [], "a": ["router"], "b": ["router"]}
        clusters, unattributed = _attribute_root_causes({"router"}, {"a", "b"}, parents)
        assert clusters == {"router": {"router", "a", "b"}}
        assert unattributed == []

    def test_multi_level_cascade_picks_topmost_down(self) -> None:
        # core DOWN -> dist DOWN -> leaf UNREACHABLE. The topmost DOWN (core)
        # is the root for the whole chain.
        parents = {"core": [], "dist": ["core"], "leaf": ["dist"]}
        clusters, unattributed = _attribute_root_causes({"core", "dist"}, {"leaf"}, parents)
        assert clusters == {"core": {"core", "dist", "leaf"}}
        assert unattributed == []

    def test_two_independent_roots(self) -> None:
        parents = {"r1": [], "r2": [], "a": ["r1"], "b": ["r2"]}
        clusters, _ = _attribute_root_causes({"r1", "r2"}, {"a", "b"}, parents)
        assert clusters == {"r1": {"r1", "a"}, "r2": {"r2", "b"}}

    def test_diamond_multi_parent_deterministic(self) -> None:
        # leaf has two DOWN parents; the topmost-down tie is broken by sorted
        # name, so 'p1' wins deterministically.
        parents = {"p1": [], "p2": [], "leaf": ["p2", "p1"]}
        clusters, _ = _attribute_root_causes({"p1", "p2"}, {"leaf"}, parents)
        assert clusters["p1"] == {"p1", "leaf"} or "leaf" in clusters["p1"]
        # leaf attributed to exactly one root
        owners = [r for r, m in clusters.items() if "leaf" in m]
        assert owners == ["p1"]

    def test_flat_estate_each_down_is_its_own_root(self) -> None:
        # No parents defined: every DOWN host is an isolated root.
        parents = {"h1": [], "h2": [], "h3": []}
        clusters, unattributed = _attribute_root_causes({"h1", "h2", "h3"}, set(), parents)
        assert clusters == {"h1": {"h1"}, "h2": {"h2"}, "h3": {"h3"}}
        assert unattributed == []

    def test_unreachable_without_down_ancestor_is_unattributed(self) -> None:
        # leaf UNREACHABLE but its parent is not in the DOWN set (cause outside
        # the window / unmonitored) -> unattributed.
        parents = {"gw": [], "leaf": ["gw"]}
        clusters, unattributed = _attribute_root_causes(set(), {"leaf"}, parents)
        assert clusters == {}
        assert unattributed == ["leaf"]

    def test_cycle_guard(self) -> None:
        # Mis-configured parent loop must not hang.
        parents = {"a": ["b"], "b": ["a"]}
        clusters, _unattributed = _attribute_root_causes({"a"}, {"b"}, parents)
        # both reachable to the DOWN node 'a'
        assert "a" in clusters
        assert clusters["a"] == {"a", "b"}

    def test_missing_parent_in_map(self) -> None:
        # leaf references a parent absent from the topology map -> treated as a
        # dead end; leaf is unattributed (no DOWN ancestor reachable).
        parents = {"leaf": ["ghost"]}
        clusters, unattributed = _attribute_root_causes(set(), {"leaf"}, parents)
        assert clusters == {}
        assert unattributed == ["leaf"]


class TestConfidence:
    def test_tiers(self) -> None:
        assert _root_cause_confidence(1) == "low"
        assert _root_cause_confidence(2) == "medium"
        assert _root_cause_confidence(3) == "high"
        assert _root_cause_confidence(50) == "high"


# ---------------------------------------------------------------------------
# thruk_root_cause — routing + end-to-end
# ---------------------------------------------------------------------------


class TestRootCauseTool:
    @pytest.mark.asyncio
    async def test_logs_query_is_hardened(self, mocked_server) -> None:
        """/logs POST must carry class=1 + type[~]=^HOST ALERT + state[gte]=1."""
        mcp, router = mocked_server
        logs = router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
        router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
        await mcp.call_tool("thruk_root_cause", {"since": "-1h"})
        assert logs.called
        p = body_params(logs.calls.last.request)
        assert p["class"] == "1"
        assert p["type[~]"] == "^HOST ALERT"
        assert p["state[gte]"] == "1"
        assert p["sort"] == "-time"

    @pytest.mark.asyncio
    async def test_end_to_end_cascade(self, mocked_server) -> None:
        mcp, router = mocked_server
        router.post("https://thruk.test/r/logs").mock(
            return_value=ok(
                [
                    _evt("router", state=1),
                    _evt("a", state=2, offset_secs=30),
                    _evt("b", state=2, offset_secs=60),
                ]
            )
        )
        router.get("https://thruk.test/r/hosts").mock(
            return_value=ok(
                [
                    _host("router", [], state=1, groups=["net"]),
                    _host("a", ["router"], state=2, groups=["edf"]),
                    _host("b", ["router"], state=2, groups=["edf"]),
                ]
            )
        )
        result = await mcp.call_tool("thruk_root_cause", {"since": "-1h"})
        body = json.loads(result[0].text)
        assert body["total_affected_hosts"] == 3
        assert body["down_count"] == 1
        assert body["unreachable_count"] == 2
        assert len(body["root_causes"]) == 1
        rc = body["root_causes"][0]
        assert rc["root_cause_host"] == "router"
        assert rc["root_cause_state"] == "DOWN"
        assert rc["impacted_count"] == 3
        assert sorted(rc["impacted_hosts"]) == ["a", "b", "router"]
        assert rc["impacted_hostgroups"] == ["edf", "net"]
        assert rc["confidence"] == "high"
        assert body["unattributed_unreachable"] == []

    @pytest.mark.asyncio
    async def test_hosts_fetched_unfiltered(self, mocked_server) -> None:
        """Topology /hosts call must not inherit the affected-set filter."""
        mcp, router = mocked_server
        router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
        hosts = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
        await mcp.call_tool(
            "thruk_root_cause",
            {"since": "-1h", "filter": {"type": "leaf", "field": "host", "op": "eq", "value": "x"}},
        )
        assert hosts.called
        # The topology fetch only constrains columns, never host_name/q.
        req = hosts.calls.last.request
        assert "columns=" in req.url.query.decode()
        assert "host_name" not in req.url.query.decode()
        assert b"q=" not in req.url.query

    @pytest.mark.asyncio
    async def test_unattributed_surfaced(self, mocked_server) -> None:
        mcp, router = mocked_server
        router.post("https://thruk.test/r/logs").mock(return_value=ok([_evt("leaf", state=2)]))
        router.get("https://thruk.test/r/hosts").mock(
            return_value=ok([_host("leaf", ["gw"], state=2)])
        )
        result = await mcp.call_tool("thruk_root_cause", {"since": "-1h"})
        body = json.loads(result[0].text)
        assert body["root_causes"] == []
        assert body["unattributed_unreachable"] == ["leaf"]

    @pytest.mark.asyncio
    async def test_impacted_hosts_truncated(self, mocked_server) -> None:
        mcp, router = mocked_server
        leaves = [f"h{i:03d}" for i in range(10)]
        router.post("https://thruk.test/r/logs").mock(
            return_value=ok(
                [_evt("router", state=1)]
                + [_evt(h, state=2, offset_secs=i) for i, h in enumerate(leaves)]
            )
        )
        router.get("https://thruk.test/r/hosts").mock(
            return_value=ok(
                [_host("router", [], state=1)] + [_host(h, ["router"], state=2) for h in leaves]
            )
        )
        result = await mcp.call_tool("thruk_root_cause", {"since": "-1h", "sample_limit": 3})
        rc = json.loads(result[0].text)["root_causes"][0]
        assert rc["impacted_count"] == 11
        assert len(rc["impacted_hosts"]) == 3
        assert rc["impacted_hosts_truncated"] is True

    @pytest.mark.asyncio
    async def test_invalid_filter_returns_error(self, mocked_server) -> None:
        mcp, _router = mocked_server
        result = await mcp.call_tool(
            "thruk_root_cause",
            {"filter": {"type": "leaf", "field": "bogus", "op": "eq", "value": "x"}},
        )
        body = json.loads(result[0].text)
        assert "error" in body


# ---------------------------------------------------------------------------
# thruk_unreachable_vs_down
# ---------------------------------------------------------------------------


class TestUnreachableVsDown:
    @pytest.mark.asyncio
    async def test_split(self, mocked_server) -> None:
        mcp, router = mocked_server
        route = router.post("https://thruk.test/r/logs").mock(
            return_value=ok(
                [
                    _evt("d1", state=1),
                    _evt("d2", state=1),
                    _evt("u1", state=2),
                    _evt("d2", state=2, offset_secs=10),  # d2 hits both states
                ]
            )
        )
        result = await mcp.call_tool("thruk_unreachable_vs_down", {"since": "-1h"})
        assert route.called
        body = json.loads(result[0].text)
        assert body["down_count"] == 2
        assert body["unreachable_count"] == 2
        assert body["both_count"] == 1
        assert body["down_hosts"] == ["d1", "d2"]
        assert body["unreachable_hosts"] == ["d2", "u1"]

    @pytest.mark.asyncio
    async def test_empty_window(self, mocked_server) -> None:
        mcp, router = mocked_server
        router.post("https://thruk.test/r/logs").mock(return_value=ok([]))
        result = await mcp.call_tool("thruk_unreachable_vs_down", {"since": "-1h"})
        body = json.loads(result[0].text)
        assert body["down_count"] == 0
        assert body["unreachable_count"] == 0
        assert body["down_hosts"] == []
