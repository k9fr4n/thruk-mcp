"""MCP server definition: tools mapped to Thruk REST endpoints.

Uses the **low-level MCP SDK** (mcp.server.Server) instead of FastMCP.

Rationale: the Docker MCP Gateway strips arguments from tool calls when the
schemas it receives are empty.  FastMCP generates schemas via
``typing.get_type_hints()``; on some SDK/Python versions this fails for
functions defined as closures inside ``build_server()``, yielding
``properties: {}``.  By using the low-level SDK we:

  1. Define ``inputSchema`` explicitly (no annotation introspection at all).
  2. Receive ``arguments`` as a raw ``dict`` in ``call_tool`` — no Pydantic
     model is created, so the gateway cannot silently drop params.
  3. Stay compatible with the Docker MCP Gateway's stdio transport without
     any catalog label gymnastics.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
from collections import Counter, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.server import ReadResourceContents
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)
from pydantic import AnyUrl

from . import audit
from .client import ThrukClient, ThrukError
from .config import ThrukConfig
from .constants import (
    _NOISY_MAX_ALERTS as _NOISY_MAX_ALERTS,
)
from .constants import (
    DEFAULT_COMMENT_COLUMNS,
    DEFAULT_DOWNTIME_COLUMNS,
    DEFAULT_GROUP_COLUMNS,
    DEFAULT_HOST_COLUMNS,
    DEFAULT_LOG_COLUMNS,
    DEFAULT_NOTIFICATION_COLUMNS,
    DEFAULT_SERVICE_COLUMNS,
    HOST_STATE_INT,
    HOST_STATE_STR,
    SVC_STATE_INT,
    SVC_STATE_STR,
)
from .filters import (
    FIELDS_ALERTS,
    FIELDS_HOSTS,
    FIELDS_LOGS,
    FIELDS_NOISY_HOSTS,
    FIELDS_NOISY_SERVICES,
    FIELDS_NOTIFICATIONS,
    FIELDS_PROBLEMS,
    FIELDS_SERVICES,
    FilterError,
    build_tool_schema,
    compile_filter,
    compile_filter_problems,
    extract_log_lookup_fields,
    filter_schema_property,
    validate_filter,
)
from .helpers import (
    _backends,
    _build_cv_params,
    _seg,
    _ts,
)
from .helpers import (
    _downtime_payload as _downtime_payload,
)
from .helpers import (
    _duration_human as _duration_human,
)
from .helpers import (
    _list_params as _list_params,
)

__all__ = ["WRITE_TOOLS", "ThrukMCPServer", "build_server"]

log = logging.getLogger("thruk_mcp.server")

# Hard limit for paginated /hosts lookups that build a host_name[regex].
# 20 000 hosts is far above any realistic hostgroup size; it serves as a
# safety net to prevent runaway memory growth while still covering all real
# deployments. A _warning is surfaced in the tool payload when this cap is hit.
_RESOLVE_HOSTS_HARD_LIMIT: int = 20_000

# WRITE_TOOLS is derived from TOOL_REGISTRY below (see end of module).
# Tools that mutate monitoring state — used by read_only mode and the audit log.
# Do NOT define it here; it is auto-generated as:
#   WRITE_TOOLS = frozenset(spec.name for spec in TOOL_REGISTRY if spec.is_write)
# This forward-reference is safe because _is_auditable_write() only reads
# WRITE_TOOLS at call-time, never at import-time.


def _is_auditable_write(name: str, arguments: dict[str, Any]) -> bool:
    """Return True when this tool call should be recorded in the audit log.

    ``WRITE_TOOLS`` covers all explicitly-listed mutating tools. ``thruk_query``
    is intentionally absent from that set (it also serves read/GET requests and
    must NOT be stripped in read-only mode), but its non-GET/HEAD invocations
    are still writes and must be audited.  We inspect ``method`` at call time.

    Note: since issue #138, non-GET/HEAD calls to ``thruk_query`` are also
    *blocked* (not merely audited) when ``THRUK_READ_ONLY=true``.  This function
    only governs audit logging — the read-only enforcement lives in the tool
    function bodies.
    """
    if name in WRITE_TOOLS:
        return True
    if name == "thruk_query":
        method = str(arguments.get("method", "GET")).upper()
        return method not in {"GET", "HEAD"}
    return False


# State maps -- sourced from constants.py (single source of truth, issue #81).
# Module-level aliases preserve any external import of these names unchanged.
HOST_STATES: dict[int, str] = HOST_STATE_STR
SERVICE_STATES: dict[int, str] = SVC_STATE_STR
HOST_STATE_MAP: dict[str, int] = HOST_STATE_INT
SVC_STATE_MAP: dict[str, int] = SVC_STATE_INT

# ---------------------------------------------------------------------------
# Module-level client accessor
# ---------------------------------------------------------------------------
_client: ThrukClient | None = None


def _get_client() -> ThrukClient:
    if _client is None:  # pragma: no cover
        raise RuntimeError("thruk-mcp: server not initialised — call build_server() first.")
    return _client


# ---------------------------------------------------------------------------
# Tool implementations (module-level so FastMCP can always introspect them)
# ---------------------------------------------------------------------------


async def thruk_list_hosts(
    filter: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "name",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List monitored hosts.

    ``filter`` is a structured AND/OR tree — see the ``filter`` parameter
    description for syntax, available fields and examples.

    Fields: ``name``, ``state`` (up/down/unreachable), ``hostgroup``,
    ``custom_var`` (e.g. ``{"var":"KERNEL","val":"windows"}``), ``address``.

    Pagination: ``limit`` (max 1000), ``offset``.
    Sort: e.g. ``'name'``, ``'-state'``.
    Columns: default is a tight subset to save tokens; pass ``''`` for all.
    """
    params = _list_params(limit, offset, sort, columns, DEFAULT_HOST_COLUMNS)
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_HOSTS)
        except FilterError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        params.update(compile_filter(filter, "hosts"))
    data = await _get_client().get("/hosts", params=params, backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_get_host(host: str, backends: str | None = None) -> str:
    """Get a single host by name."""
    data = await _get_client().get(f"/hosts/{_seg(host)}", backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_list_services(
    filter: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "host_name,description",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List monitored services.

    ``filter`` is a structured AND/OR tree — see the ``filter`` parameter
    description for syntax, available fields and examples.

    Fields: ``host``, ``description``, ``state`` (ok/warning/critical/unknown),
    ``hostgroup``, ``servicegroup``,
    ``custom_var`` (service-level, e.g. ``{"var":"CRITICALITY","val":"prod"}``),
    ``host_custom_var`` (host-level, e.g. ``{"var":"KERNEL","val":"windows"}``).

    Pagination via ``limit``/``offset``, sort via ``sort``
    (e.g. ``'-last_state_change'``). Default columns are a tight subset;
    pass ``columns=''`` for all.
    """
    params = _list_params(limit, offset, sort, columns, DEFAULT_SERVICE_COLUMNS)
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_SERVICES)
        except FilterError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        params.update(compile_filter(filter, "services"))
    data = await _get_client().get("/services", params=params, backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_get_service(host: str, service: str, backends: str | None = None) -> str:
    """Get a single service by host and description."""
    data = await _get_client().get(
        f"/services/{_seg(host)}/{_seg(service)}", backends=_backends(backends)
    )
    return json.dumps(data, indent=2, default=str)


async def thruk_list_hostgroups(
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List host groups. Default columns return name/alias and host/service counts only."""
    params = _list_params(limit, offset, sort, columns, DEFAULT_GROUP_COLUMNS)
    data = await _get_client().get("/hostgroups", params=params, backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_list_servicegroups(
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List service groups. Default columns return name/alias and counts only."""
    params = _list_params(limit, offset, sort, columns, DEFAULT_GROUP_COLUMNS)
    data = await _get_client().get("/servicegroups", params=params, backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_problems(
    filter: dict[str, Any] | None = None,
    limit: int = 100,
    offset: int = 0,
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List all current unhandled host/service problems (not acknowledged, not in downtime).

    Sorted by worst state first. Default columns are tight; pass ``columns=''`` for all.

    ``filter`` supports fields: ``hostgroup``, ``custom_var`` (host-level, applied as
    ``_VAR`` on hosts and ``_HOSTVAR`` on services), ``host_custom_var`` (services
    sub-query only), ``state``. OR is not supported (dual-query architecture requires AND).
    """
    host_params = _list_params(limit, offset, "-state,name", columns, DEFAULT_HOST_COLUMNS)
    host_params.update({"state": 1, "acknowledged": 0, "scheduled_downtime_depth": 0})
    svc_params = _list_params(
        limit, offset, "-state,host_name,description", columns, DEFAULT_SERVICE_COLUMNS
    )
    svc_params.update({"state[gte]": 1, "acknowledged": 0, "scheduled_downtime_depth": 0})
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_PROBLEMS)
        except FilterError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        try:
            extra_host, extra_svc = compile_filter_problems(filter)
        except FilterError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        host_params.update(extra_host)
        svc_params.update(extra_svc)
    hosts, host_warnings = await _get_client().get_with_fallback(
        "/hosts", params=host_params, backends=_backends(backends)
    )
    services, svc_warnings = await _get_client().get_with_fallback(
        "/services", params=svc_params, backends=_backends(backends)
    )
    result: dict[str, Any] = {"hosts": hosts, "services": services}
    all_warnings = list(dict.fromkeys(host_warnings + svc_warnings))
    if all_warnings:
        result["_warnings"] = all_warnings
    return json.dumps(result, indent=2, default=str)


async def thruk_stats(backends: str | None = None) -> str:
    """Aggregated host/service statistics."""
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/stats", backends=be),
        _get_client().get("/services/stats", backends=be),
    )
    return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)


async def thruk_list_downtimes(
    host: str | None = None,
    active_only: bool = True,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-start_time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List scheduled downtimes."""
    params = _list_params(limit, offset, sort, columns, DEFAULT_DOWNTIME_COLUMNS)
    if host:
        params["host_name"] = host
    if active_only:
        now = _now_utc_epoch()
        params["start_time[lte]"] = now
        params["end_time[gte]"] = now
    data = await _get_client().get("/downtimes", params=params, backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_list_comments(
    host: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-entry_time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List comments (acknowledgements appear here too)."""
    params = _list_params(limit, offset, sort, columns, DEFAULT_COMMENT_COLUMNS)
    if host:
        params["host_name"] = host
    data = await _get_client().get("/comments", params=params, backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_sites() -> str:
    """List configured Thruk backends (sites)."""
    return json.dumps(await _get_client().get("/sites"), indent=2, default=str)


# ------------------------------------------------------ Logs / history helper
async def _resolve_hosts_to_regex(
    backends: str | None,
    hostgroup: str | None = None,
    custom_vars: dict[str, Any] | None = None,
    hard_limit: int = _RESOLVE_HOSTS_HARD_LIMIT,
) -> tuple[str | None, bool]:
    """Resolve a hostgroup / custom-variable filter to a ``host_name[regex]`` pattern.

    The Naemon Livestatus ``log`` table exposes neither ``current_host_groups``
    nor custom-variable columns.  We work around this by querying ``/hosts``
    (which supports both) and building an explicit alternation regex from the
    matching host names.

    *hostgroup* and *custom_vars* may be combined: the ``/hosts`` call applies
    both filters simultaneously (logical AND), so we issue only one request.

    Uses ``get_all()`` to paginate through all matching hosts transparently.
    Returns ``(None, False)`` when no hosts match (caller should emit an empty
    result).  Returns ``(regex, True)`` when the hard_limit was reached and the
    list may be incomplete — callers should surface a ``_warning`` in the payload.
    """
    params: dict[str, str] = {"columns": "name"}
    if hostgroup:
        params["groups[gte]"] = hostgroup
    if custom_vars:
        params.update(_build_cv_params(custom_vars))
    names: list[str] = []
    async for row in _get_client().get_all(
        "/hosts",
        params=params,
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


async def _fetch_logs(
    path: str,
    host: str | None,
    service: str | None,
    since: str | None,
    until: str | None,
    message_regex: str | None,
    limit: int,
    offset: int,
    sort: str,
    columns: str | None,
    backends: str | None,
    extra: dict[str, Any] | None = None,
    hostgroup: str | None = None,
    default_columns: str = DEFAULT_LOG_COLUMNS,
    custom_vars: dict[str, Any] | None = None,
) -> tuple[Any, list[str]]:
    """Fetch log-family data with graceful per-backend fallback.

    Returns ``(data, warnings)``.  *warnings* is non-empty only when the
    all-backends query failed and some backends also failed individually.

    ``hostgroup`` and ``custom_vars`` are resolved to a ``host_name[regex]``
    filter via a single ``/hosts`` lookup.  The Naemon Livestatus ``log``
    table exposes neither ``current_host_groups`` nor custom-variable columns,
    so direct filtering on ``/logs`` is not possible.
    """
    params = _list_params(limit, offset, sort, columns, default_columns)
    if host:
        params["host_name"] = host
    if service:
        params["service_description"] = service
    if since:
        params["time[gte]"] = since
    if until:
        params["time[lte]"] = until
    if message_regex:
        params["message[regex]"] = message_regex
    host_truncated = False
    if hostgroup or custom_vars:
        host_regex, host_truncated = await _resolve_hosts_to_regex(
            backends, hostgroup=hostgroup, custom_vars=custom_vars
        )
        if host_regex:
            params["host_name[regex]"] = host_regex
        # None → no matching hosts; query will naturally return empty result
    if extra:
        params.update(extra)
    # Always POST: log queries can carry large host_name[regex] alternations
    # (e.g. 976-host hostgroups) that would exceed Apache URI limits with GET.
    # Thruk REST accepts POST with form-encoded body on all /r/* endpoints.
    data, warnings = await _get_client().get_with_fallback(
        path, params=params, backends=_backends(backends), method="POST"
    )
    if host_truncated:
        warnings = [
            *warnings,
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete.",
        ]
    return data, warnings


async def _aggregate_alerts(
    type_regex: str,
    key_fields: tuple[str, ...],
    state_map: dict[int, str],
    extra_params: dict[str, Any],
    backends: str | None,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Fetch and aggregate alert log entries from the Thruk /logs endpoint.

    Shared helper for :func:`thruk_top_noisy_hosts` and
    :func:`thruk_top_noisy_services`.  Callers build ``extra_params``
    (including ``type[~]`` / ``time[gte]`` / ``time[lte]``) and pass the
    key fields that identify a unique entity (e.g. ``("host_name",)`` for
    hosts, ``("host_name", "service_description")`` for services).

    Returns a 3-tuple:
    - **rows** - list of dicts with ``alert_count``, all key-field values,
      ``last_state`` (human-readable via *state_map*), and ``last_alert_time``.
      Already sorted by ``alert_count`` descending (not yet sliced to limit).
    - **warnings** - pass-through from :meth:`ThrukClient.get_with_fallback`.
    - **hit_hard_limit** - ``True`` when the raw data reached ``_NOISY_MAX_ALERTS``.
    """
    columns_set = {"host_name", "state", "time"} | set(key_fields)
    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "-time",
        "columns": ",".join(sorted(columns_set)),
        **extra_params,
        "type[~]": type_regex,  # always override: callers must not change the log type
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    counts: dict[tuple[str, ...], dict[str, Any]] = {}
    for entry in data:
        state = entry.get("state", -1)
        if state == 0:
            continue
        key = tuple(str(entry.get(f) or "") for f in key_fields)
        rec = counts.setdefault(
            key,
            {
                "alert_count": 0,
                "_last_ts": 0,
                "last_state_int": state,
                "last_alert_time": None,
            },
        )
        rec["alert_count"] += 1
        t = entry.get("time") or 0
        if t > rec["_last_ts"]:
            rec["_last_ts"] = t
            rec["last_state_int"] = state
            rec["last_alert_time"] = _ts(t)

    rows = sorted(
        [
            {
                **dict(zip(key_fields, k, strict=False)),
                "alert_count": v["alert_count"],
                "last_state": state_map.get(v["last_state_int"], str(v["last_state_int"])),
                "last_alert_time": v["last_alert_time"],
            }
            for k, v in counts.items()
        ],
        key=lambda x: x["alert_count"],
        reverse=True,
    )
    return rows, warnings, len(data) >= _NOISY_MAX_ALERTS


async def thruk_top_noisy_hosts(
    since: str | None = "-24h",
    until: str | None = None,
    limit: int = 10,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Return the top N hosts ranked by HOST ALERT count over a time window.

    Aggregates HOST ALERT log entries, excludes RECOVERY events (state UP = 0),
    and ranks by alert count descending.

    ``since`` / ``until`` accept relative (``-24h``, ``-30m``) or absolute
    (``2026-05-20 14:00:00``) values — same format as ``thruk_list_alerts``.
    Default window: last 24 h (``since="-24h"``, ``until=None``).

    ``filter`` fields: ``host`` (eq/regex), ``hostgroup``, ``custom_var``
    (host-level Nagios variable, resolved via /hosts lookup).

    Returns a wrapped object:
    ``since``, ``until``, ``total_alerts_in_window`` (after RECOVERY exclusion),
    ``results`` list sorted by ``alert_count`` desc, each entry containing
    ``host``, ``alert_count``, ``last_state``, ``last_alert_time``.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_HOSTS, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)

    if since:
        extra["time[gte]"] = since
    if until:
        extra["time[lte]"] = until

    rows, warnings, hit_limit = await _aggregate_alerts(
        type_regex="^HOST ALERT",
        key_fields=("host_name",),
        state_map=HOST_STATES,
        extra_params=extra,
        backends=backends,
    )
    total = sum(r["alert_count"] for r in rows)
    results = [
        {
            "host": r["host_name"],
            "alert_count": r["alert_count"],
            "last_state": r["last_state"],
            "last_alert_time": r["last_alert_time"],
        }
        for r in rows[:limit]
    ]

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "total_alerts_in_window": total,
        "results": results,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif hit_limit:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; aggregation may be incomplete."
        )
    if warnings:
        payload["_warnings"] = warnings
    return json.dumps(payload, indent=2, default=str)


async def thruk_top_noisy_services(
    since: str | None = "-24h",
    until: str | None = None,
    limit: int = 10,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Return the top N services ranked by SERVICE ALERT count over a time window.

    Aggregates SERVICE ALERT log entries, excludes RECOVERY events (state OK = 0),
    and ranks by alert count descending.

    ``since`` / ``until`` accept relative (``-24h``, ``-30m``) or absolute
    (``2026-05-20 14:00:00``) values — same format as ``thruk_list_alerts``.
    Default window: last 24 h (``since="-24h"``, ``until=None``).

    ``filter`` fields: ``host`` (eq/regex), ``service`` (eq/regex),
    ``hostgroup``, ``custom_var`` (host-level Nagios variable, resolved via
    /hosts lookup).

    Returns a wrapped object:
    ``since``, ``until``, ``total_alerts_in_window`` (after RECOVERY exclusion),
    ``results`` list sorted by ``alert_count`` desc, each entry containing
    ``host``, ``service``, ``alert_count``, ``last_state``, ``last_alert_time``.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)

    if since:
        extra["time[gte]"] = since
    if until:
        extra["time[lte]"] = until

    rows, warnings, hit_limit = await _aggregate_alerts(
        type_regex="^SERVICE ALERT",
        key_fields=("host_name", "service_description"),
        state_map=SERVICE_STATES,
        extra_params=extra,
        backends=backends,
    )
    total = sum(r["alert_count"] for r in rows)
    results = [
        {
            "host": r["host_name"],
            "service": r["service_description"],
            "alert_count": r["alert_count"],
            "last_state": r["last_state"],
            "last_alert_time": r["last_alert_time"],
        }
        for r in rows[:limit]
    ]

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "total_alerts_in_window": total,
        "results": results,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif hit_limit:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; aggregation may be incomplete."
        )
    if warnings:
        payload["_warnings"] = warnings
    return json.dumps(payload, indent=2, default=str)


async def thruk_flap_summary(
    since: str | None = "-24h",
    until: str | None = None,
    limit: int = 10,
    min_transitions: int = 3,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Return hosts and services with the most state transitions (flapping) over a time window.

    Aggregates HOST ALERT and SERVICE ALERT log entries, counts consecutive state
    transitions per object, and returns those with at least ``min_transitions``
    changes ranked by transition count descending.

    ``since`` / ``until`` accept relative (``-24h``, ``-30m``) or absolute
    (``2026-05-20 14:00:00``) values — same format as ``thruk_list_alerts``.
    Default window: last 24 h (``since="-24h"``, ``until=None``).

    A high transition count indicates a misconfigured check threshold or a genuinely
    unstable object -- distinct from ``thruk_top_noisy_hosts/services`` which rank by
    raw alert count regardless of state direction.

    ``filter`` fields: ``host`` (eq/regex), ``service`` (eq/regex),
    ``hostgroup``, ``custom_var``.

    Returns a wrapped object:
    ``since``, ``until``, ``min_transitions``, ``total_flapping_objects``,
    ``results`` list sorted by ``transition_count`` desc, each entry containing
    ``host``, ``service`` (null for host-level flapping), ``transition_count``,
    ``states_seen`` (sorted unique set of state names), ``last_state``,
    ``last_alert_time``.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    if since:
        extra["time[gte]"] = since
    if until:
        extra["time[lte]"] = until
    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "time",  # ascending: chronological order required for transition counting
        "columns": "host_name,service_description,state,time",
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    # Group entries by (host, service) — service="" for host-level alerts
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in data:
        h = entry.get("host_name") or ""
        svc = entry.get("service_description") or ""
        key = (h, svc)
        if key not in groups:
            groups[key] = []
        groups[key].append(entry)

    # Count consecutive state transitions per group (already sorted by time asc)
    results_raw: list[dict[str, Any]] = []
    for (h, svc), entries in groups.items():
        if len(entries) < 2:
            continue
        transitions = sum(
            1 for i in range(1, len(entries)) if entries[i]["state"] != entries[i - 1]["state"]
        )
        if transitions < min_transitions:
            continue
        state_map = HOST_STATES if not svc else SERVICE_STATES
        last_entry = entries[-1]
        last_state_int = last_entry.get("state", -1)
        states_seen = sorted(
            {state_map.get(e.get("state", -1), str(e.get("state", -1))) for e in entries}
        )
        results_raw.append(
            {
                "host": h,
                "service": svc or None,
                "transition_count": transitions,
                "states_seen": states_seen,
                "last_state": state_map.get(last_state_int, str(last_state_int)),
                "last_alert_time": _ts(last_entry.get("time")),
            }
        )

    results_raw.sort(key=lambda x: x["transition_count"], reverse=True)

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "min_transitions": min_transitions,
        "total_flapping_objects": len(results_raw),
        "results": results_raw[:limit],
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif len(data) >= _NOISY_MAX_ALERTS:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; aggregation may be incomplete."
        )
    if warnings:
        payload["_warnings"] = warnings
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# Trends & history tools (issue #57)
# ---------------------------------------------------------------------------

_BUCKET_SIZES: dict[str, int] = {
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
}

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


async def thruk_alert_heatmap(
    since: str | None = "-24h",
    until: str | None = None,
    bucket: str = "1h",
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Return alert counts grouped by time bucket over a window.

    Useful for spotting alert storms, quiet periods, and recurring patterns.
    The LLM can use the returned list as a sparkline: high counts flag
    incidents, sustained highs flag chronic issues.

    ``bucket`` controls bucket width: ``"15m"``, ``"30m"``, ``"1h"`` (default),
    ``"6h"``, ``"1d"``. Buckets with zero alerts are included so the output
    can be rendered as a continuous timeline.

    ``since`` / ``until`` accept Thruk relative (``"-24h"``, ``"-7d"``) or
    absolute (``"2026-05-21 14:00:00"``) values. Default window: last 24 h.

    ``filter`` fields: ``host``, ``service``, ``hostgroup``,
    ``custom_var`` (host-level, resolved via /hosts lookup).

    Returns a wrapped object: ``since``, ``until``, ``bucket``,
    ``total_alerts``, ``results`` list of ``{bucket_start, count}``
    ordered chronologically. Empty buckets are filled with ``count=0``.
    """
    bucket_secs = _BUCKET_SIZES.get(bucket)
    if bucket_secs is None:
        return json.dumps(
            {"error": f"Invalid bucket {bucket!r}. Allowed: {', '.join(_BUCKET_SIZES)}"},
            indent=2,
        )

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    if since:
        extra["time[gte]"] = since
    if until:
        extra["time[lte]"] = until

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "time",
        "columns": "time",
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    # Count alerts per bucket
    raw_counts: dict[int, int] = {}
    total = 0
    for entry in data:
        t = entry.get("time")
        if not t:
            continue
        b = (int(t) // bucket_secs) * bucket_secs
        raw_counts[b] = raw_counts.get(b, 0) + 1
        total += 1

    # Build continuous timeline — fill empty buckets between window boundaries
    ts_since = _parse_thruk_time(since)
    ts_until = _parse_thruk_time(until) if until else _now_utc_epoch()

    results: list[dict[str, Any]] = []
    if ts_since is not None and ts_until is not None:
        first_b = (ts_since // bucket_secs) * bucket_secs
        last_b = (ts_until // bucket_secs) * bucket_secs
        b = first_b
        while b <= last_b:
            results.append(
                {
                    "bucket_start": datetime.fromtimestamp(b, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "count": raw_counts.get(b, 0),
                }
            )
            b += bucket_secs
    else:
        # Fallback: only buckets that have data (unparseable since/until)
        for b in sorted(raw_counts):
            results.append(
                {
                    "bucket_start": datetime.fromtimestamp(b, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "count": raw_counts[b],
                }
            )

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "bucket": bucket,
        "total_alerts": total,
        "results": results,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif len(data) >= _NOISY_MAX_ALERTS:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; aggregation may be incomplete."
        )
    if warnings:
        payload["_warnings"] = warnings
    return json.dumps(payload, indent=2, default=str)


async def thruk_recurring_problems(
    since: str | None = "-24h",
    until: str | None = None,
    min_alerts: int = 5,
    limit: int = 10,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Return hosts/services that generated repeated alerts over a time window.

    Identifies chronic problems: objects that fired more than ``min_alerts``
    HOST/SERVICE ALERT entries (RECOVERY events — state 0 — are excluded) in
    the requested period. Results are sorted by alert count descending.

    ``since`` / ``until`` accept Thruk relative (``"-24h"``, ``"-7d"``) or
    absolute (``"2026-05-21 14:00:00"``) values. Default window: last 24 h.

    ``min_alerts`` minimum number of non-recovery alert events to appear in
    the results (must be ≥ 1, default 5).
    ``limit`` caps the number of returned entries (default 10).

    ``filter`` fields: ``host``, ``service``, ``hostgroup``,
    ``custom_var`` (host-level, resolved via /hosts lookup).

    Returns a wrapped object: ``since``, ``until``, ``min_alerts``,
    ``total_objects_above_threshold``, ``results`` list of
    ``{host, service, alert_count, first_seen, last_seen, last_state}``
    sorted by ``alert_count`` descending.
    """
    if min_alerts < 1:
        return json.dumps({"error": "min_alerts must be >= 1"}, indent=2)

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    if since:
        extra["time[gte]"] = since
    if until:
        extra["time[lte]"] = until

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "time",
        "columns": "host_name,service_description,state,time",
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    # Aggregate per (host, service) — exclude state=0 (UP/OK = recovery)
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in data:
        state = entry.get("state", -1)
        if state == 0:
            continue
        h = entry.get("host_name") or ""
        svc = entry.get("service_description") or ""
        key = (h, svc)
        t = int(entry.get("time") or 0)
        if key not in agg:
            agg[key] = {
                "alert_count": 0,
                "first_ts": t,
                "last_ts": t,
                "last_state_int": state,
            }
        else:
            if t < agg[key]["first_ts"]:
                agg[key]["first_ts"] = t
            if t > agg[key]["last_ts"]:
                agg[key]["last_ts"] = t
                agg[key]["last_state_int"] = state
        agg[key]["alert_count"] += 1

    above = [
        {
            "host": h,
            "service": svc or None,
            "alert_count": v["alert_count"],
            "first_seen": _ts(v["first_ts"]),
            "last_seen": _ts(v["last_ts"]),
            "last_state": (HOST_STATES if not svc else SERVICE_STATES).get(
                v["last_state_int"], str(v["last_state_int"])
            ),
        }
        for (h, svc), v in agg.items()
        if v["alert_count"] >= min_alerts
    ]
    above.sort(key=lambda x: x["alert_count"], reverse=True)

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "min_alerts": min_alerts,
        "total_objects_above_threshold": len(above),
        "results": above[:limit],
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif len(data) >= _NOISY_MAX_ALERTS:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; aggregation may be incomplete."
        )
    if warnings:
        payload["_warnings"] = warnings
    return json.dumps(payload, indent=2, default=str)


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


async def thruk_list_logs(
    filter: dict[str, Any] | None = None,
    since: str | None = "-24h",
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """Query raw Livestatus log entries (/logs).

    ``filter`` fields: ``host``, ``service``, ``message`` (regex),
    ``since`` / ``until`` (Thruk relative times, e.g. ``'-24h'``, ``'-7d'``),
    ``hostgroup`` and ``custom_var`` (resolved via a ``/hosts`` lookup — AND only).

    Default window: last 24 h. Sort ``'-time'`` = newest first.
    Pagination via ``limit``/``offset``.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_LOGS, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)
    # since/until defaults only when not overridden by filter
    if "time[gte]" not in extra and since:
        extra["time[gte]"] = since
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = until
    data, warnings = await _fetch_logs(
        "/logs",
        None,
        None,
        None,
        None,
        None,
        limit,
        offset,
        sort,
        columns,
        backends,
        extra=extra,
    )
    if host_truncated:
        warnings = [
            *warnings,
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete.",
        ]
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


async def thruk_list_alerts(
    filter: dict[str, Any] | None = None,
    since: str | None = "-24h",
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List HOST/SERVICE ALERT entries from the log.

    Queries ``/logs`` with ``type[~]=^(HOST|SERVICE) ALERT`` (client-side alias
    expansion — the ``/alerts`` endpoint is broken on some Thruk versions).

    ``filter`` fields: ``host``, ``service``, ``state``
    (up/down/unreachable for hosts, ok/warning/critical/unknown for services),
    ``since`` / ``until``, ``hostgroup`` and ``custom_var`` (AND-only, /hosts lookup).
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_ALERTS, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)
    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    if "time[gte]" not in extra and since:
        extra["time[gte]"] = since
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = until
    data, warnings = await _fetch_logs(
        "/logs",
        None,
        None,
        None,
        None,
        None,
        limit,
        offset,
        sort,
        columns,
        backends,
        extra=extra,
    )
    if host_truncated:
        warnings = [
            *warnings,
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete.",
        ]
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


async def thruk_list_notifications(
    filter: dict[str, Any] | None = None,
    since: str | None = "-24h",
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List notification entries from the log (class=3).

    Queries ``/logs`` with ``class=3`` (client-side alias expansion — the
    ``/notifications`` endpoint is broken on some Thruk versions).

    ``filter`` fields: ``host``, ``service``, ``contact``, ``state``,
    ``since`` / ``until``, ``hostgroup`` and ``custom_var`` (AND-only, /hosts lookup).
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOTIFICATIONS, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)
    extra["class"] = "3"
    if "time[gte]" not in extra and since:
        extra["time[gte]"] = since
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = until
    data, warnings = await _fetch_logs(
        "/logs",
        None,
        None,
        None,
        None,
        None,
        limit,
        offset,
        sort,
        columns,
        backends,
        extra=extra,
        default_columns=DEFAULT_NOTIFICATION_COLUMNS,
    )
    if host_truncated:
        warnings = [
            *warnings,
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete.",
        ]
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


async def thruk_recent_events(
    filter: dict[str, Any] | None = None,
    hours: int = 1,
    only_alerts: bool = False,
    limit: int = 100,
    offset: int = 0,
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """Return the most recent monitoring events from the last N hours (default 1 h).

    Set ``only_alerts=True`` to restrict to HOST/SERVICE ALERT entries.

    ``filter`` fields: ``host``, ``service``, ``since`` / ``until``,
    ``hostgroup`` and ``custom_var`` (AND-only, /hosts lookup).
    The ``since`` / ``until`` filter fields override the ``hours`` parameter.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_LOGS, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)
    if only_alerts:
        extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    if "time[gte]" not in extra:
        extra["time[gte]"] = f"-{hours}h"
    data, warnings = await _fetch_logs(
        "/logs",
        None,
        None,
        None,
        None,
        None,
        limit,
        offset,
        "-time",
        columns,
        backends,
        extra=extra,
    )
    if host_truncated:
        warnings = [
            *warnings,
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete.",
        ]
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Security constants for thruk_query / thruk_run_background_query validation
# ---------------------------------------------------------------------------

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
        return json.dumps(
            {"error": (f"Invalid path: must start with '/'. Got: {path!r}")},
            indent=2,
        )
    if ".." in path:
        return json.dumps(
            {"error": (f"Invalid path: must not contain '..'. Got: {path!r}")},
            indent=2,
        )
    if not any(path.startswith(p) for p in _REST_PATH_PREFIXES):
        return json.dumps(
            {
                "error": (
                    f"Path {path!r} does not start with a known Thruk REST prefix. "
                    f"Allowed prefixes: {sorted(_REST_PATH_PREFIXES)}"
                )
            },
            indent=2,
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
        return json.dumps(
            {"error": (f"Invalid HTTP method {method!r}. Allowed: {sorted(_ALLOWED_METHODS)}")},
            indent=2,
        )
    if _get_client().config.read_only and method_upper not in {"GET", "HEAD"}:
        return json.dumps(
            {
                "error": (
                    f"thruk_query: method {method_upper!r} blocked by THRUK_READ_ONLY=true. "
                    "Only GET and HEAD are permitted in read-only mode."
                )
            },
            indent=2,
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
        return json.dumps({"_warning": _CV_Q_WARNING, "data": result}, indent=2, default=str)
    return json.dumps(result, indent=2, default=str)


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
        return json.dumps(
            {"error": (f"Invalid HTTP method {method!r}. Allowed: {sorted(_ALLOWED_METHODS)}")},
            indent=2,
        )
    # Defense-in-depth: thruk_run_background_query is already removed from the
    # registry when read_only=True (is_write=True in ToolSpec), but guard the
    # function body as well to prevent bypasses via direct calls or future
    # refactors that re-expose the tool.
    if _get_client().config.read_only and method_upper not in {"GET", "HEAD"}:
        return json.dumps(
            {
                "error": (
                    f"thruk_run_background_query: method {method_upper!r} blocked by "
                    "THRUK_READ_ONLY=true. Only GET and HEAD are permitted in read-only mode."
                )
            },
            indent=2,
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
    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Write tools (module-level)
# ---------------------------------------------------------------------------


async def thruk_schedule_downtime(
    host: str,
    service: str | None = None,
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a host or service downtime. Time accepts 'now', relative ('+2h', '+30m')
    or ISO 8601. If `duration_minutes` is set it overrides `end_time`."""
    if duration_minutes:
        end_time = f"+{duration_minutes}m"
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/schedule_svc_downtime"
        if service
        else f"/hosts/{_seg(host)}/cmd/schedule_host_downtime"
    )
    payload = {
        "start_time": start_time,
        "end_time": end_time,
        "comment_data": comment,
        "comment_author": author,
        "fixed": "1" if fixed else "0",
    }
    return json.dumps(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends)),
        indent=2,
        default=str,
    )


async def thruk_acknowledge(
    host: str,
    service: str | None = None,
    comment: str = "acknowledged via MCP",
    author: str = "thruk-mcp",
    sticky: bool = True,
    notify: bool = True,
    persistent: bool = False,
    backends: str | None = None,
) -> str:
    """Acknowledge a host or service problem."""
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/acknowledge_svc_problem"
        if service
        else f"/hosts/{_seg(host)}/cmd/acknowledge_host_problem"
    )
    payload = {
        "comment_data": comment,
        "comment_author": author,
        "sticky_ack": "1" if sticky else "0",
        "send_notification": "1" if notify else "0",
        "persistent_comment": "1" if persistent else "0",
    }
    return json.dumps(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends)),
        indent=2,
        default=str,
    )


async def thruk_remove_acknowledgement(
    host: str, service: str | None = None, backends: str | None = None
) -> str:
    """Remove an acknowledgement."""
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/remove_svc_acknowledgement"
        if service
        else f"/hosts/{_seg(host)}/cmd/remove_host_acknowledgement"
    )
    return json.dumps(
        await _get_client().post(endpoint, backends=_backends(backends)),
        indent=2,
        default=str,
    )


async def thruk_recheck(
    host: str, service: str | None = None, forced: bool = True, backends: str | None = None
) -> str:
    """Schedule an immediate (re)check for a host or service."""
    if service:
        cmd = "schedule_forced_svc_check" if forced else "schedule_svc_check"
        endpoint = f"/services/{_seg(host)}/{_seg(service)}/cmd/{cmd}"
    else:
        cmd = "schedule_forced_host_check" if forced else "schedule_host_check"
        endpoint = f"/hosts/{_seg(host)}/cmd/{cmd}"
    return json.dumps(
        await _get_client().post(
            endpoint, data={"start_time": "now"}, backends=_backends(backends)
        ),
        indent=2,
        default=str,
    )


async def thruk_delete_downtime(
    downtime_id: int, host: str, service: str | None = None, backends: str | None = None
) -> str:
    """Delete a host or service downtime by its id.

    If `service` is omitted, the tool fetches the downtime object first
    (`GET /downtimes/{id}`) to determine whether it belongs to a host or a
    service, then routes to the correct Thruk REST endpoint
    (`/hosts/.../cmd/del_downtime` vs `/services/.../cmd/del_downtime`).
    Providing `service` explicitly skips that extra round-trip.
    """
    client = _get_client()
    be = _backends(backends)

    # Auto-detect downtime type when service is not provided to avoid silently
    # hitting the host endpoint on a service downtime (no-op with misleading
    # "Command successfully submitted" response — see issue #35).
    if service is None:
        dt = await client.get(f"/downtimes/{_seg(str(downtime_id))}", backends=be)
        svc_desc = dt.get("service_description") if isinstance(dt, dict) else None
        service = svc_desc or None

    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/del_downtime"
        if service
        else f"/hosts/{_seg(host)}/cmd/del_downtime"
    )
    return json.dumps(
        await client.post(endpoint, data={"downtime_id": str(downtime_id)}, backends=be),
        indent=2,
        default=str,
    )


async def thruk_get_downtime(downtime_id: int, backends: str | None = None) -> str:
    """Get a single downtime by id."""
    data = await _get_client().get(
        f"/downtimes/{_seg(str(downtime_id))}", backends=_backends(backends)
    )
    return json.dumps(data, indent=2, default=str)


async def thruk_schedule_host_services_downtime(
    host: str,
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on ALL services of the given host
    (schedule_host_svc_downtime). Use thruk_schedule_downtime for the host
    itself or for one specific service."""
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return json.dumps(
        await _get_client().post(
            f"/hosts/{_seg(host)}/cmd/schedule_host_svc_downtime",
            data=payload,
            backends=_backends(backends),
        ),
        indent=2,
        default=str,
    )


async def thruk_schedule_propagated_host_downtime(
    host: str,
    triggered: bool = False,
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on a host and propagate to all child hosts.
    If `triggered=True`, child downtimes are triggered by the parent (start
    when the parent enters its downtime). Useful for a parent network
    device whose children should automatically follow."""
    cmd = (
        "schedule_and_propagate_triggered_host_downtime"
        if triggered
        else "schedule_and_propagate_host_downtime"
    )
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return json.dumps(
        await _get_client().post(
            f"/hosts/{_seg(host)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        ),
        indent=2,
        default=str,
    )


async def thruk_schedule_hostgroup_downtime(
    hostgroup: str,
    target: str = "hosts",
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime for every host (`target='hosts'`, default) or
    every service (`target='services'`) of a hostgroup."""
    cmd = (
        "schedule_hostgroup_svc_downtime"
        if target == "services"
        else "schedule_hostgroup_host_downtime"
    )
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return json.dumps(
        await _get_client().post(
            f"/hostgroups/{_seg(hostgroup)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        ),
        indent=2,
        default=str,
    )


async def thruk_schedule_servicegroup_downtime(
    servicegroup: str,
    target: str = "services",
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on a servicegroup. `target='services'` (default)
    targets all services in the group; `target='hosts'` targets the hosts
    owning those services."""
    cmd = (
        "schedule_servicegroup_host_downtime"
        if target == "hosts"
        else "schedule_servicegroup_svc_downtime"
    )
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return json.dumps(
        await _get_client().post(
            f"/servicegroups/{_seg(servicegroup)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        ),
        indent=2,
        default=str,
    )


async def thruk_delete_active_downtimes(
    host: str,
    service: str | None = None,
    backends: str | None = None,
) -> str:
    """Remove ALL currently active downtimes for a host (or one specific
    service when `service` is given). Fetches all active downtime IDs first,
    then submits one DEL_*_DOWNTIME per ID. Partial failures are reported
    individually in `errors` instead of aborting the whole batch."""
    client = _get_client()
    be = _backends(backends)

    # Query active downtimes: started and not yet ended (same logic as thruk_list_downtimes).
    now = _now_utc_epoch()
    params: dict[str, Any] = {
        "host_name": host,
        "start_time[lte]": now,
        "end_time[gte]": now,
        "columns": "id,service_description,author,comment",
    }
    if service:
        params["service_description"] = service

    raw = await client.get("/downtimes", params=params, backends=be)
    all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])

    # Keep only the right type: host-level (empty service_desc) or the requested service.
    if service:
        downtimes = [d for d in all_dts if d.get("service_description") == service]
    else:
        downtimes = [d for d in all_dts if not d.get("service_description")]

    if not downtimes:
        return json.dumps(
            {"deleted": [], "errors": [], "count": 0, "message": "No active downtimes found."},
            indent=2,
        )

    # Thruk REST exposes only `del_downtime` (not `del_svc_downtime` /
    # `del_host_downtime`) — the correct Nagios external command is inferred
    # from the resource path (issue #36).
    ep = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/del_downtime"
        if service
        else f"/hosts/{_seg(host)}/cmd/del_downtime"
    )

    # Issue #141: parallelise all per-id DEL requests via asyncio.gather so that
    # N downtimes complete in ~1 RTT instead of N RTTs.  Error isolation is
    # preserved: each coroutine catches ThrukError and returns it as a value so
    # that a failure on one id never aborts the rest.
    async def _del_one(dt_id: int) -> tuple[int, Any, ThrukError | None]:
        try:
            resp = await client.post(ep, data={"downtime_id": dt_id}, backends=be)
            return dt_id, resp, None
        except ThrukError as exc:
            return dt_id, None, exc

    ids = [d["id"] for d in downtimes if d.get("id") is not None]
    _gather_results: list[tuple[int, Any, ThrukError | None]] = list(
        await asyncio.gather(*(_del_one(i) for i in ids))
    )
    deleted = [{"downtime_id": i, "result": r} for i, r, e in _gather_results if e is None]
    errors = [{"downtime_id": i, "error": str(e)} for i, _, e in _gather_results if e is not None]

    return json.dumps(
        {"deleted": deleted, "errors": errors, "count": len(deleted)},
        indent=2,
        default=str,
    )


async def thruk_delete_downtimes_by_filter(
    host: str | None = None,
    hostgroup: str | None = None,
    service: str | None = None,
    start_time: str | None = None,
    comment: str | None = None,
    backends: str | None = None,
) -> str:
    """Bulk-delete downtimes matching arbitrary filters via system commands.

    Uses `del_downtime_by_hostgroup_name`, `del_downtime_by_host_name`, or
    `del_downtime_by_start_time_comment` depending on the most specific filter.

    **Known Naemon limitation**: `DEL_DOWNTIME_BY_HOST_NAME` only covers
    service-level downtimes. When filtering by `host` (without `hostgroup`),
    this tool additionally enumerates and deletes matching host-level downtimes
    via explicit `DEL_HOST_DOWNTIME` commands. Those results appear in the
    `host_downtimes_deleted` / `host_downtimes_errors` keys of the response.

    At least one of `host`, `hostgroup`, `service`, `start_time` or `comment`
    must be provided."""
    client = _get_client()
    be = _backends(backends)

    payload: dict[str, str] = {}
    if host:
        payload["hostname"] = host
    if hostgroup:
        payload["hostgroup_name"] = hostgroup
    if service:
        payload["service_desc"] = service
    if start_time:
        payload["start_time"] = start_time
    if comment:
        payload["comment"] = comment
    if not payload:
        raise ThrukError("Provide at least one of host, hostgroup, service, start_time, comment.")
    if hostgroup:
        cmd = "del_downtime_by_hostgroup_name"
    elif host:
        cmd = "del_downtime_by_host_name"
    else:
        cmd = "del_downtime_by_start_time_comment"

    cmd_result = await client.post(f"/system/cmd/{cmd}", data=payload, backends=be)
    result: dict[str, Any] = {"system_command": cmd_result}

    # DEL_DOWNTIME_BY_HOST_NAME (Naemon) only targets service downtimes.
    # Enumerate + delete host-level downtimes explicitly when filtering by host.
    if host and not hostgroup:
        dt_params: dict[str, Any] = {
            "host_name": host,
            "columns": "id,service_description,comment,start_time",
        }
        if comment:
            dt_params["comment"] = comment
        if start_time:
            dt_params["start_time"] = start_time

        raw = await client.get("/downtimes", params=dt_params, backends=be)
        all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])
        # Host-level downtimes have an empty service_description.
        host_dts = [d for d in all_dts if not d.get("service_description")]

        # Issue #141: same gather pattern — all host-level DEL requests fire
        # concurrently rather than being serialised.
        async def _del_host_one(dt_id: int) -> tuple[int, Any, ThrukError | None]:
            try:
                resp = await client.post(
                    f"/hosts/{_seg(host)}/cmd/del_downtime",
                    data={"downtime_id": dt_id},
                    backends=be,
                )
                return dt_id, resp, None
            except ThrukError as exc:
                return dt_id, None, exc

        host_ids = [d["id"] for d in host_dts if d.get("id") is not None]
        _host_gather: list[tuple[int, Any, ThrukError | None]] = list(
            await asyncio.gather(*(_del_host_one(i) for i in host_ids))
        )
        host_deleted = [{"downtime_id": i, "result": r} for i, r, e in _host_gather if e is None]
        host_errors = [
            {"downtime_id": i, "error": str(e)} for i, _, e in _host_gather if e is not None
        ]

        result["host_downtimes_deleted"] = host_deleted
        result["host_downtimes_errors"] = host_errors

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Resources (module-level)
# ---------------------------------------------------------------------------


async def _host_resource(name: str) -> str:
    """Single host as a JSON document, addressable as thruk://hosts/<name>."""
    data = await _get_client().get(f"/hosts/{_seg(name)}")
    return json.dumps(data, indent=2, default=str)


async def _service_resource(host: str, service: str) -> str:
    """Single service as a JSON document (thruk://services/<host>/<service>)."""
    data = await _get_client().get(f"/services/{_seg(host)}/{_seg(service)}")
    return json.dumps(data, indent=2, default=str)


async def _hostgroup_resource(name: str) -> str:
    """Host group config + members as JSON (thruk://hostgroups/<name>)."""
    data = await _get_client().get(f"/hostgroups/{_seg(name)}")
    return json.dumps(data, indent=2, default=str)


async def _problems_resource() -> str:
    """Current unhandled host/service problems as a JSON document."""
    host_params = {
        "state": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "columns": DEFAULT_HOST_COLUMNS,
        "limit": 500,
    }
    svc_params = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "columns": DEFAULT_SERVICE_COLUMNS,
        "limit": 500,
    }
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts", params=host_params),
        _get_client().get("/services", params=svc_params),
    )
    return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)


