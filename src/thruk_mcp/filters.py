"""
Structured filter tree for Thruk REST API queries.

Replaces the flat, AND-only individual filter params (hostgroup=, state=,
custom_vars=, ...) with a composable AND/OR tree that maps to efficient
Thruk REST params.

Node types
----------
FilterLeaf  {"type": "leaf",  "field": "...", "op": "...", "value": ...}
FilterGroup {"type": "group", "operator": "and"|"or", "conditions": [...]}

For ``custom_var`` / ``host_custom_var`` fields, ``value`` must be a dict::

    {"var": "KERNEL", "val": "windows"}   # equality
    {"var": "KERNEL"}                      # existence check (val defaults to "")

Translation strategy
--------------------
Pure AND tree     → Thruk bracket-operator params
                    (name[regex]=, state=, groups[gte]=, _VARNAME=, ...)
Any OR node       → single Thruk ``q=`` expression
                    (``_VARNAME`` syntax also works inside q=)
Log-family tools  → callers must call :func:`extract_log_lookup_fields` to
                    separate fields that need a ``/hosts`` secondary lookup
                    (``hostgroup``, ``custom_var``) from direct params.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

LEAF_OPS: frozenset[str] = frozenset({"eq", "neq", "regex", "in", "gte", "lte"})
MAX_DEPTH: int = 4

#: Field sets per tool context — also used to build per-tool JSON Schemas.
FIELDS_HOSTS: frozenset[str] = frozenset({"name", "state", "hostgroup", "custom_var", "address"})
FIELDS_SERVICES: frozenset[str] = frozenset(
    {
        "host",
        "description",
        "state",
        "hostgroup",
        "servicegroup",
        "custom_var",
        "host_custom_var",
    }
)
FIELDS_LOGS: frozenset[str] = frozenset(
    {"host", "service", "message", "hostgroup", "custom_var", "since", "until"}
)
FIELDS_ALERTS: frozenset[str] = frozenset(
    {"host", "service", "state", "hostgroup", "custom_var", "since", "until"}
)
FIELDS_NOTIFICATIONS: frozenset[str] = frozenset(
    {"host", "service", "state", "contact", "hostgroup", "custom_var", "since", "until"}
)
FIELDS_PROBLEMS: frozenset[str] = frozenset({"state", "hostgroup", "custom_var", "host_custom_var"})
FIELDS_NOISY_HOSTS: frozenset[str] = frozenset({"host", "hostgroup", "custom_var"})
FIELDS_NOISY_SERVICES: frozenset[str] = frozenset({"host", "service", "hostgroup", "custom_var"})

#: Fields that use the _VARNAME convention.
_CV_FIELDS: frozenset[str] = frozenset({"custom_var", "host_custom_var"})

#: Fields in log-family contexts that require a secondary /hosts lookup.
LOG_LOOKUP_FIELDS: frozenset[str] = frozenset({"hostgroup", "custom_var"})

# ---------------------------------------------------------------------------
# State maps
# ---------------------------------------------------------------------------

_HOST_STATE_MAP: dict[str, int] = {
    "up": 0,
    "down": 1,
    "unreachable": 2,
    "0": 0,
    "1": 1,
    "2": 2,
}
_SVC_STATE_MAP: dict[str, int] = {
    "ok": 0,
    "warning": 1,
    "critical": 2,
    "unknown": 3,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
}

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FilterError(ValueError):
    """Raised when a filter tree is structurally invalid."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_filter(
    node: dict[str, Any],
    allowed_fields: frozenset[str],
    _depth: int = 0,
) -> None:
    """Recursively validate a filter tree.

    Raises :class:`FilterError` on unknown type/field/op, value type
    mismatches, or exceeding :data:`MAX_DEPTH`.
    """
    if _depth > MAX_DEPTH:
        raise FilterError(f"Filter tree exceeds maximum nesting depth ({MAX_DEPTH})")
    if not isinstance(node, dict):
        raise FilterError(f"Filter node must be a dict, got {type(node).__name__!r}")

    node_type = node.get("type")
    if node_type == "leaf":
        _validate_leaf(node, allowed_fields)
    elif node_type == "group":
        _validate_group(node, allowed_fields, _depth)
    else:
        raise FilterError(f"Filter node 'type' must be 'leaf' or 'group', got {node_type!r}")


