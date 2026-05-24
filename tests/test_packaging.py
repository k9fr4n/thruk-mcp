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
