"""Tests for the extracted tool building blocks (issue #257, parent #256).

The schema helpers (``_s``, ``_str``, ``_int``, ``_bool``), the reusable
schema fragments (``_OPT_*``, ``_LOG_*``, ``_BACKENDS``) and the ``ToolSpec``
dataclass were moved verbatim from ``server.py`` to ``thruk_mcp.tools.base``.

This refactor must be **behaviour-preserving**:

* the new module exposes every symbol, and
* ``thruk_mcp.server`` re-exports the *same objects* so that existing imports
  (``from thruk_mcp.server import ToolSpec, _s, ...``) keep working.

Before the extraction these symbols lived only in ``server.py``; importing
them from ``thruk_mcp.tools.base`` would have raised ``ImportError``.
"""

from __future__ import annotations

import dataclasses

from thruk_mcp import server
from thruk_mcp.tools import base

_REEXPORTED = (
    "_s",
    "_str",
    "_int",
    "_bool",
    "_OPT_STR",
    "_OPT_INT",
    "_OPT_BOOL",
    "_OPT_OBJ",
    "_LOG_HOSTGROUP",
    "_LOG_CUSTOM_VARS",
    "_BACKENDS",
    "_COLUMNS",
    "_SINCE",
    "_UNTIL",
    "_sort",
    "ToolSpec",
)


class TestBaseModuleSurface:
    def test_all_symbols_present(self) -> None:
        missing = [name for name in _REEXPORTED if not hasattr(base, name)]
        assert not missing, f"tools.base is missing: {missing}"

    def test_dunder_all_matches_symbols(self) -> None:
        assert set(base.__all__) == set(_REEXPORTED)


class TestServerReexportIdentity:
    """server.py must re-export the *identical* objects from tools.base."""

    def test_server_reexports_same_objects(self) -> None:
        for name in _REEXPORTED:
            assert hasattr(server, name), f"server.py no longer re-exports {name}"
            assert getattr(server, name) is getattr(base, name), (
                f"server.{name} is not the same object as tools.base.{name}"
            )

    def test_toolspec_is_shared_class(self) -> None:
        assert server.ToolSpec is base.ToolSpec


class TestSchemaHelperBehaviour:
    def test_s_builds_object_schema_with_required(self) -> None:
        schema = base._s("host", host=base._str("Host name"), limit=base._int(default=5))
        assert schema["type"] == "object"
        assert schema["required"] == ["host"]
        assert schema["properties"]["host"] == {"type": "string", "description": "Host name"}
        assert schema["properties"]["limit"] == {"type": "integer", "default": 5}

    def test_s_omits_required_when_empty(self) -> None:
        assert "required" not in base._s(foo=base._str())

    def test_s_passes_raw_string_type_through(self) -> None:
        assert base._s(flag="boolean")["properties"]["flag"] == {"type": "boolean"}

    def test_str_with_and_without_desc(self) -> None:
        assert base._str() == {"type": "string"}
        assert base._str("d") == {"type": "string", "description": "d"}

    def test_int_default_zero_is_emitted(self) -> None:
        # default=0 must still appear (the helper guards on ``is not None``).
        assert base._int(default=0) == {"type": "integer", "default": 0}

    def test_bool_default_false_is_emitted(self) -> None:
        assert base._bool(default=False) == {"type": "boolean", "default": False}

    def test_opt_fragments_shape(self) -> None:
        assert base._OPT_STR == {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
        assert base._OPT_INT["anyOf"][0] == {"type": "integer"}
        assert base._OPT_BOOL["anyOf"][0] == {"type": "boolean"}
        assert base._OPT_OBJ["anyOf"][0] == {"type": "object"}

    def test_backends_fragment_is_nullable_string(self) -> None:
        assert base._BACKENDS["default"] is None
        assert {"type": "string"} in base._BACKENDS["anyOf"]
        assert "backend" in base._BACKENDS["description"].lower()


class TestToolSpec:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(base.ToolSpec)
        params = base.ToolSpec.__dataclass_params__
        assert params.frozen is True

    def test_default_is_write_false(self) -> None:
        async def _noop() -> str:
            return "ok"

        spec = base.ToolSpec(name="t", fn=_noop, schema=base._s())
        assert spec.is_write is False

    def test_frozen_instance_rejects_mutation(self) -> None:
        async def _noop() -> str:
            return "ok"

        spec = base.ToolSpec(name="t", fn=_noop, schema=base._s())
        try:
            spec.name = "other"  # type: ignore[misc]  # why: assert frozen raises
        except dataclasses.FrozenInstanceError:
            pass
        else:  # pragma: no cover - guards against a non-frozen regression
            raise AssertionError("ToolSpec must be frozen")