def _validate_leaf(node: dict[str, Any], allowed_fields: frozenset[str]) -> None:
    for key in ("field", "op", "value"):
        if key not in node:
            raise FilterError(f"Filter leaf missing required key {key!r}")

    field: str = node["field"]
    op: str = node["op"]
    value: Any = node["value"]

    if not isinstance(field, str) or field not in allowed_fields:
        raise FilterError(f"Unknown filter field {field!r}. Allowed: {sorted(allowed_fields)}")
    if not isinstance(op, str) or op not in LEAF_OPS:
        raise FilterError(f"Unknown filter operator {op!r}. Allowed: {sorted(LEAF_OPS)}")
    if value is None:
        raise FilterError("Filter leaf 'value' must not be null")

    if op == "in":
        if not isinstance(value, list) or not value:
            raise FilterError("op='in' requires a non-empty list value")
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise FilterError("op='in' list elements must be strings or numbers")
    elif op == "regex":
        if not isinstance(value, str):
            raise FilterError("op='regex' requires a string value")
        try:
            re.compile(value)
        except re.error as exc:
            raise FilterError(f"op='regex' invalid pattern {value!r}: {exc}") from exc
    elif field not in _CV_FIELDS:
        if not isinstance(value, (str, int, float)):
            raise FilterError(f"op={op!r} requires a scalar value (string, integer, or float)")

    if field in _CV_FIELDS:
        if not isinstance(value, dict) or "var" not in value:
            raise FilterError(
                f"field={field!r} value must be a dict with at least a 'var' key, "
                'e.g. {"var": "KERNEL", "val": "windows"}'
            )
        if not isinstance(value["var"], str) or not value["var"]:
            raise FilterError(f"field={field!r} 'var' must be a non-empty string")


def _validate_group(
    node: dict[str, Any],
    allowed_fields: frozenset[str],
    _depth: int,
) -> None:
    for key in ("operator", "conditions"):
        if key not in node:
            raise FilterError(f"Filter group missing required key {key!r}")

    operator = node["operator"]
    conditions = node["conditions"]

    if operator not in ("and", "or"):
        raise FilterError(f"Group 'operator' must be 'and' or 'or', got {operator!r}")
    if not isinstance(conditions, list) or not conditions:
        raise FilterError("Group 'conditions' must be a non-empty list")
    for i, child in enumerate(conditions):
        if not isinstance(child, dict):
            raise FilterError(f"Condition at index {i} must be a dict")
        validate_filter(child, allowed_fields, _depth + 1)


# ---------------------------------------------------------------------------
# Tree inspection
# ---------------------------------------------------------------------------


def _has_or(node: dict[str, Any]) -> bool:
    """Return True if any OR group node exists anywhere in the tree."""
    if node.get("type") == "leaf":
        return False
    if node.get("operator") == "or":
        return True
    return any(_has_or(c) for c in node.get("conditions", []))


# ---------------------------------------------------------------------------
# AND-only compilation → bracket-operator params
# ---------------------------------------------------------------------------


