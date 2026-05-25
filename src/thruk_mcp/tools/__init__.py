"""Tool sub-package (issue #147 — server.py split).

First step: ``escape`` houses the generic Thruk REST escape-hatch tools
(``thruk_query`` / ``thruk_run_background_query``) and their shared path /
method validators.  Further tool groups (listing, problems, history,
trends, downtime, write) will land in follow-up PRs.

The ``TOOL_REGISTRY`` aggregation still lives in :mod:`thruk_mcp.server` for
now; submodules expose their tool functions for direct import.
"""

from __future__ import annotations

from .escape import (
    _ALLOWED_METHODS,
    _REST_PATH_PREFIXES,
    _validate_rest_path,
    thruk_query,
    thruk_run_background_query,
)

__all__ = [
    "_ALLOWED_METHODS",
    "_REST_PATH_PREFIXES",
    "_validate_rest_path",
    "thruk_query",
    "thruk_run_background_query",
]
