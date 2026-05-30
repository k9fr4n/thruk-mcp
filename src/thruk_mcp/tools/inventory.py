"""Read-only listing / inventory / availability tools (issue #258 - server.py split).

Parent: #256. This module hosts the 17 read-only listing/inventory tools
(``thruk_list_hosts`` ... ``thruk_sites``), their availability/SLA siblings,
and the private helpers they rely on (``_collect_hostgroup_constraints``,
``_row_matches_hostgroup_constraints``, ``_ensure_columns_param``,
``_strip_filter_field``).

The co-located ``INVENTORY_REGISTRY: list[ToolSpec]`` keeps each tool name,
implementation and explicit JSON Schema in one place; ``server.py`` splices
it into the global ``TOOL_REGISTRY`` and re-exports every symbol here for
backward compatibility.

Shared infrastructure (time parsing, log-family host resolution) lives in
:mod:`thruk_mcp.helpers` so this module never imports ``server``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

from ..constants import (
    DEFAULT_COMMENT_COLUMNS,
    DEFAULT_CONTACT_COLUMNS,
    DEFAULT_DOWNTIME_COLUMNS,
    DEFAULT_GROUP_COLUMNS,
    DEFAULT_HOST_COLUMNS,
    DEFAULT_SERVICE_COLUMNS,
    LATENCY_SANITY_CAP_SECONDS,
)
from ..filters import (
    FIELDS_COMMENTS,
    FIELDS_DOWNTIMES,
    FIELDS_HOST_STATS,
    FIELDS_HOSTS,
    FIELDS_PROBLEMS,
    FIELDS_SERVICES,
    FIELDS_TOTALS,
    FilterError,
    build_tool_schema,
    compile_filter,
    compile_filter_problems,
    filter_schema_property,
    rewrite_custom_var_to_host_custom_var,
    validate_filter,
)
from ..helpers import (
    _RESOLVE_HOSTS_HARD_LIMIT,
    _backends,
    _get_client,
    _list_params,
    _now_utc_epoch,
    _parse_thruk_time,
    _resolve_log_filter,
    _sanitize_latency,
    _seg,
    _tool_response,
)
from .base import (
    _BACKENDS,
    _OPT_STR,
    ToolSpec,
    _bool,
    _int,
    _s,
    _str,
)


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
        # Issue #244: on /services, a host-level custom_var lives under the
        # host_custom_variables column (_HOST{VAR}), not _{VAR}. Rewrite the
        # leaves before compiling so the services sub-query is not silently
        # empty when the filter contains a custom_var leaf.
        svc_params = compile_filter(rewrite_custom_var_to_host_custom_var(filter), "services")
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
        # Issue #244: rewrite host-level custom_var → host_custom_var on the
        # services side so it compiles to _HOST{VAR} (not _{VAR}).
        svc_params = compile_filter(rewrite_custom_var_to_host_custom_var(filter), "services")
    be = _backends(backends)
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/totals", params=host_params or None, backends=be),
        _get_client().get("/services/totals", params=svc_params or None, backends=be),
    )
    return _tool_response({"hosts": hosts, "services": services})


async def thruk_list_downtimes(
    filter: dict[str, Any] | None = None,
    active_only: bool = True,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-start_time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List scheduled downtimes.

    ``filter`` fields: ``host`` (forwarded directly to ``/downtimes`` as
    ``host_name[...]=``), ``hostgroup`` and ``custom_var`` (resolved via a
    secondary ``/hosts`` lookup and applied as ``host_name[regex]=...``).
    OR on ``hostgroup`` / ``custom_var`` is not supported (same constraint
    as the log-family tools). See issue #229.
    """
    # issue #229: replace the bare ``host: str | None`` param with the
    # structured ``filter`` tree shared by the rest of the read tools.
    # Pre-fix the only way to scope downtimes by hostgroup was to fetch
    # everything and filter client-side; ``_resolve_log_filter`` reuses the
    # ``/hosts`` lookup pattern to compile hostgroup/custom_var filters into
    # a single ``host_name[regex]=...`` parameter.
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_DOWNTIMES, backends)
    if errs:
        return _tool_response({"error": errs[0]})
    params = _list_params(limit, offset, sort, columns, DEFAULT_DOWNTIME_COLUMNS)
    params.update(extra)
    if active_only:
        now = _now_utc_epoch()
        params["start_time[lte]"] = now
        params["end_time[gte]"] = now
    data = await _get_client().get("/downtimes", params=params, backends=_backends(backends))
    warnings_: list[str] = []
    if host_truncated:
        warnings_.append(
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    return _tool_response(data, warnings_ or None)


async def thruk_list_comments(
    filter: dict[str, Any] | None = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "-entry_time",
    columns: str | None = None,
    backends: str | None = None,
) -> str:
    """List comments (acknowledgements appear here too).

    ``filter`` fields: ``host`` (forwarded directly to ``/comments`` as
    ``host_name[...]=``), ``hostgroup`` and ``custom_var`` (resolved via a
    secondary ``/hosts`` lookup and applied as ``host_name[regex]=...``).
    OR on ``hostgroup`` / ``custom_var`` is not supported (same constraint
    as the log-family tools). See issue #230.
    """
    # issue #230: replace the bare ``host: str | None`` param with the
    # structured ``filter`` tree shared by the rest of the read tools.
    # Pre-fix the only way to scope comments by hostgroup was to fetch
    # everything and filter client-side; ``_resolve_log_filter`` reuses the
    # ``/hosts`` lookup pattern to compile hostgroup/custom_var filters into
    # a single ``host_name[regex]=...`` parameter.
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_COMMENTS, backends)
    if errs:
        return _tool_response({"error": errs[0]})
    params = _list_params(limit, offset, sort, columns, DEFAULT_COMMENT_COLUMNS)
    params.update(extra)
    data = await _get_client().get("/comments", params=params, backends=_backends(backends))
    warnings_: list[str] = []
    if host_truncated:
        warnings_.append(
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    return _tool_response(data, warnings_ or None)


async def thruk_sites() -> str:
    """List configured Thruk backends (sites)."""
    return _tool_response(await _get_client().get("/sites"))


# ---------------------------------------------------------------------------
# INVENTORY_REGISTRY: ToolSpec entries co-located with their tools (issue #258)
# Order mirrors the original TOOL_REGISTRY ordering in server.py.
# ---------------------------------------------------------------------------
INVENTORY_REGISTRY: list[ToolSpec] = [
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
        schema=build_tool_schema(
            FIELDS_DOWNTIMES,
            filter=filter_schema_property(FIELDS_DOWNTIMES),
            active_only=_bool(default=True),
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_comments",
        fn=thruk_list_comments,
        schema=build_tool_schema(
            FIELDS_COMMENTS,
            filter=filter_schema_property(FIELDS_COMMENTS),
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_str(),
            columns=_OPT_STR,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(name="thruk_sites", fn=thruk_sites, schema=_s()),
]


__all__ = [
    "INVENTORY_REGISTRY",
    "thruk_get_contact",
    "thruk_get_host",
    "thruk_get_service",
    "thruk_host_availability",
    "thruk_hostgroup_availability",
    "thruk_list_comments",
    "thruk_list_contacts",
    "thruk_list_downtimes",
    "thruk_list_hostgroups",
    "thruk_list_hosts",
    "thruk_list_servicegroups",
    "thruk_list_services",
    "thruk_problems",
    "thruk_service_availability",
    "thruk_sites",
    "thruk_stats",
    "thruk_totals",
]
