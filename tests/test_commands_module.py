"""Issue #261 — write/command tools relocated to ``thruk_mcp.tools.commands``.

This is a *refactor* (no behaviour change): the 16 mutating ``WRITE_TOOLS``
(``thruk_schedule_downtime`` ... ``thruk_notifications``), the read-only
``thruk_get_downtime`` lookup and the private substring helper
``_delete_downtimes_by_host_comment`` moved out of ``server.py`` into
``tools/commands.py`` with two co-located registries
(``COMMANDS_READ_REGISTRY`` / ``COMMANDS_WRITE_REGISTRY``).

The tests below guard the invariants the refactor must preserve:

1. structure   - the two registries hold exactly the expected tools in the
                 original order, with faithful ``is_write`` flags;
2. WRITE_TOOLS  - the derived ``WRITE_TOOLS`` frozenset is byte-identical to
                 the historical set (the sensitive bit of this move);
3. re-exports  - every moved symbol is still importable from
                 ``thruk_mcp.server`` and is the *same object* as in
                 ``tools.commands``; the specs are spliced exactly once;
4. behaviour   - the moved tools still issue the same HTTP requests / produce
                 the same response shape after the move.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from tests.conftest import ok
from thruk_mcp import server
from thruk_mcp.tools import commands
from thruk_mcp.tools.base import ToolSpec

# Read-only command tool spliced between INVENTORY_REGISTRY and the log group.
_EXPECTED_READ_ORDER = [
    "thruk_get_downtime",
]

# Mutating tools in the exact order they appeared in the original TOOL_REGISTRY.
_EXPECTED_WRITE_ORDER = [
    "thruk_schedule_downtime",
    "thruk_schedule_host_services_downtime",
    "thruk_schedule_propagated_host_downtime",
    "thruk_schedule_hostgroup_downtime",
    "thruk_schedule_servicegroup_downtime",
    "thruk_delete_downtime",
    "thruk_delete_active_downtimes",
    "thruk_delete_downtimes_by_filter",
    "thruk_acknowledge",
    "thruk_bulk_acknowledge",
    "thruk_add_comment",
    "thruk_delete_comment",
    "thruk_remove_acknowledgement",
    "thruk_recheck",
    "thruk_checks",
    "thruk_notifications",
]


class TestCommandsRegistryStructure:
    def test_all_entries_are_toolspec(self) -> None:
        for reg in (commands.COMMANDS_READ_REGISTRY, commands.COMMANDS_WRITE_REGISTRY):
            assert all(isinstance(s, ToolSpec) for s in reg)

    def test_read_order_preserved(self) -> None:
        names = [s.name for s in commands.COMMANDS_READ_REGISTRY]
        assert names == _EXPECTED_READ_ORDER

    def test_write_order_preserved(self) -> None:
        names = [s.name for s in commands.COMMANDS_WRITE_REGISTRY]
        assert names == _EXPECTED_WRITE_ORDER

    def test_read_registry_is_read_only(self) -> None:
        assert all(s.is_write is False for s in commands.COMMANDS_READ_REGISTRY)

    def test_write_registry_is_all_writes(self) -> None:
        assert all(s.is_write is True for s in commands.COMMANDS_WRITE_REGISTRY)

    def test_fn_name_matches_tool_name(self) -> None:
        for s in (*commands.COMMANDS_READ_REGISTRY, *commands.COMMANDS_WRITE_REGISTRY):
            assert s.fn.__name__ == s.name


class TestWriteToolsNonRegression:
    """The sensitive invariant: WRITE_TOOLS must not change across the move."""

    # Historical WRITE_TOOLS set (command writes + the escape-hatch background
    # query which stays in tools/escape.py).
    _EXPECTED_WRITE_TOOLS: ClassVar[set[str]] = {
        *_EXPECTED_WRITE_ORDER,
        "thruk_run_background_query",
    }

    def test_write_tools_set_unchanged(self) -> None:
        assert frozenset(self._EXPECTED_WRITE_TOOLS) == server.WRITE_TOOLS

    def test_command_writes_are_subset(self) -> None:
        assert set(_EXPECTED_WRITE_ORDER) <= server.WRITE_TOOLS

    def test_read_only_lookup_not_in_write_tools(self) -> None:
        assert "thruk_get_downtime" not in server.WRITE_TOOLS

    def test_write_tools_matches_registry_derivation(self) -> None:
        expected = frozenset(s.name for s in server.TOOL_REGISTRY if s.is_write)
        assert expected == server.WRITE_TOOLS


class TestServerSplicingAndReExports:
    def test_registries_spliced_once(self) -> None:
        registry_names = [s.name for s in server.TOOL_REGISTRY]
        for name in (*_EXPECTED_READ_ORDER, *_EXPECTED_WRITE_ORDER):
            assert registry_names.count(name) == 1, f"{name} not spliced exactly once"

    def test_tools_dispatchable(self) -> None:
        for name in (*_EXPECTED_READ_ORDER, *_EXPECTED_WRITE_ORDER):
            assert name in server._TOOL_DISPATCH
            assert name in server._TOOL_SCHEMAS

    def test_server_reexports_are_same_object(self) -> None:
        for name in (*_EXPECTED_READ_ORDER, *_EXPECTED_WRITE_ORDER):
            assert getattr(server, name) is getattr(commands, name), (
                f"server.{name} must re-export the commands implementation"
            )

    def test_private_helper_reexported(self) -> None:
        assert server._delete_downtimes_by_host_comment is (
            commands._delete_downtimes_by_host_comment
        )

    def test_registries_reexported(self) -> None:
        assert server.COMMANDS_READ_REGISTRY is commands.COMMANDS_READ_REGISTRY
        assert server.COMMANDS_WRITE_REGISTRY is commands.COMMANDS_WRITE_REGISTRY


class TestBehaviourEquivalence:
    @pytest.mark.asyncio
    async def test_acknowledge_service_posts_correct_endpoint(self, mocked_server) -> None:
        mcp, router = mocked_server
        route = router.post(
            "https://thruk.test/r/services/srv01/CPU/cmd/acknowledge_svc_problem"
        ).mock(return_value=ok({"message": "Command successfully submitted"}))
        await mcp.call_tool(
            "thruk_acknowledge", {"host": "srv01", "service": "CPU", "comment": "ack"}
        )
        assert route.called

    @pytest.mark.asyncio
    async def test_schedule_host_downtime_posts_host_endpoint(self, mocked_server) -> None:
        mcp, router = mocked_server
        route = router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_host_downtime").mock(
            return_value=ok({"message": "ok"})
        )
        await mcp.call_tool("thruk_schedule_downtime", {"host": "srv01"})
        assert route.called

    @pytest.mark.asyncio
    async def test_get_downtime_unpacks_single_element_list(self, mocked_server) -> None:
        mcp, router = mocked_server
        router.get("https://thruk.test/r/downtimes/42").mock(
            return_value=ok([{"id": 42, "host_name": "srv01"}])
        )
        result = await mcp.call_tool("thruk_get_downtime", {"downtime_id": 42})
        body = json.loads(result[0].text)
        # Single-backend list is unpacked to the bare object (issue parity).
        assert body["id"] == 42
        assert body["host_name"] == "srv01"

    @pytest.mark.asyncio
    async def test_get_downtime_not_found_returns_error(self, mocked_server) -> None:
        mcp, router = mocked_server
        router.get("https://thruk.test/r/downtimes/99").mock(return_value=ok([]))
        result = await mcp.call_tool("thruk_get_downtime", {"downtime_id": 99})
        body = json.loads(result[0].text)
        assert "error" in body

    @pytest.mark.asyncio
    async def test_notifications_disable_host_only(self, mocked_server) -> None:
        mcp, router = mocked_server
        route = router.post("https://thruk.test/r/hosts/srv01/cmd/disable_host_notifications").mock(
            return_value=ok({"message": "ok"})
        )
        result = await mcp.call_tool("thruk_notifications", {"host": "srv01", "enabled": False})
        assert route.called
        body = json.loads(result[0].text)
        assert body["action"] == "disabled"
        assert body["target"] == "srv01"
