"""Issue #258 - inventory/listing tools relocated to ``thruk_mcp.tools.inventory``.

This is a *refactor* (no behaviour change): the 17 read-only listing/inventory
tools and their private helpers moved out of ``server.py`` into
``tools/inventory.py`` with a co-located ``INVENTORY_REGISTRY``.

The tests below guard the three invariants the refactor must preserve:

1. structure   - ``INVENTORY_REGISTRY`` holds exactly the expected tools,
                 in the original order, and excludes non-inventory tools
                 (``thruk_get_downtime``);
2. re-exports  - every moved symbol is still importable from
                 ``thruk_mcp.server`` and is the *same object* as in
                 ``tools.inventory`` / ``helpers`` (no accidental copy);
3. behaviour   - tools still issue the same HTTP request after the move,
                 including ``thruk_list_downtimes`` which depends on the
                 cross-module ``_resolve_log_filter`` helper that also moved
                 (to ``helpers.py``) as part of the cycle break.
"""

from __future__ import annotations

import pytest

from tests.conftest import ok
from thruk_mcp import helpers, server
from thruk_mcp.tools import inventory
from thruk_mcp.tools.base import ToolSpec

# Expected names in the exact order they appeared in the original TOOL_REGISTRY.
_EXPECTED_INVENTORY_ORDER = [
    "thruk_list_hosts",
    "thruk_get_host",
    "thruk_list_services",
    "thruk_get_service",
    "thruk_host_availability",
    "thruk_service_availability",
    "thruk_hostgroup_availability",
    "thruk_hostgroup_availability_summary",
    "thruk_list_hostgroups",
    "thruk_list_servicegroups",
    "thruk_list_contacts",
    "thruk_get_contact",
    "thruk_problems",
    "thruk_stats",
    "thruk_totals",
    "thruk_list_downtimes",
    "thruk_list_comments",
    "thruk_sites",
]


class TestInventoryRegistryStructure:
    def test_all_entries_are_toolspec(self) -> None:
        assert all(isinstance(s, ToolSpec) for s in inventory.INVENTORY_REGISTRY)

    def test_order_preserved(self) -> None:
        names = [s.name for s in inventory.INVENTORY_REGISTRY]
        assert names == _EXPECTED_INVENTORY_ORDER

    def test_get_downtime_not_in_inventory(self) -> None:
        # thruk_get_downtime is NOT an inventory tool: it stays in server.py.
        names = {s.name for s in inventory.INVENTORY_REGISTRY}
        assert "thruk_get_downtime" not in names


class TestServerSplicingAndReExports:
    def test_inventory_registry_spliced_once(self) -> None:
        registry_names = [s.name for s in server.TOOL_REGISTRY]
        for name in _EXPECTED_INVENTORY_ORDER:
            assert registry_names.count(name) == 1, f"{name} not spliced exactly once"

    def test_get_downtime_still_registered(self) -> None:
        registry_names = {s.name for s in server.TOOL_REGISTRY}
        assert "thruk_get_downtime" in registry_names

    def test_server_reexports_are_same_object(self) -> None:
        for name in _EXPECTED_INVENTORY_ORDER:
            assert getattr(server, name) is getattr(inventory, name), (
                f"server.{name} must re-export the inventory implementation"
            )

    def test_private_helpers_reexported(self) -> None:
        for name in (
            "_collect_hostgroup_constraints",
            "_row_matches_hostgroup_constraints",
            "_ensure_columns_param",
            "_strip_filter_field",
        ):
            assert getattr(server, name) is getattr(inventory, name)

    def test_shared_helpers_moved_to_helpers_module(self) -> None:
        # Cycle-break: these now live in helpers.py and are re-exported by server.
        for name in (
            "_now_utc_epoch",
            "_parse_thruk_time",
            "_resolve_log_filter",
            "_resolve_hosts_to_regex_from_params",
            "_RESOLVE_HOSTS_HARD_LIMIT",
        ):
            assert getattr(server, name) is getattr(helpers, name)


class TestBehaviourEquivalence:
    @pytest.mark.asyncio
    async def test_list_hosts_still_hits_hosts_endpoint(self, mocked_server) -> None:
        mcp, router = mocked_server
        route = router.get("https://thruk.test/r/hosts").mock(return_value=ok([{"name": "a"}]))
        await mcp.call_tool("thruk_list_hosts", {"limit": 5})
        assert route.called
        assert route.calls.last.request.url.params["limit"] == "5"

    @pytest.mark.asyncio
    async def test_sites_still_hits_sites_endpoint(self, mocked_server) -> None:
        mcp, router = mocked_server
        route = router.get("https://thruk.test/r/sites").mock(return_value=ok([{"id": "s1"}]))
        await mcp.call_tool("thruk_sites", {})
        assert route.called

    @pytest.mark.asyncio
    async def test_list_downtimes_resolves_hostgroup_via_moved_helper(self, mocked_server) -> None:
        """thruk_list_downtimes relies on _resolve_log_filter (moved to helpers.py).

        A hostgroup filter must trigger a /hosts lookup, then a /downtimes query
        scoped by the resolved host_name[regex] - proving the cross-module helper
        move preserved end-to-end behaviour.
        """
        mcp, router = mocked_server
        hosts = router.get("https://thruk.test/r/hosts").mock(
            return_value=ok([{"name": "web01"}, {"name": "web02"}])
        )
        downtimes = router.get("https://thruk.test/r/downtimes").mock(return_value=ok([]))
        await mcp.call_tool(
            "thruk_list_downtimes",
            {"filter": {"type": "leaf", "field": "hostgroup", "op": "eq", "value": "web"}},
        )
        assert hosts.called, "hostgroup filter must trigger a /hosts resolution lookup"
        assert downtimes.called
        regex = downtimes.calls.last.request.url.params["host_name[regex]"]
        assert "web01" in regex and "web02" in regex
