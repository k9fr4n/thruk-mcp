"""Shared helper utilities for Thruk MCP tool implementations.

Pure utility functions extracted from server.py (issue #87, step 1 of the
module split).  These are internal helpers — all prefixed with ``_`` by
convention.
"""

from __future__ import annotations

import contextvars
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as _urlquote

if TYPE_CHECKING:
    from .client import ThrukClient

# ---------------------------------------------------------------------------
# Module-level client accessor (shared by server.py and tools/* submodules)
# ---------------------------------------------------------------------------
# Use a ContextVar instead of a bare module-level global so two
# build_server() calls in the same process (e.g. tests, multi-tenant hosts)
# do not clobber each other.  Each asyncio task inherits the parent context,
# so set() in build_server() is visible to every tool coroutine spawned from
# that same event-loop context.  (issue #143)
_client_var: contextvars.ContextVar[ThrukClient] = contextvars.ContextVar("thruk_mcp_client")


def _get_client() -> ThrukClient:
    """Return the ThrukClient bound to the current async context.

    Raises ``RuntimeError`` if no client has been registered yet — this means
    ``build_server()`` has not been called in the active context.
    """
    try:
        return _client_var.get()
    except LookupError as exc:  # pragma: no cover
        raise RuntimeError(
            "thruk-mcp: server not initialised — call build_server() first."
        ) from exc


# ---------------------------------------------------------------------------
# Query parameter helpers
# ---------------------------------------------------------------------------


def _list_params(
    limit: int,
    offset: int,
    sort: str | None,
    columns: str | None,
    default_columns: str | None,
    *,
    max_limit: int = 1000,
) -> dict[str, Any]:
    """Build the common limit/offset/sort/columns query params for list endpoints.

    `columns=''` (empty string) means "return all columns" — explicit opt-out
    from the token-saving default. `columns=None` falls back to default_columns.
    """
    p: dict[str, Any] = {"limit": max(1, min(limit, max_limit))}
    if offset > 0:
        p["offset"] = offset
    if sort:
        p["sort"] = sort
    effective = default_columns if columns is None else columns
    if effective:  # non-empty string
        p["columns"] = effective
    return p


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _ts(value: Any) -> str:
    if not value:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def _duration_human(seconds: int | float) -> str:
    """Format a duration in seconds as a human-readable string, e.g. '3d 2h 15m'."""
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Backend / URL helpers
# ---------------------------------------------------------------------------


def _backends(backends: str | None) -> tuple[str, ...] | None:
    if backends is None:
        return None
    parts = tuple(b.strip() for b in backends.split(",") if b.strip())
    return parts or None


async def _resolve_peer_for_host(client: ThrukClient, host: str) -> tuple[str, ...] | None:
    """Resolve which backend owns ``host`` so caller commands target it only.

    Used to avoid broadcasting host-scoped commands (e.g.
    ``DEL_DOWNTIME_BY_HOST_NAME``) to every configured backend when the host
    is known to only one — see issue #196.

    Performs ``GET /hosts/{host}?columns=peer_key`` with no ``backends=``
    override (so Thruk fans the query out across every site) and inspects
    the response:

    - exactly one entry → returns ``(peer_key,)``;
    - zero or multiple entries (host unknown or ambiguous name collision) →
      returns ``None`` so the caller falls back to its default behaviour
      (broadcast, current pre-fix semantics).

    Surfaces ``ThrukError`` verbatim — the caller is responsible for any
    fallback policy on lookup failure.
    """
    raw = await client.get(
        f"/hosts/{_urlquote(str(host), safe='')}",
        params={"columns": "peer_key"},
    )
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = [raw]
    else:
        return None
    peer_keys: set[str] = {
        str(row["peer_key"]) for row in rows if isinstance(row, dict) and row.get("peer_key")
    }
    if len(peer_keys) != 1:
        return None
    return (next(iter(peer_keys)),)


def _seg(value: str) -> str:
    """URL-encode a single REST path segment.

    Prevents injection of '/' or '..' into Thruk REST paths when host or
    service names are interpolated directly into URL path f-strings.
    Nagios/Naemon forbids slashes in object names at configuration time, so
    no legitimate name is affected by this encoding.
    """
    return _urlquote(str(value), safe="")


def _build_cv_params(
    custom_vars: dict[str, Any] | None,
    *,
    host_prefix: bool = False,
) -> dict[str, str]:
    """Translate {VARNAME: value} → Thruk REST ``_[HOST]VARNAME=value`` params.

    Thruk's ``_fixup_livestatus_filter`` (rest_v1.pm ~L1699) rewrites any
    query param starting with ``_`` to the Livestatus filter
    ``custom_variables = 'VARNAME value'``.  Varnames are upper-cased to
    match the Nagios convention (custom-var names are stored in uppercase).

    ``host_prefix=True`` generates ``_HOST<X>=<v>`` which Thruk routes to
    ``host_custom_variables`` — used to filter *services* by a *host*-level
    custom variable (the ``HOST`` prefix is stripped server-side).
    """
    if not custom_vars:
        return {}
    prefix = "_HOST" if host_prefix else "_"
    return {f"{prefix}{k.upper()}": str(v) for k, v in custom_vars.items()}


# ---------------------------------------------------------------------------
# Downtime payload helper
# ---------------------------------------------------------------------------


def _downtime_payload(
    comment: str,
    author: str,
    start_time: str,
    end_time: str,
    duration_minutes: int | None,
    fixed: bool,
    triggered_by: int,
) -> dict[str, str]:
    if duration_minutes:
        end_time = f"+{duration_minutes}m"
    return {
        "start_time": start_time,
        "end_time": end_time,
        "comment_data": comment,
        "comment_author": author,
        "fixed": "1" if fixed else "0",
        "triggered_by": str(triggered_by),
    }


# ---------------------------------------------------------------------------
# Response builder (issue #146)
# ---------------------------------------------------------------------------


def _tool_response(payload: Any, warnings: list[str] | None = None) -> str:
    """Centralised MCP tool JSON response builder.

    Wraps *payload* as ``json.dumps(..., indent=2, default=str)`` — the
    canonical wire format used by every tool implementation in
    ``server.py``.

    When *warnings* is a non-empty list, the warnings are merged into the
    payload:

    - a ``dict`` payload gains a ``_warnings`` key;
    - any other payload is wrapped as ``{"data": payload, "_warnings": warnings}``.

    When *warnings* is empty or ``None``, the wire output is byte-for-byte
    identical to a plain ``json.dumps(payload, indent=2, default=str)`` —
    no extra keys, no wrapping. This guarantees the migration from the
    inlined call sites is a pure refactor with no observable change on
    the protocol surface.

    Future serialization changes (compact mode, custom default encoder,
    response schema versioning) only need to be applied here.
    """
    if warnings:
        if isinstance(payload, dict):
            payload = {**payload, "_warnings": warnings}
        else:
            payload = {"data": payload, "_warnings": warnings}
    return json.dumps(payload, indent=2, default=str)


__all__ = [
    "_backends",
    "_build_cv_params",
    "_client_var",
    "_downtime_payload",
    "_duration_human",
    "_get_client",
    "_list_params",
    "_resolve_peer_for_host",
    "_seg",
    "_tool_response",
    "_ts",
]
