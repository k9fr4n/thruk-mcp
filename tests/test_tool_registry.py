"""Tests for the unified ToolSpec registry (issue #85).

Before this fix, server.py maintained three separate structures that had to
be kept in sync by hand:

    _TOOL_SCHEMAS  -- dict[str, schema]        (manual, ~337 lines)
    _TOOL_DISPATCH -- dict[str, fn]            (manual, ~43 lines)
    WRITE_TOOLS    -- frozenset[str]           (manual, ~16 lines)

A mismatch would cause a silent failure (schema without handler, or handler
without schema, or write tool not stripped in read-only mode).

Fix (issue #85): replace the three manual structures with a single
``TOOL_REGISTRY: list[ToolSpec]`` from which the others are derived.
"""

from __future__ import annotations

import inspect
from typing import ClassVar

import pytest

from thruk_mcp.server import (
    _TOOL_DISPATCH,
    _TOOL_SCHEMAS,
    TOOL_REGISTRY,
    WRITE_TOOLS,
    ToolSpec,
)


class TestToolRegistryInvariants:
    def test_registry_is_nonempty(self) -> None:
        assert len(TOOL_REGISTRY) > 0

    def test_all_entries_are_toolspec(self) -> None:
        for entry in TOOL_REGISTRY:
            assert isinstance(entry, ToolSpec)

    def test_no_duplicate_names(self) -> None:
        names = [spec.name for spec in TOOL_REGISTRY]
        duplicates = [n for n in names if names.count(n) > 1]
        assert not duplicates, f"Duplicate names: {set(duplicates)}"

    def test_dispatch_matches_registry(self) -> None:
        expected = {spec.name: spec.fn for spec in TOOL_REGISTRY}
        assert expected == _TOOL_DISPATCH

    def test_schemas_matches_registry(self) -> None:
        expected = {spec.name: spec.schema for spec in TOOL_REGISTRY}
        assert expected == _TOOL_SCHEMAS

    def test_write_tools_matches_registry(self) -> None:
        expected = frozenset(spec.name for spec in TOOL_REGISTRY if spec.is_write)
        assert expected == WRITE_TOOLS

    def test_dispatch_and_schemas_have_same_keys(self) -> None:
        assert set(_TOOL_DISPATCH) == set(_TOOL_SCHEMAS)


class TestToolSpecEntries:
    def test_every_fn_is_coroutine(self) -> None:
        non_async = [
            spec.name for spec in TOOL_REGISTRY if not inspect.iscoroutinefunction(spec.fn)
        ]
        assert not non_async, f"Non-async handlers: {non_async}"

    def test_every_schema_is_object_type(self) -> None:
        bad = [
            spec.name
            for spec in TOOL_REGISTRY
            if not isinstance(spec.schema, dict) or spec.schema.get("type") != "object"
        ]
        assert not bad, f"Schemas without 'type': 'object': {bad}"

    def test_every_schema_has_properties(self) -> None:
        bad = [spec.name for spec in TOOL_REGISTRY if "properties" not in spec.schema]
        assert not bad, f"Schemas missing 'properties': {bad}"

    def test_fn_name_matches_tool_name(self) -> None:
        mismatches = [spec.name for spec in TOOL_REGISTRY if spec.fn.__name__ != spec.name]
        assert not mismatches, f"name/fn mismatch: {mismatches}"


class TestWriteTools:
    _EXPECTED_WRITE_TOOLS: ClassVar[set[str]] = {
        "thruk_schedule_downtime",
        "thruk_schedule_host_services_downtime",
        "thruk_schedule_propagated_host_downtime",
        "thruk_schedule_hostgroup_downtime",
        "thruk_schedule_servicegroup_downtime",
        "thruk_delete_downtime",
        "thruk_delete_active_downtimes",
        "thruk_delete_downtimes_by_filter",
        "thruk_acknowledge",
        "thruk_bulk_acknowledge",  # added: bulk ack of matching unhandled problems (issue #170)
        "thruk_add_comment",  # added: free-form comment on host/service (issue #168)
        "thruk_delete_comment",  # added: delete a comment by id (issue #169)
        "thruk_remove_acknowledgement",
        "thruk_recheck",
        "thruk_run_background_query",
        "thruk_notifications",  # added: enable/disable notifications
        "thruk_checks",  # added: enable/disable active checks (issue #167)
    }

    def test_all_known_write_tools_present(self) -> None:
        missing = self._EXPECTED_WRITE_TOOLS - WRITE_TOOLS
        assert not missing, f"Known write tools missing from WRITE_TOOLS: {missing}"

    def test_no_unexpected_write_tools(self) -> None:
        unexpected = WRITE_TOOLS - self._EXPECTED_WRITE_TOOLS
        assert not unexpected, f"Unexpected write tools: {unexpected}"

    def test_thruk_query_not_in_write_tools(self) -> None:
        assert "thruk_query" not in WRITE_TOOLS


def test_registry_tool_count() -> None:
    """Registry must contain exactly 57 tools (sentinel for accidental removals).

    Count history:
      39 → +1 thruk_notifications (enable/disable host+service notifications)
         → +2 thruk_host_availability, thruk_service_availability (issue #171)
         → +1 thruk_hostgroup_availability (issue #171)
         → +1 thruk_checks (enable/disable active checks, issue #167)
         → +1 thruk_add_comment (free-form host/service comment, issue #168)
         → +1 thruk_delete_comment (delete a comment by id, issue #169)
         → +1 thruk_bulk_acknowledge (bulk ack of unhandled problems, issue #170)
         → +1 thruk_list_contacts (list configured contacts, issue #172)
         → +1 thruk_get_contact (get single contact by name, issue #173)
         → +1 thruk_totals (compact host+service counts, issue #222)
         → +1 thruk_notification_summary (count notifications by dimension, issue #271)
         → +1 thruk_notification_heatmap (notification counts per time bucket, issue #272)
         → +3 thruk_get_perfdata, thruk_perfdata_snapshot,
              thruk_perfdata_near_threshold (perfdata expose, issue #284)
         → +1 thruk_reliability_report (MTTR/MTBF/incident metrics, issue #286)
         → +1 thruk_stale_checks (stale/overdue check-execution detector, issue #287)
         → +1 thruk_hostgroup_availability_summary (aggregated SLA rollup, issue #319)
         → +1 thruk_worker_health (mod-gearman worker/queue artefact scan, issue #320)
         → +1 thruk_incident_timeline (ordered post-mortem event chronology, issue #321)
         → +2 thruk_root_cause, thruk_unreachable_vs_down (parent-topology
              root-cause analysis, issue #322)
         → +1 thruk_backend_health (per-site backend latency/replication-lag
              health, issue #323)
         → +2 thruk_state_at, thruk_state_diff (point-in-time parc-state
              reconstruction from /logs, issue #324)
      = 65
    """
    assert len(TOOL_REGISTRY) == 65, (
        f"Expected 65 tools in TOOL_REGISTRY, got {len(TOOL_REGISTRY)}. "
        "Update this sentinel if you intentionally added/removed a tool."
    )


# ---------------------------------------------------------------------------
# Issue #262 — TOOL_REGISTRY aggregation moved to ``thruk_mcp.tools``
#
# ``server.py`` no longer builds the registry inline; it imports the single
# aggregated ``TOOL_REGISTRY`` from the ``tools`` package, which splices the
# per-module registries together. These tests pin the aggregation location and
# the exact splice order so the derived ``_TOOL_SCHEMAS`` / ``WRITE_TOOLS`` keep
# identical keys and ordering (the issue's definition-of-done).
# ---------------------------------------------------------------------------


class TestIssue262RegistryAggregation:
    def test_server_reexports_tools_registry(self) -> None:
        """``server.TOOL_REGISTRY`` must be the very object from ``tools``."""
        from thruk_mcp import server, tools

        assert server.TOOL_REGISTRY is tools.TOOL_REGISTRY

    def test_aggregation_order_matches_submodule_splice(self) -> None:
        """Splice order is byte-for-byte the original server.py ordering."""
        from thruk_mcp import tools

        expected = [
            *tools.HISTORY_TRENDS_REGISTRY,
            *tools.INVENTORY_REGISTRY,
            *tools.COMMANDS_READ_REGISTRY,
            *tools.HISTORY_LOGS_REGISTRY,
            *tools.ESCAPE_REGISTRY,
            *tools.COMMANDS_WRITE_REGISTRY,
            *tools.TRIAGE_REGISTRY,
            *tools.PERFDATA_REGISTRY,
        ]
        assert [s.name for s in tools.TOOL_REGISTRY] == [s.name for s in expected]

    def test_escape_registry_holds_raw_query_tools(self) -> None:
        """The two raw-query tools live in ESCAPE_REGISTRY (moved from server.py)."""
        from thruk_mcp import tools

        names = [s.name for s in tools.ESCAPE_REGISTRY]
        assert names == ["thruk_query", "thruk_run_background_query"]
        # thruk_query must NOT be a write tool (usable for GET in read-only mode);
        # thruk_run_background_query must be.
        by_name = {s.name: s for s in tools.ESCAPE_REGISTRY}
        assert by_name["thruk_query"].is_write is False
        assert by_name["thruk_run_background_query"].is_write is True


# ---------------------------------------------------------------------------
# ThrukMCPServer interface contract (issue #89: disallow_untyped_defs)
# ---------------------------------------------------------------------------


class TestThrukMCPServerInterface:
    """Verify that ThrukMCPServer wrapper methods are correctly typed and callable.

    These tests guard against regressions introduced when adding type annotations
    to the previously-untyped run() and create_initialization_options() methods
    (issue #89 — enable disallow_untyped_defs=true).
    """

    def test_run_is_coroutine_function(self) -> None:
        """ThrukMCPServer.run must be an async coroutine function."""
        from thruk_mcp.server import ThrukMCPServer

        assert inspect.iscoroutinefunction(ThrukMCPServer.run)

    def test_create_initialization_options_is_callable(self) -> None:
        """ThrukMCPServer.create_initialization_options must be a plain callable."""
        from thruk_mcp.server import ThrukMCPServer

        assert callable(ThrukMCPServer.create_initialization_options)
        assert not inspect.iscoroutinefunction(ThrukMCPServer.create_initialization_options)

    def test_create_initialization_options_returns_value(self) -> None:
        """create_initialization_options() must return a non-None value."""
        from thruk_mcp.config import ThrukConfig
        from thruk_mcp.server import build_server

        cfg = ThrukConfig(base_url="http://thruk.test", api_key="test-key")
        server = build_server(cfg)
        result = server.create_initialization_options()
        assert result is not None

    def test_run_signature_has_required_params(self) -> None:
        """run() must accept read_stream, write_stream, and optional init_options."""
        from thruk_mcp.server import ThrukMCPServer

        sig = inspect.signature(ThrukMCPServer.run)
        params = list(sig.parameters)
        assert "read_stream" in params
        assert "write_stream" in params
        assert "init_options" in params
        # init_options must be optional (has a default value)
        init_param = sig.parameters["init_options"]
        assert init_param.default is not inspect.Parameter.empty

    async def test_run_forwards_extra_kwargs(self) -> None:
        """run() must forward extra kwargs (e.g. ``stateless``) to the wrapped Server.

        Regression: the MCP SDK's StreamableHTTPSessionManager.run_server() calls
        ``app.run(read, write, init_options, stateless=...)``. The wrapper override
        previously dropped **kwargs, so every streamable-http session crashed with
        ``TypeError: run() got an unexpected keyword argument 'stateless'``.
        """
        from unittest.mock import AsyncMock, MagicMock

        from thruk_mcp.server import ThrukMCPServer

        inner = MagicMock()
        inner.run = AsyncMock()
        wrapper = ThrukMCPServer(inner, {}, MagicMock(), MagicMock())

        await wrapper.run("read", "write", "init", stateless=True)

        inner.run.assert_awaited_once_with("read", "write", "init", stateless=True)


# ---------------------------------------------------------------------------
# Regression: issue #177
#
# Bug: thruk_top_noisy_hosts / thruk_top_noisy_services / thruk_flap_summary
# declared `hours: int` in their MCP schema but their async signatures
# accept `since`/`until` only. Any client honoring the schema and passing
# `hours=...` triggered `TypeError` re-raised as `ValueError("Invalid
# arguments for ...")`.
#
# Pre-fix reproduction (would now fail):
#
#     await mcp.call_tool("thruk_top_noisy_hosts", {"hours": 24})
#     # → ValueError: Invalid arguments for 'thruk_top_noisy_hosts':
#     #   thruk_top_noisy_hosts() got an unexpected keyword argument 'hours'
#
# Fix: replace `hours=_int(default=24)` with the canonical `since`/`until`
# pair used by sibling tools (thruk_alert_heatmap, thruk_recurring_problems).
# ---------------------------------------------------------------------------


class TestIssue177SchemaSignatureAlignment:
    """Schema parameters of the three trend tools must match their signatures."""

    AFFECTED: ClassVar[tuple[str, ...]] = (
        "thruk_top_noisy_hosts",
        "thruk_top_noisy_services",
        "thruk_flap_summary",
    )

    @pytest.mark.parametrize(
        "tool_name",
        ["thruk_top_noisy_hosts", "thruk_top_noisy_services", "thruk_flap_summary"],
    )
    def test_schema_exposes_since_until_not_hours(self, tool_name: str) -> None:
        schema = _TOOL_SCHEMAS[tool_name]
        props = schema.get("properties", {})
        assert "since" in props, f"{tool_name} schema must expose 'since'"
        assert "until" in props, f"{tool_name} schema must expose 'until'"
        assert "hours" not in props, (
            f"{tool_name} schema must NOT expose 'hours' "
            f"(function signature uses since/until — see issue #177)"
        )
        # since must default to "-24h" to preserve the previous default window
        assert props["since"].get("default") == "-24h"

    @pytest.mark.parametrize(
        "tool_name",
        ["thruk_top_noisy_hosts", "thruk_top_noisy_services", "thruk_flap_summary"],
    )
    def test_schema_keys_are_subset_of_function_signature(self, tool_name: str) -> None:
        """Every schema property must correspond to a real function parameter."""
        fn = _TOOL_DISPATCH[tool_name]
        sig = inspect.signature(fn)
        accepts_var_keyword = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        fn_params = set(sig.parameters)
        schema_props = set(_TOOL_SCHEMAS[tool_name].get("properties", {}))
        unknown = schema_props - fn_params
        assert accepts_var_keyword or not unknown, (
            f"{tool_name}: schema declares {sorted(unknown)} not in function signature "
            f"{sorted(fn_params)}"
        )


class TestListParamDescriptions:
    """Issue #300: list tools must describe sort/columns and advertise the
    sort default, kept in lock-step with the implementation signature."""

    @staticmethod
    def _list_tools_with(prop: str) -> list[str]:
        return [
            name for name, schema in _TOOL_SCHEMAS.items() if prop in schema.get("properties", {})
        ]

    def test_columns_param_is_described(self) -> None:
        tools = self._list_tools_with("columns")
        assert tools, "expected at least one tool exposing a 'columns' param"
        for name in tools:
            desc = _TOOL_SCHEMAS[name]["properties"]["columns"].get("description", "")
            assert "Comma-separated columns" in desc, f"{name} columns lacks description"

    def test_sort_param_is_described(self) -> None:
        tools = self._list_tools_with("sort")
        assert tools, "expected at least one tool exposing a 'sort' param"
        for name in tools:
            desc = _TOOL_SCHEMAS[name]["properties"]["sort"].get("description", "")
            assert "Sort order" in desc, f"{name} sort lacks description"

    def test_sort_default_matches_function_signature(self) -> None:
        """The advertised sort default must equal the real signature default."""
        for name in self._list_tools_with("sort"):
            schema_default = _TOOL_SCHEMAS[name]["properties"]["sort"].get("default")
            sig_default = inspect.signature(_TOOL_DISPATCH[name]).parameters["sort"].default
            assert schema_default == sig_default, (
                f"{name}: schema sort default {schema_default!r} != "
                f"function default {sig_default!r}"
            )


class TestTimeWindowDescriptions:
    """Issue #302: every tool exposing since/until must document the accepted
    time formats (relative or ISO), consistent with the analytics tools."""

    @staticmethod
    def _tools_with(prop: str) -> list[str]:
        return [
            name for name, schema in _TOOL_SCHEMAS.items() if prop in schema.get("properties", {})
        ]

    def test_since_param_is_described(self) -> None:
        tools = self._tools_with("since")
        assert tools, "expected at least one tool exposing a 'since' param"
        for name in tools:
            desc = _TOOL_SCHEMAS[name]["properties"]["since"].get("description", "")
            assert "relative time" in desc, f"{name} since lacks a time-format description"

    def test_until_param_is_described(self) -> None:
        tools = self._tools_with("until")
        assert tools, "expected at least one tool exposing an 'until' param"
        for name in tools:
            desc = _TOOL_SCHEMAS[name]["properties"]["until"].get("description", "")
            assert desc, f"{name} until lacks a description"
