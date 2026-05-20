"""Unit tests for thruk_mcp.filters — validate_filter, compile_filter, helpers."""

from __future__ import annotations

import pytest

from thruk_mcp.filters import (
    FIELDS_HOSTS,
    FIELDS_LOGS,
    FIELDS_SERVICES,
    FilterError,
    _has_or,
    build_tool_schema,
    compile_filter,
    compile_filter_problems,
    extract_log_lookup_fields,
    filter_schema_property,
    validate_filter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def leaf(field, op, value):
    return {"type": "leaf", "field": field, "op": op, "value": value}


def group(operator, *conditions):
    return {"type": "group", "operator": operator, "conditions": list(conditions)}


# ---------------------------------------------------------------------------
# validate_filter — structural errors
# ---------------------------------------------------------------------------


def test_validate_unknown_type():
    with pytest.raises(FilterError, match="'type' must be 'leaf' or 'group'"):
        validate_filter({"type": "unknown"}, FIELDS_HOSTS)


def test_validate_non_dict():
    with pytest.raises(FilterError, match="must be a dict"):
        validate_filter("not a dict", FIELDS_HOSTS)  # type: ignore[arg-type]


def test_validate_depth_exceeded():
    node = leaf("state", "eq", "down")
    for _ in range(5):
        node = group("and", node)
    with pytest.raises(FilterError, match="maximum nesting depth"):
        validate_filter(node, FIELDS_HOSTS)


# ---------------------------------------------------------------------------
# validate_filter — leaf errors
# ---------------------------------------------------------------------------


def test_validate_leaf_missing_field():
    with pytest.raises(FilterError, match="missing required key 'field'"):
        validate_filter({"type": "leaf", "op": "eq", "value": "x"}, FIELDS_HOSTS)


def test_validate_leaf_unknown_field():
    with pytest.raises(FilterError, match="Unknown filter field"):
        validate_filter(leaf("nonexistent", "eq", "x"), FIELDS_HOSTS)


def test_validate_leaf_unknown_op():
    with pytest.raises(FilterError, match="Unknown filter operator"):
        validate_filter(leaf("state", "contains", "x"), FIELDS_HOSTS)


def test_validate_leaf_null_value():
    with pytest.raises(FilterError, match="must not be null"):
        validate_filter({"type": "leaf", "field": "state", "op": "eq", "value": None}, FIELDS_HOSTS)


def test_validate_leaf_in_requires_list():
    with pytest.raises(FilterError, match="non-empty list"):
        validate_filter(leaf("state", "in", "down"), FIELDS_HOSTS)


def test_validate_leaf_in_empty_list():
    with pytest.raises(FilterError, match="non-empty list"):
        validate_filter(leaf("state", "in", []), FIELDS_HOSTS)


def test_validate_leaf_in_bad_item():
    with pytest.raises(FilterError, match="list elements must be strings or numbers"):
        validate_filter(leaf("name", "in", [{"nested": "obj"}]), FIELDS_HOSTS)


def test_validate_leaf_regex_not_string():
    with pytest.raises(FilterError, match="requires a string value"):
        validate_filter(leaf("name", "regex", 42), FIELDS_HOSTS)


def test_validate_leaf_bad_regex():
    with pytest.raises(FilterError, match="invalid pattern"):
        validate_filter(leaf("name", "regex", "[unclosed"), FIELDS_HOSTS)


def test_validate_leaf_scalar_required():
    with pytest.raises(FilterError, match="requires a scalar value"):
        validate_filter(leaf("name", "eq", {"nested": "obj"}), FIELDS_HOSTS)


def test_validate_custom_var_requires_dict():
    with pytest.raises(FilterError, match="must be a dict"):
        validate_filter(leaf("custom_var", "eq", "KERNEL=windows"), FIELDS_HOSTS)


def test_validate_custom_var_missing_var_key():
    with pytest.raises(FilterError, match="'var' key"):
        validate_filter(leaf("custom_var", "eq", {"val": "windows"}), FIELDS_HOSTS)


def test_validate_custom_var_empty_var():
    with pytest.raises(FilterError, match="non-empty string"):
        validate_filter(leaf("custom_var", "eq", {"var": ""}), FIELDS_HOSTS)


# ---------------------------------------------------------------------------
# validate_filter — group errors
# ---------------------------------------------------------------------------


def test_validate_group_missing_operator():
    with pytest.raises(FilterError, match="missing required key 'operator'"):
        validate_filter({"type": "group", "conditions": []}, FIELDS_HOSTS)


def test_validate_group_bad_operator():
    with pytest.raises(FilterError, match="must be 'and' or 'or'"):
        validate_filter({"type": "group", "operator": "xor", "conditions": []}, FIELDS_HOSTS)


def test_validate_group_empty_conditions():
    with pytest.raises(FilterError, match="non-empty list"):
        validate_filter(group("and"), FIELDS_HOSTS)


def test_validate_group_condition_not_dict():
    with pytest.raises(FilterError, match="must be a dict"):
        validate_filter(
            {"type": "group", "operator": "and", "conditions": ["not a dict"]}, FIELDS_HOSTS
        )


# ---------------------------------------------------------------------------
# validate_filter — valid cases
# ---------------------------------------------------------------------------


def test_validate_leaf_ok():
    validate_filter(leaf("state", "eq", "down"), FIELDS_HOSTS)


def test_validate_leaf_in_ok():
    validate_filter(leaf("state", "in", ["down", "unreachable"]), FIELDS_HOSTS)


def test_validate_leaf_custom_var_ok():
    validate_filter(leaf("custom_var", "eq", {"var": "KERNEL", "val": "windows"}), FIELDS_HOSTS)


def test_validate_nested_group_ok():
    node = group(
        "and",
        leaf("hostgroup", "eq", "HG_AGILE"),
        group("or", leaf("state", "eq", "down"), leaf("state", "eq", "unreachable")),
    )
    validate_filter(node, FIELDS_HOSTS)


# ---------------------------------------------------------------------------
# _has_or
# ---------------------------------------------------------------------------


def test_has_or_leaf():
    assert _has_or(leaf("state", "eq", "down")) is False


def test_has_or_and_group():
    assert _has_or(group("and", leaf("state", "eq", "down"))) is False


def test_has_or_or_group():
    assert _has_or(group("or", leaf("state", "eq", "down"))) is True


def test_has_or_nested():
    node = group("and", leaf("hostgroup", "eq", "X"), group("or", leaf("state", "eq", "down")))
    assert _has_or(node) is True


# ---------------------------------------------------------------------------
# compile_filter — AND-only (bracket-operator params)
# ---------------------------------------------------------------------------


def test_compile_state_down_hosts():
    p = compile_filter(leaf("state", "eq", "down"), "hosts")
    assert p["state"] == 1


def test_compile_state_warning_services():
    p = compile_filter(leaf("state", "eq", "warning"), "services")
    assert p["state"] == 1


def test_compile_state_numeric_string():
    p = compile_filter(leaf("state", "eq", "1"), "hosts")
    assert p["state"] == 1


def test_compile_hostgroup_hosts():
    p = compile_filter(leaf("hostgroup", "eq", "HG_AGILE"), "hosts")
    assert p["groups[gte]"] == "HG_AGILE"


def test_compile_hostgroup_services():
    p = compile_filter(leaf("hostgroup", "eq", "HG_AGILE"), "services")
    assert p["host_groups[gte]"] == "HG_AGILE"


def test_compile_servicegroup():
    p = compile_filter(leaf("servicegroup", "eq", "db"), "services")
    assert p["groups[gte]"] == "db"


def test_compile_custom_var():
    p = compile_filter(leaf("custom_var", "eq", {"var": "KERNEL", "val": "windows"}), "hosts")
    assert p["_KERNEL"] == "windows"


def test_compile_custom_var_uppercased():
    p = compile_filter(leaf("custom_var", "eq", {"var": "kernel", "val": "linux"}), "hosts")
    assert p["_KERNEL"] == "linux"


def test_compile_host_custom_var():
    p = compile_filter(
        leaf("host_custom_var", "eq", {"var": "KERNEL", "val": "windows"}), "services"
    )
    assert p["_HOSTKERNEL"] == "windows"


def test_compile_name_regex():
    p = compile_filter(leaf("name", "regex", "web.*"), "hosts")
    assert p["name[regex]"] == "web.*"


def test_compile_name_neq():
    p = compile_filter(leaf("name", "neq", "router01"), "hosts")
    assert p["name[!]"] == "router01"


def test_compile_name_in():
    p = compile_filter(leaf("name", "in", ["a", "b"]), "hosts")
    assert "name[regex]" in p
    assert "a" in p["name[regex]"] and "b" in p["name[regex]"]


def test_compile_since_until():
    p = compile_filter(
        group("and", leaf("since", "gte", "-2h"), leaf("until", "lte", "-1h")),
        "logs",
    )
    assert p["time[gte]"] == "-2h"
    assert p["time[lte]"] == "-1h"


def test_compile_and_group():
    node = group(
        "and",
        leaf("hostgroup", "eq", "HG_AGILE"),
        leaf("state", "eq", "down"),
    )
    p = compile_filter(node, "hosts")
    assert p["groups[gte]"] == "HG_AGILE"
    assert p["state"] == 1


# ---------------------------------------------------------------------------
# compile_filter — OR → q= expression
# ---------------------------------------------------------------------------


def test_compile_or_produces_q():
    node = group("or", leaf("hostgroup", "eq", "A"), leaf("hostgroup", "eq", "B"))
    p = compile_filter(node, "hosts")
    assert "q" in p
    assert "groups" in p["q"]
    assert " or " in p["q"]


def test_compile_or_custom_var_in_q():
    node = group(
        "or",
        leaf("hostgroup", "eq", "HG_AGILE"),
        leaf("custom_var", "eq", {"var": "KERNEL", "val": "windows"}),
    )
    p = compile_filter(node, "hosts")
    assert "q" in p
    assert "_KERNEL" in p["q"]
    assert "groups" in p["q"]


def test_compile_nested_or_and():
    node = group(
        "and",
        group("or", leaf("hostgroup", "eq", "A"), leaf("hostgroup", "eq", "B")),
        leaf("state", "eq", "down"),
    )
    p = compile_filter(node, "hosts")
    # Hybrid mode: OR subtree → q=, AND scalar leaf (state) → bracket param.
    # Thruk silently returns [] when state is inside q= together with a groups
    # OR expression, so state must be a top-level bracket param.
    assert "q" in p
    assert "state" not in p["q"]  # state extracted as bracket param
    assert p.get("state") == 1


def test_compile_hybrid_hostgroup_or_custom_var_with_state():
    """AND(state=down, OR(hostgroup=HG_WINDOWS, cv=KERNEL=windows)) must not put
    state inside q= — Thruk silently returns [] in that case (confirmed live)."""
    node = group(
        "and",
        leaf("state", "eq", "down"),
        group(
            "or",
            leaf("hostgroup", "eq", "HG_WINDOWS"),
            leaf("custom_var", "eq", {"var": "KERNEL", "val": "windows"}),
        ),
    )
    p = compile_filter(node, "hosts")
    assert p.get("state") == 1
    assert "q" in p
    assert "HG_WINDOWS" in p["q"]
    assert "_KERNEL" in p["q"]
    assert "state" not in p["q"]


def test_q_expr_state_in():
    node = group(
        "or",
        leaf("state", "in", ["down", "unreachable"]),
        leaf("hostgroup", "eq", "X"),
    )
    p = compile_filter(node, "hosts")
    assert "q" in p
    q = p["q"]
    assert "state = 1" in q or "state = 2" in q


def test_q_expr_host_groups_services():
    node = group("or", leaf("hostgroup", "eq", "A"), leaf("hostgroup", "eq", "B"))
    p = compile_filter(node, "services")
    assert "host_groups" in p["q"]


def test_q_expr_neq():
    node = group("or", leaf("name", "neq", "x"), leaf("name", "eq", "y"))
    p = compile_filter(node, "hosts")
    assert "!=" in p["q"]


def test_q_expr_name_in():
    node = group("or", leaf("name", "in", ["a", "b"]), leaf("state", "eq", "down"))
    p = compile_filter(node, "hosts")
    assert "q" in p


def test_q_expr_single_condition_group():
    node = group("or", leaf("hostgroup", "eq", "X"))
    p = compile_filter(node, "hosts")
    assert "q" in p
    assert "and" not in p["q"] and "or" not in p["q"]


# ---------------------------------------------------------------------------
# extract_log_lookup_fields
# ---------------------------------------------------------------------------


def test_log_split_direct_only():
    node = leaf("host", "eq", "srv01")
    direct, lookup = extract_log_lookup_fields(node)
    assert direct is not None
    assert lookup is None


def test_log_split_lookup_only():
    node = leaf("hostgroup", "eq", "HG_AGILE")
    direct, lookup = extract_log_lookup_fields(node)
    assert direct is None
    assert lookup is not None
    assert lookup["field"] == "hostgroup"


def test_log_split_mixed():
    node = group(
        "and",
        leaf("host", "eq", "srv01"),
        leaf("hostgroup", "eq", "HG_AGILE"),
    )
    direct, lookup = extract_log_lookup_fields(node)
    assert direct is not None
    assert lookup is not None


def test_log_split_or_direct_ok():
    """OR between direct fields (no lookup) is allowed."""
    node = group("or", leaf("host", "eq", "a"), leaf("service", "eq", "b"))
    direct, lookup = extract_log_lookup_fields(node)
    assert direct is not None
    assert lookup is None


def test_log_split_or_with_lookup_raises():
    node = group("or", leaf("host", "eq", "a"), leaf("hostgroup", "eq", "HG"))
    with pytest.raises(FilterError, match="do not support OR on"):
        extract_log_lookup_fields(node)


# ---------------------------------------------------------------------------
# compile_filter_problems
# ---------------------------------------------------------------------------


def test_problems_custom_var():
    host_p, svc_p = compile_filter_problems(leaf("custom_var", "eq", {"var": "ENV", "val": "prod"}))
    assert host_p["_ENV"] == "prod"
    assert svc_p["_HOSTENV"] == "prod"


def test_problems_host_custom_var():
    host_p, svc_p = compile_filter_problems(
        leaf("host_custom_var", "eq", {"var": "KERNEL", "val": "windows"})
    )
    assert "_HOSTKERNEL" not in host_p
    assert svc_p["_HOSTKERNEL"] == "windows"


def test_problems_hostgroup():
    host_p, svc_p = compile_filter_problems(leaf("hostgroup", "eq", "HG_AGILE"))
    assert host_p["groups[gte]"] == "HG_AGILE"
    assert svc_p["host_groups[gte]"] == "HG_AGILE"


def test_problems_state():
    host_p, svc_p = compile_filter_problems(leaf("state", "eq", "down"))
    assert host_p["state"] == 1
    assert svc_p["state"] == 1


def test_problems_or_raises():
    node = group("or", leaf("hostgroup", "eq", "A"), leaf("hostgroup", "eq", "B"))
    with pytest.raises(FilterError, match="does not support OR"):
        compile_filter_problems(node)


def test_problems_and_group():
    node = group(
        "and",
        leaf("hostgroup", "eq", "HG_AGILE"),
        leaf("custom_var", "eq", {"var": "ENV", "val": "prod"}),
    )
    host_p, svc_p = compile_filter_problems(node)
    assert host_p["groups[gte]"] == "HG_AGILE"
    assert host_p["_ENV"] == "prod"
    assert svc_p["host_groups[gte]"] == "HG_AGILE"
    assert svc_p["_HOSTENV"] == "prod"


# ---------------------------------------------------------------------------
# JSON Schema helpers
# ---------------------------------------------------------------------------


def test_filter_schema_property_has_anyof():
    s = filter_schema_property(FIELDS_HOSTS)
    assert "anyOf" in s
    assert any("$ref" in item for item in s["anyOf"])


def test_build_tool_schema_has_defs():
    s = build_tool_schema(FIELDS_HOSTS, filter=filter_schema_property(FIELDS_HOSTS))
    assert "$defs" in s
    assert "FilterLeaf" in s["$defs"]
    assert "FilterGroup" in s["$defs"]
    assert "filter" in s["properties"]


def test_build_tool_schema_required():
    s = build_tool_schema(FIELDS_HOSTS, "host", host={"type": "string"})
    assert s["required"] == ["host"]


def test_filter_schema_examples_hosts():
    s = filter_schema_property(FIELDS_HOSTS)
    assert "HG_AGILE" in s["description"]


def test_filter_schema_examples_services():
    s = filter_schema_property(FIELDS_SERVICES)
    # FIELDS_SERVICES has both state+hostgroup → uses the same example block as hosts
    assert "HG_AGILE" in s["description"]


def test_filter_schema_examples_logs():
    s = filter_schema_property(FIELDS_LOGS)
    assert "DISK" in s["description"]
