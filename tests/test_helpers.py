"""Unit tests for thruk_mcp.helpers.

Covers the pure utility functions extracted from server.py (issue #87 step 1).
No HTTP mocking needed — these are all stateless functions.
"""

from __future__ import annotations

import json

from thruk_mcp.helpers import (
    _backends,
    _build_cv_params,
    _downtime_payload,
    _duration_human,
    _list_params,
    _seg,
    _tool_response,
    _ts,
)

# ---------------------------------------------------------------------------
# _list_params
# ---------------------------------------------------------------------------

_DEFAULT = "name,state"


def test_list_params_default_columns() -> None:
    p = _list_params(limit=10, offset=0, sort="name", columns=None, default_columns=_DEFAULT)
    assert p["limit"] == 10
    assert "offset" not in p
    assert p["sort"] == "name"
    assert p["columns"] == _DEFAULT


def test_list_params_explicit_columns_override() -> None:
    p = _list_params(50, 0, None, "name,host_name", _DEFAULT)
    assert p["columns"] == "name,host_name"
    assert "sort" not in p


def test_list_params_empty_columns_means_all() -> None:
    """columns='' is the explicit opt-out: no `columns` param sent, Thruk returns all."""
    p = _list_params(50, 0, None, "", _DEFAULT)
    assert "columns" not in p


def test_list_params_limit_clamped() -> None:
    assert _list_params(99999, 0, None, None, None)["limit"] == 1000
    assert _list_params(0, 0, None, None, None)["limit"] == 1
    assert _list_params(-5, 0, None, None, None)["limit"] == 1


def test_list_params_offset_propagated_only_when_positive() -> None:
    assert "offset" not in _list_params(10, 0, None, None, None)
    assert _list_params(10, 50, None, None, None)["offset"] == 50


def test_list_params_custom_max_limit() -> None:
    assert _list_params(500, 0, None, None, None, max_limit=200)["limit"] == 200


# ---------------------------------------------------------------------------
# _ts
# ---------------------------------------------------------------------------


def test_ts_falsy_returns_na() -> None:
    assert _ts(None) == "N/A"
    assert _ts(0) == "N/A"
    assert _ts("") == "N/A"


def test_ts_unix_timestamp_format() -> None:
    # 2024-01-01 UTC; exact local-time string depends on TZ but format is fixed
    result = _ts(1704067200)
    assert len(result) == 19  # "YYYY-MM-DD HH:MM:SS"
    assert result[4] == "-" and result[7] == "-"
    assert result[10] == " " and result[13] == ":" and result[16] == ":"


def test_ts_invalid_string_returned_as_is() -> None:
    assert _ts("not-a-timestamp") == "not-a-timestamp"


def test_ts_string_integer_converted() -> None:
    result = _ts("1704067200")
    assert len(result) == 19


# ---------------------------------------------------------------------------
# _duration_human
# ---------------------------------------------------------------------------


def test_duration_human_minutes_only() -> None:
    assert _duration_human(300) == "5m"


def test_duration_human_hours_and_minutes() -> None:
    assert _duration_human(3600 + 900) == "1h 15m"


def test_duration_human_days() -> None:
    assert _duration_human(3 * 86400 + 2 * 3600 + 15 * 60) == "3d 2h 15m"


def test_duration_human_zero() -> None:
    assert _duration_human(0) == "0m"


def test_duration_human_negative() -> None:
    assert _duration_human(-100) == "0m"


def test_duration_human_exactly_one_hour() -> None:
    # 3600s = 1h exactly; no minutes to show, so just "1h"
    assert _duration_human(3600) == "1h"


def test_duration_human_exactly_one_day() -> None:
    assert _duration_human(86400) == "1d"


# ---------------------------------------------------------------------------
# _backends
# ---------------------------------------------------------------------------


def test_backends_none_returns_none() -> None:
    assert _backends(None) is None


def test_backends_single() -> None:
    assert _backends("site1") == ("site1",)


def test_backends_multiple() -> None:
    assert _backends("site1,site2,site3") == ("site1", "site2", "site3")


def test_backends_strips_whitespace() -> None:
    assert _backends("site1, site2 , site3") == ("site1", "site2", "site3")


def test_backends_empty_string_returns_none() -> None:
    assert _backends("") is None


def test_backends_only_commas_returns_none() -> None:
    assert _backends(",,") is None


def test_backends_trims_surrounding_whitespace() -> None:
    assert _backends("  a  ") == ("a",)


# ---------------------------------------------------------------------------
# _seg
# ---------------------------------------------------------------------------


def test_seg_plain_hostname() -> None:
    assert _seg("hostname") == "hostname"


def test_seg_slash_is_encoded() -> None:
    encoded = _seg("host/name")
    assert "/" not in encoded
    assert encoded == "host%2Fname"


def test_seg_dotdot_encoded() -> None:
    encoded = _seg("../etc/passwd")
    assert "/" not in encoded
    assert ".." in encoded  # dots stay, slashes are encoded


def test_seg_space_encoded() -> None:
    assert " " not in _seg("my host")