def _leaf_to_params(leaf: dict[str, Any], context: str) -> dict[str, Any]:
    """Translate one leaf node to Thruk REST query params."""
    field: str = leaf["field"]
    op: str = leaf["op"]
    value: Any = leaf["value"]

    if field in _CV_FIELDS:
        var_name: str = value["var"].upper()
        val: str = str(value.get("val", ""))
        prefix = "_HOST" if field == "host_custom_var" else "_"
        return {f"{prefix}{var_name}": val}

    if field == "state":
        # Alerts/logs accept both host states (down=1) and service states (warning=1).
        # Merge both maps so "warning"→1 and "down"→1 both resolve correctly.
        if context == "services":
            state_map = _SVC_STATE_MAP
        else:
            state_map = {**_HOST_STATE_MAP, **_SVC_STATE_MAP}
        raw = str(value).lower()
        int_val = state_map.get(raw, int(value) if str(value).isdigit() else value)
        op_map = {"eq": "state", "gte": "state[gte]", "lte": "state[lte]"}
        return {op_map.get(op, "state"): int_val}

    if field == "hostgroup":
        thruk_key = "host_groups[gte]" if context == "services" else "groups[gte]"
        return {thruk_key: value}

    if field == "servicegroup":
        return {"groups[gte]": value}

    if field == "since":
        return {"time[gte]": value}
    if field == "until":
        return {"time[lte]": value}

    _field_map: dict[str, str] = {
        "name": "name",
        "address": "address",
        "host": "host_name",
        "service": "service_description",
        "description": "description",
        "message": "message",
        "contact": "contact_name",
    }
    thruk_field = _field_map.get(field, field)

    if op == "eq":
        return {thruk_field: value}
    if op == "neq":
        return {f"{thruk_field}[!]": value}
    if op == "regex":
        return {f"{thruk_field}[regex]": value}
    if op == "gte":
        return {f"{thruk_field}[gte]": value}
    if op == "lte":
        return {f"{thruk_field}[lte]": value}
    if op == "in":
        return {f"{thruk_field}[regex]": "|".join(re.escape(str(v)) for v in value)}
    return {thruk_field: value}


def _and_tree_to_params(node: dict[str, Any], context: str) -> dict[str, Any]:
    """Recursively compile a pure-AND tree to bracket-operator params."""
    if node.get("type") == "leaf":
        return _leaf_to_params(node, context)
    params: dict[str, Any] = {}
    for child in node["conditions"]:
        params.update(_and_tree_to_params(child, context))
    return params


# ---------------------------------------------------------------------------
# q= expression builder (any OR node present)
# ---------------------------------------------------------------------------

_Q_FIELD: dict[str, str] = {
    "name": "name",
    "address": "address",
    "state": "state",
    "host": "host_name",
    "service": "service_description",
    "description": "description",
    "message": "message",
    "contact": "contact_name",
}


def _q_leaf(leaf: dict[str, Any], context: str) -> str:
    """Build a q= expression fragment for a single leaf."""
    field: str = leaf["field"]
    op: str = leaf["op"]
    value: Any = leaf["value"]

    if field in _CV_FIELDS:
        var_name = value["var"].upper()
        val = str(value.get("val", ""))
        prefix = "_HOST" if field == "host_custom_var" else "_"
        return f'({prefix}{var_name} = "{val}")'

    if field == "state":
        state_map = _SVC_STATE_MAP if context == "services" else _HOST_STATE_MAP
        raw = str(value).lower()
        int_val = state_map.get(raw, value)
        if op == "in":
            parts = [f"(state = {state_map.get(str(v).lower(), v)})" for v in value]
            return "(" + " or ".join(parts) + ")"
        _op = {"eq": "=", "neq": "!=", "gte": ">=", "lte": "<="}.get(op, "=")
        return f"(state {_op} {int_val})"

    if field in ("hostgroup", "servicegroup"):
        q_field = "host_groups" if (field == "hostgroup" and context == "services") else "groups"
        if op == "in":
            parts = [f'({q_field} >= "{v}")' for v in value]
            return "(" + " or ".join(parts) + ")"
        if op == "neq":
            return f'({q_field} != "{value}")'
        return f'({q_field} >= "{value}")'

    if field == "since":
        return f"(time >= {value})"
    if field == "until":
        return f"(time <= {value})"

    q_field = _Q_FIELD.get(field, field)
    if op == "eq":
        return f'({q_field} = "{value}")'
    if op == "neq":
        return f'({q_field} != "{value}")'
    if op == "regex":
        return f'({q_field} ~~ "{value}")'
    if op == "gte":
        return f'({q_field} >= "{value}")'
    if op == "lte":
        return f'({q_field} <= "{value}")'
    if op == "in":
        parts = [f'({q_field} = "{v}")' for v in value]
        return "(" + " or ".join(parts) + ")"
    return f'({q_field} = "{value}")'