async def _stats_resource() -> str:
    """Aggregated host/service stats (cached ~15s)."""
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/stats"),
        _get_client().get("/services/stats"),
    )
    return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)


# ---------------------------------------------------------------------------
# Prompts (module-level)
# ---------------------------------------------------------------------------


def investigate_alert(host: str, service: str | None = None) -> str:
    target = f"host '{host}'" if not service else f"service '{service}' on host '{host}'"
    steps = "\n".join(
        [
            f"1. Fetch the current state of {target} using `thruk_get_host`"
            + ("/`thruk_get_service`" if service else ""),
            "2. Pull the recent alert history via `thruk_list_alerts` (last 6h)",
            "3. Check notifications sent via `thruk_list_notifications`",
            "4. Inspect related comments and acknowledgements with `thruk_list_comments`",
            "5. Verify there is no active downtime via `thruk_list_downtimes`",
            "6. Summarise root-cause hypotheses and propose 2-3 remediation steps",
            "7. If the operator confirms, acknowledge with `thruk_acknowledge` "
            "and/or trigger a forced recheck with `thruk_recheck`.",
        ]
    )
    return (
        f"You are the on-call SRE assistant. The user wants to investigate the "
        f"current alert on {target}. Proceed methodically:\n\n{steps}\n\n"
        "Do not modify the monitoring state without explicit user confirmation."
    )


