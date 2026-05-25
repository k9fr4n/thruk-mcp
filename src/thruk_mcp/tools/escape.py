"""Generic Thruk REST escape-hatch tools (issue #147 — server.py split).

This module hosts:

* ``_ALLOWED_METHODS`` / ``_REST_PATH_PREFIXES`` — security constants used by
  the path / method validators.
* ``_validate_rest_path`` — path-traversal & prefix guard.
* ``thruk_query`` — synchronous catch-all for any Thruk REST endpoint.
* ``thruk_run_background_query`` — async ``background=1`` variant for
  long-running queries.

Both tools enforce ``THRUK_READ_ONLY`` defense-in-depth at call time:
the ``thruk_query`` registry entry is read-only by design (it must remain
usable for GET in read-only mode), so a method check in the body itself is
required to block non-GET/HEAD calls.
"""

from __future__ import annotations

import logging
from typing import Any

from ..helpers import _backends, _get_client, _tool_response

log = logging.getLogger("thruk_mcp.tools.escape")

# Allowed HTTP verbs for the escape-hatch tools.  TRACE and CONNECT are
# omitted intentionally: TRACE can leak auth headers (HTTP TRACE attack) and
# CONNECT is a proxy-tunnelling verb that has no valid Thruk REST use-case.
_ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

# Known Thruk REST resource prefixes.  Any path that does NOT start with one
# of these is rejected before a request is attempted, preventing callers from
# routing to CGI endpoints (e.g. /cgi-bin/cmd.cgi) that bypass the Thruk REST
# authentication layer.
_REST_PATH_PREFIXES: tuple[str, ...] = (
    "/hosts",
    "/services",
    "/hostgroups",
    "/servicegroups",
    "/contacts",
    "/contactgroups",
    "/timeperiods",
    "/commands",
    "/downtimes",
    "/comments",
    "/logs",
    "/sites",
    "/processinfo",
    "/system",
    "/thruk",
)


def _validate_rest_path(path: str) -> str | None:
    """Return an error JSON string if *path* is unsafe, or ``None`` when valid.

    Rules enforced:
    - Must start with ``/`` (not a relative reference).
    - Must not contain ``..`` (path-traversal segment) which could escape the
      ``/thruk/r/`` REST prefix and reach internal CGI endpoints.
    - Must start with a known Thruk REST resource prefix (see
      ``_REST_PATH_PREFIXES``) to prevent routing to non-REST CGI endpoints.

    Callers should return the error string immediately without making any
    HTTP request.
    """
    if not path.startswith("/"):
        return _tool_response({"error": (f"Invalid path: must start with '/'. Got: {path!r}")})
    if ".." in path:
        return _tool_response({"error": (f"Invalid path: must not contain '..'. Got: {path!r}")})
    if not any(path.startswith(p) for p in _REST_PATH_PREFIXES):
        return _tool_response(
            {
                "error": (
                    f"Path {path!r} does not start with a known Thruk REST prefix. "
                    f"Allowed prefixes: {sorted(_REST_PATH_PREFIXES)}"
                )
            }
        )
    return None


async def thruk_query(
    path: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Escape hatch: call any Thruk REST endpoint. `path` is everything after `/thruk/r`
    (e.g. `/hosts/srv01/services`). `params` is the query string, `data` the form body.
    See https://www.thruk.org/documentation/rest.html for the full catalogue.

    WARNING — custom-variable filtering: do NOT use ``q="custom_variables >= 'NAME val'"``
    or ``q="custom_variables = 'NAME val'"`` — Thruk's REST q= parser silently drops these
    filters and returns ALL objects (no error, just wrong results).  Instead, pass the
    variable as a top-level param: ``params={"_VARNAME": "value"}`` for host/service own
    vars, or ``params={"_HOSTVARNAME": "value"}`` for host vars on a service endpoint.
    Prefer ``thruk_list_hosts``/``thruk_list_services`` with ``custom_vars={}`` which
    handle this automatically.
    """
    method_upper = method.upper()
    if method_upper not in _ALLOWED_METHODS:
        return _tool_response(
            {"error": (f"Invalid HTTP method {method!r}. Allowed: {sorted(_ALLOWED_METHODS)}")}
        )
    if _get_client().config.read_only and method_upper not in {"GET", "HEAD"}:
        return _tool_response(
            {
                "error": (
                    f"thruk_query: method {method_upper!r} blocked by THRUK_READ_ONLY=true. "
                    "Only GET and HEAD are permitted in read-only mode."
                )
            }
        )
    path_err = _validate_rest_path(path)
    if path_err is not None:
        return path_err

    _CV_Q_WARNING = (
        "q= filter contains 'custom_variables' which is silently ignored by Thruk's REST "
        "q= parser — results likely include ALL objects (filter not applied). "
        "Pass the variable as a top-level param instead: "
        "_VARNAME=value (own var) or _HOSTVARNAME=value (host var on service endpoint). "
        "Or use thruk_list_hosts / thruk_list_services with custom_vars={'VARNAME': 'value'}."
    )
    q_val = str((params or {}).get("q", ""))
    if "custom_variables" in q_val:
        log.warning("thruk_query: %s", _CV_Q_WARNING)
    result = await _get_client().request(
        method_upper,
        path,
        params=params,
        data=data,
        backends=_backends(backends),
    )
    if "custom_variables" in q_val:
        return _tool_response({"_warning": _CV_Q_WARNING, "data": result})
    return _tool_response(result)


async def thruk_run_background_query(
    path: str,
    method: str = "POST",
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    backends: str | None = None,
    poll_timeout: float = 300.0,
) -> str:
    """Run a potentially long Thruk REST request via the `background=1`
    mechanism. The server returns a job id immediately, then we poll
    `/thruk/jobs/<id>/output` until completion (default 5 min timeout).

    Use this for expensive queries: full config dumps, large availability
    reports, recursive config checks. Same `path` semantics as
    `thruk_query`."""
    method_upper = method.upper()
    if method_upper not in _ALLOWED_METHODS:
        return _tool_response(
            {"error": (f"Invalid HTTP method {method!r}. Allowed: {sorted(_ALLOWED_METHODS)}")}
        )
    # Defense-in-depth: thruk_run_background_query is already removed from the
    # registry when read_only=True (is_write=True in ToolSpec), but guard the
    # function body as well to prevent bypasses via direct calls or future
    # refactors that re-expose the tool.
    if _get_client().config.read_only and method_upper not in {"GET", "HEAD"}:
        return _tool_response(
            {
                "error": (
                    f"thruk_run_background_query: method {method_upper!r} blocked by "
                    "THRUK_READ_ONLY=true. Only GET and HEAD are permitted in read-only mode."
                )
            }
        )
    path_err = _validate_rest_path(path)
    if path_err is not None:
        return path_err

    result = await _get_client().run_background(
        path,
        method=method_upper,
        params=params,
        data=data,
        backends=_backends(backends),
        poll_timeout=poll_timeout,
    )
    return _tool_response(result)
