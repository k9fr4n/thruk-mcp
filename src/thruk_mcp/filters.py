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

from .constants import HOST_STATE_INT, SVC_STATE_INT

__all__ = [
    "FIELDS_ALERTS",
    "FIELDS_COMMENTS",
    "FIELDS_DOWNTIMES",
    "FIELDS_HOSTS",
    "FIELDS_HOST_STATS",
    "FIELDS_LOGS",
    "FIELDS_NOISY_HOSTS",
    "FIELDS_NOISY_SERVICES",
    "FIELDS_NOTIFICATIONS",
    "FIELDS_OLDEST_PROBLEMS",
    "FIELDS_PROBLEMS",
    "FIELDS_PROBLEM_COUNTS",
    "FIELDS_SERVICES",
    "FIELDS_STALE_ACKS",
    "FIELDS_TOTALS",
    "FIELDS_UNACKED",
    "FilterError",
    "build_tool_schema",
    "compile_filter",
    "compile_filter_problems",
    "extract_log_lookup_fields",
    "filter_schema_property",
    "infer_alert_type_regex",
    "validate_filter",
]

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
#: ``thruk_oldest_problems`` scopes the unhandled-problem view across
#: ``/hosts`` + ``/services``. ``state`` and ``host`` are intentionally excluded:
#: the tool is already constrained to non-OK states, and per-host filtering
#: would duplicate ``thruk_list_hosts``. See issue #226.
FIELDS_OLDEST_PROBLEMS: frozenset[str] = frozenset({"hostgroup", "custom_var"})
#: ``thruk_unacked_critical`` scopes the unacknowledged CRITICAL/DOWN view across
#: ``/hosts`` + ``/services``. ``state`` is intentionally excluded: the tool is
#: hardcoded to CRITICAL/DOWN by design. ``host`` is excluded to avoid ambiguity
#: with the internal host-name resolution logic. See issue #227.
FIELDS_UNACKED: frozenset[str] = frozenset({"hostgroup", "custom_var"})
#: ``thruk_stale_acks`` scopes the forgotten-acknowledgement review across the
#: ``/comments`` endpoint. Since ``/comments`` does not natively accept hostgroup
#: or custom-variable filters, the matching host set is resolved server-side via
#: ``/hosts`` and applied as a host-name intersection on the comments rows.
#: ``state`` and ``host`` are intentionally excluded — the tool is already
#: constrained to acknowledgement comments and per-host filtering would
#: duplicate ``thruk_list_comments``. See issue #228.
FIELDS_STALE_ACKS: frozenset[str] = frozenset({"hostgroup", "custom_var"})
FIELDS_NOISY_HOSTS: frozenset[str] = frozenset({"host", "hostgroup", "custom_var"})
#: ``thruk_list_downtimes`` filters scheduled downtimes by host scope. The
#: ``/downtimes`` endpoint exposes ``host_name`` natively but not
#: ``host_groups`` or custom-variable columns; ``hostgroup`` / ``custom_var``
#: leaves are therefore resolved via a secondary ``/hosts`` lookup and applied
#: as ``host_name[regex]=...``. ``state`` and ``service`` are intentionally
#: excluded — downtimes are not state-bearing entities and per-service
#: downtimes are addressed by ``thruk_get_downtime`` / writes. See issue #229.
FIELDS_DOWNTIMES: frozenset[str] = frozenset({"host", "hostgroup", "custom_var"})
#: ``thruk_list_comments`` filters comments (operator notes + ack traces) by
#: host scope. The ``/comments`` endpoint exposes ``host_name`` natively but
#: not ``host_groups`` or custom-variable columns; ``hostgroup`` /
#: ``custom_var`` leaves are therefore resolved via a secondary ``/hosts``
#: lookup and applied as ``host_name[regex]=...``. ``service`` is intentionally
#: excluded — comment CRUD is exposed via the dedicated write tools. Identical
#: in shape to :data:`FIELDS_DOWNTIMES` (kept as a distinct constant to document
#: intent and decouple the two endpoints). See issue #230.
FIELDS_COMMENTS: frozenset[str] = frozenset({"host", "hostgroup", "custom_var"})
FIELDS_NOISY_SERVICES: frozenset[str] = frozenset({"host", "service", "hostgroup", "custom_var"})
#: ``thruk_stats`` only supports scope filters on ``/hosts/stats`` + ``/services/stats``.
#: ``servicegroup`` is intentionally excluded (meaningless on ``/hosts/stats``).
FIELDS_HOST_STATS: frozenset[str] = frozenset({"hostgroup", "custom_var"})
#: ``thruk_totals`` scopes ``/hosts/totals`` + ``/services/totals``. ``servicegroup``
#: is accepted but only forwarded to ``/services/totals`` (it has no meaning on
#: ``/hosts/totals`` and is stripped from the host-side params at compile time).
FIELDS_TOTALS: frozenset[str] = frozenset({"hostgroup", "servicegroup", "custom_var"})
#: ``thruk_problem_counts`` shares the same filter contract as ``thruk_totals``
#: — it is a problem-state-only projection over ``/hosts/totals`` +
#: ``/services/totals``. Kept as a distinct constant so callers / catalog
#: generators can advertise the tool's accepted fields independently.
FIELDS_PROBLEM_COUNTS: frozenset[str] = FIELDS_TOTALS

