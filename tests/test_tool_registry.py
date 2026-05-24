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
        "thruk_remove_acknowledgement",
        "thruk_recheck",
        "thruk_run_background_query",
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
    """Registry must contain exactly 39 tools (sentinel for accidental removals)."""
    assert len(TOOL_REGISTRY) == 39, (
        f"Expected 39 tools in TOOL_REGISTRY, got {len(TOOL_REGISTRY)}. "
        "Update this sentinel if you intentionally added/removed a tool."
    )