def _build_q_expr(node: dict[str, Any], context: str, _root: bool = True) -> str:
    """Recursively build a Thruk q= expression from a filter tree.

    Thruk's q= parser rejects a top-level expression wrapped in parentheses
    (e.g. ``((A) or (B))`` is invalid, ``(A) or (B)`` is valid).  Only nested
    groups — i.e. groups that are children of another group — are wrapped.
    """
    if node.get("type") == "leaf":
        return _q_leaf(node, context)
    operator = node["operator"]
    parts = [_build_q_expr(child, context, _root=False) for child in node["conditions"]]
    if len(parts) == 1:
        return parts[0]
    joined = f" {operator} ".join(parts)
    # Don't wrap the root expression — Thruk rejects outer parens at q= top level
    return joined if _root else f"({joined})"


# ---------------------------------------------------------------------------
# Log-family: split lookup fields from direct fields
# ---------------------------------------------------------------------------


def extract_log_lookup_fields(
    node: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Split a log-family filter into (direct_node, lookup_node).

    Log tables don't expose ``hostgroup`` or ``custom_var`` columns directly;
    those require a secondary ``/hosts`` lookup. This function walks the
    top-level AND tree and separates:

    - Direct leaves (host, service, message, since, until) → direct_node
    - Lookup leaves (hostgroup, custom_var)               → lookup_node

    OR nodes containing lookup fields raise :class:`FilterError` (unsupported).
    """
    direct_nodes: list[dict[str, Any]] = []
    lookup_leaves: list[dict[str, Any]] = []

    def _has_lookup(n: dict[str, Any]) -> bool:
        if n.get("type") == "leaf":
            return n["field"] in LOG_LOOKUP_FIELDS
        return any(_has_lookup(c) for c in n.get("conditions", []))

    def _walk(n: dict[str, Any]) -> None:
        if n.get("type") == "leaf":
            (lookup_leaves if n["field"] in LOG_LOOKUP_FIELDS else direct_nodes).append(n)
        elif n.get("type") == "group":
            if n["operator"] == "or":
                if _has_lookup(n):
                    raise FilterError(
                        "Log/alert/notification filters do not support OR on "
                        "'hostgroup' or 'custom_var' — these fields require a "
                        "secondary /hosts lookup and can only be AND-combined."
                    )
                direct_nodes.append(n)
            else:
                for child in n["conditions"]:
                    _walk(child)

    _walk(node)

    def _wrap(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not nodes:
            return None
        if len(nodes) == 1:
            return nodes[0]
        return {"type": "group", "operator": "and", "conditions": nodes}

    return _wrap(direct_nodes), _wrap(lookup_leaves)


# ---------------------------------------------------------------------------
# Problems: compile to (host_params, service_params)
# ---------------------------------------------------------------------------


def compile_filter_problems(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile a problems-context filter to ``(host_params, svc_params)``.

    Routing:
    - ``custom_var``      → ``_VAR`` on hosts + ``_HOSTVAR`` on services
    - ``host_custom_var`` → ``_HOSTVAR`` on services only
    - ``hostgroup``       → ``groups[gte]`` on hosts + ``host_groups[gte]`` on services
    - ``state``           → same integer on both

    OR nodes are not supported (raises :class:`FilterError`).
    """
    host_params: dict[str, Any] = {}
    svc_params: dict[str, Any] = {}

    def _apply(leaf: dict[str, Any]) -> None:
        field, _op, value = leaf["field"], leaf["op"], leaf["value"]
        if field == "custom_var":
            k = value["var"].upper()
            v = str(value.get("val", ""))
            host_params[f"_{k}"] = v
            svc_params[f"_HOST{k}"] = v
        elif field == "host_custom_var":
            k = value["var"].upper()
            svc_params[f"_HOST{k}"] = str(value.get("val", ""))
        elif field == "hostgroup":
            host_params["groups[gte]"] = value
            svc_params["host_groups[gte]"] = value
        elif field == "state":
            iv = _HOST_STATE_MAP.get(str(value).lower(), value)
            host_params["state"] = iv
            svc_params["state"] = iv

    def _walk(n: dict[str, Any]) -> None:
        if n.get("type") == "leaf":
            _apply(n)
        else:
            if n.get("operator") == "or":
                raise FilterError(
                    "thruk_problems filter does not support OR — "
                    "the dual-query architecture requires AND-only filters."
                )
            for child in n["conditions"]:
                _walk(child)

    _walk(node)
    return host_params, svc_params


# ---------------------------------------------------------------------------
# Main compile entry point
# ---------------------------------------------------------------------------


def _compile_hybrid(node: dict[str, Any], context: str) -> dict[str, Any]:
    """Compile a root AND tree that contains at least one OR subtree.

    Thruk's ``q=`` parser silently returns empty results when an expression
    of the form ``((groups >= X) or (_VAR = Y)) and (state = N)`` is used —
    it cannot evaluate OR across heterogeneous list-columns (``groups``,
    ``custom_variables``) combined with an outer AND on a scalar column.

    Work-around: extract top-level AND conditions that contain no OR node
    and compile them as bracket-operator params; keep the OR subtree(s) in
    ``q=``.  Thruk evaluates both independently and intersects the results,
    which is exactly the AND semantics we need.

    Example
    -------
    Filter: ``AND(state=down, OR(hostgroup=HG_WINDOWS, cv=KERNEL=windows))``
    Output: ``{"state": 1, "q": "(groups >= \\"HG_WINDOWS\\") or (_KERNEL = \\"windows\\")"}``
    """
    # Root must be an AND group here (caller guarantees it).
    bracket_params: dict[str, Any] = {}
    or_nodes: list[dict[str, Any]] = []

    for child in node["conditions"]:
        if _has_or(child):
            or_nodes.append(child)
        else:
            bracket_params.update(_and_tree_to_params(child, context))

    if or_nodes:
        if len(or_nodes) == 1:
            q_node = or_nodes[0]
        else:
            # Multiple OR subtrees at the AND level → AND them in q=
            q_node = {"type": "group", "operator": "and", "conditions": or_nodes}
        bracket_params["q"] = _build_q_expr(q_node, context)

    return bracket_params


def compile_filter(node: dict[str, Any], context: str) -> dict[str, Any]:
    """Compile a validated filter tree to Thruk REST query params.

    Parameters
    ----------
    node:
        Root filter node, already validated by :func:`validate_filter`.
    context:
        ``'hosts'``, ``'services'``, ``'logs'``, ``'alerts'``,
        ``'notifications'``.  For ``'problems'`` use
        :func:`compile_filter_problems` instead.

    Returns
    -------
    dict
        - Pure-AND tree → bracket-operator params only.
        - Root OR tree  → single ``q=`` expression.
        - AND tree with OR subtree(s) → bracket params for the AND leaves
          + ``q=`` for the OR subtree(s).  This hybrid mode avoids the Thruk
          ``q=`` parser bug where ``((groups >= X) or (_VAR = Y)) and (state
          = N)`` silently returns empty results.
    """
    if not _has_or(node):
        return _and_tree_to_params(node, context)
    # Root is OR (or a bare leaf with no AND wrapper) → full q= as before
    if node.get("type") == "leaf" or node.get("operator") == "or":
        return {"q": _build_q_expr(node, context)}
    # Root is AND containing at least one OR subtree → hybrid
    return _compile_hybrid(node, context)


# ---------------------------------------------------------------------------
# JSON Schema helpers
# ---------------------------------------------------------------------------


def _make_filter_defs(fields: frozenset[str]) -> dict[str, Any]:
    """Build the ``$defs`` block (FilterLeaf + FilterGroup) for a field set."""
    leaf: dict[str, Any] = {
        "type": "object",
        "required": ["type", "field", "op", "value"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "leaf"},
            "field": {
                "type": "string",
                "enum": sorted(fields),
                "description": "The monitoring attribute to filter on.",
            },
            "op": {
                "type": "string",
                "enum": sorted(LEAF_OPS),
                "description": (
                    "Comparison operator: eq (=), neq (≠), "
                    "regex (case-insensitive ~), in (list), gte (≥), lte (≤)."
                ),
            },
            "value": {
                "description": (
                    "Comparison value. Use a list for op='in'. "
                    "For custom_var / host_custom_var use "
                    '{"var": "NAME", "val": "value"}.'
                ),
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                    {"type": "number"},
                    {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    {
                        "type": "object",
                        "required": ["var"],
                        "properties": {
                            "var": {
                                "type": "string",
                                "description": "Custom variable name (auto-uppercased).",
                            },
                            "val": {"type": "string", "description": "Expected value."},
                        },
                        "additionalProperties": False,
                    },
                ],
            },
        },
    }
    group: dict[str, Any] = {
        "type": "object",
        "required": ["type", "operator", "conditions"],
        "additionalProperties": False,
        "properties": {
            "type": {"const": "group"},
            "operator": {
                "type": "string",
                "enum": ["and", "or"],
                "description": "Logical operator applied to all conditions.",
            },
            "conditions": {
                "type": "array",
                "minItems": 1,
                "description": "Sub-conditions (leaf or nested group nodes).",
                "items": {
                    "oneOf": [
                        {"$ref": "#/$defs/FilterLeaf"},
                        {"$ref": "#/$defs/FilterGroup"},
                    ]
                },
            },
        },
    }
    return {"FilterLeaf": leaf, "FilterGroup": group}