#: Fields that use the _VARNAME convention.
_CV_FIELDS: frozenset[str] = frozenset({"custom_var", "host_custom_var"})

#: Group-membership fields (list-valued columns: ``groups`` / ``host_groups``).
_GROUP_FIELDS: frozenset[str] = frozenset({"hostgroup", "servicegroup"})

#: Operators that are semantically meaningful on a list-valued group column.
#: ``regex`` and ``lte`` are rejected at validation time because they cannot be
#: expressed against Thruk's group columns without producing surprising results
#: (no bracket-op for regex on list columns; ``[lte]`` would mean "subset" which
#: is rarely what callers intend). See issue #240.
_GROUP_FIELDS_ALLOWED_OPS: frozenset[str] = frozenset({"eq", "neq", "gte", "in"})

#: Fields in log-family contexts that require a secondary /hosts lookup.
LOG_LOOKUP_FIELDS: frozenset[str] = frozenset({"hostgroup", "custom_var"})

# ---------------------------------------------------------------------------
# State maps  (imported from constants — single source of truth)
# ---------------------------------------------------------------------------

_HOST_STATE_MAP: dict[str, int] = HOST_STATE_INT
_SVC_STATE_MAP: dict[str, int] = SVC_STATE_INT

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
    if field in _GROUP_FIELDS and op not in _GROUP_FIELDS_ALLOWED_OPS:
        # Issue #240: hostgroup/servicegroup are list-valued group columns.
        # regex/lte on group columns silently produced wrong results before;
        # reject them at validation time with a clear, actionable message.
        raise FilterError(
            f"op={op!r} is not supported on field={field!r}. "
            f"Allowed ops for group fields: {sorted(_GROUP_FIELDS_ALLOWED_OPS)}"
        )
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
        # Issue #241: `neq` must map to the [!] bracket op (was silently compiled
        # as equality), `in` is pre-rewritten to OR(eq, …) by `_expand_in` before
        # reaching this branch and routes through q= instead.
        op_map = {
            "eq": "state",
            "neq": "state[!]",
            "gte": "state[gte]",
            "lte": "state[lte]",
        }
        if op not in op_map:
            raise FilterError(
                f"op={op!r} on field='state' cannot be compiled to a single "
                "bracket-op param; this is a bug in compile_filter."
            )
        return {op_map[op]: int_val}

    if field in _GROUP_FIELDS:
        # Issue #240: honour leaf op on list-valued group columns.
        # eq/gte → membership ([gte] bracket op = "contains").
        # neq    → non-membership ([!] bracket op).
        # in     → rewritten to OR(eq, …) by compile_filter before reaching here.
        base = "host_groups" if (field == "hostgroup" and context == "services") else "groups"
        if op in ("eq", "gte"):
            return {f"{base}[gte]": value}
        if op == "neq":
            return {f"{base}[!]": value}
        # Defensive: 'in' should have been pre-rewritten; 'regex'/'lte' are
        # rejected at validation time. Surface a clear FilterError if we ever
        # reach this branch — never silently fall through to membership.
        raise FilterError(
            f"op={op!r} on field={field!r} cannot be compiled to a single "
            "bracket-op param; this is a bug in compile_filter."
        )

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
# Alerts: state-aware HOST/SERVICE narrowing (issue #198)
# ---------------------------------------------------------------------------

