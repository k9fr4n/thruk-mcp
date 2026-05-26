"""Regression tests for issue #195: catalog/metadata.json drift.

The Docker MCP Gateway discovers a server's tool list from the
``io.docker.server.metadata`` OCI label, which is built from
``catalog/metadata.json``. If a new tool is registered in
``TOOL_REGISTRY`` but the catalog is not regenerated, the gateway will
not forward calls to it -- clients see ``Tool not found`` even though
the tool is fully implemented and advertised by the MCP server's own
``list_tools`` response.

Bug before fix (issue #195):
    # TOOL_REGISTRY contained 49 tools, catalog/metadata.json had only 46.
    # Missing from the gateway: thruk_get_contact, thruk_bulk_acknowledge,
    # thruk_delete_comment (and historically others). Symptom reported by
    # users: `Tool "<name>" not found` on calls to existing tools.

Fix:
    1. Regenerate catalog/metadata.json via ``scripts/gen_metadata.py``.
    2. Add this test so CI fails on any future drift between TOOL_REGISTRY
       and the catalog (i.e. forgetting to re-run the generator after
       adding a tool).

To refresh the catalog after intentionally adding/removing a tool::

    python scripts/gen_metadata.py >/dev/null

Then commit the resulting ``catalog/metadata.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thruk_mcp.server import TOOL_REGISTRY

CATALOG = Path(__file__).parent.parent / "catalog" / "metadata.json"

_REGEN_HINT = (
    "Run `python scripts/gen_metadata.py >/dev/null` and commit the updated "
    "catalog/metadata.json. This file is the source of truth for the Docker "
    "MCP Gateway's tool list (io.docker.server.metadata label)."
)


@pytest.fixture(scope="module")
def metadata() -> dict[str, object]:
    """Load catalog/metadata.json once per test module."""
    assert CATALOG.is_file(), f"missing {CATALOG}"
    with CATALOG.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def catalog_tool_names(metadata: dict[str, object]) -> set[str]:
    tools = metadata.get("tools")
    assert isinstance(tools, list), "catalog/metadata.json: 'tools' must be a list"
    return {t["name"] for t in tools if isinstance(t, dict) and "name" in t}


@pytest.fixture(scope="module")
def registry_tool_names() -> set[str]:
    return {spec.name for spec in TOOL_REGISTRY}


class TestCatalogTrackRegistry:
    """catalog/metadata.json must advertise exactly the tools in TOOL_REGISTRY."""

    def test_no_tool_missing_from_catalog(
        self, registry_tool_names: set[str], catalog_tool_names: set[str]
    ) -> None:
        """Every TOOL_REGISTRY entry must appear in catalog/metadata.json.

        This is the precise check that would have caught issue #195:
        thruk_get_contact / thruk_bulk_acknowledge / thruk_delete_comment
        were in TOOL_REGISTRY but absent from catalog/metadata.json, so
        the Docker MCP Gateway refused to forward calls to them.
        """
        missing = registry_tool_names - catalog_tool_names
        assert not missing, (
            f"{len(missing)} tool(s) registered in TOOL_REGISTRY but missing "
            f"from catalog/metadata.json: {sorted(missing)}.\n{_REGEN_HINT}"
        )

    def test_no_stale_tool_in_catalog(
        self, registry_tool_names: set[str], catalog_tool_names: set[str]
    ) -> None:
        """No catalog entry may reference a tool that no longer exists.

        Catches the opposite drift: a tool removed from TOOL_REGISTRY but
        still advertised by the gateway -- clients would call into a void.
        """
        stale = catalog_tool_names - registry_tool_names
        assert not stale, (
            f"{len(stale)} tool(s) in catalog/metadata.json no longer exist in "
            f"TOOL_REGISTRY: {sorted(stale)}.\n{_REGEN_HINT}"
        )

    def test_catalog_tool_count_matches_registry(
        self, registry_tool_names: set[str], catalog_tool_names: set[str]
    ) -> None:
        """Belt-and-braces count check (redundant with the two above but
        gives a clearer diff in pytest output when both sets drift)."""
        assert len(catalog_tool_names) == len(registry_tool_names), (
            f"catalog has {len(catalog_tool_names)} tools, registry has "
            f"{len(registry_tool_names)}.\n{_REGEN_HINT}"
        )


class TestCatalogShape:
    """Defensive checks on the catalog payload itself."""

    def test_every_tool_has_a_description(self, metadata: dict[str, object]) -> None:
        """The gateway surfaces descriptions to the LLM; empty ones degrade UX."""
        tools = metadata["tools"]
        assert isinstance(tools, list)
        empty = [
            t["name"]
            for t in tools
            if isinstance(t, dict) and not (t.get("description") or "").strip()
        ]
        assert not empty, f"tools with empty description in catalog: {empty}"

    def test_server_identity(self, metadata: dict[str, object]) -> None:
        assert metadata.get("name") == "thruk-mcp"
        assert metadata.get("type") == "server"