def schedule_maintenance(target: str, duration_minutes: int = 120, kind: str = "hostgroup") -> str:
    kind = kind.lower()
    if kind not in {"host", "service", "hostgroup", "servicegroup"}:
        kind = "hostgroup"
    tool_map = {
        "host": "thruk_schedule_downtime",
        "service": "thruk_schedule_downtime",
        "hostgroup": "thruk_schedule_hostgroup_downtime",
        "servicegroup": "thruk_schedule_servicegroup_downtime",
    }
    return (
        f"The user wants to schedule {duration_minutes} minutes of maintenance "
        f"on the {kind} '{target}'.\n\n"
        f"1. Confirm the {kind} exists by listing it (e.g. `thruk_list_{kind}s` "
        "or `thruk_get_host`).\n"
        "2. Show the user the list of impacted hosts/services.\n"
        "3. Ask explicit confirmation before applying.\n"
        f"4. On 'yes', call `{tool_map[kind]}` with "
        f"duration_minutes={duration_minutes} and a clear comment explaining the reason.\n"
        "5. Verify the downtime is active via `thruk_list_downtimes`.\n"
    )


def diagnose_flapping(host: str, service: str) -> str:
    return (
        f"The user reports that service '{service}' on host '{host}' is flapping. "
        "Carry out a focused investigation:\n\n"
        "1. `thruk_get_service` to confirm state and current `is_flapping` flag.\n"
        "2. `thruk_list_alerts` for the same host/service over the last 24h, "
        "sorted -time, to count state transitions.\n"
        "3. `thruk_list_logs` filtered on `message_regex='flapp'` to confirm "
        "flap-detection events.\n"
        "4. If perf-data is available in the service row, inspect the metric "
        "that is oscillating (rta, latency, queue depth, ...).\n"
        "5. Summarise likely causes (network jitter, threshold too tight, "
        "passive check freshness, ...).\n"
        "6. Propose remediation: widen warning/critical thresholds, increase "
        "max_check_attempts, disable flap detection if intentional, or add a "
        "downtime while a fix is rolled out.\n"
        "7. Do not change Thruk state without confirmation."
    )