def _build_examples(fields: frozenset[str]) -> str:
    lines = ["Examples:"]
    if "state" in fields and "hostgroup" in fields:
        lines += [
            "  # Hosts DOWN in HG_AGILE:",
            '  {"type":"group","operator":"and","conditions":[',
            '    {"type":"leaf","field":"hostgroup","op":"eq","value":"HG_AGILE"},',
            '    {"type":"leaf","field":"state","op":"eq","value":"down"}',
            "  ]}",
            "",
            "  # Hosts in HG_AGILE OR with KERNEL=windows:",
            '  {"type":"group","operator":"or","conditions":[',
            '    {"type":"leaf","field":"hostgroup","op":"eq","value":"HG_AGILE"},',
            '    {"type":"leaf","field":"custom_var","op":"eq",'
            '"value":{"var":"KERNEL","val":"windows"}}',
            "  ]}",
        ]
    elif "host" in fields and "state" in fields:
        lines += [
            "  # Critical/Unknown services on web-01:",
            '  {"type":"group","operator":"and","conditions":[',
            '    {"type":"leaf","field":"host","op":"eq","value":"web-01"},',
            '    {"type":"leaf","field":"state","op":"in","value":["critical","unknown"]}',
            "  ]}",
        ]
    elif "message" in fields:
        lines += [
            "  # Log entries matching a pattern since -2h:",
            '  {"type":"group","operator":"and","conditions":[',
            '    {"type":"leaf","field":"message","op":"regex","value":"DISK.*CRITICAL"},',
            '    {"type":"leaf","field":"since","op":"gte","value":"-2h"}',
            "  ]}",
        ]
    return "\n".join(lines)