#: Host-only state name strings (Livestatus host states).
_HOST_ONLY_STATE_NAMES: frozenset[str] = frozenset({"up", "down", "unreachable"})

#: Service-only state name strings (Livestatus service states).
_SVC_ONLY_STATE_NAMES: frozenset[str] = frozenset({"ok", "warning", "critical", "unknown"})


def infer_alert_type_regex(node: dict[str, Any] | None) -> str | None:
    """Infer a narrowed ``type[~]`` regex for an alerts query from the filter tree.

    Naemon Livestatus packs both host states (``DOWN=1``) and service states
    (``WARNING=1``) into the same integer ``state`` column.  A naive
    ``state=down`` filter therefore matches both HOST ALERT DOWN and
    SERVICE ALERT WARNING rows (issue #198).

    To disambiguate, this helper inspects every ``state`` leaf in the
    AND-portion of the filter tree:

    - If every state value is a *host-only* state name (``up``, ``down``,
      ``unreachable``) → return ``"^HOST ALERT"``.
    - If every state value is a *service-only* state name (``ok``,
      ``warning``, ``critical``, ``unknown``) → return ``"^SERVICE ALERT"``.
    - Otherwise (no state filter, numeric value, ``neq``/``gte``/``lte``
      operator, mixed classifications, or a state leaf inside an OR
      subtree) → return ``None`` so the caller keeps the default
      ``^(HOST|SERVICE) ALERT`` regex.

    The narrowing is purely additive (it only restricts results that were
    semantically inconsistent before) and never widens the query.
    """
    if node is None:
        return None

    classes: set[str] = set()

    def _classify(val: Any) -> str:
        if isinstance(val, str):
            v = val.lower()
            if v in _HOST_ONLY_STATE_NAMES:
                return "host"
            if v in _SVC_ONLY_STATE_NAMES:
                return "service"
        return "ambiguous"

    def _contains_state(n: dict[str, Any]) -> bool:
        if n.get("type") == "leaf":
            return n.get("field") == "state"
        return any(_contains_state(c) for c in n.get("conditions", []))

    def _walk(n: dict[str, Any]) -> None:
        node_type = n.get("type")
        if node_type == "leaf":
            if n.get("field") != "state":
                return
            op = n.get("op")
            value = n.get("value")
            if op == "eq":
                classes.add(_classify(value))
            elif op == "in" and isinstance(value, list):
                for v in value:
                    classes.add(_classify(v))
            else:
                # neq/gte/lte/regex on state → cannot narrow safely.
                classes.add("ambiguous")
            return
        if node_type == "group":
            if n.get("operator") == "or":
                # OR semantics across heterogeneous branches → bail out
                # if any branch references state at all.
                if _contains_state(n):
                    classes.add("ambiguous")
                return
            for child in n.get("conditions", []):
                _walk(child)

    _walk(node)

    if not classes or "ambiguous" in classes:
        return None
    if classes == {"host"}:
        return "^HOST ALERT"
    if classes == {"service"}:
        return "^SERVICE ALERT"
    return None


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
            # Issue #240: honour `neq` on the list-valued group column.
            # Other ops (eq/gte/in) keep the legacy ``[gte]`` passthrough —
            # Thruk's bracket-op accepts repeated values for list-valued
            # columns (Thruk OR-joins them), and ``thruk_problems`` re-validates
            # the merged rows client-side via _row_matches_hostgroup_constraints
            # (defense-in-depth, issue #200). ``regex`` / ``lte`` are rejected
            # at validation time and never reach this branch.
            op = leaf["op"]
            if op == "neq":
                host_params["groups[!]"] = value
                svc_params["host_groups[!]"] = value
            else:
                host_params["groups[gte]"] = value
                svc_params["host_groups[gte]"] = value
        elif field == "state":
            # Issue #241: honour `neq` on the scalar state column. `in` cannot
            # be expressed without OR on a scalar Livestatus column and OR is
            # rejected at the tree level for `thruk_problems` anyway, so we
            # surface a clear FilterError instead of silently HTTP 400'ing.
            op = leaf["op"]
            if op == "in":
                raise FilterError(
                    "op='in' on field='state' is not supported by thruk_problems "
                    "— the dual-query architecture is AND-only and the scalar "
                    "state column cannot be OR-joined inside a single param."
                )
            # Resolve symbolic state names from BOTH the host and service
            # maps — ``thruk_problems`` mirrors the integer to both /hosts and
            # /services endpoints, so "ok"→0 (service) and "up"→0 (host) must
            # both work even though /hosts doesn't accept "ok" as a string.
            raw = str(value).lower()
            iv = _HOST_STATE_MAP.get(raw, _SVC_STATE_MAP.get(raw, value))
            key = "state[!]" if op == "neq" else "state" if op == "eq" else f"state[{op}]"
            host_params[key] = iv
            svc_params[key] = iv

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