# ---------------------------------------------------------------------------
# Semantic problem-management tools (issue #52)
# ---------------------------------------------------------------------------


async def thruk_oldest_problems(
    limit: int = 20,
    backends: str | None = None,
) -> str:
    """Unhandled problems sorted by age (oldest first).

    Combines DOWN/UNREACHABLE hosts and WARNING/CRITICAL/UNKNOWN services that
    are neither acknowledged nor in scheduled downtime. Results are merged and
    sorted by ``last_state_change`` ascending so the longest-standing problems
    appear first.

    Returns a flat list of ``{host, service, state, since, duration_human}``
    (at most ``limit`` items, default 20).
    """
    now = _now_utc_epoch()
    host_params = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "sort": "last_state_change",
        "columns": "name,state,last_state_change,peer_name",
        "limit": limit,
    }
    svc_params = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "sort": "last_state_change",
        "columns": "host_name,description,state,last_state_change,peer_name",
        "limit": limit,
    }
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts", params=host_params, backends=be),
        _get_client().get("/services", params=svc_params, backends=be),
    )

    rows: list[dict[str, Any]] = []
    for h in hosts or []:
        lsc = int(h.get("last_state_change") or 0)
        rows.append(
            {
                "_lsc": lsc,
                "host": h.get("name", ""),
                "service": None,
                "state": HOST_STATES.get(int(h.get("state", -1)), str(h.get("state", ""))),
                "since": _ts(lsc),
                "duration_human": _duration_human(now - lsc),
            }
        )
    for s in services or []:
        lsc = int(s.get("last_state_change") or 0)
        rows.append(
            {
                "_lsc": lsc,
                "host": s.get("host_name", ""),
                "service": s.get("description", ""),
                "state": SERVICE_STATES.get(int(s.get("state", -1)), str(s.get("state", ""))),
                "since": _ts(lsc),
                "duration_human": _duration_human(now - lsc),
            }
        )

    rows.sort(key=lambda r: r["_lsc"])
    trimmed = [{k: v for k, v in r.items() if k != "_lsc"} for r in rows[:limit]]
    return json.dumps(trimmed, indent=2, default=str)


