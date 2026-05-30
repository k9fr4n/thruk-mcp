"""Tool building blocks: JSON-Schema helpers + the ``ToolSpec`` registry type.

Extracted from :mod:`thruk_mcp.server` (issue #257, parent #256 — server.py
split). These are pure, dependency-free primitives with **no functional
behaviour change**:

* ``_s`` / ``_str`` / ``_int`` / ``_bool`` — shorthand JSON-Schema builders.
* ``_OPT_STR`` / ``_OPT_INT`` / ``_OPT_BOOL`` / ``_OPT_OBJ`` — reusable
  ``anyOf [<type>, null]`` fragments defaulting to ``None``.
* ``_LOG_HOSTGROUP`` / ``_LOG_CUSTOM_VARS`` — log-family host-resolution
  filter fragments.
* ``_BACKENDS`` — multi-backend selector fragment.
* ``ToolSpec`` — single source of truth for a registered MCP tool.

``server.py`` re-exports every symbol here for backward compatibility, so
existing imports (``from thruk_mcp.server import ToolSpec, _s, ...``) keep
working unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Explicit JSON Schemas — no annotation introspection, no Pydantic
# ---------------------------------------------------------------------------


def _s(*required: str, **props: Any) -> dict[str, Any]:
    """Shorthand to build a JSON-Schema object."""
    properties = {k: (v if isinstance(v, dict) else {"type": v}) for k, v in props.items()}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _str(desc: str = "") -> dict[str, Any]:
    return {"type": "string", "description": desc} if desc else {"type": "string"}


def _int(desc: str = "", default: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "integer"}
    if desc:
        d["description"] = desc
    if default is not None:
        d["default"] = default
    return d


def _bool(desc: str = "", default: bool | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "boolean"}
    if desc:
        d["description"] = desc
    if default is not None:
        d["default"] = default
    return d


_OPT_STR = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
_OPT_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
_OPT_BOOL = {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": None}
_OPT_OBJ = {"anyOf": [{"type": "object"}, {"type": "null"}], "default": None}
# Reusable schema fragment for log-family host-resolution filters.
_LOG_HOSTGROUP = {
    **_OPT_STR,
    "description": (
        "Filter to hosts belonging to this hostgroup. Resolved via a /hosts lookup "
        "then host_name[regex] — works on all backends (log table has no group column)."
    ),
}
_LOG_CUSTOM_VARS = {
    **_OPT_OBJ,
    "description": (
        'Filter by host-level Nagios custom variables, e.g. {"KERNEL": "windows"}. '
        "Resolved via a /hosts lookup then host_name[regex] — the log table does not "
        "expose custom-variable columns directly."
    ),
}
_BACKENDS = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": None,
    "description": "Comma-separated backend names (sites). Omit for all backends.",
}


# ---------------------------------------------------------------------------
# ToolSpec: unified tool registration (issue #85)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for a registered MCP tool.

    Ties together the tool name, its async implementation, the explicit JSON
    Schema for its input, and whether it mutates monitoring state (``is_write``).

    Downstream structures are auto-derived — never edit them by hand:
    - ``_TOOL_DISPATCH``  = {spec.name: spec.fn   for spec in TOOL_REGISTRY}
    - ``_TOOL_SCHEMAS``   = {spec.name: spec.schema for spec in TOOL_REGISTRY}
    - ``WRITE_TOOLS``     = frozenset(spec.name for spec in TOOL_REGISTRY if spec.is_write)

    Adding a new tool requires exactly one entry here; ``WRITE_TOOLS`` cannot
    fall out of sync with the schema or dispatch table.
    """

    name: str
    fn: Callable[..., Coroutine[Any, Any, str]]
    schema: dict[str, Any]
    is_write: bool = False


__all__ = [
    "_BACKENDS",
    "_LOG_CUSTOM_VARS",
    "_LOG_HOSTGROUP",
    "_OPT_BOOL",
    "_OPT_INT",
    "_OPT_OBJ",
    "_OPT_STR",
    "ToolSpec",
    "_bool",
    "_int",
    "_s",
    "_str",
]
