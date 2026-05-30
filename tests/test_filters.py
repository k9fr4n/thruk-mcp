"""Unit tests for thruk_mcp.filters — validate_filter, compile_filter, helpers."""

from __future__ import annotations

import pytest

from thruk_mcp.filters import (
    FIELDS_ALERTS,
    FIELDS_COMMENTS,
    FIELDS_DOWNTIMES,
    FIELDS_HOST_STATS,
    FIELDS_HOSTS,
    FIELDS_LOGS,
    FIELDS_NOISY_HOSTS,
    FIELDS_NOISY_SERVICES,
    FIELDS_NOTIFICATIONS,
    FIELDS_OLDEST_PROBLEMS,
    FIELDS_PROBLEM_COUNTS,
    FIELDS_PROBLEMS,
    FIELDS_SERVICES,
    FIELDS_STALE_ACKS,
    FIELDS_TOTALS,
    FIELDS_UNACKED,
    FilterError,
    _has_or,
    build_tool_schema,
    compile_filter,
    compile_filter_problems,
    extract_log_lookup_fields,
    filter_schema_property,
    rewrite_custom_var_to_host_custom_var,
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


# ---------------------------------------------------------------------------
# Issue #241: state neq / state in in the AND-only (bracket) path
# ---------------------------------------------------------------------------


def test_compile_state_neq_services():
    """Before the fix, ``state neq ok`` compiled to ``{"state": 0}`` (silent
    equality flip — returned ONLY ok services, the exact opposite of intent).
    After the fix, it must emit the ``state[!]`` bracket-op."""
    p = compile_filter(leaf("state", "neq", "ok"), "services")
    assert p == {"state[!]": 0}
    assert "state" not in p  # the buggy silent-equality key must not leak


def test_compile_state_neq_hosts_numeric():
    p = compile_filter(leaf("state", "neq", "down"), "hosts")
    assert p == {"state[!]": 1}


def test_compile_state_neq_combined_with_hostgroup():
    """AND of ``state neq`` with another scalar leaf must keep using the
    bracket path on both sides (no q= fallback needed)."""
    p = compile_filter(
        group("and", leaf("state", "neq", "ok"), leaf("hostgroup", "eq", "HG_AGILE")),
        "services",
    )
    assert p["state[!]"] == 0
    assert p["host_groups[gte]"] == "HG_AGILE"
    assert "q" not in p


def test_compile_state_in_services_uses_q():
    """Before the fix, ``state in [warning, critical]`` was forwarded as a raw
    list under bare ``state=`` and Thruk replied HTTP 400 ("could not convert
    warning to integer"). After the fix, ``in`` is rewritten to OR(eq, …) and
    routed through the q= builder which already handles state correctly."""
    p = compile_filter(leaf("state", "in", ["warning", "critical"]), "services")
    assert "q" in p
    q = p["q"]
    assert "state = 1" in q
    assert "state = 2" in q
    assert " or " in q
    # No raw list / string leak under a bracket key.
    assert "state" not in {k for k in p if k != "q"}


def test_compile_state_in_hosts_combined_and():
    """AND(state in [down, unreachable], hostgroup=HG_AGILE) must go through
    hybrid mode: state OR-rewritten in q=, hostgroup as bracket param."""
    p = compile_filter(
        group(
            "and",
            leaf("state", "in", ["down", "unreachable"]),
            leaf("hostgroup", "eq", "HG_AGILE"),
        ),
        "hosts",
    )
    assert p["groups[gte]"] == "HG_AGILE"
    assert "q" in p
    q = p["q"]
    assert "state = 1" in q
    assert "state = 2" in q


def test_compile_problems_state_neq():
    """``compile_filter_problems`` had the same bracket-path bug for state."""
    host_p, svc_p = compile_filter_problems(leaf("state", "neq", "ok"))
    assert host_p == {"state[!]": 0}
    assert svc_p == {"state[!]": 0}


def test_compile_problems_state_in_rejected():
    """``thruk_problems`` is AND-only by design — ``state in`` cannot be
    expressed and must surface a clear FilterError instead of HTTP 400'ing."""
    with pytest.raises(FilterError, match="op='in' on field='state'"):
        compile_filter_problems(leaf("state", "in", ["warning", "critical"]))


def test_compile_hostgroup_hosts():
    p = compile_filter(leaf("hostgroup", "eq", "HG_AGILE"), "hosts")
    assert p["groups[gte]"] == "HG_AGILE"


def test_compile_hostgroup_services():
    p = compile_filter(leaf("hostgroup", "eq", "HG_AGILE"), "services")
    assert p["host_groups[gte]"] == "HG_AGILE"


def test_compile_servicegroup():
    p = compile_filter(leaf("servicegroup", "eq", "db"), "services")
    assert p["groups[gte]"] == "db"


# ---------------------------------------------------------------------------
# Issue #240 — hostgroup/servicegroup leaf must honour `op`
# ---------------------------------------------------------------------------


def test_compile_hostgroup_neq_hosts():
    """Before #240: ``hostgroup neq X`` was silently compiled as membership.
    After: emits the [!] non-membership bracket op."""
    p = compile_filter(leaf("hostgroup", "neq", "rol-edf"), "hosts")
    assert p == {"groups[!]": "rol-edf"}


def test_compile_hostgroup_neq_services():
    p = compile_filter(leaf("hostgroup", "neq", "rol-edf"), "services")
    assert p == {"host_groups[!]": "rol-edf"}


def test_compile_servicegroup_neq():
    p = compile_filter(leaf("servicegroup", "neq", "db"), "services")
    assert p == {"groups[!]": "db"}


def test_compile_hostgroup_in_routes_through_q():
    """``hostgroup in [A, B]`` must compile to a membership-OR via q=, not
    a bogus single ``groups[gte]`` param. Issue #240."""
    p = compile_filter(leaf("hostgroup", "in", ["A", "B"]), "hosts")
    # Bracket-op param must NOT contain the list verbatim.
    assert "groups[gte]" not in p
    # Must compile to a q= expression with both membership clauses OR-joined.
    assert "q" in p
    assert '(groups >= "A")' in p["q"]
    assert '(groups >= "B")' in p["q"]
    assert " or " in p["q"]


def test_compile_hostgroup_in_services_uses_host_groups():
    p = compile_filter(leaf("hostgroup", "in", ["A", "B"]), "services")
    assert "q" in p
    assert '(host_groups >= "A")' in p["q"]
    assert '(host_groups >= "B")' in p["q"]


def test_validate_hostgroup_regex_rejected():
    """Per-field op constraint: regex on a list-valued group column is not
    expressible without surprising semantics. Reject at validation time."""
    with pytest.raises(FilterError, match="op='regex' is not supported on field='hostgroup'"):
        validate_filter(leaf("hostgroup", "regex", "rol-.*"), FIELDS_HOSTS)


def test_validate_hostgroup_lte_rejected():
    with pytest.raises(FilterError, match="op='lte' is not supported on field='hostgroup'"):
        validate_filter(leaf("hostgroup", "lte", "z"), FIELDS_HOSTS)


def test_validate_servicegroup_regex_rejected():
    with pytest.raises(FilterError, match="op='regex' is not supported on field='servicegroup'"):
        validate_filter(leaf("servicegroup", "regex", "db.*"), FIELDS_SERVICES)


def test_validate_hostgroup_neq_accepted():
    """neq is now an explicitly supported op on group fields."""
    validate_filter(leaf("hostgroup", "neq", "rol-edf"), FIELDS_HOSTS)
    validate_filter(leaf("hostgroup", "in", ["A", "B"]), FIELDS_HOSTS)


def test_problems_hostgroup_neq():
    host_p, svc_p = compile_filter_problems(leaf("hostgroup", "neq", "rol-edf"))
    assert host_p["groups[!]"] == "rol-edf"
    assert svc_p["host_groups[!]"] == "rol-edf"


def test_problems_hostgroup_in_still_uses_legacy_gte_passthrough():
    """``thruk_problems`` keeps the legacy ``[gte]`` bracket-op for ``in`` —
    Thruk OR-joins repeated values on the list-valued ``groups`` column, and
    the tool's client-side re-validation (issue #200) enforces strict ``in``
    semantics on the merged rows. The fix for #240 must NOT regress this."""
    host_p, svc_p = compile_filter_problems(leaf("hostgroup", "in", ["A", "B"]))
    assert host_p["groups[gte]"] == ["A", "B"]
    assert svc_p["host_groups[gte]"] == ["A", "B"]


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
    # Issue #254: a host_custom_var leaf must constrain BOTH sub-queries —
    # ``_{VAR}`` on /hosts and ``_HOST{VAR}`` on /services. Previously the
    # /hosts param was missing, leaking every host problem.
    host_p, svc_p = compile_filter_problems(
        leaf("host_custom_var", "eq", {"var": "KERNEL", "val": "windows"})
    )
    assert host_p["_KERNEL"] == "windows"
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
# rewrite_custom_var_to_host_custom_var  (issue #244)
# ---------------------------------------------------------------------------


def test_rewrite_cv_to_hcv_single_leaf():
    """custom_var leaf → host_custom_var leaf (value preserved)."""
    src = leaf("custom_var", "eq", {"var": "KERNEL", "val": "windows"})
    out = rewrite_custom_var_to_host_custom_var(src)
    assert out == {
        "type": "leaf",
        "field": "host_custom_var",
        "op": "eq",
        "value": {"var": "KERNEL", "val": "windows"},
    }
    # And it must compile to _HOST{VAR} under both services & hosts contexts.
    assert compile_filter(out, "services") == {"_HOSTKERNEL": "windows"}
    assert compile_filter(out, "hosts") == {"_HOSTKERNEL": "windows"}


def test_rewrite_cv_to_hcv_does_not_mutate_input():
    """Original tree is untouched — caller can compile both sides safely."""
    src = leaf("custom_var", "eq", {"var": "ENV", "val": "prod"})
    out = rewrite_custom_var_to_host_custom_var(src)
    assert src["field"] == "custom_var"  # input unchanged
    assert out["field"] == "host_custom_var"
    # Mutating the rewritten value dict must not bleed into the original.
    out["value"]["val"] = "tampered"
    assert src["value"]["val"] == "prod"


def test_rewrite_cv_to_hcv_preserves_non_cv_leaves():
    """Non custom_var leaves are deep-copied verbatim."""
    src = leaf("hostgroup", "eq", "HG_WIN")
    out = rewrite_custom_var_to_host_custom_var(src)
    assert out == src
    assert out is not src  # still a fresh copy


def test_rewrite_cv_to_hcv_handles_nested_and_or():
    """Recurses into AND/OR groups; only custom_var leaves are rewritten."""
    src = group(
        "and",
        leaf("hostgroup", "eq", "HG_WIN"),
        group(
            "or",
            leaf("custom_var", "eq", {"var": "ENV", "val": "prod"}),
            leaf("custom_var", "eq", {"var": "TIER", "val": "1"}),
        ),
    )
    out = rewrite_custom_var_to_host_custom_var(src)
    assert out["conditions"][0]["field"] == "hostgroup"
    inner = out["conditions"][1]
    assert inner["operator"] == "or"
    assert [c["field"] for c in inner["conditions"]] == [
        "host_custom_var",
        "host_custom_var",
    ]


def test_rewrite_cv_to_hcv_host_custom_var_passthrough():
    """An existing host_custom_var leaf is preserved (idempotent)."""
    src = leaf("host_custom_var", "eq", {"var": "ENV", "val": "prod"})
    out = rewrite_custom_var_to_host_custom_var(src)
    assert out["field"] == "host_custom_var"
    assert compile_filter(out, "services") == {"_HOSTENV": "prod"}


def test_rewrite_cv_then_compile_services_emits_hostvar():
    """End-to-end reproducer of issue #244.

    Before the fix:
        compile_filter(custom_var=KERNEL=windows, "services")
            → {"_KERNEL": "windows"}        # silently matches nothing

    After the fix (applied at call sites in server.py):
        compile_filter(rewrite_cv_to_hcv(...), "services")
            → {"_HOSTKERNEL": "windows"}    # matches host-level CV correctly
    """
    src = leaf("custom_var", "eq", {"var": "KERNEL", "val": "windows"})
    # Demonstrate the bug: the raw services compile still emits _{VAR} —
    # this is intentional (thruk_list_services depends on it).
    assert compile_filter(src, "services") == {"_KERNEL": "windows"}
    # The host-level-cv tools must apply the rewrite first.
    rewritten = rewrite_custom_var_to_host_custom_var(src)
    assert compile_filter(rewritten, "services") == {"_HOSTKERNEL": "windows"}


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


# ---------------------------------------------------------------------------
# Issue #263 — build_tool_schema auto-derives the `filter` property from the
# same FIELDS_* set, removing the build_tool_schema(F, filter=filter_schema_
# property(F)) duplication. These lock in equivalence with the old explicit
# form and the auto-inject ordering contract.
# ---------------------------------------------------------------------------


def test_build_tool_schema_auto_injects_filter_property():
    """Omitting `filter` must yield the same property as passing it explicitly."""
    s = build_tool_schema(FIELDS_HOSTS, limit={"type": "integer"})
    assert "filter" in s["properties"]
    assert s["properties"]["filter"] == filter_schema_property(FIELDS_HOSTS)


def test_build_tool_schema_auto_inject_equals_explicit_form():
    """The new call form is schema-equivalent to the pre-#263 explicit form."""
    auto = build_tool_schema(FIELDS_SERVICES, limit={"type": "integer"})
    explicit = build_tool_schema(
        FIELDS_SERVICES,
        filter=filter_schema_property(FIELDS_SERVICES),
        limit={"type": "integer"},
    )
    assert auto == explicit


def test_build_tool_schema_auto_injected_filter_is_first():
    """Auto-injected `filter` keeps its conventional first position."""
    s = build_tool_schema(FIELDS_LOGS, limit={"type": "integer"}, backends={"type": "string"})
    assert list(s["properties"]) == ["filter", "limit", "backends"]


def test_build_tool_schema_explicit_filter_override_wins():
    """An explicit `filter=` override is honoured and not double-injected."""
    custom = {"type": "string", "description": "custom override"}
    s = build_tool_schema(FIELDS_HOSTS, filter=custom)
    assert s["properties"]["filter"] == custom


@pytest.mark.parametrize(
    "fields",
    [
        FIELDS_ALERTS,
        FIELDS_COMMENTS,
        FIELDS_DOWNTIMES,
        FIELDS_HOSTS,
        FIELDS_HOST_STATS,
        FIELDS_LOGS,
        FIELDS_NOISY_HOSTS,
        FIELDS_NOISY_SERVICES,
        FIELDS_NOTIFICATIONS,
        FIELDS_OLDEST_PROBLEMS,
        FIELDS_PROBLEMS,
        FIELDS_PROBLEM_COUNTS,
        FIELDS_SERVICES,
        FIELDS_STALE_ACKS,
        FIELDS_TOTALS,
        FIELDS_UNACKED,
    ],
)
def test_build_tool_schema_equivalence_all_field_sets(fields):
    """For every FIELDS_* the auto-derived schema equals the old explicit form."""
    assert build_tool_schema(fields, backends={"type": "string"}) == build_tool_schema(
        fields, filter=filter_schema_property(fields), backends={"type": "string"}
    )


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