async def thruk_unacked_critical(
    threshold_minutes: int = 60,
    backends: str | None = None,
) -> str:
    """CRITICAL services and DOWN hosts not acknowledged for more than N minutes.

    ``threshold_minutes`` (default 60) sets the minimum duration a problem must
    have been active without acknowledgement to be included.

    Returns ``[{host, service, state, duration_minutes}]`` sorted by
    ``duration_minutes`` descending (longest-unacked first).
    """
    now = _now_utc_epoch()
    threshold_ts = now - threshold_minutes * 60

    host_params = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "last_state_change[lte]": threshold_ts,
        "columns": "name,state,last_state_change,peer_name",
        "limit": 500,
    }
    svc_params = {
        "state": 2,  # CRITICAL only
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "last_state_change[lte]": threshold_ts,
        "columns": "host_name,description,state,last_state_change,peer_name",
        "limit": 500,
    }
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts", params=host_params, backends=be),
        _get_client().get("/services", params=svc_params, backends=be),
    )

    rows: list[dict[str, Any]] = []
    for h in hosts or []:
        lsc = int(h.get("last_state_change") or 0)
        rows.append(
            {
                "host": h.get("name", ""),
                "service": None,
                "state": HOST_STATES.get(int(h.get("state", -1)), str(h.get("state", ""))),
                "duration_minutes": (now - lsc) // 60,
            }
        )
    for s in services or []:
        lsc = int(s.get("last_state_change") or 0)
        rows.append(
            {
                "host": s.get("host_name", ""),
                "service": s.get("description", ""),
                "state": SERVICE_STATES.get(int(s.get("state", -1)), str(s.get("state", ""))),
                "duration_minutes": (now - lsc) // 60,
            }
        )

    rows.sort(key=lambda r: r["duration_minutes"], reverse=True)
    return json.dumps(rows, indent=2, default=str)