#: Fields whose ``op='in'`` leaf must be rewritten to ``OR(eq, …)`` before
#: compilation. Two distinct reasons:
#: - ``hostgroup``/``servicegroup`` (issue #240): list-valued columns, no single
#:   bracket-op param expresses multi-membership.
#: - ``state`` (issue #241): scalar integer column, but the bracket AND-path
#:   would forward the raw list under ``state=`` and Thruk would HTTP 400 on
#:   the first non-integer value.
_IN_REWRITE_FIELDS: frozenset[str] = _GROUP_FIELDS | frozenset({"state"})


def _expand_group_in(node: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``op='in'`` leaves on group/state fields into ``OR(eq, …)`` groups.

    Group columns are list-valued and have no single bracket-op param that
    expresses set-membership against several candidate group names (issue #240).
    The scalar ``state`` column has the same problem in the AND-only bracket
    path because Thruk cannot OR-join several integer literals under the same
    ``state=`` key (issue #241). Expanding ``in`` into an OR routes the leaf
    through the existing ``q=`` builder, which already handles both cases
    correctly (``(groups >= "A") or (groups >= "B")``, ``(state = 1) or
    (state = 2)``). Non-rewritten fields and other ops are passed through
    untouched.
    """
    node_type = node.get("type")
    if node_type == "leaf":
        if node.get("field") in _IN_REWRITE_FIELDS and node.get("op") == "in":
            values = node["value"]
            return {
                "type": "group",
                "operator": "or",
                "conditions": [
                    {"type": "leaf", "field": node["field"], "op": "eq", "value": v} for v in values
                ],
            }
        return node
    if node_type == "group":
        return {
            "type": "group",
            "operator": node["operator"],
            "conditions": [_expand_group_in(c) for c in node["conditions"]],
        }
    return node


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
    # Issue #240: rewrite `in` on group fields into OR(eq, ...) so it routes
    # naturally through the q= builder instead of being silently coerced to
    # membership by the bracket-op path.
    node = _expand_group_in(node)

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
