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

import fnmatch
import json
import logging
import re
from datetime import datetime
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from . import audit
from .client import ThrukClient, ThrukError
from .config import ThrukConfig
from .filters import (
    FIELDS_ALERTS,
    FIELDS_HOSTS,
    FIELDS_LOGS,
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

log = logging.getLogger("thruk_mcp.server")

# Tools that mutate the monitoring state. Used by:
# - read_only mode: removed entirely from the registry
# - audit log: wrapped to emit a JSON line per invocation
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "thruk_schedule_downtime",
        "thruk_schedule_host_services_downtime",
        "thruk_schedule_propagated_host_downtime",
        "thruk_schedule_hostgroup_downtime",
        "thruk_schedule_servicegroup_downtime",
        "thruk_delete_downtime",
        "thruk_delete_active_downtimes",
        "thruk_delete_downtimes_by_filter",
        "thruk_acknowledge",
        "thruk_remove_acknowledgement",
        "thruk_recheck",
        "thruk_run_background_query",
    }
)

HOST_STATES = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}
SERVICE_STATES = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
HOST_STATE_MAP = {"up": 0, "down": 1, "unreachable": 2, "0": 0, "1": 1, "2": 2}
SVC_STATE_MAP = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3, "0": 0, "1": 1, "2": 2, "3": 3}

# Default columns: tight by design to minimize LLM token usage. A typical
# Thruk host row has ~80 attributes (custom vars, perf_data expansions, ...);
# returning them all blows the context for no reason. Callers can always
# override via the `columns` argument or use `thruk_query` for the raw row.
DEFAULT_HOST_COLUMNS = (
    "name,state,plugin_output,last_check,last_state_change,"
    "acknowledged,scheduled_downtime_depth,notifications_enabled,"
    "current_attempt,max_check_attempts,peer_name"
)
DEFAULT_SERVICE_COLUMNS = (
    "host_name,description,state,plugin_output,last_check,last_state_change,"
    "acknowledged,scheduled_downtime_depth,notifications_enabled,"
    "current_attempt,max_check_attempts,peer_name"
)
DEFAULT_GROUP_COLUMNS = "name,alias,num_hosts,num_services,worst_host_state,worst_service_state"
DEFAULT_LOG_COLUMNS = "time,type,class,host_name,service_description,state,state_type,message"
# Notification-specific columns: contact_name and command_name are populated for class=3
# log entries; state_type is alert-only and always null for notifications.
DEFAULT_NOTIFICATION_COLUMNS = (
    "time,type,class,host_name,service_description,state,contact_name,command_name,message"
)
DEFAULT_DOWNTIME_COLUMNS = (
    "id,host_name,service_description,author,comment,"
    "start_time,end_time,fixed,duration,triggered_by,peer_name"
)
DEFAULT_COMMENT_COLUMNS = (
    "id,host_name,service_description,author,comment,entry_time,entry_type,persistent,peer_name"
)


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


def _ts(value: Any) -> str:
    if not value:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def _backends(backends: str | None) -> tuple[str, ...] | None:
    if backends is None:
        return None
    parts = tuple(b.strip() for b in backends.split(",") if b.strip())
    return parts or None