def test_seg_colon_encoded() -> None:
    assert _seg("host:port") == "host%3Aport"


def test_seg_unicode_encoded() -> None:
    result = _seg("hôte")
    assert "%" in result  # non-ASCII chars are percent-encoded


# ---------------------------------------------------------------------------
# _build_cv_params
# ---------------------------------------------------------------------------


def test_build_cv_params_none_returns_empty() -> None:
    assert _build_cv_params(None) == {}


def test_build_cv_params_empty_dict_returns_empty() -> None:
    assert _build_cv_params({}) == {}


def test_build_cv_params_simple() -> None:
    assert _build_cv_params({"KERNEL": "linux"}) == {"_KERNEL": "linux"}


def test_build_cv_params_lowercased_key_uppercased() -> None:
    assert _build_cv_params({"kernel": "linux"}) == {"_KERNEL": "linux"}


def test_build_cv_params_host_prefix() -> None:
    assert _build_cv_params({"DC": "eu"}, host_prefix=True) == {"_HOSTDC": "eu"}


def test_build_cv_params_numeric_value_coerced() -> None:
    assert _build_cv_params({"COUNT": 42}) == {"_COUNT": "42"}


def test_build_cv_params_multiple_vars() -> None:
    result = _build_cv_params({"A": "1", "b": "2"})
    assert result == {"_A": "1", "_B": "2"}


# ---------------------------------------------------------------------------
# _downtime_payload
# ---------------------------------------------------------------------------


def test_downtime_payload_basic() -> None:
    p = _downtime_payload("planned maintenance", "ops", "now", "+2h", None, True, 0)
    assert p["start_time"] == "now"
    assert p["end_time"] == "+2h"
    assert p["comment_data"] == "planned maintenance"
    assert p["comment_author"] == "ops"
    assert p["fixed"] == "1"
    assert p["triggered_by"] == "0"


def test_downtime_payload_duration_overrides_end_time() -> None:
    p = _downtime_payload("maint", "ops", "now", "+2h", 30, True, 0)
    assert p["end_time"] == "+30m"


def test_downtime_payload_flexible_false() -> None:
    p = _downtime_payload("maint", "ops", "now", "+2h", None, False, 0)
    assert p["fixed"] == "0"


def test_downtime_payload_triggered_by() -> None:
    p = _downtime_payload("child", "ops", "now", "+1h", None, True, 99)
    assert p["triggered_by"] == "99"


def test_downtime_payload_returns_string_values() -> None:
    p = _downtime_payload("maint", "ops", "now", "+2h", None, True, 0)
    for v in p.values():
        assert isinstance(v, str), f"Expected str, got {type(v)} for value {v!r}"


# ---------------------------------------------------------------------------
# _tool_response (issue #146)
# ---------------------------------------------------------------------------


def test_tool_response_no_warnings_dict_payload_is_byte_identical() -> None:
    """Without warnings, output matches the legacy json.dumps(..., indent=2, default=str).

    Pre-fix repro (would have asserted equal but the helper did not exist):
        return json.dumps({"a": 1, "b": [2, 3]}, indent=2, default=str)
    """
    payload = {"a": 1, "b": [2, 3]}
    assert _tool_response(payload) == json.dumps(payload, indent=2, default=str)


def test_tool_response_no_warnings_list_payload_is_byte_identical() -> None:
    rows = [{"x": 1}, {"x": 2}]
    assert _tool_response(rows) == json.dumps(rows, indent=2, default=str)


def test_tool_response_no_warnings_empty_list_is_byte_identical() -> None:
    """Empty warnings argument must not add a _warnings key or wrap."""
    assert _tool_response({"k": "v"}, warnings=[]) == json.dumps({"k": "v"}, indent=2, default=str)
    assert _tool_response({"k": "v"}, warnings=None) == json.dumps(
        {"k": "v"}, indent=2, default=str
    )


def test_tool_response_warnings_merged_into_dict_payload() -> None:
    out = _tool_response({"a": 1}, warnings=["w1", "w2"])
    parsed = json.loads(out)
    assert parsed == {"a": 1, "_warnings": ["w1", "w2"]}


def test_tool_response_warnings_wrap_non_dict_payload() -> None:
    """List / other payloads with non-empty warnings become {data, _warnings}."""
    out = _tool_response([1, 2, 3], warnings=["oops"])
    parsed = json.loads(out)
    assert parsed == {"data": [1, 2, 3], "_warnings": ["oops"]}


def test_tool_response_default_str_handles_non_json_types() -> None:
    """default=str must serialise types json doesn't know about (sets here)."""
    out = _tool_response({"s": {1, 2}})
    # set repr is non-deterministic; just check it parsed and contained a str
    parsed = json.loads(out)
    assert isinstance(parsed["s"], str)


def test_tool_response_does_not_mutate_input_dict() -> None:
    payload = {"a": 1}
    _tool_response(payload, warnings=["x"])
    assert payload == {"a": 1}  # _warnings must NOT leak back into the caller's dict


def test_tool_response_uses_indent_2() -> None:
    out = _tool_response({"a": 1})
    assert "\n  " in out  # 2-space indent present