async def thruk_stale_acks(
    min_days: int = 7,
    limit: int = 100,
    backends: str | None = None,
) -> str:
    """Acknowledgements older than N days (potentially forgotten ones).

    Queries ``/comments`` for ``entry_type=4`` (acknowledgements) and
    returns entries whose ``entry_time`` is older than ``min_days`` days
    (default 7). Useful for identifying problems that have been silenced
    but never actually fixed.

    Returns ``[{host, service, ack_author, ack_comment, ack_since_days}]``
    sorted by age descending (stalest first).
    """
    now = _now_utc_epoch()
    threshold_ts = now - min_days * 86400

    params: dict[str, Any] = {
        "entry_type": 4,
        "entry_time[lte]": threshold_ts,
        "columns": "host_name,service_description,author,comment,entry_time,peer_name",
        "limit": limit,
        "sort": "entry_time",
    }
    data = await _get_client().get("/comments", params=params, backends=_backends(backends))

    rows: list[dict[str, Any]] = []
    for c in data or []:
        et = int(c.get("entry_time") or 0)
        rows.append(
            {
                "host": c.get("host_name", ""),
                "service": c.get("service_description") or None,
                "ack_author": c.get("author", ""),
                "ack_comment": c.get("comment", ""),
                "ack_since_days": round((now - et) / 86400, 1),
            }
        )

    rows.sort(key=lambda r: r["ack_since_days"], reverse=True)
    return json.dumps(rows, indent=2, default=str)


async def thruk_problems_by_hostgroup(
    backends: str | None = None,
) -> str:
    """Problem count aggregated per hostgroup.

    Returns ``[{hostgroup, alias, hosts_down, services_crit, services_warn,
    services_unknown}]`` sorted by severity (DOWN > CRIT > WARN). Only groups
    with at least one problem are included.
    """
    params: dict[str, Any] = {
        "columns": (
            "name,alias,"
            "num_hosts_down,num_hosts_unreachable,"
            "num_services_warn,num_services_crit,num_services_unknown"
        ),
    }
    data = await _get_client().get("/hostgroups", params=params, backends=_backends(backends))

    rows: list[dict[str, Any]] = []
    for hg in data or []:
        hosts_down = int(hg.get("num_hosts_down") or 0) + int(hg.get("num_hosts_unreachable") or 0)
        services_crit = int(hg.get("num_services_crit") or 0)
        services_warn = int(hg.get("num_services_warn") or 0)
        services_unknown = int(hg.get("num_services_unknown") or 0)
        total = hosts_down + services_crit + services_warn + services_unknown
        if total == 0:
            continue
        rows.append(
            {
                "hostgroup": hg.get("name", ""),
                "alias": hg.get("alias", ""),
                "hosts_down": hosts_down,
                "services_crit": services_crit,
                "services_warn": services_warn,
                "services_unknown": services_unknown,
            }
        )

    rows.sort(
        key=lambda r: r["hosts_down"] * 10000 + r["services_crit"] * 100 + r["services_warn"],
        reverse=True,
    )
    return json.dumps(rows, indent=2, default=str)


async def thruk_concurrent_failures(
    since: str | None = "-1h",
    until: str | None = None,
    window_minutes: int = 5,
    min_hosts: int = 3,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Detect time windows where multiple hosts failed concurrently.

    Fetches HOST ALERT DOWN/UNREACHABLE log entries in the given time range,
    applies a sliding window of ``window_minutes``, and returns merged bursts
    where at least ``min_hosts`` distinct hosts failed. Useful for detecting
    network incidents or datacenter outages.

    ``since`` / ``until`` accept Thruk relative times (``"-2h"``, ``"-7d"``)
    or ISO datetime strings (``"2026-05-20 14:00:00"``). Defaults to last 1 hour.

    ``filter`` fields: ``host`` (eq/regex), ``hostgroup``, ``custom_var``
    (host-level Nagios variable, resolved via /hosts lookup).

    Returns a wrapped object:
    ``since``, ``until``, ``window_minutes``, ``min_hosts``,
    ``total_down_events``, ``results`` -- list of merged burst windows sorted by
    start time, each with ``window_start``, ``window_end``, ``hosts`` (sorted
    list of distinct host names), ``count``.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_HOSTS, backends)
    if errs:
        return json.dumps({"error": errs[0]}, indent=2)

    extra["type[~]"] = "^HOST ALERT"
    extra["state[gte]"] = "1"  # exclude state 0 (UP / recovery)
    if "time[gte]" not in extra and since:
        extra["time[gte]"] = since
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = until

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "time",  # ascending -- needed for the sliding window scan
        "columns": "host_name,state,time",
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    # Build a sorted (ascending) list of (timestamp, host_name) -- valid entries only
    events: list[tuple[int, str]] = sorted(
        [
            (int(e["time"]), str(e["host_name"]))
            for e in data
            if e.get("time") and e.get("host_name")
        ],
        key=lambda x: x[0],
    )

    window_secs = window_minutes * 60

    # Sliding-window scan — O(n log n) overall, O(n) window pass.
    #
    # Previous implementation (O(n²), issue #86):
    #   for i in range(n):
    #       t_anchor = events[i][0]
    #       t_end = t_anchor + window_secs
    #       hosts_in_window: set[str] = set()
    #       for j in range(i, n):           # ← inner O(n) scan per anchor
    #           if events[j][0] > t_end:
    #               break
    #           hosts_in_window.add(events[j][1])
    #
    # New implementation:
    #   - A right-anchored deque tracks events in [ts - window_secs, ts].
    #     Each event enters/leaves the deque at most once → O(n) pointer moves.
    #   - A Counter tracks distinct hosts so that len(host_counts) is O(1)
    #     instead of recomputing a set comprehension over the whole deque on
    #     every iteration (which would re-introduce O(n²) behaviour when all
    #     events fall in the same window).
    #   - Capturing the host snapshot for hit_windows is O(k) where k is the
    #     number of *distinct* hosts in the window — bounded by min(n, num_hosts).
    hit_windows: list[dict[str, Any]] = []
    window_dq: deque[tuple[int, str]] = deque()
    host_counts: Counter[str] = Counter()

    for ts, host in events:
        window_dq.append((ts, host))
        host_counts[host] += 1
        # Evict events that have fallen outside the left edge of the window
        while window_dq and window_dq[0][0] < ts - window_secs:
            old_host = window_dq.popleft()[1]
            host_counts[old_host] -= 1
            if host_counts[old_host] == 0:
                del host_counts[old_host]
        if len(host_counts) >= min_hosts:
            hit_windows.append({"start": window_dq[0][0], "end": ts, "hosts": set(host_counts)})

    # Merge overlapping hit windows into bursts
    merged: list[dict[str, Any]] = []
    for w in hit_windows:
        if merged and w["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], w["end"])
            merged[-1]["hosts"] |= w["hosts"]
        else:
            merged.append({"start": w["start"], "end": w["end"], "hosts": set(w["hosts"])})

    results = [
        {
            "window_start": _ts(m["start"]),
            "window_end": _ts(m["end"]),
            "hosts": sorted(m["hosts"]),
            "count": len(m["hosts"]),
        }
        for m in merged
    ]

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "window_minutes": window_minutes,
        "min_hosts": min_hosts,
        "total_down_events": len(events),
        "results": results,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif len(data) >= _NOISY_MAX_ALERTS:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; detection may be incomplete."
        )
    if warnings:
        payload["_warnings"] = warnings
    return json.dumps(payload, indent=2, default=str)


# ---------------------------------------------------------------------------
# build_server: registers module-level functions into a fresh FastMCP instance
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Explicit JSON Schemas — no annotation introspection, no Pydantic
# ---------------------------------------------------------------------------


def _s(*required: str, **props: Any) -> dict[str, Any]:
    """Shorthand to build a JSON-Schema object."""
    properties = {k: (v if isinstance(v, dict) else {"type": v}) for k, v in props.items()}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _str(desc: str = "") -> dict[str, Any]:
    return {"type": "string", "description": desc} if desc else {"type": "string"}