def _build_cv_params(
    custom_vars: dict | None,
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
    filter: dict | None = None,
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
    data = await _get_client().get(f"/hosts/{host}", backends=_backends(backends))
    return json.dumps(data, indent=2, default=str)


async def thruk_list_services(
    filter: dict | None = None,
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
    data = await _get_client().get(f"/services/{host}/{service}", backends=_backends(backends))
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
    filter: dict | None = None,
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
    hosts = await _get_client().get("/hosts/stats", backends=_backends(backends))
    services = await _get_client().get("/services/stats", backends=_backends(backends))
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
        now = int(datetime.now().timestamp())
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
    custom_vars: dict | None = None,
) -> str | None:
    """Resolve a hostgroup / custom-variable filter to a ``host_name[regex]`` pattern.

    The Naemon Livestatus ``log`` table exposes neither ``current_host_groups``
    nor custom-variable columns.  We work around this by querying ``/hosts``
    (which supports both) and building an explicit alternation regex from the
    matching host names.

    *hostgroup* and *custom_vars* may be combined: the ``/hosts`` call applies
    both filters simultaneously (logical AND), so we issue only one request.

    Returns ``None`` when no hosts match (caller should emit an empty result).
    """
    params: dict[str, str] = {"columns": "name", "limit": "1000"}
    if hostgroup:
        params["groups[gte]"] = hostgroup
    if custom_vars:
        params.update(_build_cv_params(custom_vars))
    data = await _get_client().get("/hosts", params=params, backends=_backends(backends))
    names = [row["name"] for row in (data if isinstance(data, list) else []) if row.get("name")]
    if not names:
        return None
    return f"^({'|'.join(re.escape(n) for n in names)})$"


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
    custom_vars: dict | None = None,
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
    if hostgroup or custom_vars:
        host_regex = await _resolve_hosts_to_regex(
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
    return await _get_client().get_with_fallback(
        path, params=params, backends=_backends(backends), method="POST"
    )


async def _resolve_log_filter(
    filter_node: dict | None,
    allowed_fields: frozenset,
    backends: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate + compile a log-family filter.

    Returns ``(extra_params, error_list)``. On error, ``error_list`` is
    non-empty and ``extra_params`` is empty.  Hostgroup/custom_var fields
    are resolved via a ``/hosts`` lookup.
    """
    if filter_node is None:
        return {}, []
    try:
        validate_filter(filter_node, allowed_fields)
        direct_node, lookup_node = extract_log_lookup_fields(filter_node)
    except FilterError as exc:
        return {}, [str(exc)]

    extra: dict[str, Any] = {}
    if direct_node is not None:
        extra.update(compile_filter(direct_node, "logs"))
    if lookup_node is not None:
        lookup_params = compile_filter(lookup_node, "hosts")
        host_regex = await _resolve_hosts_to_regex_from_params(lookup_params, backends)
        if host_regex is None:
            return {}, ["No hosts matched the hostgroup/custom_var filter"]
        extra["host_name[regex]"] = host_regex
    return extra, []


async def _resolve_hosts_to_regex_from_params(
    params: dict[str, Any], backends: str | None
) -> str | None:
    """Like _resolve_hosts_to_regex but accepts a pre-built params dict."""
    host_params: dict[str, Any] = {"columns": "name", "limit": "1000", **params}
    data = await _get_client().get("/hosts", params=host_params, backends=_backends(backends))
    names = [r["name"] for r in (data if isinstance(data, list) else []) if r.get("name")]
    if not names:
        return None
    return f"^({'|'.join(re.escape(n) for n in names)})$"


async def thruk_list_logs(
    filter: dict | None = None,
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
    extra, errs = await _resolve_log_filter(filter, FIELDS_LOGS, backends)
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
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


async def thruk_list_alerts(
    filter: dict | None = None,
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
    extra, errs = await _resolve_log_filter(filter, FIELDS_ALERTS, backends)
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
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


async def thruk_list_notifications(
    filter: dict | None = None,
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
    extra, errs = await _resolve_log_filter(filter, FIELDS_NOTIFICATIONS, backends)
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
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


async def thruk_recent_events(
    filter: dict | None = None,
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
    extra, errs = await _resolve_log_filter(filter, FIELDS_LOGS, backends)
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
    if warnings:
        return json.dumps({"data": data, "_warnings": warnings}, indent=2, default=str)
    return json.dumps(data, indent=2, default=str)


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
        method.upper(),
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
    result = await _get_client().run_background(
        path,
        method=method.upper(),
        params=params,
        data=data,
        backends=_backends(backends),
        poll_timeout=poll_timeout,
    )
    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Downtime helper (module-level)
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
        f"/services/{host}/{service}/cmd/schedule_svc_downtime"
        if service
        else f"/hosts/{host}/cmd/schedule_host_downtime"
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
        f"/services/{host}/{service}/cmd/acknowledge_svc_problem"
        if service
        else f"/hosts/{host}/cmd/acknowledge_host_problem"
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
        f"/services/{host}/{service}/cmd/remove_svc_acknowledgement"
        if service
        else f"/hosts/{host}/cmd/remove_host_acknowledgement"
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
        endpoint = f"/services/{host}/{service}/cmd/{cmd}"
    else:
        cmd = "schedule_forced_host_check" if forced else "schedule_host_check"
        endpoint = f"/hosts/{host}/cmd/{cmd}"
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
        dt = await client.get(f"/downtimes/{downtime_id}", backends=be)
        svc_desc = dt.get("service_description") if isinstance(dt, dict) else None
        service = svc_desc or None

    endpoint = (
        f"/services/{host}/{service}/cmd/del_downtime"
        if service
        else f"/hosts/{host}/cmd/del_downtime"
    )
    return json.dumps(
        await client.post(endpoint, data={"downtime_id": str(downtime_id)}, backends=be),
        indent=2,
        default=str,
    )


async def thruk_get_downtime(downtime_id: int, backends: str | None = None) -> str:
    """Get a single downtime by id."""
    data = await _get_client().get(f"/downtimes/{downtime_id}", backends=_backends(backends))
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
            f"/hosts/{host}/cmd/schedule_host_svc_downtime",
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
            f"/hosts/{host}/cmd/{cmd}",
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
            f"/hostgroups/{hostgroup}/cmd/{cmd}",
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
            f"/servicegroups/{servicegroup}/cmd/{cmd}",
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
    now = int(datetime.now().timestamp())
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

    deleted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for dt in downtimes:
        dt_id = dt.get("id")
        if dt_id is None:
            continue
        # Thruk REST exposes only `del_downtime` (not `del_svc_downtime` /
        # `del_host_downtime`) — the correct Nagios external command is inferred
        # from the resource path (issue #36).
        ep = (
            f"/services/{host}/{service}/cmd/del_downtime"
            if service
            else f"/hosts/{host}/cmd/del_downtime"
        )
        try:
            resp = await client.post(ep, data={"downtime_id": dt_id}, backends=be)
            deleted.append({"downtime_id": dt_id, "result": resp})
        except ThrukError as exc:
            errors.append({"downtime_id": dt_id, "error": str(exc)})

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
        raise ValueError("Provide at least one of host, hostgroup, service, start_time, comment.")
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

        host_deleted: list[dict[str, Any]] = []
        host_errors: list[dict[str, Any]] = []
        for dt in host_dts:
            dt_id = dt.get("id")
            if dt_id is None:
                continue
            try:
                resp = await client.post(
                    f"/hosts/{host}/cmd/del_downtime",
                    data={"downtime_id": dt_id},
                    backends=be,
                )
                host_deleted.append({"downtime_id": dt_id, "result": resp})
            except ThrukError as exc:
                host_errors.append({"downtime_id": dt_id, "error": str(exc)})

        result["host_downtimes_deleted"] = host_deleted
        result["host_downtimes_errors"] = host_errors

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Resources (module-level)
# ---------------------------------------------------------------------------


async def _host_resource(name: str) -> str:
    """Single host as a JSON document, addressable as thruk://hosts/<name>."""
    data = await _get_client().get(f"/hosts/{name}")
    return json.dumps(data, indent=2, default=str)


async def _service_resource(host: str, service: str) -> str:
    """Single service as a JSON document (thruk://services/<host>/<service>)."""
    data = await _get_client().get(f"/services/{host}/{service}")
    return json.dumps(data, indent=2, default=str)


async def _hostgroup_resource(name: str) -> str:
    """Host group config + members as JSON (thruk://hostgroups/<name>)."""
    data = await _get_client().get(f"/hostgroups/{name}")
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
    hosts = await _get_client().get("/hosts", params=host_params)
    services = await _get_client().get("/services", params=svc_params)
    return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)


async def _stats_resource() -> str:
    """Aggregated host/service stats (cached ~15s)."""
    hosts = await _get_client().get("/hosts/stats")
    services = await _get_client().get("/services/stats")
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
# build_server: registers module-level functions into a fresh FastMCP instance
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Explicit JSON Schemas — no annotation introspection, no Pydantic
# ---------------------------------------------------------------------------


def _s(*required: str, **props: Any) -> dict:
    """Shorthand to build a JSON-Schema object."""
    properties = {k: (v if isinstance(v, dict) else {"type": v}) for k, v in props.items()}
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _str(desc: str = "") -> dict:
    return {"type": "string", "description": desc} if desc else {"type": "string"}


def _int(desc: str = "", default: int | None = None) -> dict:
    d: dict = {"type": "integer"}
    if desc:
        d["description"] = desc
    if default is not None:
        d["default"] = default
    return d


def _bool(desc: str = "", default: bool | None = None) -> dict:
    d: dict = {"type": "boolean"}
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


_TOOL_SCHEMAS: dict[str, dict] = {
    "thruk_list_hosts": build_tool_schema(
        FIELDS_HOSTS,
        filter=filter_schema_property(FIELDS_HOSTS),
        limit=_int(default=50),
        offset=_int(default=0),
        sort=_str(),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_get_host": _s("host", host=_str("Host name"), backends=_BACKENDS),
    "thruk_list_services": build_tool_schema(
        FIELDS_SERVICES,
        filter=filter_schema_property(FIELDS_SERVICES),
        limit=_int(default=50),
        offset=_int(default=0),
        sort=_str(),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_get_service": _s(
        "host",
        "service",
        host=_str("Host name"),
        service=_str("Service description"),
        backends=_BACKENDS,
    ),
    "thruk_list_hostgroups": _s(
        limit=_int(default=100),
        offset=_int(default=0),
        sort=_str(),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_list_servicegroups": _s(
        limit=_int(default=100),
        offset=_int(default=0),
        sort=_str(),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_problems": build_tool_schema(
        FIELDS_PROBLEMS,
        filter=filter_schema_property(FIELDS_PROBLEMS),
        limit=_int(default=100),
        offset=_int(default=0),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_stats": _s(backends=_BACKENDS),
    "thruk_list_downtimes": _s(
        host=_OPT_STR,
        active_only=_bool(default=True),
        limit=_int(default=100),
        offset=_int(default=0),
        sort=_str(),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_get_downtime": _s("downtime_id", downtime_id=_int(), backends=_BACKENDS),
    "thruk_list_comments": _s(
        host=_OPT_STR,
        limit=_int(default=100),
        offset=_int(default=0),
        sort=_str(),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_sites": _s(),
    "thruk_list_logs": build_tool_schema(
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
    "thruk_list_alerts": build_tool_schema(
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
    "thruk_list_notifications": build_tool_schema(
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
    "thruk_recent_events": build_tool_schema(
        FIELDS_LOGS,
        filter=filter_schema_property(FIELDS_LOGS),
        hours=_int(default=1),
        only_alerts=_bool(default=False),
        limit=_int(default=100),
        offset=_int(default=0),
        columns=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_query": _s(
        "path",
        path=_str("Path after /thruk/r, e.g. /hosts/srv01/services"),
        method=_str(),
        params=_OPT_OBJ,
        data=_OPT_OBJ,
        backends=_BACKENDS,
    ),
    "thruk_run_background_query": _s(
        "path",
        path=_str("Path after /thruk/r"),
        method=_str(),
        params=_OPT_OBJ,
        data=_OPT_OBJ,
        backends=_BACKENDS,
        poll_timeout={"type": "number", "default": 300.0},
    ),
    # write tools
    "thruk_schedule_downtime": _s(
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
    "thruk_schedule_host_services_downtime": _s(
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
    "thruk_schedule_propagated_host_downtime": _s(
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
    "thruk_schedule_hostgroup_downtime": _s(
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
    "thruk_schedule_servicegroup_downtime": _s(
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
    "thruk_delete_downtime": _s(
        "downtime_id",
        "host",
        downtime_id=_int(),
        host=_str(),
        service=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_delete_active_downtimes": _s(
        "host",
        host=_str(),
        service=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_delete_downtimes_by_filter": _s(
        host=_OPT_STR,
        hostgroup=_OPT_STR,
        service=_OPT_STR,
        start_time=_OPT_STR,
        comment=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_acknowledge": _s(
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
    "thruk_remove_acknowledgement": _s(
        "host",
        host=_str(),
        service=_OPT_STR,
        backends=_BACKENDS,
    ),
    "thruk_recheck": _s(
        "host",
        host=_str("Host name"),
        service=_OPT_STR,
        forced=_bool(default=True),
        backends=_BACKENDS,
    ),
}

# ---------------------------------------------------------------------------
# Dispatch table: tool name → implementation coroutine
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, Any] = {
    "thruk_list_hosts": thruk_list_hosts,
    "thruk_get_host": thruk_get_host,
    "thruk_list_services": thruk_list_services,
    "thruk_get_service": thruk_get_service,
    "thruk_list_hostgroups": thruk_list_hostgroups,
    "thruk_list_servicegroups": thruk_list_servicegroups,
    "thruk_problems": thruk_problems,
    "thruk_stats": thruk_stats,
    "thruk_list_downtimes": thruk_list_downtimes,
    "thruk_get_downtime": thruk_get_downtime,
    "thruk_list_comments": thruk_list_comments,
    "thruk_sites": thruk_sites,
    "thruk_list_logs": thruk_list_logs,
    "thruk_list_alerts": thruk_list_alerts,
    "thruk_list_notifications": thruk_list_notifications,
    "thruk_recent_events": thruk_recent_events,
    "thruk_query": thruk_query,
    "thruk_run_background_query": thruk_run_background_query,
    "thruk_schedule_downtime": thruk_schedule_downtime,
    "thruk_schedule_host_services_downtime": thruk_schedule_host_services_downtime,
    "thruk_schedule_propagated_host_downtime": thruk_schedule_propagated_host_downtime,
    "thruk_schedule_hostgroup_downtime": thruk_schedule_hostgroup_downtime,
    "thruk_schedule_servicegroup_downtime": thruk_schedule_servicegroup_downtime,
    "thruk_delete_downtime": thruk_delete_downtime,
    "thruk_delete_active_downtimes": thruk_delete_active_downtimes,
    "thruk_delete_downtimes_by_filter": thruk_delete_downtimes_by_filter,
    "thruk_acknowledge": thruk_acknowledge,
    "thruk_remove_acknowledgement": thruk_remove_acknowledgement,
    "thruk_recheck": thruk_recheck,
}


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

    async def call_tool(self, name: str, arguments: dict) -> list[TextContent]:
        fn = self._enabled.get(name)
        if fn is None:
            raise ValueError(f"Unknown or disabled tool: {name!r}")
        try:
            result = await fn(**arguments)
        except TypeError as exc:
            if self._cfg.audit_log and name in WRITE_TOOLS:
                audit.log_call(
                    name, arguments, user=self._cfg.auth_user, status="error", error=str(exc)
                )
            raise ValueError(f"Invalid arguments for {name!r}: {exc}") from exc
        except ThrukError as exc:
            if self._cfg.audit_log and name in WRITE_TOOLS:
                audit.log_call(
                    name, arguments, user=self._cfg.auth_user, status="error", error=str(exc)
                )
            # Return as tool-level error content instead of raising.
            # Raising here causes the low-level MCP SDK to emit a protocol-level
            # McpError(-32603) which the client shows as the generic
            # "tool execution failed" message, discarding the actual Thruk error.
            return [TextContent(type="text", text=f"Error: {exc}")]
        if self._cfg.audit_log and name in WRITE_TOOLS:
            audit.log_call(name, arguments, user=self._cfg.auth_user, status="ok")
        return [TextContent(type="text", text=result)]

    async def run(self, read_stream, write_stream, init_options=None):
        await self._server.run(read_stream, write_stream, init_options)

    def create_initialization_options(self):
        return self._server.create_initialization_options()


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
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await wrapper.call_tool(name, arguments)

    return wrapper


# _apply_security_filters was removed: its logic is now inlined in build_server()
# (enabled dict filtering + audit logging in call_tool handler).
