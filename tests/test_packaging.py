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
