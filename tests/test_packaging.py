"""Regression tests for issue #78: dependency bounds in pyproject.toml.

These tests ensure that upper-bound constraints on high-risk dependencies
(`mcp` and `httpx`) are never silently removed. Both libraries have a
history of minor-version breaking changes:
- httpx 0.28 introduced transport API changes that broke third-party code.
- The MCP SDK has introduced breaking changes at every minor release.

Bug before fix (issue #78):
    # pyproject.toml had only lower bounds:
    # "mcp[cli]>=1.2.0"   <-- no upper bound, could pick up mcp 2.x
    # "httpx>=0.27"       <-- no upper bound, could pick up httpx 1.x
    # A fresh 'pip install --upgrade' would silently pull in incompatible versions.

Fix: add explicit upper bounds:
    "mcp[cli]>=1.2.0,<2.0"
    "httpx>=0.27,<1.0"
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    tomllib = None  # type: ignore[assignment]  # why: tomllib is stdlib only on 3.11+

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _get_dependencies(text: str) -> list[str]:
    """Return the list of dependency specifier strings from pyproject.toml.

    Uses ``tomllib`` (stdlib ≥ 3.11) when available for exact parsing;
    falls back to a regex-based parser on Python 3.10 that correctly handles
    bracket characters inside quoted values such as ``mcp[cli]>=1.2.0,<2.0``.

    Lines in the TOML array look like:
        "mcp[cli]>=1.2.0,<2.0",  # inline comment
        "httpx>=0.27,<1.0",
    """
    if tomllib is not None:
        data = tomllib.loads(text)
        return list(data.get("project", {}).get("dependencies", []))

    # --- Python 3.10 fallback: regex-based parser ---
    # Use a non-greedy dot-all match that stops at the first '\n]'
    # (the closing bracket of a TOML array is always on its own line).
    # This avoids being fooled by ']' inside quoted values like 'mcp[cli]'.
    m = re.search(r"dependencies\s*=\s*\[(.*?)\n\]", text, re.DOTALL)
    assert m, "Could not find dependencies array in pyproject.toml"
    raw = m.group(1)
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # 1. Strip inline TOML comment (everything from the first bare `#`)
        line = re.sub(r"\s*#.*$", "", line).strip()
        # 2. Strip trailing comma
        line = line.rstrip(",").strip()
        # 3. Unwrap surrounding quotes
        if (line.startswith('"') and line.endswith('"')) or (
            line.startswith("'") and line.endswith("'")
        ):
            entries.append(line[1:-1])
    return entries


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDependencyBounds:
    """Ensure pyproject.toml has the correct version bounds (issue #78)."""

    def setup_method(self) -> None:
        self.content = PYPROJECT.read_text(encoding="utf-8")
        self.deps = _get_dependencies(self.content)

    def _find_dep(self, prefix: str) -> str | None:
        """Return the full specifier string for a dependency whose name starts with `prefix`."""
        for dep in self.deps:
            if dep.lower().startswith(prefix.lower()):
                return dep
        return None

    # --- mcp ----------------------------------------------------------------

    def test_mcp_has_upper_bound(self) -> None:
        """mcp must declare an upper bound to prevent pulling in mcp 2.x."""
        dep = self._find_dep("mcp")
        assert dep is not None, "mcp dependency not found in pyproject.toml"
        assert "<2.0" in dep or "<2" in dep, (
            f"mcp dependency '{dep}' is missing upper bound '<2.0'. "
            "See issue #78: MCP SDK breaks at every minor release."
        )

    def test_mcp_lower_bound_preserved(self) -> None:
        """mcp lower bound must remain >= 1.2.0."""
        dep = self._find_dep("mcp")
        assert dep is not None
        assert ">=1.2.0" in dep or ">=1.2" in dep, (
            f"mcp dependency '{dep}' lost its lower bound '>=1.2.0'."
        )

    # --- httpx --------------------------------------------------------------

    def test_httpx_has_upper_bound(self) -> None:
        """httpx must declare an upper bound to prevent pulling in httpx 1.x."""
        dep = self._find_dep("httpx")
        assert dep is not None, "httpx dependency not found in pyproject.toml"
        assert "<1.0" in dep or "<1" in dep, (
            f"httpx dependency '{dep}' is missing upper bound '<1.0'. "
            "See issue #78: httpx 0.28 introduced transport API breaking changes."
        )

    def test_httpx_lower_bound_preserved(self) -> None:
        """httpx lower bound must remain >= 0.27."""
        dep = self._find_dep("httpx")
        assert dep is not None
        assert ">=0.27" in dep, f"httpx dependency '{dep}' lost its lower bound '>=0.27'."

    # --- pydantic / uvicorn (must stay open-ended per issue rationale) -------

    def test_pydantic_still_open_ended(self) -> None:
        """pydantic was explicitly left without an upper bound in issue #78."""
        dep = self._find_dep("pydantic")
        assert dep is not None, "pydantic dependency not found in pyproject.toml"
        # Must NOT have an upper-bound specifier
        assert "<3" not in dep and "<2" not in dep, (
            f"pydantic dependency '{dep}' unexpectedly gained an upper bound."
        )

    def test_uvicorn_still_open_ended(self) -> None:
        """uvicorn was explicitly left without an upper bound in issue #78."""
        dep = self._find_dep("uvicorn")
        assert dep is not None, "uvicorn dependency not found in pyproject.toml"
        assert "<" not in dep, f"uvicorn dependency '{dep}' unexpectedly gained an upper bound."


class TestFutureAnnotations:
    """Ensure every source file has `from __future__ import annotations` (issue #79).

    Without this import, union syntax like ``X | None`` and ``dict[str, Any]``
    used throughout the package are runtime expressions evaluated at import
    time on Python 3.10.  With it they become lazy strings — consistent with
    the rest of the codebase and slightly faster at module load.

    Bug before fix (issue #79):
        # server.py was the only file missing the import:
        # import asyncio
        # import fnmatch
        # ...
        # Union types such as `str | None` were runtime-evaluated.

    Fix: add `from __future__ import annotations` after the module docstring
    in server.py so every source file is consistent.
    """

    SRC_DIR = Path(__file__).parent.parent / "src" / "thruk_mcp"

    def _source_files(self) -> list[Path]:
        return sorted(self.SRC_DIR.glob("*.py"))

    def test_all_source_files_have_future_annotations(self) -> None:
        """Every .py file under src/thruk_mcp must carry `from __future__ import annotations`."""
        missing: list[str] = []
        for path in self._source_files():
            source = path.read_text(encoding="utf-8")
            if "from __future__ import annotations" not in source:
                missing.append(path.name)
        assert not missing, (
            f"Files missing `from __future__ import annotations` (issue #79): {missing}"
        )

    def test_server_py_has_future_annotations(self) -> None:
        """Regression: server.py specifically must have the future-annotations import."""
        server_py = self.SRC_DIR / "server.py"
        assert server_py.exists(), "src/thruk_mcp/server.py not found"
        source = server_py.read_text(encoding="utf-8")
        assert "from __future__ import annotations" in source, (
            "server.py is missing `from __future__ import annotations` (issue #79)"
        )

    def test_server_py_future_import_before_stdlib(self) -> None:
        """The future import must appear before any stdlib import in server.py."""
        server_py = self.SRC_DIR / "server.py"
        source = server_py.read_text(encoding="utf-8")
        future_pos = source.find("from __future__ import annotations")
        asyncio_pos = source.find("import asyncio")
        assert future_pos != -1, "server.py missing future annotations import"
        assert asyncio_pos != -1, "server.py missing `import asyncio`"
        assert future_pos < asyncio_pos, (
            "`from __future__ import annotations` must appear before `import asyncio` in server.py"
        )


class TestDunderAll:
    """Verify that every public module declares __all__ with the expected symbols (issue #88).

    Without __all__, `from thruk_mcp.<module> import *` leaks private helpers,
    and static analysers cannot distinguish intended public API from internals.

    Bug before fix (issue #88):
        # None of the public source modules declared __all__, e.g.:
        # from thruk_mcp.client import *   # leaked _url, _backoff, CACHEABLE_PATHS, ...
        # from thruk_mcp.filters import *  # leaked LEAF_OPS, _has_or, _leaf_to_params, ...

    Fix: add __all__ to each public module listing only the symbols intended
    for external use, as specified in the issue body.
    """

    # Expected public symbols per module (subset: must all be present).
    # Only the symbols explicitly listed in the issue are checked; modules
    # may legitimately add more in future without breaking this test.
    from typing import ClassVar

    EXPECTED: ClassVar[dict[str, list[str]]] = {
        "client": ["ThrukClient", "ThrukError"],
        "config": ["ThrukConfig"],
        "audit": ["configure", "log_call", "audited"],
        "cache": ["TTLCache"],
        "filters": [
            "FilterError",
            "validate_filter",
            "compile_filter",
            "compile_filter_problems",
            "extract_log_lookup_fields",
            "build_tool_schema",
            "filter_schema_property",
            "FIELDS_HOSTS",
            "FIELDS_SERVICES",
            "FIELDS_LOGS",
            "FIELDS_ALERTS",
            "FIELDS_NOTIFICATIONS",
            "FIELDS_PROBLEMS",
            "FIELDS_NOISY_HOSTS",
            "FIELDS_NOISY_SERVICES",
        ],
        "server": ["build_server", "ThrukMCPServer", "WRITE_TOOLS"],
    }

    def _import_module(self, name: str) -> object:
        import importlib

        return importlib.import_module(f"thruk_mcp.{name}")

    def test_all_public_modules_declare_dunder_all(self) -> None:
        """Every public module must have an __all__ attribute."""
        missing: list[str] = []
        for mod_name in self.EXPECTED:
            mod = self._import_module(mod_name)
            if not hasattr(mod, "__all__"):
                missing.append(mod_name)
        assert not missing, (
            f"Modules missing __all__ (issue #88): {missing}. "
            "Add `__all__ = [...]` listing the intended public API."
        )

    def test_all_expected_symbols_in_dunder_all(self) -> None:
        """Every expected public symbol must appear in its module's __all__."""
        problems: list[str] = []
        for mod_name, symbols in self.EXPECTED.items():
            mod = self._import_module(mod_name)
            declared: list[str] = getattr(mod, "__all__", [])
            for sym in symbols:
                if sym not in declared:
                    problems.append(f"{mod_name}.{sym}")
        assert not problems, f"Symbols missing from __all__ (issue #88): {problems}"

    def test_all_dunder_all_symbols_are_importable(self) -> None:
        """Every name listed in __all__ must actually exist in the module namespace."""
        problems: list[str] = []
        for mod_name in self.EXPECTED:
            mod = self._import_module(mod_name)
            declared: list[str] = getattr(mod, "__all__", [])
            for sym in declared:
                if not hasattr(mod, sym):
                    problems.append(f"{mod_name}.{sym}")
        assert not problems, (
            f"Names in __all__ that do not exist in the module (issue #88): {problems}"
        )

    def test_private_helpers_not_in_dunder_all(self) -> None:
        """Names starting with '_' must not appear in __all__ for public modules."""
        violations: list[str] = []
        for mod_name in self.EXPECTED:
            mod = self._import_module(mod_name)
            declared: list[str] = getattr(mod, "__all__", [])
            for sym in declared:
                if sym.startswith("_"):
                    violations.append(f"{mod_name}.{sym}")
        assert not violations, (
            f"Private names (underscore-prefixed) found in __all__ (issue #88): {violations}"
        )


class TestParametrisedTypeHints:
    """Ensure bare ``dict`` / ``list[dict]`` annotations are gone from server.py (issue #80).

    Un-parametrised ``dict`` and ``list[dict]`` give mypy no structural
    information.  All occurrences should be ``dict[str, Any]`` (or a more
    specific parametrised form) and ``list[dict[str, Any]]``.

    Bug before fix (issue #80):
        # server.py had >20 bare occurrences, e.g.:
        # filter: dict | None = None
        # rows: list[dict] = []
        # params: dict = { ... }
        # def _s(...) -> dict:

    Fix: replace every occurrence with the properly parametrised equivalent.
    """

    SRC_DIR = Path(__file__).parent.parent / "src" / "thruk_mcp"

    def _server_source(self) -> str:
        return (self.SRC_DIR / "server.py").read_text(encoding="utf-8")

    def test_no_bare_dict_or_none_annotation(self) -> None:
        """``dict | None`` must not appear anywhere in server.py (use ``dict[str, Any] | None``)."""
        import re

        source = self._server_source()
        # Exclude comments and docstrings (lines starting with # or inside triple-quotes)
        code_lines = [
            (i + 1, line)
            for i, line in enumerate(source.splitlines())
            if not line.lstrip().startswith("#")
        ]
        bad = [
            (lineno, ln.strip()) for lineno, ln in code_lines if re.search(r"\bdict \| None\b", ln)
        ]
        assert not bad, (
            "Bare `dict | None` found in server.py (issue #80); use `dict[str, Any] | None`:\n"
            + "\n".join(f"  L{no}: {txt}" for no, txt in bad)
        )

    def test_no_bare_list_dict_annotation(self) -> None:
        """``list[dict]`` must not appear in server.py (use ``list[dict[str, Any]]``)."""
        import re

        source = self._server_source()
        bad = [
            (i + 1, ln.strip())
            for i, ln in enumerate(source.splitlines())
            if re.search(r"list\[dict\](?!\[)", ln) and not ln.lstrip().startswith("#")
        ]
        assert not bad, (
            "Bare `list[dict]` found in server.py (issue #80); use `list[dict[str, Any]]`:\n"
            + "\n".join(f"  L{no}: {txt}" for no, txt in bad)
        )

    def test_no_bare_dict_variable_annotation(self) -> None:
        """Local ``var: dict = {`` must not appear in server.py (use ``dict[str, Any]``)."""
        import re

        source = self._server_source()
        bad = [
            (i + 1, ln.strip())
            for i, ln in enumerate(source.splitlines())
            if re.search(r": dict\s*=\s*\{", ln) and not ln.lstrip().startswith("#")
        ]
        assert not bad, (
            "Bare `: dict = {` annotation found in server.py (issue #80); use `dict[str, Any]`:\n"
            + "\n".join(f"  L{no}: {txt}" for no, txt in bad)
        )

    def test_no_bare_dict_return_annotation(self) -> None:
        """Function return type ``-> dict:`` must not appear in server.py."""
        import re

        source = self._server_source()
        bad = [
            (i + 1, ln.strip())
            for i, ln in enumerate(source.splitlines())
            if re.search(r"-> dict:", ln) and not ln.lstrip().startswith("#")
        ]
        assert not bad, (
            "Bare `-> dict:` return annotation found in server.py (issue #80);"
            " use `-> dict[str, Any]:`:\n" + "\n".join(f"  L{no}: {txt}" for no, txt in bad)
        )

    def test_schema_helpers_return_parametrised_dict(self) -> None:
        """_s, _str, _int, _bool must declare ``-> dict[str, Any]`` return type in source."""
        import re

        source = self._server_source()
        for fn in ("_s", "_str", "_int", "_bool"):
            pattern = rf"def {re.escape(fn)}\(.*\) -> dict\[str, Any\]:"
            assert re.search(pattern, source), (
                f"Helper `{fn}` in server.py does not declare `-> dict[str, Any]:` (issue #80)"
            )


class TestConsolidatedStateMaps:
    """Regression tests for issue #81: state maps must live only in constants.py.

    Bug before fix (issue #81):
        # server.py defined HOST_STATES, SERVICE_STATES, HOST_STATE_MAP, SVC_STATE_MAP
        # filters.py defined _HOST_STATE_MAP, _SVC_STATE_MAP
        # Both sets were duplicates with no shared source of truth.
        # Editing one file but not the other would silently cause inconsistencies.

    Fix: create constants.py as the single source of truth and import from there
    in both server.py and filters.py.
    """

    SRC_DIR = Path(__file__).parent.parent / "src" / "thruk_mcp"

    def _read(self, name: str) -> str:
        return (self.SRC_DIR / name).read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # constants.py exports
    # ------------------------------------------------------------------

    def test_constants_module_exists(self) -> None:
        """src/thruk_mcp/constants.py must exist (issue #81)."""
        assert (self.SRC_DIR / "constants.py").exists(), (
            "constants.py not found in src/thruk_mcp/ (issue #81)"
        )

    def test_constants_exports_all_four_maps(self) -> None:
        """constants.py must export HOST_STATE_STR, HOST_STATE_INT, SVC_STATE_STR, SVC_STATE_INT."""
        from thruk_mcp import constants

        for name in ("HOST_STATE_STR", "HOST_STATE_INT", "SVC_STATE_STR", "SVC_STATE_INT"):
            assert hasattr(constants, name), f"constants.py is missing {name!r} (issue #81)"

    def test_host_state_str_values(self) -> None:
        """HOST_STATE_STR must map 0->UP, 1->DOWN, 2->UNREACHABLE."""
        from thruk_mcp.constants import HOST_STATE_STR

        assert HOST_STATE_STR[0] == "UP"
        assert HOST_STATE_STR[1] == "DOWN"
        assert HOST_STATE_STR[2] == "UNREACHABLE"

    def test_svc_state_str_values(self) -> None:
        """SVC_STATE_STR must map 0->OK, 1->WARNING, 2->CRITICAL, 3->UNKNOWN."""
        from thruk_mcp.constants import SVC_STATE_STR

        assert SVC_STATE_STR[0] == "OK"
        assert SVC_STATE_STR[1] == "WARNING"
        assert SVC_STATE_STR[2] == "CRITICAL"
        assert SVC_STATE_STR[3] == "UNKNOWN"

    def test_host_state_int_roundtrip(self) -> None:
        """HOST_STATE_INT must accept both lowercase names and numeric strings."""
        from thruk_mcp.constants import HOST_STATE_INT

        assert HOST_STATE_INT["up"] == 0
        assert HOST_STATE_INT["down"] == 1
        assert HOST_STATE_INT["unreachable"] == 2
        assert HOST_STATE_INT["0"] == 0
        assert HOST_STATE_INT["2"] == 2

    def test_svc_state_int_roundtrip(self) -> None:
        """SVC_STATE_INT must accept both lowercase names and numeric strings."""
        from thruk_mcp.constants import SVC_STATE_INT

        assert SVC_STATE_INT["ok"] == 0
        assert SVC_STATE_INT["warning"] == 1
        assert SVC_STATE_INT["critical"] == 2
        assert SVC_STATE_INT["unknown"] == 3
        assert SVC_STATE_INT["3"] == 3

    # ------------------------------------------------------------------
    # server.py must NOT define its own copies
    # ------------------------------------------------------------------

    def test_server_does_not_define_host_states_inline(self) -> None:
        """server.py must not have an inline dict literal for HOST_STATES (issue #81)."""
        import re

        source = self._read("server.py")
        # Match only literal dict assignments like: HOST_STATES = {0: "UP", ...}
        # An alias like: HOST_STATES: dict[int, str] = HOST_STATE_STR is fine.
        m = re.search(r"^HOST_STATES\s*=\s*\{", source, re.MULTILINE)
        assert not m, (
            "server.py defines HOST_STATES as an inline dict -- should import from constants.py "
            "(issue #81)"
        )

    def test_server_does_not_define_service_states_inline(self) -> None:
        """server.py must not have an inline dict literal for SERVICE_STATES (issue #81)."""
        import re

        source = self._read("server.py")
        m = re.search(r"^SERVICE_STATES\s*=\s*\{", source, re.MULTILINE)
        assert not m, (
            "server.py defines SERVICE_STATES as an inline dict -- should import from constants.py "
            "(issue #81)"
        )

    # ------------------------------------------------------------------
    # filters.py must NOT define its own copies
    # ------------------------------------------------------------------

    def test_filters_does_not_define_host_state_map_inline(self) -> None:
        """filters.py must not have an inline dict literal for _HOST_STATE_MAP (issue #81)."""
        import re

        source = self._read("filters.py")
        m = re.search(r"^_HOST_STATE_MAP\s*:\s*dict.*=\s*\{", source, re.MULTILINE)
        assert not m, (
            "filters.py defines _HOST_STATE_MAP as an inline dict -- "
            "should alias from constants.py (issue #81)"
        )

    def test_filters_does_not_define_svc_state_map_inline(self) -> None:
        """filters.py must not have an inline dict literal for _SVC_STATE_MAP (issue #81)."""
        import re

        source = self._read("filters.py")
        m = re.search(r"^_SVC_STATE_MAP\s*:\s*dict.*=\s*\{", source, re.MULTILINE)
        assert not m, (
            "filters.py defines _SVC_STATE_MAP as an inline dict -- "
            "should alias from constants.py (issue #81)"
        )

    # ------------------------------------------------------------------
    # Runtime consistency: server aliases must point to the same objects
    # ------------------------------------------------------------------

    def test_server_aliases_are_identity(self) -> None:
        """server.HOST_STATES and server.HOST_STATE_MAP must be the same objects as in constants."""
        from thruk_mcp import server
        from thruk_mcp.constants import HOST_STATE_INT, HOST_STATE_STR, SVC_STATE_INT, SVC_STATE_STR

        assert server.HOST_STATES is HOST_STATE_STR, (
            "server.HOST_STATES must be the same object as constants.HOST_STATE_STR (issue #81)"
        )
        assert server.SERVICE_STATES is SVC_STATE_STR, (
            "server.SERVICE_STATES must be the same object as constants.SVC_STATE_STR (issue #81)"
        )
        assert server.HOST_STATE_MAP is HOST_STATE_INT, (
            "server.HOST_STATE_MAP must be the same object as constants.HOST_STATE_INT (issue #81)"
        )
        assert server.SVC_STATE_MAP is SVC_STATE_INT, (
            "server.SVC_STATE_MAP must be the same object as constants.SVC_STATE_INT (issue #81)"
        )


class TestNoMiscOverride:
    """Regression tests for issue #82: stale mypy override must not silence misc errors.

    Bug before fix (issue #82):
        # pyproject.toml contained:
        # [[tool.mypy.overrides]]
        # module = "thruk_mcp.server"
        # disable_error_code = ["misc"]  # FastMCP decorators confuse type-checker
        #
        # The comment referenced FastMCP decorators, but the project had already
        # migrated to ``mcp.server.Server`` directly.  The override silently hid
        # real type errors across the largest file in the project (2 496 lines).

    Fix (issue #82):
        - Remove the ``[[tool.mypy.overrides]]`` block.
        - Verify mypy's ``warn_unused_ignores = true`` is set to catch future stale suppressions.
        - Add these regression tests to prevent the override from coming back.
    """

    PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"
    SRC_DIR = Path(__file__).parent.parent / "src" / "thruk_mcp"

    def _pyproject_text(self) -> str:
        return self.PYPROJECT.read_text(encoding="utf-8")

    def _server_source(self) -> str:
        return (self.SRC_DIR / "server.py").read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # pyproject.toml must NOT suppress misc for server
    # ------------------------------------------------------------------

    def test_no_misc_override_for_server(self) -> None:
        """pyproject.toml must not contain a [[tool.mypy.overrides]] block that disables
        misc errors for thruk_mcp.server (issue #82).

        The override was added when the project used FastMCP decorators.  After migrating
        to ``mcp.server.Server`` directly it became stale and masked real type errors.
        """
        import re

        text = self._pyproject_text()
        # Look for any overrides block that mentions thruk_mcp.server AND misc together
        # (within a reasonable proximity - 5 lines of each other).
        overrides_blocks = re.findall(
            r"\[\[tool\.mypy\.overrides\]\].*?(?=\[\[|\Z)", text, re.DOTALL
        )
        for block in overrides_blocks:
            if "thruk_mcp.server" in block and "misc" in block:
                raise AssertionError(
                    "pyproject.toml still contains a [[tool.mypy.overrides]] block that disables "
                    "'misc' for thruk_mcp.server (issue #82). Remove it and fix the underlying "
                    "type errors instead."
                )

    def test_warn_unused_ignores_is_enabled(self) -> None:
        """mypy must have ``warn_unused_ignores = true`` to catch stale type suppressions.

        This setting causes mypy to flag ``# type: ignore`` comments that are no longer
        needed, preventing the accumulation of stale suppressions like the one removed
        in issue #82.
        """
        text = self._pyproject_text()
        assert "warn_unused_ignores = true" in text, (
            "pyproject.toml is missing `warn_unused_ignores = true` in [tool.mypy] (issue #82). "
            "Without this, stale `# type: ignore` comments accumulate silently."
        )

    # ------------------------------------------------------------------
    # server.py must not have bare (codeless) type: ignore comments
    # ------------------------------------------------------------------

    def test_no_bare_type_ignore_in_server(self) -> None:
        """server.py must not contain bare ``# type: ignore`` without an error code.

        Narrow per-error-code suppression (e.g. ``# type: ignore[misc]``) is acceptable
        when strictly necessary, but bare ``# type: ignore`` silences ALL mypy errors on
        a line and is too broad.  Existing suppressed lines must include a code and an
        inline explanation comment (``# why: ...``).
        """
        import re

        source = self._server_source()
        # Match `# type: ignore` NOT followed by `[` (i.e. no error-code qualifier).
        # Exclude lines that are themselves pure-comment lines (unlikely, but safe).
        bad_lines = []
        for i, line in enumerate(source.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # skip pure comment lines
            if re.search(r"#\s*type:\s*ignore\s*(?!\[)", line):
                bad_lines.append((i, line.strip()))

        assert not bad_lines, (
            "server.py contains bare `# type: ignore` without an error code (issue #82).\n"
            "Use narrow suppressions like `# type: ignore[misc]  # why: <reason>` instead.\n"
            "Offending lines:\n" + "\n".join(f"  L{no}: {txt}" for no, txt in bad_lines)
        )
