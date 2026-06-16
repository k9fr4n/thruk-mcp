"""Shared helper utilities for Thruk MCP tool implementations.

Pure utility functions extracted from server.py (issue #87, step 1 of the
module split).  These are internal helpers — all prefixed with ``_`` by
convention.
"""

from __future__ import annotations

import contextvars
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as _urlquote
from urllib.parse import unquote_plus as _urlunquote_plus

from .filters import (
    FilterError,
    compile_filter,
    extract_log_lookup_fields,
    validate_filter,
)

if TYPE_CHECKING:
    from .client import ThrukClient
    from .config import ThrukConfig

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


# Per-context active config. Set by build_server() and, in header-auth
# multi-tenant mode, overridden per request by the middleware so that audit
# attribution (auth_user) reflects the calling tenant. Mirrors _client_var.
_cfg_var: contextvars.ContextVar[ThrukConfig] = contextvars.ContextVar("thruk_mcp_cfg")


def _get_cfg(default: ThrukConfig | None = None) -> ThrukConfig | None:
    """Return the ThrukConfig bound to the current async context, or ``default``.

    Never raises — callers pass the server's base config as the fallback so the
    behaviour is unchanged outside header-auth mode.
    """
    try:
        return _cfg_var.get()
    except LookupError:
        return default


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


def _decode_form_value(value: Any) -> Any:
    """Best-effort URL/form decode of a legacy comment field (issue #249).

    Older Naemon acknowledgements store the author/comment form-encoded
    (``+`` for spaces, ``%C3%A9`` for ``é``) while newer entries are already
    plain text. Apply :func:`urllib.parse.unquote_plus`, which is a no-op on
    values that contain no ``%``/``+`` escapes, so the transform stays
    idempotent on already-decoded data.

    Non-string inputs are returned unchanged.
    """
    if not isinstance(value, str):
        return value
    return _urlunquote_plus(value)


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


# ---------------------------------------------------------------------------
# Latency sanitizer (issue #202)
# ---------------------------------------------------------------------------

#: Field names whose value we treat as a "latency in seconds" — both the
#: host row's ``latency`` and a service row's ``host_latency`` column have
#: been observed carrying a spurious Unix timestamp (see issue #202).
_LATENCY_KEYS: tuple[str, ...] = ("latency", "host_latency")

#: Maximum number of host names listed in the aggregated warning.  Keeps
#: the LLM-facing payload bounded even on a federated install where every
#: backend exposes the bug.
_LATENCY_WARN_SAMPLE: int = 5


def _sanitize_latency(
    payload: Any,
    *,
    cap_seconds: float,
) -> tuple[Any, list[str]]:
    """Nullify spurious ``latency`` / ``host_latency`` values.

    Naemon/Livestatus sometimes writes a Unix-timestamp-shaped value
    (~1.7e9) into a host row's latency column.  Surfacing that verbatim
    misleads LLM clients into reporting decades of latency
    (issue #202).

    Walks *payload* (a single dict or a list of dicts — anything else is
    returned untouched), replaces any ``latency`` / ``host_latency``
    value strictly greater than *cap_seconds* with ``None``, and returns
    a single aggregated human-readable warning naming up to
    :data:`_LATENCY_WARN_SAMPLE` affected hosts.

    The field key is preserved (set to ``null``) rather than removed, so
    callers depending on the row shape do not break.
    """
    affected: list[str] = []

    def _patch(row: dict[str, Any]) -> None:
        for key in _LATENCY_KEYS:
            value = row.get(key)
            if (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and value > cap_seconds
            ):
                row[key] = None
                # Prefer host_name (service row) then name (host row);
                # fall back to the literal key to never raise.
                host = row.get("host_name") or row.get("name") or "<unknown>"
                affected.append(str(host))

    if isinstance(payload, dict):
        _patch(payload)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                _patch(item)

    if not affected:
        return payload, []

    # Deduplicate while preserving order so the sample is meaningful.
    seen: set[str] = set()
    unique: list[str] = []
    for host in affected:
        if host not in seen:
            seen.add(host)
            unique.append(host)

    sample = ", ".join(unique[:_LATENCY_WARN_SAMPLE])
    overflow = len(unique) - _LATENCY_WARN_SAMPLE
    extra = "" if overflow <= 0 else f" (+{overflow} more)"
    warning = (
        f"Sanitized {len(affected)} spurious latency value(s) > {cap_seconds:g}s "
        f"(likely a Naemon/Livestatus bug surfacing a Unix timestamp); "
        f"affected host(s): {sample}{extra}. See issue #202."
    )
    return payload, [warning]


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


def _format_state_label(state: Any, state_map: dict[int, str]) -> str:
    """Render a Naemon state code as a human-readable label.

    Maps known integer state codes via ``state_map`` (e.g. ``HOST_STATE_STR``
    or ``SVC_STATE_STR``).  Codes that are not in the map are rendered as
    ``"UNKNOWN(<n>)"`` rather than a bare integer string, so LLM clients
    never see a meaningless ``"3"`` for a host state (issue #245).

    Naemon HOST ALERT log lines occasionally carry a ``state`` value of 3
    (the host state space only spans 0=UP, 1=DOWN, 2=UNREACHABLE).  This
    helper makes that leak explicit instead of surfacing the raw int.

    Non-int / non-coercible inputs are rendered as ``"UNKNOWN(<raw>)"``.
    """
    try:
        s = int(state)
    except (TypeError, ValueError):
        return f"UNKNOWN({state})"
    label = state_map.get(s)
    if label is not None:
        return label
    return f"UNKNOWN({s})"


