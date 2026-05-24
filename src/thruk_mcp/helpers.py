"""Shared helper utilities for Thruk MCP tool implementations.

Pure utility functions extracted from server.py (issue #87, step 1 of the
module split).  These are internal helpers — all prefixed with ``_`` by
convention.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote as _urlquote

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
