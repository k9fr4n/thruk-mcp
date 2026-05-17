from __future__ import annotations

from thruk_mcp.server import _list_params

DEFAULT = "name,state"


def test_default_columns_applied() -> None:
    p = _list_params(limit=10, offset=0, sort="name", columns=None, default_columns=DEFAULT)
    assert p["limit"] == 10
    assert "offset" not in p
    assert p["sort"] == "name"
    assert p["columns"] == DEFAULT


def test_columns_explicit_override() -> None:
    p = _list_params(50, 0, None, "name,host_name", DEFAULT)
    assert p["columns"] == "name,host_name"
    assert "sort" not in p


def test_empty_columns_means_all() -> None:
    """columns='' is the explicit opt-out: no `columns` param sent, Thruk returns all."""
    p = _list_params(50, 0, None, "", DEFAULT)
    assert "columns" not in p


def test_limit_clamped() -> None:
    assert _list_params(99999, 0, None, None, None)["limit"] == 1000
    assert _list_params(0, 0, None, None, None)["limit"] == 1
    assert _list_params(-5, 0, None, None, None)["limit"] == 1


def test_offset_propagated_only_when_positive() -> None:
    assert "offset" not in _list_params(10, 0, None, None, None)
    assert _list_params(10, 50, None, None, None)["offset"] == 50