def _int(desc: str = "", default: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "integer"}
    if desc:
        d["description"] = desc
    if default is not None:
        d["default"] = default
    return d


def _bool(desc: str = "", default: bool | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "boolean"}
    if desc:
        d["description"] = desc
    if default is not None:
        d["default"] = default
    return d


_OPT_STR = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
_OPT_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
_OPT_BOOL = {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": None}
_OPT_OBJ = {"anyOf": [{"type": "object"}, {"type": "null"}], "default": None}
# Reusable schema fragment for log-family host-resolution filters.
_LOG_HOSTGROUP = {
    **_OPT_STR,
    "description": (
        "Filter to hosts belonging to this hostgroup. Resolved via a /hosts lookup "
        "then host_name[regex] — works on all backends (log table has no group column)."
    ),
}
_LOG_CUSTOM_VARS = {
    **_OPT_OBJ,
    "description": (
        'Filter by host-level Nagios custom variables, e.g. {"KERNEL": "windows"}. '
        "Resolved via a /hosts lookup then host_name[regex] — the log table does not "
        "expose custom-variable columns directly."
    ),
}
_BACKENDS = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": None,
    "description": "Comma-separated backend names (sites). Omit for all backends.",
}


# ---------------------------------------------------------------------------
# ToolSpec: unified tool registration (issue #85)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for a registered MCP tool.

    Ties together the tool name, its async implementation, the explicit JSON
    Schema for its input, and whether it mutates monitoring state (``is_write``).

    Downstream structures are auto-derived — never edit them by hand:
    - ``_TOOL_DISPATCH``  = {spec.name: spec.fn   for spec in TOOL_REGISTRY}
    - ``_TOOL_SCHEMAS``   = {spec.name: spec.schema for spec in TOOL_REGISTRY}
    - ``WRITE_TOOLS``     = frozenset(spec.name for spec in TOOL_REGISTRY if spec.is_write)

    Adding a new tool requires exactly one entry here; ``WRITE_TOOLS`` cannot
    fall out of sync with the schema or dispatch table.
    """

    name: str
    fn: Callable[..., Coroutine[Any, Any, str]]
    schema: dict[str, Any]
    is_write: bool = False


# ---------------------------------------------------------------------------
# TOOL_REGISTRY: one entry per tool (issue #85)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolSpec] = [
    # ---------------------------------------------------------------- noisy / flap
    ToolSpec(
        name="thruk_top_noisy_hosts",
        fn=thruk_top_noisy_hosts,
        schema=build_tool_schema(
            FIELDS_NOISY_HOSTS,
            filter=filter_schema_property(FIELDS_NOISY_HOSTS),
            hours=_int(default=24),
            limit=_int(default=10),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_top_noisy_services",
        fn=thruk_top_noisy_services,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            filter=filter_schema_property(FIELDS_NOISY_SERVICES),
            hours=_int(default=24),
            limit=_int(default=10),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_flap_summary",
        fn=thruk_flap_summary,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            filter=filter_schema_property(FIELDS_NOISY_SERVICES),
            hours=_int(default=24),
            limit=_int(default=10),
            min_transitions=_int(default=3),
            backends=_BACKENDS,
        ),
    ),
    # ---------------------------------------------------------------- trends & history (issue #57)
    ToolSpec(
        name="thruk_alert_heatmap",
        fn=thruk_alert_heatmap,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            filter=filter_schema_property(FIELDS_NOISY_SERVICES),
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-24h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-21 14:00:00"). Default: last 24 h.'
                ),
            },
            until=_OPT_STR,
            bucket={
                "type": "string",
                "default": "1h",
                "description": "Time bucket width: '15m', '30m', '1h' (default), '6h', '1d'.",
                "enum": ["15m", "30m", "1h", "6h", "1d"],
            },
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_recurring_problems",
        fn=thruk_recurring_problems,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            filter=filter_schema_property(FIELDS_NOISY_SERVICES),
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-24h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-21 14:00:00"). Default: last 24 h.'
                ),
            },
            until=_OPT_STR,
            min_alerts=_int(
                "Minimum number of non-recovery alert events to be included (default 5).",
                default=5,
            ),
            limit=_int("Maximum number of results (default 10).", default=10),
            backends=_BACKENDS,
        ),
    ),
    # ---------------------------------------------------------------- host / service listing
    ToolSpec(
        name="thruk_list_hosts",
        fn=thruk_list_hosts,
        schema=build_tool_schema(
            FIELDS_HOSTS,
            filter=filter_schema_property(FIELDS_HOSTS),
            limit=_int(default=50),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_get_host",
        fn=thruk_get_host,
        schema=_s("host", host=_str("Host name"), backends=_BACKENDS),
    ),
    ToolSpec(
        name="thruk_list_services",
        fn=thruk_list_services,
        schema=build_tool_schema(
            FIELDS_SERVICES,
            filter=filter_schema_property(FIELDS_SERVICES),
            limit=_int(default=50),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_get_service",
        fn=thruk_get_service,
        schema=_s(
            "host",
            "service",
            host=_str("Host name"),
            service=_str("Service description"),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_hostgroups",
        fn=thruk_list_hostgroups,
        schema=_s(
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_servicegroups",
        fn=thruk_list_servicegroups,
        schema=_s(
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_problems",
        fn=thruk_problems,
        schema=build_tool_schema(
            FIELDS_PROBLEMS,
            filter=filter_schema_property(FIELDS_PROBLEMS),
            limit=_int(default=100),
            offset=_int(default=0),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(name="thruk_stats", fn=thruk_stats, schema=_s(backends=_BACKENDS)),
    # ---------------------------------------------------------------- downtime / comment
    ToolSpec(
        name="thruk_list_downtimes",
        fn=thruk_list_downtimes,
        schema=_s(
            host=_OPT_STR,
            active_only=_bool(default=True),
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_get_downtime",
        fn=thruk_get_downtime,
        schema=_s("downtime_id", downtime_id=_int(), backends=_BACKENDS),
    ),
    ToolSpec(
        name="thruk_list_comments",
        fn=thruk_list_comments,
        schema=_s(
            host=_OPT_STR,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(name="thruk_sites", fn=thruk_sites, schema=_s()),
    # ---------------------------------------------------------------- log / alert / notification
    ToolSpec(
        name="thruk_list_logs",
        fn=thruk_list_logs,
        schema=build_tool_schema(
            FIELDS_LOGS,
            filter=filter_schema_property(FIELDS_LOGS),
            since=_OPT_STR,
            until=_OPT_STR,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_alerts",
        fn=thruk_list_alerts,
        schema=build_tool_schema(
            FIELDS_ALERTS,
            filter=filter_schema_property(FIELDS_ALERTS),
            since=_OPT_STR,
            until=_OPT_STR,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_notifications",
        fn=thruk_list_notifications,
        schema=build_tool_schema(
            FIELDS_NOTIFICATIONS,
            filter=filter_schema_property(FIELDS_NOTIFICATIONS),
            since=_OPT_STR,
            until=_OPT_STR,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_recent_events",
        fn=thruk_recent_events,
        schema=build_tool_schema(
            FIELDS_LOGS,
            filter=filter_schema_property(FIELDS_LOGS),
            hours=_int(default=1),
            only_alerts=_bool(default=False),
            limit=_int(default=100),
            offset=_int(default=0),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    # ---------------------------------------------------------------- raw query (read + write)
    ToolSpec(
        name="thruk_query",
        fn=thruk_query,
        schema=_s(
            "path",
            path=_str("Path after /thruk/r, e.g. /hosts/srv01/services"),
            method=_str(),
            params=_OPT_OBJ,
            data=_OPT_OBJ,
            backends=_BACKENDS,
        ),
        # thruk_query serves both reads (GET/HEAD) and writes (POST/PUT/DELETE).
        # It is intentionally NOT marked is_write=True here so it is never
        # stripped in read_only mode; _is_auditable_write() handles write-method
        # auditing at call-time by inspecting the `method` argument.
    ),
    ToolSpec(
        name="thruk_run_background_query",
        fn=thruk_run_background_query,
        schema=_s(
            "path",
            path=_str("Path after /thruk/r"),
            method=_str(),
            params=_OPT_OBJ,
            data=_OPT_OBJ,
            backends=_BACKENDS,
            poll_timeout={"type": "number", "default": 300.0},
        ),
        is_write=True,
    ),
    # ---------------------------------------------------------------- write: downtime scheduling
    ToolSpec(
        name="thruk_schedule_downtime",
        fn=thruk_schedule_downtime,
        schema=_s(
            "host",
            host=_str("Host name"),
            service=_OPT_STR,
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_host_services_downtime",
        fn=thruk_schedule_host_services_downtime,
        schema=_s(
            "host",
            host=_str("Host name"),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_propagated_host_downtime",
        fn=thruk_schedule_propagated_host_downtime,
        schema=_s(
            "host",
            host=_str("Host name"),
            triggered=_bool(default=False),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_hostgroup_downtime",
        fn=thruk_schedule_hostgroup_downtime,
        schema=_s(
            "hostgroup",
            hostgroup=_str("Hostgroup name"),
            target=_str(),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_servicegroup_downtime",
        fn=thruk_schedule_servicegroup_downtime,
        schema=_s(
            "servicegroup",
            servicegroup=_str("Servicegroup name"),
            target=_str(),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    # ---------------------------------------------------------------- write: downtime deletion
    ToolSpec(
        name="thruk_delete_downtime",
        fn=thruk_delete_downtime,
        schema=_s(
            "downtime_id",
            "host",
            downtime_id=_int(),
            host=_str(),
            service=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_delete_active_downtimes",
        fn=thruk_delete_active_downtimes,
        schema=_s(
            "host",
            host=_str(),
            service=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_delete_downtimes_by_filter",
        fn=thruk_delete_downtimes_by_filter,
        schema=_s(
            host=_OPT_STR,
            hostgroup=_OPT_STR,
            service=_OPT_STR,
            start_time=_OPT_STR,
            comment=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    # ---------------------------------------------------------------- write: ack / recheck
    ToolSpec(
        name="thruk_acknowledge",
        fn=thruk_acknowledge,
        schema=_s(
            "host",
            host=_str("Host name"),
            service=_OPT_STR,
            comment=_str(),
            author=_str(),
            sticky=_bool(default=True),
            notify=_bool(default=True),
            persistent=_bool(default=False),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_remove_acknowledgement",
        fn=thruk_remove_acknowledgement,
        schema=_s(
            "host",
            host=_str(),
            service=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_recheck",
        fn=thruk_recheck,
        schema=_s(
            "host",
            host=_str("Host name"),
            service=_OPT_STR,
            forced=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    # -------------------------------------------------------- semantic problem tools (issue #52)
    ToolSpec(
        name="thruk_oldest_problems",
        fn=thruk_oldest_problems,
        schema=_s(
            limit=_int("Maximum number of results (default 20).", default=20),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_unacked_critical",
        fn=thruk_unacked_critical,
        schema=_s(
            threshold_minutes=_int(
                "Minimum unacknowledged duration in minutes (default 60).", default=60
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_stale_acks",
        fn=thruk_stale_acks,
        schema=_s(
            min_days=_int("Minimum acknowledgement age in days (default 7).", default=7),
            limit=_int("Maximum number of results (default 100).", default=100),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_problems_by_hostgroup",
        fn=thruk_problems_by_hostgroup,
        schema=_s(backends=_BACKENDS),
    ),
    # -------------------------------------------------- concurrent failure detection (issue #54)
    ToolSpec(
        name="thruk_concurrent_failures",
        fn=thruk_concurrent_failures,
        schema=build_tool_schema(
            FIELDS_NOISY_HOSTS,
            filter=filter_schema_property(FIELDS_NOISY_HOSTS),
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-1h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-20 14:00:00"). Default: last 1 hour.'
                ),
            },
            until=_OPT_STR,
            window_minutes=_int("Sliding window width in minutes.", default=5),
            min_hosts=_int(
                "Minimum number of distinct hosts failing in a window to be reported.",
                default=3,
            ),
            backends=_BACKENDS,
        ),
    ),
]

# ---------------------------------------------------------------------------
# Derived structures — auto-generated from TOOL_REGISTRY; never edit manually
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, Any] = {spec.name: spec.fn for spec in TOOL_REGISTRY}
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {spec.name: spec.schema for spec in TOOL_REGISTRY}
# Tools that mutate monitoring state — used by read_only mode and the audit log.
WRITE_TOOLS: frozenset[str] = frozenset(spec.name for spec in TOOL_REGISTRY if spec.is_write)


# ---------------------------------------------------------------------------
# build_server: returns a low-level mcp.server.Server
# ---------------------------------------------------------------------------


class ThrukMCPServer:
    """Thin wrapper around mcp.server.Server that adds convenience methods
    (list_tools, call_tool) used in tests and the __main__ entry point.
    """

    def __init__(
        self,
        server: Server,
        enabled: dict[str, Any],
        client: ThrukClient,
        cfg: ThrukConfig,
    ) -> None:
        self._server = server
        self._enabled = enabled
        self._thruk_client = client
        self._cfg = cfg

    # --- Delegate MCP protocol methods to the wrapped Server ----------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    # --- Convenience methods (used by tests + __main__) ---------------------

    async def list_tools(self) -> list[Tool]:
        tools = []
        for name, fn in self._enabled.items():
            schema = _TOOL_SCHEMAS.get(name, {"type": "object", "properties": {}})
            tools.append(
                Tool(
                    name=name,
                    description=(fn.__doc__ or "").strip().split("\n")[0],
                    inputSchema=schema,
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> list[TextContent]:
        fn = self._enabled.get(name)
        if fn is None:
            raise ValueError(f"Unknown or disabled tool: {name!r}")
        try:
            result = await fn(**arguments)
        except TypeError as exc:
            if self._cfg.audit_log and _is_auditable_write(name, arguments):
                audit.log_call(
                    name, arguments, user=self._cfg.auth_user, status="error", error=str(exc)
                )
            raise ValueError(f"Invalid arguments for {name!r}: {exc}") from exc
        except (ThrukError, ValueError) as exc:
            if self._cfg.audit_log and _is_auditable_write(name, arguments):
                audit.log_call(
                    name, arguments, user=self._cfg.auth_user, status="error", error=str(exc)
                )
            # Return as tool-level error content instead of raising.
            # Raising here causes the low-level MCP SDK to emit a protocol-level
            # McpError(-32603) which the client shows as the generic
            # "tool execution failed" message, discarding the actual Thruk error.
            # ValueError is included as a defensive catch: tools that raise it
            # (e.g. validation guards before the fix for issue #71) must not
            # escape to the MCP protocol layer as an unhandled exception.
            return [TextContent(type="text", text=f"Error: {exc}")]
        if self._cfg.audit_log and _is_auditable_write(name, arguments):
            audit.log_call(name, arguments, user=self._cfg.auth_user, status="ok")
        return [TextContent(type="text", text=result)]

    async def run(self, read_stream: Any, write_stream: Any, init_options: Any = None) -> None:
        await self._server.run(read_stream, write_stream, init_options)

    def create_initialization_options(self) -> Any:
        return self._server.create_initialization_options()

    # --- Resources ---------------------------------------------------------

    async def read_resource(self, uri: AnyUrl) -> list[ReadResourceContents]:
        """Read a Thruk resource by URI, delegating to the module-level helpers.

        Handles:
          thruk://hosts/{name}
          thruk://services/{host}/{service}
          thruk://hostgroups/{name}
          thruk://problems
          thruk://stats
        """
        s = str(uri)
        if s.startswith("thruk://hosts/"):
            content = await _host_resource(s.split("/hosts/", 1)[1])
        elif s.startswith("thruk://services/"):
            parts = s.split("/services/", 1)[1].split("/", 1)
            content = await _service_resource(parts[0], parts[1])
        elif s.startswith("thruk://hostgroups/"):
            content = await _hostgroup_resource(s.split("/hostgroups/", 1)[1])
        elif s == "thruk://problems":
            content = await _problems_resource()
        elif s == "thruk://stats":
            content = await _stats_resource()
        else:
            raise ValueError(f"Unknown resource URI: {uri!r}")
        return [ReadResourceContents(content=content, mime_type="application/json")]

    async def list_resources(self) -> list[Resource]:
        """Return the two static Thruk resources exposed over MCP."""
        return [
            Resource(uri=AnyUrl("thruk://problems"), name="Current unhandled problems"),
            Resource(uri=AnyUrl("thruk://stats"), name="Aggregated host/service stats"),
        ]

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        """Return the parametric Thruk resource URI templates."""
        return [
            ResourceTemplate(uriTemplate="thruk://hosts/{name}", name="Thruk host"),
            ResourceTemplate(uriTemplate="thruk://services/{host}/{service}", name="Thruk service"),
            ResourceTemplate(uriTemplate="thruk://hostgroups/{name}", name="Thruk hostgroup"),
        ]

    # --- Prompts -----------------------------------------------------------

    async def list_prompts(self) -> list[Prompt]:
        """Return the three Thruk prompt templates."""
        return [
            Prompt(
                name="investigate_alert",
                description="Investigate a current alert on a host or service",
                arguments=[
                    PromptArgument(name="host", description="Host name", required=True),
                    PromptArgument(
                        name="service",
                        description="Service description (optional)",
                        required=False,
                    ),
                ],
            ),
            Prompt(
                name="schedule_maintenance",
                description="Schedule maintenance downtime for a target",
                arguments=[
                    PromptArgument(
                        name="target", description="Host/service/group name", required=True
                    ),
                    PromptArgument(
                        name="duration_minutes",
                        description="Duration in minutes (default 120)",
                        required=False,
                    ),
                    PromptArgument(
                        name="kind",
                        description=(
                            "host, service, hostgroup or servicegroup (default hostgroup)"
                        ),
                        required=False,
                    ),
                ],
            ),
            Prompt(
                name="diagnose_flapping",
                description="Diagnose a flapping service",
                arguments=[
                    PromptArgument(name="host", description="Host name", required=True),
                    PromptArgument(
                        name="service", description="Service description", required=True
                    ),
                ],
            ),
        ]

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> GetPromptResult:
        """Render a prompt by name with the provided arguments.

        Delegates to the module-level prompt functions
        (``investigate_alert``, ``schedule_maintenance``, ``diagnose_flapping``).
        """
        args = arguments or {}
        if name == "investigate_alert":
            text = investigate_alert(
                host=args.get("host", ""),
                service=args.get("service") or None,
            )
        elif name == "schedule_maintenance":
            raw_dur = args.get("duration_minutes", "120")
            duration = int(raw_dur) if str(raw_dur).isdigit() else 120
            text = schedule_maintenance(
                target=args.get("target", ""),
                duration_minutes=duration,
                kind=args.get("kind", "hostgroup"),
            )
        elif name == "diagnose_flapping":
            text = diagnose_flapping(
                host=args.get("host", ""),
                service=args.get("service", ""),
            )
        else:
            raise ValueError(f"Unknown prompt: {name!r}")
        return GetPromptResult(
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))]
        )


def build_server(config: ThrukConfig | None = None) -> ThrukMCPServer:
    """Build the MCP server with all Thruk tools registered.

    Uses mcp.server.Server directly (not FastMCP) so that:
    - inputSchema is defined explicitly — no annotation introspection
    - arguments arrive as a raw dict in call_tool — no Pydantic model
    - the Docker MCP Gateway cannot silently strip arguments
    """
    global _client
    cfg = config or ThrukConfig.from_env()
    _client = ThrukClient(cfg)

    audit.configure(enabled=cfg.audit_log)

    # Build enabled tool set (read_only / allowlist filtering)
    enabled: dict[str, Any] = {}
    for name, fn in _TOOL_DISPATCH.items():
        if cfg.read_only and name in WRITE_TOOLS:
            continue
        if cfg.enabled_tools and not any(fnmatch.fnmatch(name, pat) for pat in cfg.enabled_tools):
            continue
        enabled[name] = fn

    wrapper = ThrukMCPServer(Server("thruk-mcp"), enabled, _client, cfg)

    @wrapper._server.list_tools()
    async def list_tools() -> list[Tool]:
        return await wrapper.list_tools()

    @wrapper._server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        return await wrapper.call_tool(name, arguments)

    # issue #145 — register resource and prompt handlers so that MCP clients
    # (Claude Desktop, MCP Gateway, etc.) can discover and use them.
    @wrapper._server.list_resource_templates()
    async def list_resource_templates() -> list[ResourceTemplate]:
        return await wrapper.list_resource_templates()

    @wrapper._server.list_resources()
    async def list_resources() -> list[Resource]:
        return await wrapper.list_resources()

    @wrapper._server.read_resource()
    async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        return await wrapper.read_resource(uri)

    @wrapper._server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return await wrapper.list_prompts()

    @wrapper._server.get_prompt()
    async def get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        return await wrapper.get_prompt(name, arguments)

    return wrapper


# _apply_security_filters was removed: its logic is now inlined in build_server()
# (enabled dict filtering + audit logging in call_tool handler).