# ---------------------------------------------------------------------------
# Shared time + log-family host-resolution helpers (moved from server.py,
# issue #258) so the tools/ sub-package can reach them without importing
# server.py.  Re-exported from server.py for backward compatibility.
# ---------------------------------------------------------------------------
# Hard limit for paginated /hosts lookups that build a host_name[regex].
# 20 000 hosts is far above any realistic hostgroup size; it serves as a
# safety net to prevent runaway memory growth while still covering all real
# deployments. A _warning is surfaced in the tool payload when this cap is hit.
_RESOLVE_HOSTS_HARD_LIMIT: int = 20_000


_THRUK_REL_RE = re.compile(r"^-(\d+)([smhdw])$")
_THRUK_REL_MULT: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _now_utc_epoch() -> int:
    """Current Unix epoch as Thruk expects it (always UTC, TZ-independent)."""
    return int(datetime.now(timezone.utc).timestamp())


def _parse_thruk_time(value: str | None) -> int | None:
    """Parse a Thruk relative ('-2h', '-30m', '-7d') or absolute time to a Unix timestamp.

    Returns ``None`` when the value cannot be parsed (caller decides fallback).
    Absolute formats accepted: integer epoch, ``'YYYY-MM-DD HH:MM:SS'``,
    ``'YYYY-MM-DDTHH:MM:SS'``, ``'YYYY-MM-DDTHH:MM:SSZ'``.
    """
    if value is None:
        return None
    value = value.strip()
    m = _THRUK_REL_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return _now_utc_epoch() - n * _THRUK_REL_MULT[unit]
    try:
        return int(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            # Bare ISO strings from callers have no TZ offset; Thruk stores times in UTC,
            # so we interpret them as UTC (not local TZ) to avoid off-by-1h DST errors.
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


async def _resolve_log_filter(
    filter_node: dict[str, Any] | None,
    allowed_fields: frozenset,
    backends: str | None,
) -> tuple[dict[str, Any], list[str], bool]:
    """Validate + compile a log-family filter.

    Returns ``(extra_params, error_list, host_truncated)``. On error,
    ``error_list`` is non-empty and ``extra_params`` is empty.
    Hostgroup/custom_var fields are resolved via a paginated ``/hosts``
    lookup.  ``host_truncated`` is ``True`` when the host list reached
    ``_RESOLVE_HOSTS_HARD_LIMIT`` and may be incomplete — callers should
    surface a ``_warning`` key in their payload.
    """
    if filter_node is None:
        return {}, [], False
    try:
        validate_filter(filter_node, allowed_fields)
        direct_node, lookup_node = extract_log_lookup_fields(filter_node)
    except FilterError as exc:
        return {}, [str(exc)], False

    extra: dict[str, Any] = {}
    if direct_node is not None:
        extra.update(compile_filter(direct_node, "logs"))
    if lookup_node is not None:
        lookup_params = compile_filter(lookup_node, "hosts")
        host_regex, host_truncated = await _resolve_hosts_to_regex_from_params(
            lookup_params, backends
        )
        if host_regex is None:
            return {}, ["No hosts matched the hostgroup/custom_var filter"], False
        extra["host_name[regex]"] = host_regex
        return extra, [], host_truncated
    return extra, [], False


async def _resolve_hosts_to_regex_from_params(
    params: dict[str, Any],
    backends: str | None,
    hard_limit: int = _RESOLVE_HOSTS_HARD_LIMIT,
) -> tuple[str | None, bool]:
    """Like ``_resolve_hosts_to_regex`` but accepts a pre-built params dict.

    Uses ``get_all()`` to paginate through all matching hosts transparently.
    Returns ``(regex, truncated)`` — ``truncated`` is ``True`` when the
    ``hard_limit`` was reached and the result may be incomplete.
    """
    host_params: dict[str, Any] = {"columns": "name", **params}
    names: list[str] = []
    async for row in _get_client().get_all(
        "/hosts",
        params=host_params,
        backends=_backends(backends),
        page_size=500,
        hard_limit=hard_limit,
    ):
        n = row.get("name") if isinstance(row, dict) else None
        if n:
            names.append(n)
    if not names:
        return None, False
    truncated = len(names) >= hard_limit
    return f"^({'|'.join(re.escape(n) for n in names)})$", truncated


__all__ = [
    "_RESOLVE_HOSTS_HARD_LIMIT",
    "_backends",
    "_build_cv_params",
    "_cfg_var",
    "_client_var",
    "_downtime_payload",
    "_duration_human",
    "_format_state_label",
    "_get_cfg",
    "_get_client",
    "_list_params",
    "_now_utc_epoch",
    "_parse_thruk_time",
    "_resolve_hosts_to_regex_from_params",
    "_resolve_log_filter",
    "_resolve_peer_for_host",
    "_sanitize_latency",
    "_seg",
    "_tool_response",
    "_ts",
]