def filter_schema_property(fields: frozenset[str]) -> dict[str, Any]:
    """Return the JSON Schema ``filter`` property fragment (uses ``$ref`` to ``$defs``)."""
    return {
        "anyOf": [
            {"$ref": "#/$defs/FilterLeaf"},
            {"$ref": "#/$defs/FilterGroup"},
            {"type": "null"},
        ],
        "default": None,
        "description": (
            "Structured filter tree supporting AND/OR nesting.\n\n"
            "Two node types:\n"
            '  leaf:  {"type":"leaf",  "field":"...", "op":"...", "value":...}\n'
            '  group: {"type":"group", "operator":"and"|"or", "conditions":[...]}\n\n'
            f"Available fields: {', '.join(sorted(fields))}\n"
            f"Operators: {', '.join(sorted(LEAF_OPS))}\n\n" + _build_examples(fields)
        ),
    }


def build_tool_schema(
    fields: frozenset[str],
    *required: str,
    **props: Any,
) -> dict[str, Any]:
    """Build a complete ``inputSchema`` with ``$defs`` for filter-aware tools.

    Usage::

        _TOOL_SCHEMAS["thruk_list_hosts"] = build_tool_schema(
            FIELDS_HOSTS,
            filter=filter_schema_property(FIELDS_HOSTS),
            limit=_int(default=50),
            ...
        )
    """
    properties = {k: (v if isinstance(v, dict) else {"type": v}) for k, v in props.items()}
    schema: dict[str, Any] = {
        "type": "object",
        "$defs": _make_filter_defs(fields),
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema
