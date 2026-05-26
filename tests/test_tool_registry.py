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
    """Registry must contain exactly 46 tools (sentinel for accidental removals).

    Count history:
      39 → +1 thruk_notifications (enable/disable host+service notifications)
         → +2 thruk_host_availability, thruk_service_availability (issue #171)
         → +1 thruk_hostgroup_availability (issue #171)
         → +1 thruk_checks (enable/disable active checks, issue #167)
         → +1 thruk_add_comment (free-form host/service comment, issue #168)
         → +1 thruk_delete_comment (delete a comment by id, issue #169)
      = 46
    """
    assert len(TOOL_REGISTRY) == 46, (
        f"Expected 46 tools in TOOL_REGISTRY, got {len(TOOL_REGISTRY)}. "
        "Update this sentinel if you intentionally added/removed a tool."
    )


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
