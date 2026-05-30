"""Tool sub-package (issue #147 — server.py split).

First step: ``escape`` houses the generic Thruk REST escape-hatch tools
(``thruk_query`` / ``thruk_run_background_query``) and their shared path /
method validators.  Further tool groups (listing, problems, history,
trends, downtime, write) will land in follow-up PRs.

The ``TOOL_REGISTRY`` aggregation still lives in :mod:`thruk_mcp.server` for
now; submodules expose their tool functions for direct import.
"""

from __future__ import annotations

from .base import (
    _BACKENDS,
    _LOG_CUSTOM_VARS,
    _LOG_HOSTGROUP,
    _OPT_BOOL,
    _OPT_INT,
    _OPT_OBJ,
    _OPT_STR,
    ToolSpec,
    _bool,
    _int,
    _s,
    _str,
)
from .escape import (
    _ALLOWED_METHODS,
    _REST_PATH_PREFIXES,
    _validate_rest_path,
    thruk_query,
    thruk_run_background_query,
)
from .triage import (
    TRIAGE_REGISTRY,
    _project_problem_counts,
    thruk_concurrent_failures,
    thruk_oldest_problems,
    thruk_problem_counts,
    thruk_stale_acks,
    thruk_unacked_critical,
)

__all__ = [
    "TRIAGE_REGISTRY",
    "_ALLOWED_METHODS",
    "_BACKENDS",
    "_LOG_CUSTOM_VARS",
    "_LOG_HOSTGROUP",
    "_OPT_BOOL",
    "_OPT_INT",
    "_OPT_OBJ",
    "_OPT_STR",
    "_REST_PATH_PREFIXES",
    "ToolSpec",
    "_bool",
    "_int",
    "_project_problem_counts",
    "_s",
    "_str",
    "_validate_rest_path",
    "thruk_concurrent_failures",
    "thruk_oldest_problems",
    "thruk_problem_counts",
    "thruk_query",
    "thruk_run_background_query",
    "thruk_stale_acks",
    "thruk_unacked_critical",
]
