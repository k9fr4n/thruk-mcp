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
import contextlib
import fnmatch
import logging
import re
import warnings
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
    DEFAULT_CONTACT_COLUMNS,
    DEFAULT_DOWNTIME_COLUMNS,
    DEFAULT_GROUP_COLUMNS,
    DEFAULT_HOST_COLUMNS,
    DEFAULT_LOG_COLUMNS,
    DEFAULT_NOTIFICATION_COLUMNS,
    DEFAULT_SERVICE_COLUMNS,
    HOST_STATE_INT,
    HOST_STATE_STR,
    LATENCY_SANITY_CAP_SECONDS,
    SVC_STATE_INT,
    SVC_STATE_STR,
)
from .filters import (
    FIELDS_ALERTS,
    FIELDS_HOST_STATS,
    FIELDS_HOSTS,
    FIELDS_LOGS,
    FIELDS_NOISY_HOSTS,
    FIELDS_NOISY_SERVICES,
    FIELDS_NOTIFICATIONS,
    FIELDS_OLDEST_PROBLEMS,
    FIELDS_PROBLEM_COUNTS,
    FIELDS_PROBLEMS,
    FIELDS_SERVICES,
    FIELDS_TOTALS,
    FIELDS_UNACKED,
    FilterError,
    build_tool_schema,
    compile_filter,
    compile_filter_problems,
    extract_log_lookup_fields,
    filter_schema_property,
    infer_alert_type_regex,
    validate_filter,
)
from .helpers import (
    _backends,
    _build_cv_params,
    _client_var,
    _get_client,
    _resolve_peer_for_host,
    _sanitize_latency,
    _seg,
    _tool_response,
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
from .prompts import diagnose_flapping, investigate_alert, schedule_maintenance
from .resources import (
    _host_resource,
    _hostgroup_resource,
    _problems_resource,
    _service_resource,
    _stats_resource,
)
from .tools.escape import (
    _ALLOWED_METHODS as _ALLOWED_METHODS,
)
from .tools.escape import (
    _REST_PATH_PREFIXES as _REST_PATH_PREFIXES,
)
from .tools.escape import (
    _validate_rest_path as _validate_rest_path,
)
from .tools.escape import (
    thruk_query,
    thruk_run_background_query,
)

__all__ = ["WRITE_TOOLS", "ThrukMCPServer", "build_server"]

# Actionable suffix appended to every "Result capped at ..." warning so that
# users / LLMs immediately know how to mitigate the truncation (issue #201).
_NOISY_CAP_HINT: str = (
    " Narrow the time window (e.g. since='-2h') or raise the cap by setting "
    "the THRUK_NOISY_MAX_ALERTS env var (default 10000)."
)

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
# Module-level client accessor (issue #143)
# ---------------------------------------------------------------------------
# ``_client_var`` and ``_get_client`` now live in :mod:`thruk_mcp.helpers` so
# that the ``tools/`` sub-package (issue #147) and any other module can reach
# the active ThrukClient without creating a cycle through ``server.py``.
# They are re-exported from this module so callers that do
# ``from thruk_mcp.server import _client_var`` (e.g. tests) keep working.


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

    .. note::
       The ``latency`` field is sanitised before being returned: values
       greater than ``THRUK_LATENCY_CAP_SECONDS`` (default 3600 s) are
       replaced with ``null`` to mitigate a known Naemon/Livestatus bug
       that occasionally leaks a Unix timestamp into that column.
       Affected hosts are listed in ``_warnings`` (issue #202).
    """
    params = _list_params(limit, offset, sort, columns, DEFAULT_HOST_COLUMNS)
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_HOSTS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        params.update(compile_filter(filter, "hosts"))
    data = await _get_client().get("/hosts", params=params, backends=_backends(backends))
    data, warns = _sanitize_latency(data, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
    return _tool_response(data, warns or None)


async def thruk_get_host(host: str, backends: str | None = None) -> str:
    """Get a single host by name.

    The Thruk REST ``/hosts/{name}`` endpoint always returns a JSON list
    (one entry per backend in a federated setup). This tool unpacks that
    list so callers get the expected single object:

    - empty list  -> ``{"error": "Host 'X' not found"}``
    - one entry   -> the dict itself
    - many entries (same hostname on multiple backends) -> the list, with
      a ``_warnings`` entry flagging the collision so the caller can
      disambiguate via ``backends=``.

    .. note::
       The ``latency`` field is sanitised — see ``thruk_list_hosts`` for
       details (issue #202).
    """
    data = await _get_client().get(f"/hosts/{_seg(host)}", backends=_backends(backends))
    if not isinstance(data, list):
        data, warns = _sanitize_latency(data, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
        return _tool_response(data, warns or None)
    if not data:
        return _tool_response({"error": f"Host {host!r} not found"})
    if len(data) == 1:
        single, warns = _sanitize_latency(data[0], cap_seconds=LATENCY_SANITY_CAP_SECONDS)
        return _tool_response(single, warns or None)
    data, lat_warns = _sanitize_latency(data, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
    warnings = [f"{len(data)} backends returned a result for host {host!r}; listing all."]
    warnings.extend(lat_warns)
    return _tool_response(data, warnings)


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

    .. note::
       The ``host_latency`` field is sanitised before being returned:
       values greater than ``THRUK_LATENCY_CAP_SECONDS`` (default 3600 s)
       are replaced with ``null`` to mitigate a known Naemon/Livestatus
       bug (issue #202). Service-level ``latency`` is unaffected.
    """
    params = _list_params(limit, offset, sort, columns, DEFAULT_SERVICE_COLUMNS)
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_SERVICES)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        params.update(compile_filter(filter, "services"))
    data = await _get_client().get("/services", params=params, backends=_backends(backends))
    data, warns = _sanitize_latency(data, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
    return _tool_response(data, warns or None)


async def thruk_get_service(host: str, service: str, backends: str | None = None) -> str:
    """Get a single service by host and description.

    The Thruk REST ``/services/{host}/{svc}`` endpoint always returns a JSON
    list (one entry per backend in a federated setup). This tool unpacks
    that list so callers get the expected single object — see
    :func:`thruk_get_host` for the exact unpacking rules.

    .. note::
       The ``host_latency`` field is sanitised — see
       :func:`thruk_list_services` for details (issue #202).
    """
    data = await _get_client().get(
        f"/services/{_seg(host)}/{_seg(service)}", backends=_backends(backends)
    )
    if not isinstance(data, list):
        data, warns = _sanitize_latency(data, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
        return _tool_response(data, warns or None)
    if not data:
        return _tool_response({"error": f"Service {host!r}/{service!r} not found"})
    if len(data) == 1:
        single, warns = _sanitize_latency(data[0], cap_seconds=LATENCY_SANITY_CAP_SECONDS)
        return _tool_response(single, warns or None)
    data, lat_warns = _sanitize_latency(data, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
    warnings = [
        f"{len(data)} backends returned a result for service {host!r}/{service!r}; listing all."
    ]
    warnings.extend(lat_warns)
    return _tool_response(data, warnings)


async def thruk_host_availability(
    host: str,
    since: str | None = "-7d",
    until: str | None = None,
    timeperiod: str | None = None,
    with_downtimes: bool = False,
    include_soft_states: bool = False,
    backends: str | None = None,
) -> str:
    """Compute availability (uptime / SLA %) for a host over a configurable time window.

    Returns ``time_up_percent``, ``time_down_percent``, ``time_unreachable_percent``
    and their ``scheduled_*`` equivalents (time spent in that state *during* a
    scheduled downtime), plus indeterminate buckets (``time_indeterminate_nodata``,
    ``time_indeterminate_notrunning``, ``time_indeterminate_outside_timeperiod``).

    ``since`` / ``until`` accept Thruk relative times (``"-7d"``, ``"-1m"``) or
    ISO datetimes (``"2026-05-01 00:00:00"``). Default window: last 7 days.

    ``timeperiod`` (e.g. ``"lastmonth"``, ``"thismonth"``, ``"last24hours"``,
    ``"lastweek"``) is a Thruk-native shortcut that overrides ``since``/``until``
    when provided.

    ``with_downtimes=True`` makes scheduled downtimes count as outages in the
    percentage calculations (``withdowntimes=1``).
    ``include_soft_states=True`` includes soft state changes (``includesoftstates=1``).
    """
    params: dict[str, Any] = {}
    if timeperiod:
        params["timeperiod"] = timeperiod
    else:
        ts_since = _parse_thruk_time(since)
        ts_until = _parse_thruk_time(until) if until else _now_utc_epoch()
        if ts_since is not None:
            params["start"] = ts_since
        if ts_until is not None:
            params["end"] = ts_until
    if with_downtimes:
        params["withdowntimes"] = 1
    if include_soft_states:
        params["includesoftstates"] = 1

    data = await _get_client().get(
        f"/hosts/{_seg(host)}/availability",
        params=params,
        backends=_backends(backends),
    )
    result: dict[str, Any] = {"host": host}
    if timeperiod:
        result["timeperiod"] = timeperiod
    else:
        result["since"] = since
        result["until"] = until
    if isinstance(data, dict):
        result.update(data)
    elif isinstance(data, list) and data:
        result.update(data[0])
    return _tool_response(result)


async def thruk_service_availability(
    host: str,
    service: str,
    since: str | None = "-7d",
    until: str | None = None,
    timeperiod: str | None = None,
    with_downtimes: bool = False,
    include_soft_states: bool = False,
    backends: str | None = None,
) -> str:
    """Compute availability (uptime / SLA %) for a service over a configurable time window.

    Returns ``time_ok_percent``, ``time_warning_percent``, ``time_critical_percent``,
    ``time_unknown_percent`` and their ``scheduled_*`` equivalents, plus indeterminate
    buckets (``time_indeterminate_nodata``, ``time_indeterminate_notrunning``,
    ``time_indeterminate_outside_timeperiod``).

    ``since`` / ``until`` accept Thruk relative times (``"-7d"``, ``"-1m"``) or
    ISO datetimes (``"2026-05-01 00:00:00"``). Default window: last 7 days.

    ``timeperiod`` (e.g. ``"lastmonth"``, ``"thismonth"``, ``"last24hours"``,
    ``"lastweek"``) is a Thruk-native shortcut that overrides ``since``/``until``
    when provided.

    ``with_downtimes=True`` makes scheduled downtimes count as outages in the
    percentage calculations (``withdowntimes=1``).
    ``include_soft_states=True`` includes soft state changes (``includesoftstates=1``).
    """
    params: dict[str, Any] = {}
    if timeperiod:
        params["timeperiod"] = timeperiod
    else:
        ts_since = _parse_thruk_time(since)
        ts_until = _parse_thruk_time(until) if until else _now_utc_epoch()
        if ts_since is not None:
            params["start"] = ts_since
        if ts_until is not None:
            params["end"] = ts_until
    if with_downtimes:
        params["withdowntimes"] = 1
    if include_soft_states:
        params["includesoftstates"] = 1

    data = await _get_client().get(
        f"/services/{_seg(host)}/{_seg(service)}/availability",
        params=params,
        backends=_backends(backends),
    )
    result: dict[str, Any] = {"host": host, "service": service}
    if timeperiod:
        result["timeperiod"] = timeperiod
    else:
        result["since"] = since
        result["until"] = until
    if isinstance(data, dict):
        result.update(data)
    elif isinstance(data, list) and data:
        result.update(data[0])
    return _tool_response(result)


async def thruk_hostgroup_availability(
    hostgroup: str,
    type: str = "hosts",
    since: str | None = "-7d",
    until: str | None = None,
    timeperiod: str | None = None,
    with_downtimes: bool = False,
    include_soft_states: bool = False,
    backends: str | None = None,
) -> str:
    """Compute availability (uptime / SLA %) for all hosts/services in a hostgroup.

    Returns a list sorted by ``time_up_percent`` ascending (worst performers first),
    so the LLM can immediately answer "which hosts were below SLA this month?".

    ``type`` controls what is returned:
    - ``"hosts"`` (default) — one entry per host with ``time_up_percent``,
      ``time_down_percent``, ``time_unreachable_percent`` and ``scheduled_*``
      equivalents.
    - ``"services"`` — one entry per service with ``time_ok_percent``,
      ``time_warning_percent``, ``time_critical_percent``, ``time_unknown_percent``.
    - ``"both"`` — hosts and services combined.

    ``since`` / ``until`` accept Thruk relative times (``"-7d"``, ``"-1m"``) or
    ISO datetimes (``"2026-05-01 00:00:00"``). Default window: last 7 days.

    ``timeperiod`` (e.g. ``"lastmonth"``, ``"thismonth"``, ``"last24hours"``,
    ``"lastweek"``) is a Thruk-native shortcut that overrides ``since``/``until``
    when provided.
    """
    if type not in ("hosts", "services", "both"):
        return _tool_response(
            {"error": f"Invalid type {type!r}. Must be 'hosts', 'services' or 'both'."}
        )

    params: dict[str, Any] = {"type": type}
    if timeperiod:
        params["timeperiod"] = timeperiod
    else:
        ts_since = _parse_thruk_time(since)
        ts_until = _parse_thruk_time(until) if until else _now_utc_epoch()
        if ts_since is not None:
            params["start"] = ts_since
        if ts_until is not None:
            params["end"] = ts_until
    if with_downtimes:
        params["withdowntimes"] = 1
    if include_soft_states:
        params["includesoftstates"] = 1

    data = await _get_client().get(
        f"/hostgroups/{_seg(hostgroup)}/availability",
        params=params,
        backends=_backends(backends),
    )

    rows: list[Any] = data if isinstance(data, list) else ([data] if data else [])
    # Sort worst performers first so the LLM sees the outliers immediately
    sort_key = "time_ok_percent" if type == "services" else "time_up_percent"
    with contextlib.suppress(TypeError, ValueError):
        rows = sorted(rows, key=lambda r: float(r.get(sort_key, 100.0)))

    meta: dict[str, Any] = {"hostgroup": hostgroup, "type": type}
    if timeperiod:
        meta["timeperiod"] = timeperiod
    else:
        meta["since"] = since
        meta["until"] = until
    meta["total"] = len(rows)
    meta["results"] = rows
    return _tool_response(meta)


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
    return _tool_response(data)


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
    return _tool_response(data)


async def thruk_list_contacts(
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List configured Nagios/Naemon contacts (notification targets).

    Useful for on-call lookup ("who gets paged if host X goes DOWN?") and
    notification routing audits. Hits ``GET /thruk/r/contacts``.

    Default columns: ``name,alias,email,pager,host_notifications_enabled,
    service_notifications_enabled``. Pass ``columns=''`` for the full
    Thruk contact record (notification commands, timeperiods, custom vars).
    """
    params = _list_params(limit, offset, sort, columns, DEFAULT_CONTACT_COLUMNS)
    data = await _get_client().get("/contacts", params=params, backends=_backends(backends))
    return _tool_response(data)


async def thruk_get_contact(contact: str, backends: str | None = None) -> str:
    """Get a single Nagios/Naemon contact by name.

    Hits ``GET /thruk/r/contacts/{name}`` and returns the full contact record
    (email, pager, notification commands and timeperiods, host/service
    notification flags, contact groups, custom variables).

    Raises ``ThrukError`` (404 surfaced verbatim) if the contact does not exist.
    """
    data = await _get_client().get(f"/contacts/{_seg(contact)}", backends=_backends(backends))
    return _tool_response(data)


# ---------------------------------------------------------------------------
# Hostgroup defense-in-depth (issue #200)
# ---------------------------------------------------------------------------
# The dual-query architecture of ``thruk_problems`` relies on each Thruk
# backend honouring ``groups[gte]`` / ``host_groups[gte]`` server-side.  In
# multi-backend federations a single mis-behaving backend can leak rows that
# don't actually belong to the requested hostgroup(s).  We therefore
# re-validate the ``groups`` / ``host_groups`` membership of every returned
# row and surface a ``_warnings`` entry whenever something had to be dropped.


def _collect_hostgroup_constraints(node: dict[str, Any]) -> list[tuple[str, Any]]:
    """Extract every ``hostgroup`` leaf from a validated AND-only filter tree.

    Returns a list of ``(op, value)`` tuples suitable for client-side
    re-validation in :func:`thruk_problems` (defense-in-depth, see issue #200).
    """
    out: list[tuple[str, Any]] = []

    def _walk(n: dict[str, Any]) -> None:
        if n.get("type") == "leaf":
            if n.get("field") == "hostgroup":
                out.append((str(n["op"]), n["value"]))
        else:
            for child in n.get("conditions") or []:
                _walk(child)

    _walk(node)
    return out


def _row_matches_hostgroup_constraints(
    groups: Any,
    constraints: list[tuple[str, Any]],
) -> bool:
    """Return True iff ``groups`` (a list-of-strings column) satisfies every constraint.

    Conservative on unknown/missing data: if ``groups`` is not a list we treat
    the row as non-matching only when at least one positive constraint
    (``eq``/``in``/``regex``) exists — that way a backend that strips the
    column entirely still triggers the warning, instead of silently leaking.
    """
    g: list[str] = [str(x) for x in groups] if isinstance(groups, list) else []
    g_set = set(g)
    for op, val in constraints:
        if op == "eq":
            if str(val) not in g_set:
                return False
        elif op == "in":
            allowed = {str(v) for v in val} if isinstance(val, list) else {str(val)}
            if g_set.isdisjoint(allowed):
                return False
        elif op == "neq":
            if str(val) in g_set:
                return False
        elif op == "regex":
            pat = re.compile(str(val), re.IGNORECASE)
            if not any(pat.search(x) for x in g):
                return False
        # gte / lte don't make sense for the list ``groups`` column; ignore.
    return True


def _ensure_columns_param(params: dict[str, Any], required: str) -> None:
    """Ensure ``required`` appears in ``params['columns']`` (no-op if columns unset).

    When ``columns`` is absent the response carries every column already, so
    nothing to do.  Otherwise append ``required`` if it's not already listed.
    """
    cur = params.get("columns")
    if not cur:  # None or empty string → all columns coming back
        return
    cols = [c.strip() for c in str(cur).split(",") if c.strip()]
    if required not in cols:
        cols.append(required)
        params["columns"] = ",".join(cols)


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

    Hostgroup constraints are additionally **re-validated client-side** on the merged
    response (issue #200): if any backend leaks a row that does not actually carry the
    requested hostgroup in its ``groups`` / ``host_groups`` column the row is dropped
    and a ``_warnings`` entry is appended.
    """
    host_params = _list_params(limit, offset, "-state,name", columns, DEFAULT_HOST_COLUMNS)
    host_params.update({"state": 1, "acknowledged": 0, "scheduled_downtime_depth": 0})
    svc_params = _list_params(
        limit, offset, "-state,host_name,description", columns, DEFAULT_SERVICE_COLUMNS
    )
    svc_params.update({"state[gte]": 1, "acknowledged": 0, "scheduled_downtime_depth": 0})
    hostgroup_constraints: list[tuple[str, Any]] = []
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_PROBLEMS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        try:
            extra_host, extra_svc = compile_filter_problems(filter)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_params.update(extra_host)
        svc_params.update(extra_svc)
        hostgroup_constraints = _collect_hostgroup_constraints(filter)
        if hostgroup_constraints:
            # Ensure we receive the column we need to re-validate.
            _ensure_columns_param(host_params, "groups")
            _ensure_columns_param(svc_params, "host_groups")
    hosts, host_warnings = await _get_client().get_with_fallback(
        "/hosts", params=host_params, backends=_backends(backends)
    )
    services, svc_warnings = await _get_client().get_with_fallback(
        "/services", params=svc_params, backends=_backends(backends)
    )
    all_warnings = list(dict.fromkeys(host_warnings + svc_warnings))
    if hostgroup_constraints:
        before_h, before_s = len(hosts), len(services)
        hosts = [
            h
            for h in hosts
            if _row_matches_hostgroup_constraints(h.get("groups"), hostgroup_constraints)
        ]
        services = [
            s
            for s in services
            if _row_matches_hostgroup_constraints(s.get("host_groups"), hostgroup_constraints)
        ]
        leaked_h = before_h - len(hosts)
        leaked_s = before_s - len(services)
        if leaked_h or leaked_s:
            all_warnings.append(
                "hostgroup_filter_leak: dropped "
                f"{leaked_h} host(s) and {leaked_s} service(s) returned by a backend "
                "but not actually members of the requested hostgroup(s) — "
                "this usually indicates a misbehaving backend in a multi-backend "
                "federation (issue #200)."
            )
    result: dict[str, Any] = {"hosts": hosts, "services": services}
    if all_warnings:
        result["_warnings"] = all_warnings
    return _tool_response(result)


async def thruk_stats(
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Aggregated host/service statistics.

    Optional ``filter`` is a structured AND/OR tree scoping the underlying
    ``/hosts/stats`` and ``/services/stats`` calls. Supported fields:
    ``hostgroup``, ``custom_var`` (e.g. ``{"var":"ENV","val":"prod"}``).

    The filter is compiled twice — once with ``context='hosts'`` (yielding
    ``groups[gte]=`` on ``/hosts/stats``) and once with ``context='services'``
    (yielding ``host_groups[gte]=`` on ``/services/stats``). The output
    shape is unchanged from the unfiltered call (issue #221).
    """
    host_params: dict[str, Any] = {}
    svc_params: dict[str, Any] = {}
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_HOST_STATS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_params = compile_filter(filter, "hosts")
        svc_params = compile_filter(filter, "services")
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/stats", params=host_params or None, backends=be),
        _get_client().get("/services/stats", params=svc_params or None, backends=be),
    )
    return _tool_response({"hosts": hosts, "services": services})


def _strip_filter_field(node: dict[str, Any], field: str) -> dict[str, Any] | None:
    """Return a deep-copied filter tree with every leaf on ``field`` removed.

    Used by :func:`thruk_totals` to drop the ``servicegroup`` leaf before
    compiling the host-side params (``/hosts/totals`` has no service-group
    scope and would otherwise emit a stray ``groups[gte]=`` colliding with
    a ``hostgroup`` leaf).

    Returns ``None`` when the pruned tree would be empty (no remaining
    leaves) so the caller can short-circuit and skip filter compilation.
    Empty groups produced by pruning are also collapsed.
    """
    if node.get("type") == "leaf":
        return None if node.get("field") == field else dict(node)
    pruned_children: list[dict[str, Any]] = []
    for child in node.get("conditions", []):
        kept = _strip_filter_field(child, field)
        if kept is not None:
            pruned_children.append(kept)
    if not pruned_children:
        return None
    if len(pruned_children) == 1:
        # Collapse a single-child group to the child itself.
        return pruned_children[0]
    return {
        "type": "group",
        "operator": node["operator"],
        "conditions": pruned_children,
    }


async def thruk_totals(
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Compact host+service totals — 16 fields versus ~100 from ``thruk_stats``.

    Calls ``/hosts/totals`` and ``/services/totals`` concurrently and returns
    a merged ``{"hosts": {...}, "services": {...}}`` payload, ideal for a
    quick "how is everything?" overview.

    Optional ``filter`` is a structured AND/OR tree scoping both endpoints.
    Supported fields:

    - ``hostgroup``   → ``groups[gte]=`` on ``/hosts/totals`` and
      ``host_groups[gte]=`` on ``/services/totals``.
    - ``servicegroup`` → ``groups[gte]=`` on ``/services/totals`` only
      (stripped from the host-side params — it has no meaning there).
    - ``custom_var``  → ``_VARNAME=value`` on both endpoints.
    """
    host_params: dict[str, Any] = {}
    svc_params: dict[str, Any] = {}
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_TOTALS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_filter = _strip_filter_field(filter, "servicegroup")
        if host_filter is not None:
            host_params = compile_filter(host_filter, "hosts")
        svc_params = compile_filter(filter, "services")
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/totals", params=host_params or None, backends=be),
        _get_client().get("/services/totals", params=svc_params or None, backends=be),
    )
    return _tool_response({"hosts": hosts, "services": services})


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
    return _tool_response(data)


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
    return _tool_response(data)


async def thruk_sites() -> str:
    """List configured Thruk backends (sites)."""
    return _tool_response(await _get_client().get("/sites"))


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


# ---------------------------------------------------------------------------
# Backward-compat shim for issue #191 (legacy `hours` parameter)
#
# Issue #177 aligned the *schema* of the three trend tools (top_noisy_hosts /
# top_noisy_services / flap_summary) to use `since` / `until` instead of the
# legacy `hours: int` parameter. However, deployed Docker MCP Gateways and
# any cached `metadata.json` snapshots may still advertise `hours`, so callers
# honoring those stale schemas pass `hours=24` and trigger:
#
#   TypeError: thruk_top_noisy_hosts() got an unexpected keyword argument 'hours'
#
# This shim accepts `hours` for backward compatibility and translates it to
# the canonical `since="-{N}h"`. An explicit `since` always wins over `hours`.
# ---------------------------------------------------------------------------
_DEFAULT_SINCE = "-24h"


def _coerce_hours_to_since(hours: int | None, since: str | None, tool_name: str) -> str | None:
    """Translate the deprecated ``hours`` kwarg to a ``since`` relative window.

    Returns the resolved ``since`` value. If ``hours`` is set, it is used only
    when the caller did not override the default ``since``; otherwise the
    explicit ``since`` wins. Emits a ``DeprecationWarning`` when ``hours`` is
    supplied so clients migrate to ``since``/``until``.
    """
    if hours is None:
        return since
    if hours <= 0:
        raise ThrukError(f"{tool_name}: 'hours' must be a positive integer (got {hours!r})")
    warnings.warn(
        f"{tool_name}: parameter 'hours' is deprecated; use since=\"-{hours}h\" instead",
        DeprecationWarning,
        stacklevel=2,
    )
    # Explicit, non-default `since` wins over the legacy `hours` shim.
    if since not in (None, _DEFAULT_SINCE):
        return since
    return f"-{hours}h"


async def thruk_top_noisy_hosts(
    since: str | None = _DEFAULT_SINCE,
    until: str | None = None,
    limit: int = 10,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
    hours: int | None = None,
) -> str:
    """Return the top N hosts ranked by HOST ALERT count over a time window.

    Aggregates HOST ALERT log entries, excludes RECOVERY events (state UP = 0),
    and ranks by alert count descending.

    ``since`` / ``until`` accept relative (``-24h``, ``-30m``) or absolute
    (``2026-05-20 14:00:00``) values — same format as ``thruk_list_alerts``.
    Default window: last 24 h (``since="-24h"``, ``until=None``).

    ``filter`` fields: ``host`` (eq/regex), ``hostgroup``, ``custom_var``
    (host-level Nagios variable, resolved via /hosts lookup).

    ``hours`` is **deprecated** (issue #191 backward-compat shim): clients
    still using the legacy ``hours: int`` schema have it translated to
    ``since="-{hours}h"``. Prefer ``since`` / ``until`` for new code.

    Returns a wrapped object:
    ``since``, ``until``, ``total_alerts_in_window`` (after RECOVERY exclusion),
    ``results`` list sorted by ``alert_count`` desc, each entry containing
    ``host``, ``alert_count``, ``last_state``, ``last_alert_time``.
    """
    since = _coerce_hours_to_since(hours, since, "thruk_top_noisy_hosts")
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_HOSTS, backends)
    if errs:
        return _tool_response({"error": errs[0]})

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
            + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


async def thruk_top_noisy_services(
    since: str | None = _DEFAULT_SINCE,
    until: str | None = None,
    limit: int = 10,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
    hours: int | None = None,
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

    ``hours`` is **deprecated** (issue #191 backward-compat shim): clients
    still using the legacy ``hours: int`` schema have it translated to
    ``since="-{hours}h"``. Prefer ``since`` / ``until`` for new code.

    Returns a wrapped object:
    ``since``, ``until``, ``total_alerts_in_window`` (after RECOVERY exclusion),
    ``results`` list sorted by ``alert_count`` desc, each entry containing
    ``host``, ``service``, ``alert_count``, ``last_state``, ``last_alert_time``.
    """
    since = _coerce_hours_to_since(hours, since, "thruk_top_noisy_services")
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

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
            + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


async def thruk_flap_summary(
    since: str | None = _DEFAULT_SINCE,
    until: str | None = None,
    limit: int = 10,
    min_transitions: int = 3,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
    hours: int | None = None,
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

    ``hours`` is **deprecated** (issue #191 backward-compat shim): clients
    still using the legacy ``hours: int`` schema have it translated to
    ``since="-{hours}h"``. Prefer ``since`` / ``until`` for new code.

    Returns a wrapped object:
    ``since``, ``until``, ``min_transitions``, ``total_flapping_objects``,
    ``results`` list sorted by ``transition_count`` desc, each entry containing
    ``host``, ``service`` (null for host-level flapping), ``transition_count``,
    ``states_seen`` (sorted unique set of state names), ``last_state``,
    ``last_alert_time``.
    """
    since = _coerce_hours_to_since(hours, since, "thruk_flap_summary")
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    # Defence-in-depth (issues #176 / #193): Naemon Livestatus does not exclude
    # type=NULL rows from regex filters, so class=0 system messages (e.g.
    # "Auto-save of retention data completed successfully.") leak past ``type[~]``
    # and inflate transition counts. All HOST/SERVICE ALERT rows have class=1.
    extra["class"] = "1"
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
            + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


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

    When the underlying log fetch hits the ``_NOISY_MAX_ALERTS`` cap, the
    response also carries ``truncated_after`` (ISO-UTC timestamp of the last
    fetched event) and every bucket starting *after* the bucket that contains
    that event is marked as ``{"count": null, "truncated": true}`` so the
    consumer can distinguish "no alerts in this bucket" from "bucket not
    covered by the capped fetch".
    """
    bucket_secs = _BUCKET_SIZES.get(bucket)
    if bucket_secs is None:
        return _tool_response(
            {"error": f"Invalid bucket {bucket!r}. Allowed: {', '.join(_BUCKET_SIZES)}"}
        )

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    # Defence-in-depth (issues #176 / #193): drop class=0 system messages and
    # class=5/6 external-command / current-state rows that Naemon Livestatus
    # leaks past ``type[~]`` because their ``type`` column is NULL/distinct.
    extra["class"] = "1"
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

    # When the log cap is hit, sort=time ascending means we got the *earliest*
    # entries only — buckets past the last fetched timestamp would silently
    # show count=0. Mark them as null+truncated so consumers (LLM or human)
    # do not confuse "missing data" with "quiet period".
    log_capped = len(data) >= _NOISY_MAX_ALERTS
    if log_capped and raw_counts:
        last_ts = max(int(e["time"]) for e in data if e.get("time"))
        last_bucket = (last_ts // bucket_secs) * bucket_secs
        truncated_after_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        payload["truncated_after"] = truncated_after_iso
        for bucket_obj in results:
            bs_str = bucket_obj["bucket_start"]
            bs_epoch = int(
                datetime.strptime(bs_str, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            if bs_epoch > last_bucket:
                bucket_obj["count"] = None
                bucket_obj["truncated"] = True

    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif log_capped:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; aggregation may be incomplete. "
            "Buckets after 'truncated_after' are reported as count=null (data not fetched)."
            + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


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
        return _tool_response({"error": "min_alerts must be >= 1"})

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    # Defence-in-depth (issues #176 / #193): without ``class=1`` Naemon
    # Livestatus returns rows with ``type=NULL`` (class=0 system messages,
    # class=5 external commands, class=6 current-state snapshots) that pass
    # the ``type[~]`` regex and inflate the per-object alert count.
    extra["class"] = "1"
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
            + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


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
        return _tool_response({"error": errs[0]})
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
    return _tool_response(data, warnings)


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

    Note: Naemon Livestatus packs host and service states into the same
    integer ``state`` column (``DOWN=1`` collides with ``WARNING=1``).  When
    every ``state`` filter uses host-only names (``up``/``down``/``unreachable``)
    the server-side ``type[~]`` regex is narrowed to ``^HOST ALERT``; when
    every state value is service-only (``ok``/``warning``/``critical``/
    ``unknown``) it is narrowed to ``^SERVICE ALERT``.  Numeric values or
    mixed inputs fall back to ``^(HOST|SERVICE) ALERT`` (issue #198).
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_ALERTS, backends)
    if errs:
        return _tool_response({"error": errs[0]})
    # Disambiguate HOST DOWN (state=1) from SERVICE WARNING (state=1) by
    # narrowing the type regex when the filter uses unambiguous state names.
    extra["type[~]"] = infer_alert_type_regex(filter) or "^(HOST|SERVICE) ALERT"
    # Defence-in-depth: Naemon Livestatus does not exclude rows where ``type`` is
    # NULL/empty from regex filters, so class=0 system messages (e.g. "Auto-save
    # of retention data completed successfully.") slip past ``type[~]``. All
    # HOST/SERVICE ALERT rows have class=1, so a server-side class filter is a
    # cheap and reliable cut. See issue #176.
    extra["class"] = "1"
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
    return _tool_response(data, warnings)


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
        return _tool_response({"error": errs[0]})
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
    return _tool_response(data, warnings)


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
        return _tool_response({"error": errs[0]})
    if only_alerts:
        extra["type[~]"] = "^(HOST|SERVICE) ALERT"
        # Defence-in-depth (issues #176 / #193): pair the regex with a
        # server-side class=1 cut so class=0 system messages do not leak
        # past ``type[~]`` (Naemon Livestatus does not exclude rows with
        # ``type=NULL`` from the regex filter).
        extra["class"] = "1"
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
    return _tool_response(data, warnings)


# ---------------------------------------------------------------------------
# Escape-hatch tools (thruk_query, thruk_run_background_query) — issue #147
# ---------------------------------------------------------------------------
# Moved to :mod:`thruk_mcp.tools.escape`.  Names are re-exported at the top
# of this module via ``from .tools.escape import ...`` for backward
# compatibility with callers that still do
# ``from thruk_mcp.server import thruk_query`` (tests, external users).


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
    or ISO 8601. If `duration_minutes` is set it overrides `end_time`.

    Note: Naemon processes scheduling commands asynchronously through its
    command pipe. A newly scheduled downtime may not be immediately visible
    in Livestatus queries (`thruk_list_downtimes`, `thruk_delete_active_downtimes`,
    ...). Allow ~5-10 seconds before querying or deleting (issue #194)."""
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
    return _tool_response(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends))
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
    return _tool_response(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends))
    )


async def thruk_bulk_acknowledge(
    author: str = "thruk-mcp",
    comment: str = "bulk acknowledged via MCP",
    hostgroup: str | None = None,
    state: str | None = None,
    hosts_only: bool = False,
    services_only: bool = False,
    sticky: bool = True,
    notify: bool = True,
    persistent: bool = False,
    backends: str | None = None,
) -> str:
    """Acknowledge every unhandled problem matching the given filters in one call.

    Collects all currently unhandled (not acknowledged, not in downtime) host
    and/or service problems matching the optional ``hostgroup`` / ``state``
    filters, then fires every ``acknowledge_{host,svc}_problem`` POST
    concurrently via ``asyncio.gather``.

    Parameters:
    - ``state``: one of ``"down"``, ``"unreachable"`` (hosts) or
      ``"critical"``, ``"warning"``, ``"unknown"`` (services). ``None`` =
      every non-OK state.  Numeric strings ("0".."3") are also accepted via
      the canonical state-int maps.
    - ``hosts_only``: skip service problems entirely.
    - ``services_only``: skip host problems entirely.
    - ``hostgroup``: restrict to members of this hostgroup (resolved via
      Livestatus ``groups[gte]`` / ``host_groups[gte]`` — same semantics as
      ``thruk_problems``).
    - ``sticky`` / ``notify`` / ``persistent``: forwarded verbatim to
      ``acknowledge_*_problem`` (payload keys ``sticky_ack``,
      ``send_notification``, ``persistent_comment``).

    Returns a JSON summary:

    .. code-block:: json

        {
          "acknowledged": 12,
          "failed": 0,
          "targets": [{"host": "srv01", "service": null, "state": "DOWN"}, ...],
          "errors": []
        }

    When zero targets match, returns ``acknowledged=0`` plus a ``_warning``
    note — it is not an error.
    """
    if hosts_only and services_only:
        return _tool_response({"error": "hosts_only and services_only are mutually exclusive"})

    # Resolve state filter to host / service int (None = any non-OK).
    host_state_int: int | None = None
    svc_state_int: int | None = None
    skip_hosts = services_only
    skip_services = hosts_only
    if state is not None:
        key = state.lower()
        if key in HOST_STATE_INT and HOST_STATE_INT[key] != 0:
            host_state_int = HOST_STATE_INT[key]
            skip_services = True  # host-only state
        elif key in SVC_STATE_INT and SVC_STATE_INT[key] != 0:
            svc_state_int = SVC_STATE_INT[key]
            skip_hosts = True  # service-only state
        else:
            return _tool_response(
                {
                    "error": (
                        f"invalid state {state!r}: expected one of "
                        "down, unreachable, critical, warning, unknown"
                    )
                }
            )

    be = _backends(backends)

    async def _collect_hosts() -> list[dict[str, Any]]:
        if skip_hosts:
            return []
        params: dict[str, Any] = {
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
            "columns": "name,state,peer_name",
        }
        if host_state_int is not None:
            params["state"] = host_state_int
        else:
            params["state[gte]"] = 1
        if hostgroup:
            params["groups[gte]"] = hostgroup
        rows: list[dict[str, Any]] = []
        async for row in _get_client().get_all("/hosts", params=params, backends=be):
            if isinstance(row, dict) and row.get("name"):
                rows.append(row)
        return rows

    async def _collect_services() -> list[dict[str, Any]]:
        if skip_services:
            return []
        params: dict[str, Any] = {
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
            "columns": "host_name,description,state,peer_name",
        }
        if svc_state_int is not None:
            params["state"] = svc_state_int
        else:
            params["state[gte]"] = 1
        if hostgroup:
            params["host_groups[gte]"] = hostgroup
        rows: list[dict[str, Any]] = []
        async for row in _get_client().get_all("/services", params=params, backends=be):
            if isinstance(row, dict) and row.get("host_name") and row.get("description"):
                rows.append(row)
        return rows

    hosts, services = await asyncio.gather(_collect_hosts(), _collect_services())

    targets: list[dict[str, Any]] = []
    coros: list[Coroutine[Any, Any, Any]] = []
    payload = {
        "comment_data": comment,
        "comment_author": author,
        "sticky_ack": "1" if sticky else "0",
        "send_notification": "1" if notify else "0",
        "persistent_comment": "1" if persistent else "0",
    }

    for h in hosts:
        name = str(h.get("name", ""))
        targets.append(
            {
                "host": name,
                "service": None,
                "state": HOST_STATE_STR.get(int(h.get("state", -1)), str(h.get("state", ""))),
            }
        )
        coros.append(
            _get_client().post(
                f"/hosts/{_seg(name)}/cmd/acknowledge_host_problem",
                data=payload,
                backends=be,
            )
        )
    for s in services:
        h_name = str(s.get("host_name", ""))
        svc = str(s.get("description", ""))
        targets.append(
            {
                "host": h_name,
                "service": svc,
                "state": SVC_STATE_STR.get(int(s.get("state", -1)), str(s.get("state", ""))),
            }
        )
        coros.append(
            _get_client().post(
                f"/services/{_seg(h_name)}/{_seg(svc)}/cmd/acknowledge_svc_problem",
                data=payload,
                backends=be,
            )
        )

    result: dict[str, Any] = {
        "acknowledged": 0,
        "failed": 0,
        "targets": targets,
        "errors": [],
    }

    if not coros:
        result["_warning"] = "no matching unhandled problems found — nothing to acknowledge"
        return _tool_response(result)

    results = await asyncio.gather(*coros, return_exceptions=True)
    errors: list[dict[str, Any]] = []
    ok_count = 0
    for tgt, res in zip(targets, results, strict=True):
        if isinstance(res, Exception):
            errors.append({**tgt, "error": str(res)})
        else:
            ok_count += 1
    result["acknowledged"] = ok_count
    result["failed"] = len(errors)
    result["errors"] = errors
    return _tool_response(result)


async def thruk_add_comment(
    host: str,
    comment: str,
    service: str | None = None,
    author: str = "thruk-mcp",
    persistent: bool = True,
    backends: str | None = None,
) -> str:
    """Add a free-form operator comment on a host or service.

    Posts a timestamped note via Thruk REST without acknowledging the problem
    or scheduling a downtime.  Typical use-cases: incident timeline annotations
    ("Investigating high load, ETA 30 min"), false-positive markers, ops handoff
    notes.

    Thruk commands used:
    - host:    ``POST /hosts/{host}/cmd/add_host_comment``
    - service: ``POST /services/{host}/{svc}/cmd/add_svc_comment``

    Payload keys forwarded to Thruk:
    - ``comment_data``   — the comment text
    - ``comment_author`` — display name of the author
    - ``persistent``     — when "1" the comment survives Nagios restarts /
      subsequent check results; when "0" it is dropped on the next check.

    This tool does **not** acknowledge a problem (use ``thruk_acknowledge``)
    and does **not** schedule a downtime (use ``thruk_schedule_downtime``).
    """
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/add_svc_comment"
        if service
        else f"/hosts/{_seg(host)}/cmd/add_host_comment"
    )
    payload = {
        "comment_data": comment,
        "comment_author": author,
        "persistent": "1" if persistent else "0",
    }
    return _tool_response(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends))
    )


async def thruk_delete_comment(
    comment_id: int,
    host: str,
    service: str | None = None,
    backends: str | None = None,
) -> str:
    """Delete a host or service comment by its id.

    Closes the CRUD loop for operator notes: ``thruk_list_comments`` exposes
    comment ids, ``thruk_add_comment`` creates them, and this tool deletes
    them.  Typical use-cases:

    - remove a stale investigation note after the incident is resolved,
    - clean up comments created by an LLM assistant during an incident.

    Thruk commands used (the command-based path is selected because the REST
    ``DELETE /comments/{id}`` endpoint is not guaranteed across Thruk
    versions):

    - host:    ``POST /hosts/{host}/cmd/del_host_comment``
    - service: ``POST /services/{host}/{svc}/cmd/del_svc_comment``

    Payload key forwarded to Thruk:

    - ``comment_id`` — the numeric id returned by ``thruk_list_comments``.

    ``host`` is required so Thruk can route the command to the correct
    backend.  ``service`` selects the service-scoped command path; omit it
    for host comments.
    """
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/del_svc_comment"
        if service
        else f"/hosts/{_seg(host)}/cmd/del_host_comment"
    )
    return _tool_response(
        await _get_client().post(
            endpoint, data={"comment_id": str(comment_id)}, backends=_backends(backends)
        )
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
    return _tool_response(await _get_client().post(endpoint, backends=_backends(backends)))


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
    return _tool_response(
        await _get_client().post(endpoint, data={"start_time": "now"}, backends=_backends(backends))
    )


async def thruk_notifications(
    host: str,
    enabled: bool,
    service: str | None = None,
    cascade: bool = False,
    backends: str | None = None,
) -> str:
    """Enable or disable notifications for a host or service.

    ``enabled=True``  → enable notifications.
    ``enabled=False`` → disable notifications.

    When ``service`` is omitted the command targets the host itself.
    Set ``cascade=True`` to also apply the same command to **all services**
    of the host (ignored when ``service`` is specified).

    Thruk commands used:
    - host:    ``enable_host_notifications`` / ``disable_host_notifications``
    - service: ``enable_svc_notifications``  / ``disable_svc_notifications``

    This tool does **not** schedule a downtime and does **not** acknowledge
    any problem — it only controls whether Thruk sends out alerts.
    """
    client = _get_client()
    be = _backends(backends)
    results: list[Any] = []

    if service:
        # Single service — cascade is irrelevant
        verb = "enable_svc_notifications" if enabled else "disable_svc_notifications"
        endpoint = f"/services/{_seg(host)}/{_seg(service)}/cmd/{verb}"
        results.append(await client.post(endpoint, backends=be))
    else:
        # Host-level command
        verb_host = "enable_host_notifications" if enabled else "disable_host_notifications"
        results.append(await client.post(f"/hosts/{_seg(host)}/cmd/{verb_host}", backends=be))

        if cascade:
            # Apply to every service of this host
            verb_svc = "enable_svc_notifications" if enabled else "disable_svc_notifications"
            svc_data = await client.get(
                f"/hosts/{_seg(host)}/services",
                params={"columns": "description"},
                backends=be,
            )
            services: list[str] = []
            if isinstance(svc_data, list):
                services = [
                    s["description"]
                    for s in svc_data
                    if isinstance(s, dict) and s.get("description")
                ]
            svc_results = await asyncio.gather(
                *(
                    client.post(f"/services/{_seg(host)}/{_seg(svc)}/cmd/{verb_svc}", backends=be)
                    for svc in services
                )
            )
            results.extend(svc_results)

    action = "enabled" if enabled else "disabled"
    target = f"{host}/{service}" if service else host
    if cascade and not service:
        target = f"{host} (host + all services)"
    return _tool_response({"action": action, "target": target, "results": results})


async def thruk_checks(
    host: str,
    enabled: bool,
    service: str | None = None,
    cascade: bool = False,
    backends: str | None = None,
) -> str:
    """Enable or disable active checks for a host or service.

    ``enabled=True``  → enable active checks.
    ``enabled=False`` → disable active checks.

    When ``service`` is omitted the command targets the host itself.
    Set ``cascade=True`` to also apply the same command to **all services**
    of the host (ignored when ``service`` is specified).

    Thruk commands used:
    - host:    ``enable_host_checks`` / ``disable_host_checks``
    - service: ``enable_svc_checks``  / ``disable_svc_checks``

    This tool does **not** schedule a downtime and does **not** acknowledge
    any problem — it only controls whether Thruk runs active checks. Passive
    check submissions are unaffected.
    """
    client = _get_client()
    be = _backends(backends)
    results: list[Any] = []

    if service:
        # Single service — cascade is irrelevant
        verb = "enable_svc_checks" if enabled else "disable_svc_checks"
        endpoint = f"/services/{_seg(host)}/{_seg(service)}/cmd/{verb}"
        results.append(await client.post(endpoint, backends=be))
    else:
        # Host-level command
        verb_host = "enable_host_checks" if enabled else "disable_host_checks"
        results.append(await client.post(f"/hosts/{_seg(host)}/cmd/{verb_host}", backends=be))

        if cascade:
            # Apply to every service of this host
            verb_svc = "enable_svc_checks" if enabled else "disable_svc_checks"
            svc_data = await client.get(
                f"/hosts/{_seg(host)}/services",
                params={"columns": "description"},
                backends=be,
            )
            services: list[str] = []
            if isinstance(svc_data, list):
                services = [
                    s["description"]
                    for s in svc_data
                    if isinstance(s, dict) and s.get("description")
                ]
            svc_results = await asyncio.gather(
                *(
                    client.post(f"/services/{_seg(host)}/{_seg(svc)}/cmd/{verb_svc}", backends=be)
                    for svc in services
                )
            )
            results.extend(svc_results)

    action = "enabled" if enabled else "disabled"
    target = f"{host}/{service}" if service else host
    if cascade and not service:
        target = f"{host} (host + all services)"
    return _tool_response({"action": action, "target": target, "results": results})


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
    return _tool_response(
        await client.post(endpoint, data={"downtime_id": str(downtime_id)}, backends=be)
    )


async def thruk_get_downtime(downtime_id: int, backends: str | None = None) -> str:
    """Get a single downtime by id.

    The Thruk REST ``/downtimes/{id}`` endpoint always returns a JSON list
    (one entry per backend in a federated setup). This tool unpacks that
    list so callers get the expected single object, mirroring
    ``thruk_get_host`` / ``thruk_get_service``:

    - empty list  -> ``{"error": "Downtime <id> not found"}``
    - one entry   -> the dict itself
    - many entries (same downtime id on multiple backends) -> the list,
      with a ``_warnings`` entry flagging the collision so the caller can
      disambiguate via ``backends=``.
    """
    data = await _get_client().get(
        f"/downtimes/{_seg(str(downtime_id))}", backends=_backends(backends)
    )
    if not isinstance(data, list):
        return _tool_response(data)
    if not data:
        return _tool_response({"error": f"Downtime {downtime_id} not found"})
    if len(data) == 1:
        return _tool_response(data[0])
    return _tool_response(
        data,
        [f"{len(data)} backends returned a result for downtime {downtime_id}; listing all."],
    )


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
    itself or for one specific service.

    Note: Naemon processes scheduling commands asynchronously; new downtimes
    may not be immediately visible in Livestatus (issue #194)."""
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return _tool_response(
        await _get_client().post(
            f"/hosts/{_seg(host)}/cmd/schedule_host_svc_downtime",
            data=payload,
            backends=_backends(backends),
        )
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
    return _tool_response(
        await _get_client().post(
            f"/hosts/{_seg(host)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        )
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
    return _tool_response(
        await _get_client().post(
            f"/hostgroups/{_seg(hostgroup)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        )
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
    return _tool_response(
        await _get_client().post(
            f"/servicegroups/{_seg(servicegroup)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        )
    )


async def thruk_delete_active_downtimes(
    host: str,
    service: str | None = None,
    backends: str | None = None,
    retry_on_empty: bool = True,
    retry_delay_seconds: float = 2.0,
) -> str:
    """Remove ALL currently active downtimes for a host (or one specific
    service when `service` is given). Fetches all active downtime IDs first,
    then submits one DEL_*_DOWNTIME per ID. Partial failures are reported
    individually in `errors` instead of aborting the whole batch.

    Naemon processes scheduling commands asynchronously through its command
    pipe (issue #194): a downtime created by ``thruk_schedule_downtime`` /
    ``thruk_schedule_host_services_downtime`` may not be visible in
    Livestatus for a few seconds. When the initial ``/downtimes`` lookup
    returns zero matches and ``retry_on_empty=True`` (the default), the
    tool waits ``retry_delay_seconds`` and re-queries once. If still empty,
    the response includes a structured ``_warning`` so callers can detect
    the lag instead of assuming there is nothing to delete."""
    client = _get_client()
    be = _backends(backends)

    # Query active downtimes: started and not yet ended (same logic as thruk_list_downtimes).
    def _build_params() -> dict[str, Any]:
        p: dict[str, Any] = {
            "host_name": host,
            "start_time[lte]": _now_utc_epoch(),
            "end_time[gte]": _now_utc_epoch(),
            "columns": "id,service_description,author,comment",
        }
        if service:
            p["service_description"] = service
        return p

    async def _fetch_matching() -> list[dict[str, Any]]:
        raw = await client.get("/downtimes", params=_build_params(), backends=be)
        all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])
        # Keep only the right type: host-level (empty service_desc) or the requested service.
        if service:
            return [d for d in all_dts if d.get("service_description") == service]
        return [d for d in all_dts if not d.get("service_description")]

    downtimes = await _fetch_matching()

    # Issue #194: Naemon command pipe is async — a freshly-scheduled downtime
    # may not yet be visible in Livestatus. Retry once after a short backoff
    # before giving up, unless the caller explicitly opts out.
    if not downtimes and retry_on_empty and retry_delay_seconds > 0:
        await asyncio.sleep(retry_delay_seconds)
        downtimes = await _fetch_matching()

    if not downtimes:
        return _tool_response(
            {
                "deleted": [],
                "errors": [],
                "count": 0,
                "message": "No active downtimes found.",
                "_warning": (
                    "No active downtimes visible in Livestatus. Naemon processes "
                    "scheduling commands asynchronously through its command pipe — "
                    "if a downtime was just created, retry in a few seconds. "
                    "See issue #194."
                ),
            }
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

    return _tool_response({"deleted": deleted, "errors": errors, "count": len(deleted)})


async def thruk_delete_downtimes_by_filter(
    host: str | None = None,
    hostgroup: str | None = None,
    service: str | None = None,
    start_time: str | None = None,
    comment: str | None = None,
    backends: str | None = None,
) -> str:
    """Bulk-delete downtimes matching arbitrary filters.

    Strategy depends on the filter combination:

    * **``host`` + ``comment``** (issue #197): the tool enumerates downtimes
      for the host via ``/downtimes`` and applies a **case-insensitive
      substring** match on ``comment`` client-side, then issues per-id
      ``del_downtime`` commands against the matching host- or service-level
      endpoint. This avoids Naemon's exact-string comparison on the comment
      field (``DEL_DOWNTIME_BY_HOST_NAME`` would otherwise silently no-op for
      partial-comment filters). Matches are reported under
      ``host_downtimes_*`` and ``service_downtimes_*``.
    * **``host`` only**: bulk via ``del_downtime_by_host_name`` system command
      (service downtimes) plus explicit enumeration of host-level downtimes
      (which the system command does not cover).
    * **``hostgroup``**: bulk via ``del_downtime_by_hostgroup_name``.
    * **``comment`` or ``start_time`` only**: bulk via
      ``del_downtime_by_start_time_comment`` — **exact** match on ``comment``
      (Naemon limitation, no client-side fallback available).

    At least one of ``host``, ``hostgroup``, ``service``, ``start_time`` or
    ``comment`` must be provided."""
    client = _get_client()
    be = _backends(backends)

    if not any([host, hostgroup, service, start_time, comment]):
        raise ThrukError("Provide at least one of host, hostgroup, service, start_time, comment.")

    # Issue #196: when filtering by host without an explicit `backends=`
    # override, pre-resolve the backend owning the host so commands are not
    # broadcast to every Naemon site (11/12 useless commands in a typical
    # federation). Ambiguous lookups fall back to broadcast.
    if host and not hostgroup and be is None:
        resolved = await _resolve_peer_for_host(client, host)
        if resolved is not None:
            be = resolved

    # ------------------------------------------------------------------
    # Issue #197: host + comment → client-side substring match path.
    # Skip the system command entirely (its comment match is exact and
    # silently no-ops for partial filters).
    # ------------------------------------------------------------------
    if host and not hostgroup and comment:
        return await _delete_downtimes_by_host_comment(
            client, be, host=host, comment=comment, service=service, start_time=start_time
        )

    # ------------------------------------------------------------------
    # Bulk system-command path (no client-side filtering available).
    # ------------------------------------------------------------------
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
        # NOTE: exact-match only — see docstring (issue #197).
        payload["comment"] = comment

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
        if start_time:
            dt_params["start_time"] = start_time

        raw = await client.get("/downtimes", params=dt_params, backends=be)
        all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])
        # Host-level downtimes have an empty service_description.
        host_dts = [d for d in all_dts if not d.get("service_description")]

        # Issue #141: parallelise per-id DEL via asyncio.gather.
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

    return _tool_response(result)


async def _delete_downtimes_by_host_comment(
    client: Any,
    be: tuple[str, ...] | None,
    *,
    host: str,
    comment: str,
    service: str | None,
    start_time: str | None,
) -> str:
    """Issue #197: client-side substring filter on the ``comment`` field.

    Enumerates downtimes for ``host`` (optionally narrowed by ``service`` and
    ``start_time``), keeps only those whose comment contains ``comment``
    (case-insensitive), then issues per-id ``del_downtime`` against the
    correct endpoint (host- vs service-level) in parallel via
    :func:`asyncio.gather`.

    This works around Naemon's exact-string comparison on the comment field
    in ``DEL_DOWNTIME_BY_HOST_NAME`` which would silently no-op on partial
    matches and return ``{"message": "Command successfully submitted"}``."""
    dt_params: dict[str, Any] = {
        "host_name": host,
        "columns": "id,service_description,comment,start_time",
    }
    if service:
        dt_params["service_description"] = service
    if start_time:
        dt_params["start_time"] = start_time

    raw = await client.get("/downtimes", params=dt_params, backends=be)
    all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])

    needle = comment.lower()
    matching = [d for d in all_dts if needle in str(d.get("comment", "")).lower()]
    host_dts = [d for d in matching if not d.get("service_description")]
    svc_dts = [d for d in matching if d.get("service_description")]

    async def _del_one(endpoint: str, dt_id: int) -> tuple[int, Any, ThrukError | None]:
        try:
            resp = await client.post(endpoint, data={"downtime_id": dt_id}, backends=be)
            return dt_id, resp, None
        except ThrukError as exc:
            return dt_id, None, exc

    host_ep = f"/hosts/{_seg(host)}/cmd/del_downtime"
    host_coros = [_del_one(host_ep, d["id"]) for d in host_dts if d.get("id") is not None]
    svc_coros = [
        _del_one(
            f"/services/{_seg(host)}/{_seg(str(d['service_description']))}/cmd/del_downtime",
            d["id"],
        )
        for d in svc_dts
        if d.get("id") is not None
    ]

    host_results: list[tuple[int, Any, ThrukError | None]] = list(await asyncio.gather(*host_coros))
    svc_results: list[tuple[int, Any, ThrukError | None]] = list(await asyncio.gather(*svc_coros))

    def _split(
        rows: list[tuple[int, Any, ThrukError | None]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ok_rows = [{"downtime_id": i, "result": r} for i, r, e in rows if e is None]
        err_rows = [{"downtime_id": i, "error": str(e)} for i, _, e in rows if e is not None]
        return ok_rows, err_rows

    host_ok, host_err = _split(host_results)
    svc_ok, svc_err = _split(svc_results)

    return _tool_response(
        {
            "match_mode": "substring",
            "comment_substring": comment,
            "matched": len(matching),
            "host_downtimes_deleted": host_ok,
            "host_downtimes_errors": host_err,
            "service_downtimes_deleted": svc_ok,
            "service_downtimes_errors": svc_err,
        }
    )


# ---------------------------------------------------------------------------
# Resources & prompts (issue #147 — server.py split)
# ---------------------------------------------------------------------------
# Resource handlers (``_host_resource``, ``_service_resource``, ...) moved to
# :mod:`thruk_mcp.resources`; prompt templates (``investigate_alert``,
# ``schedule_maintenance``, ``diagnose_flapping``) moved to
# :mod:`thruk_mcp.prompts`.  Both sets are re-imported at the top of this
# module so external callers and tests keep working unchanged.


# ---------------------------------------------------------------------------
# Semantic problem-management tools (issue #52)
# ---------------------------------------------------------------------------


async def thruk_oldest_problems(
    limit: int = 20,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Unhandled problems sorted by age (oldest first).

    Combines DOWN/UNREACHABLE hosts and WARNING/CRITICAL/UNKNOWN services that
    are neither acknowledged nor in scheduled downtime. Results are merged and
    sorted by ``last_state_change`` ascending so the longest-standing problems
    appear first.

    Returns a flat list of ``{host, service, state, since, duration_human}``
    (at most ``limit`` items, default 20).

    Optional ``filter`` is a structured AND/OR tree scoping both underlying
    ``/hosts`` and ``/services`` calls. Supported fields (see issue #226):
    ``hostgroup`` (``groups[gte]=`` on hosts, ``host_groups[gte]=`` on
    services) and ``custom_var`` (``_VARNAME=value`` on both). ``state`` and
    ``host`` are intentionally not exposed — the tool is already constrained
    to non-OK states and per-host filtering would duplicate
    ``thruk_list_hosts``.
    """
    now = _now_utc_epoch()
    host_params: dict[str, Any] = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "sort": "last_state_change",
        "columns": "name,state,last_state_change,peer_name",
        "limit": limit,
    }
    svc_params: dict[str, Any] = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "sort": "last_state_change",
        "columns": "host_name,description,state,last_state_change,peer_name",
        "limit": limit,
    }
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_OLDEST_PROBLEMS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_params.update(compile_filter(filter, "hosts"))
        svc_params.update(compile_filter(filter, "services"))
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
    return _tool_response(trimmed)


async def thruk_unacked_critical(
    threshold_minutes: int = 60,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """CRITICAL services and DOWN hosts not acknowledged for more than N minutes.

    ``threshold_minutes`` (default 60) sets the minimum duration a problem must
    have been active without acknowledgement to be included.

    Returns ``[{host, service, state, duration_minutes}]`` sorted by
    ``duration_minutes`` descending (longest-unacked first).

    Optional ``filter`` is a structured AND/OR tree scoping both underlying
    ``/hosts`` and ``/services`` calls. Supported fields (see issue #227):
    ``hostgroup`` (``groups[gte]=`` on hosts, ``host_groups[gte]=`` on
    services) and ``custom_var`` (``_VARNAME=value`` on both). ``state`` is
    intentionally not exposed — the tool is hardcoded to CRITICAL/DOWN by
    design; ``host`` is excluded to avoid ambiguity with the internal
    host-name resolution logic.
    """
    now = _now_utc_epoch()
    threshold_ts = now - threshold_minutes * 60

    host_params: dict[str, Any] = {
        "state[gte]": 1,
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "last_state_change[lte]": threshold_ts,
        "columns": "name,state,last_state_change,peer_name",
        "limit": 500,
    }
    svc_params: dict[str, Any] = {
        "state": 2,  # CRITICAL only
        "acknowledged": 0,
        "scheduled_downtime_depth": 0,
        "last_state_change[lte]": threshold_ts,
        "columns": "host_name,description,state,last_state_change,peer_name",
        "limit": 500,
    }
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_UNACKED)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_params.update(compile_filter(filter, "hosts"))
        svc_params.update(compile_filter(filter, "services"))
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
    return _tool_response(rows)


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
    return _tool_response(rows)


#: Problem-state subset of ``/hosts/totals`` returned by :func:`thruk_problem_counts`.
_HOST_PROBLEM_KEYS: tuple[str, ...] = (
    "down",
    "unreachable",
    "down_and_unhandled",
    "unreachable_and_unhandled",
)
#: Problem-state subset of ``/services/totals`` returned by :func:`thruk_problem_counts`.
_SVC_PROBLEM_KEYS: tuple[str, ...] = (
    "warning",
    "critical",
    "unknown",
    "warning_and_unhandled",
    "critical_and_unhandled",
    "unknown_and_unhandled",
)


def _project_problem_counts(payload: Any, keys: tuple[str, ...]) -> dict[str, int]:
    """Project a ``/totals`` response down to the problem-state keys.

    Missing keys default to ``0`` so the response shape stays stable when
    Thruk omits zero-valued fields. Non-dict payloads (the empty-backend
    edge case) collapse to an all-zero dict.
    """
    src = payload if isinstance(payload, dict) else {}
    return {k: int(src.get(k) or 0) for k in keys}


async def thruk_problem_counts(
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Flat aggregate of unhealthy-state counts across hosts and services.

    Generic replacement for the old ``thruk_problems_by_hostgroup`` —
    rather than hard-coding the grouping dimension in the tool name, this
    tool exposes a structured ``filter`` and returns a stable flat shape
    suitable for any scope (hostgroup, servicegroup, custom_var, or no
    filter at all = global).

    Calls ``/hosts/totals`` and ``/services/totals`` concurrently and
    projects the response down to the non-OK / non-pending fields only:

    - hosts: ``down``, ``unreachable``, ``down_and_unhandled``,
      ``unreachable_and_unhandled``
    - services: ``warning``, ``critical``, ``unknown``,
      ``warning_and_unhandled``, ``critical_and_unhandled``,
      ``unknown_and_unhandled``

    Filter contract is identical to :func:`thruk_totals` (same fields,
    same param forwarding rules — see :data:`FIELDS_PROBLEM_COUNTS`).
    """
    host_params: dict[str, Any] = {}
    svc_params: dict[str, Any] = {}
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_PROBLEM_COUNTS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_filter = _strip_filter_field(filter, "servicegroup")
        if host_filter is not None:
            host_params = compile_filter(host_filter, "hosts")
        svc_params = compile_filter(filter, "services")
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/totals", params=host_params or None, backends=be),
        _get_client().get("/services/totals", params=svc_params or None, backends=be),
    )
    return _tool_response(
        {
            "hosts": _project_problem_counts(hosts, _HOST_PROBLEM_KEYS),
            "services": _project_problem_counts(services, _SVC_PROBLEM_KEYS),
        }
    )


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
        return _tool_response({"error": errs[0]})

    extra["type[~]"] = "^HOST ALERT"
    # Defence-in-depth (issues #176 / #193): Naemon Livestatus does not
    # exclude rows with ``type=NULL`` from regex filters, so class=0/5/6
    # entries can leak past ``type[~]``. ``class=1`` keeps the result set
    # strictly ALERT rows even when other constraints (e.g. ``state[gte]``)
    # match an unrelated row.
    extra["class"] = "1"
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
            + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


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
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-24h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-21 14:00:00"). Default: last 24 h.'
                ),
            },
            until=_OPT_STR,
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
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-24h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-21 14:00:00"). Default: last 24 h.'
                ),
            },
            until=_OPT_STR,
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
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-24h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-21 14:00:00"). Default: last 24 h.'
                ),
            },
            until=_OPT_STR,
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
    # ---------------------------------------------------------------- availability / SLA
    ToolSpec(
        name="thruk_host_availability",
        fn=thruk_host_availability,
        schema=_s(
            "host",
            host=_str("Host name"),
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-7d",
                "description": (
                    'Start of analysis window. Thruk relative time ("-7d", "-1m") '
                    'or ISO datetime ("2026-05-01 00:00:00"). Default: last 7 days. '
                    "Ignored when ``timeperiod`` is set."
                ),
            },
            until={
                **_OPT_STR,
                "description": (
                    "End of analysis window (same formats as ``since``). "
                    "Defaults to now. Ignored when ``timeperiod`` is set."
                ),
            },
            timeperiod={
                **_OPT_STR,
                "description": (
                    "Thruk-native time period shortcut: "
                    '"last24hours", "lastweek", "lastmonth", "thismonth", etc. '
                    "Overrides ``since``/``until`` when provided."
                ),
            },
            with_downtimes=_bool(
                "Count scheduled downtime periods as outages (withdowntimes=1).",
                default=False,
            ),
            include_soft_states=_bool(
                "Include soft state changes in calculations (includesoftstates=1).",
                default=False,
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_service_availability",
        fn=thruk_service_availability,
        schema=_s(
            "host",
            "service",
            host=_str("Host name"),
            service=_str("Service description"),
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-7d",
                "description": (
                    'Start of analysis window. Thruk relative time ("-7d", "-1m") '
                    'or ISO datetime ("2026-05-01 00:00:00"). Default: last 7 days. '
                    "Ignored when ``timeperiod`` is set."
                ),
            },
            until={
                **_OPT_STR,
                "description": (
                    "End of analysis window (same formats as ``since``). "
                    "Defaults to now. Ignored when ``timeperiod`` is set."
                ),
            },
            timeperiod={
                **_OPT_STR,
                "description": (
                    "Thruk-native time period shortcut: "
                    '"last24hours", "lastweek", "lastmonth", "thismonth", etc. '
                    "Overrides ``since``/``until`` when provided."
                ),
            },
            with_downtimes=_bool(
                "Count scheduled downtime periods as outages (withdowntimes=1).",
                default=False,
            ),
            include_soft_states=_bool(
                "Include soft state changes in calculations (includesoftstates=1).",
                default=False,
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_hostgroup_availability",
        fn=thruk_hostgroup_availability,
        schema=_s(
            "hostgroup",
            hostgroup=_str("Hostgroup name"),
            type={
                "type": "string",
                "default": "hosts",
                "enum": ["hosts", "services", "both"],
                "description": (
                    "What to return: 'hosts' (default), 'services', or 'both'. "
                    "Hosts return time_up_percent; services return time_ok_percent."
                ),
            },
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-7d",
                "description": (
                    'Start of analysis window. Thruk relative time ("-7d", "-1m") '
                    'or ISO datetime ("2026-05-01 00:00:00"). Default: last 7 days. '
                    "Ignored when ``timeperiod`` is set."
                ),
            },
            until={
                **_OPT_STR,
                "description": (
                    "End of analysis window (same formats as ``since``). "
                    "Defaults to now. Ignored when ``timeperiod`` is set."
                ),
            },
            timeperiod={
                **_OPT_STR,
                "description": (
                    "Thruk-native time period shortcut: "
                    '"last24hours", "lastweek", "lastmonth", "thismonth", etc. '
                    "Overrides ``since``/``until`` when provided."
                ),
            },
            with_downtimes=_bool(
                "Count scheduled downtime periods as outages (withdowntimes=1).",
                default=False,
            ),
            include_soft_states=_bool(
                "Include soft state changes in calculations (includesoftstates=1).",
                default=False,
            ),
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
        name="thruk_list_contacts",
        fn=thruk_list_contacts,
        schema=_s(
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_get_contact",
        fn=thruk_get_contact,
        schema=_s("contact", contact=_str("Contact name"), backends=_BACKENDS),
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
    ToolSpec(
        name="thruk_stats",
        fn=thruk_stats,
        schema=build_tool_schema(
            FIELDS_HOST_STATS,
            filter=filter_schema_property(FIELDS_HOST_STATS),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_totals",
        fn=thruk_totals,
        schema=build_tool_schema(
            FIELDS_TOTALS,
            filter=filter_schema_property(FIELDS_TOTALS),
            backends=_BACKENDS,
        ),
    ),
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
            retry_on_empty=_bool(
                desc=(
                    "Retry the /downtimes lookup once after a short delay if the first "
                    "query returns no matches. Works around Naemon's async command pipe "
                    "(issue #194). Default: True."
                ),
                default=True,
            ),
            retry_delay_seconds={
                "type": "number",
                "default": 2.0,
                "description": (
                    "Seconds to wait before the retry when retry_on_empty=True. "
                    "Set to 0 to disable the wait. Default: 2.0."
                ),
            },
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
        name="thruk_bulk_acknowledge",
        fn=thruk_bulk_acknowledge,
        schema=_s(
            author=_str(),
            comment=_str(),
            hostgroup=_OPT_STR,
            state={
                **_OPT_STR,
                "description": (
                    "Restrict to a single state: 'down' / 'unreachable' (hosts) or "
                    "'critical' / 'warning' / 'unknown' (services). "
                    "None (default) matches every non-OK problem."
                ),
            },
            hosts_only=_bool(default=False),
            services_only=_bool(default=False),
            sticky=_bool(default=True),
            notify=_bool(default=True),
            persistent=_bool(default=False),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_add_comment",
        fn=thruk_add_comment,
        schema=_s(
            "host",
            "comment",
            host=_str("Host name"),
            comment=_str("Free-form comment text to attach to the host or service."),
            service=_OPT_STR,
            author=_str(),
            persistent=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_delete_comment",
        fn=thruk_delete_comment,
        schema=_s(
            "comment_id",
            "host",
            comment_id=_int("Numeric comment id (as returned by thruk_list_comments)."),
            host=_str("Host name owning the comment."),
            service=_OPT_STR,
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
    ToolSpec(
        name="thruk_checks",
        fn=thruk_checks,
        schema=_s(
            "host",
            "enabled",
            host=_str("Host name"),
            enabled=_bool(
                "True to enable active checks, False to disable.",
            ),
            service={
                **_OPT_STR,
                "description": (
                    "Service description. Omit to target the host only "
                    "(use cascade=true to also cover all its services)."
                ),
            },
            cascade=_bool(
                "When true and no service is specified, also apply to all services "
                "of the host. Ignored when service is set.",
                default=False,
            ),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_notifications",
        fn=thruk_notifications,
        schema=_s(
            "host",
            "enabled",
            host=_str("Host name"),
            enabled=_bool(
                "True to enable notifications, False to disable.",
            ),
            service={
                **_OPT_STR,
                "description": (
                    "Service description. Omit to target the host only "
                    "(use cascade=true to also cover all its services)."
                ),
            },
            cascade=_bool(
                "When true and no service is specified, also apply to all services "
                "of the host. Ignored when service is set.",
                default=False,
            ),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    # -------------------------------------------------------- semantic problem tools (issue #52)
    ToolSpec(
        name="thruk_oldest_problems",
        fn=thruk_oldest_problems,
        schema=build_tool_schema(
            FIELDS_OLDEST_PROBLEMS,
            limit=_int("Maximum number of results (default 20).", default=20),
            filter=filter_schema_property(FIELDS_OLDEST_PROBLEMS),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_unacked_critical",
        fn=thruk_unacked_critical,
        schema=build_tool_schema(
            FIELDS_UNACKED,
            threshold_minutes=_int(
                "Minimum unacknowledged duration in minutes (default 60).", default=60
            ),
            filter=filter_schema_property(FIELDS_UNACKED),
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
        name="thruk_problem_counts",
        fn=thruk_problem_counts,
        schema=build_tool_schema(
            FIELDS_PROBLEM_COUNTS,
            filter=filter_schema_property(FIELDS_PROBLEM_COUNTS),
            backends=_BACKENDS,
        ),
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
            return [TextContent(type="text", text=audit.scrub(f"Error: {exc}"))]
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
    cfg = config or ThrukConfig.from_env()
    client = ThrukClient(cfg)
    # Bind the new client to the current context so that all tool coroutines
    # spawned from this event-loop context reach the right instance.  Each
    # build_server() call operates independently — no shared module-level state.
    _client_var.set(client)

    audit.configure(enabled=cfg.audit_log)

    # Build enabled tool set (read_only / allowlist filtering)
    enabled: dict[str, Any] = {}
    for name, fn in _TOOL_DISPATCH.items():
        if cfg.read_only and name in WRITE_TOOLS:
            continue
        if cfg.enabled_tools and not any(fnmatch.fnmatch(name, pat) for pat in cfg.enabled_tools):
            continue
        enabled[name] = fn

    wrapper = ThrukMCPServer(Server("thruk-mcp"), enabled, client, cfg)

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
