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
import logging
from collections.abc import Coroutine
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
    _NOISY_CAP_HINT as _NOISY_CAP_HINT,
)
from .constants import (
    _NOISY_MAX_ALERTS as _NOISY_MAX_ALERTS,
)

# Log-family column defaults whose only in-server users (the logs/history
# tools) moved to ``tools/history.py`` (issue #260). Re-exported here so
# existing ``from thruk_mcp.server import <name>`` imports keep working.
from .constants import (
    DEFAULT_LOG_COLUMNS as DEFAULT_LOG_COLUMNS,
)
from .constants import (
    DEFAULT_NOTIFICATION_COLUMNS as DEFAULT_NOTIFICATION_COLUMNS,
)
from .constants import (
    HOST_STATE_INT,
    HOST_STATE_STR,
    SVC_STATE_INT,
    SVC_STATE_STR,
)

# Filter primitives whose only in-server users (the logs/history/trends tools)
# moved to ``tools/history.py`` (issue #260). Re-exported here so existing
# ``from thruk_mcp.server import <name>`` imports keep working unchanged.
from .filters import (
    FIELDS_ALERTS as FIELDS_ALERTS,
)
from .filters import (
    FIELDS_LOGS as FIELDS_LOGS,
)
from .filters import (
    FIELDS_NOISY_HOSTS as FIELDS_NOISY_HOSTS,
)
from .filters import (
    FIELDS_NOISY_SERVICES as FIELDS_NOISY_SERVICES,
)
from .filters import (
    FIELDS_NOTIFICATIONS as FIELDS_NOTIFICATIONS,
)

# Filter primitives whose only in-server users (the triage/analytics tools)
# moved to ``tools/triage.py`` (issue #259). Re-exported here so existing
# ``from thruk_mcp.server import <name>`` imports keep working unchanged.
from .filters import (
    FIELDS_OLDEST_PROBLEMS as FIELDS_OLDEST_PROBLEMS,
)
from .filters import (
    FIELDS_PROBLEM_COUNTS as FIELDS_PROBLEM_COUNTS,
)
from .filters import (
    FIELDS_STALE_ACKS as FIELDS_STALE_ACKS,
)
from .filters import (
    FIELDS_UNACKED as FIELDS_UNACKED,
)
from .filters import (
    FilterError as FilterError,
)
from .filters import (
    build_tool_schema as build_tool_schema,
)
from .filters import (
    compile_filter as compile_filter,
)
from .filters import (
    filter_schema_property as filter_schema_property,
)
from .filters import (
    infer_alert_type_regex as infer_alert_type_regex,
)
from .filters import (
    rewrite_custom_var_to_host_custom_var as rewrite_custom_var_to_host_custom_var,
)
from .filters import (
    validate_filter as validate_filter,
)

# Helpers whose only in-server users (the logs/history/trends tools) moved to
# ``tools/history.py`` (issue #260); kept as re-exports for backward compat.
from .helpers import (
    _RESOLVE_HOSTS_HARD_LIMIT as _RESOLVE_HOSTS_HARD_LIMIT,
)

# Shared helpers + inventory tools moved out of server.py (issue #258).
# Re-exported so ``from thruk_mcp.server import <name>`` keeps working.
from .helpers import (
    _backends,
    _client_var,
    _get_client,
    _now_utc_epoch,
    _resolve_peer_for_host,
    _seg,
    _tool_response,
)
from .helpers import (
    _build_cv_params as _build_cv_params,
)

# ``_decode_form_value``'s only in-server user (thruk_stale_acks) moved to
# ``tools/triage.py`` (issue #259); kept as a re-export for backward compat.
from .helpers import (
    _decode_form_value as _decode_form_value,
)
from .helpers import (
    _downtime_payload as _downtime_payload,
)
from .helpers import (
    _duration_human as _duration_human,
)
from .helpers import (
    _format_state_label as _format_state_label,
)
from .helpers import (
    _list_params as _list_params,
)
from .helpers import (
    _parse_thruk_time as _parse_thruk_time,
)
from .helpers import (
    _resolve_hosts_to_regex_from_params as _resolve_hosts_to_regex_from_params,
)
from .helpers import (
    _resolve_log_filter as _resolve_log_filter,
)
from .helpers import (
    _ts as _ts,
)
from .prompts import diagnose_flapping, investigate_alert, schedule_maintenance
from .resources import (
    _host_resource,
    _hostgroup_resource,
    _problems_resource,
    _service_resource,
    _stats_resource,
)
from .tools.base import (
    _BACKENDS,
    _OPT_INT,
    _OPT_OBJ,
    _OPT_STR,
    ToolSpec,
    _bool,
    _int,
    _s,
    _str,
)

# Re-exported for backward compat (defined but unused inside server.py itself):
from .tools.base import (
    _LOG_CUSTOM_VARS as _LOG_CUSTOM_VARS,
)
from .tools.base import (
    _LOG_HOSTGROUP as _LOG_HOSTGROUP,
)
from .tools.base import (
    _OPT_BOOL as _OPT_BOOL,
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
from .tools.history import (
    _BUCKET_SIZES as _BUCKET_SIZES,
)
from .tools.history import (
    _DEFAULT_SINCE as _DEFAULT_SINCE,
)

# Logs / history / trends tools moved out of server.py (issue #260, parent
# #256). ``HISTORY_TRENDS_REGISTRY`` / ``HISTORY_LOGS_REGISTRY`` are spliced
# into ``TOOL_REGISTRY`` below (preserving the original, non-contiguous
# registration order); the tool functions and the private helpers
# (``_resolve_hosts_to_regex``, ``_fetch_logs``, ``_aggregate_alerts``,
# ``_coerce_hours_to_since``) plus ``_BUCKET_SIZES`` / ``_DEFAULT_SINCE`` are
# re-exported here for backward compatibility.
from .tools.history import (
    HISTORY_LOGS_REGISTRY,
    HISTORY_TRENDS_REGISTRY,
)
from .tools.history import (
    _aggregate_alerts as _aggregate_alerts,
)
from .tools.history import (
    _coerce_hours_to_since as _coerce_hours_to_since,
)
from .tools.history import (
    _fetch_logs as _fetch_logs,
)
from .tools.history import (
    _resolve_hosts_to_regex as _resolve_hosts_to_regex,
)
from .tools.history import (
    thruk_alert_heatmap as thruk_alert_heatmap,
)
from .tools.history import (
    thruk_flap_summary as thruk_flap_summary,
)
from .tools.history import (
    thruk_list_alerts as thruk_list_alerts,
)
from .tools.history import (
    thruk_list_logs as thruk_list_logs,
)
from .tools.history import (
    thruk_list_notifications as thruk_list_notifications,
)
from .tools.history import (
    thruk_recent_events as thruk_recent_events,
)
from .tools.history import (
    thruk_recurring_problems as thruk_recurring_problems,
)
from .tools.history import (
    thruk_top_noisy_hosts as thruk_top_noisy_hosts,
)
from .tools.history import (
    thruk_top_noisy_services as thruk_top_noisy_services,
)
from .tools.inventory import INVENTORY_REGISTRY
from .tools.inventory import (
    _collect_hostgroup_constraints as _collect_hostgroup_constraints,
)
from .tools.inventory import (
    _ensure_columns_param as _ensure_columns_param,
)
from .tools.inventory import (
    _row_matches_hostgroup_constraints as _row_matches_hostgroup_constraints,
)
from .tools.inventory import (
    _strip_filter_field as _strip_filter_field,
)
from .tools.inventory import (
    thruk_get_contact as thruk_get_contact,
)
from .tools.inventory import (
    thruk_get_host as thruk_get_host,
)
from .tools.inventory import (
    thruk_get_service as thruk_get_service,
)
from .tools.inventory import (
    thruk_host_availability as thruk_host_availability,
)
from .tools.inventory import (
    thruk_hostgroup_availability as thruk_hostgroup_availability,
)
from .tools.inventory import (
    thruk_list_comments as thruk_list_comments,
)
from .tools.inventory import (
    thruk_list_contacts as thruk_list_contacts,
)
from .tools.inventory import (
    thruk_list_downtimes as thruk_list_downtimes,
)
from .tools.inventory import (
    thruk_list_hostgroups as thruk_list_hostgroups,
)
from .tools.inventory import (
    thruk_list_hosts as thruk_list_hosts,
)
from .tools.inventory import (
    thruk_list_servicegroups as thruk_list_servicegroups,
)
from .tools.inventory import (
    thruk_list_services as thruk_list_services,
)
from .tools.inventory import (
    thruk_problems as thruk_problems,
)
from .tools.inventory import (
    thruk_service_availability as thruk_service_availability,
)
from .tools.inventory import (
    thruk_sites as thruk_sites,
)
from .tools.inventory import (
    thruk_stats as thruk_stats,
)
from .tools.inventory import (
    thruk_totals as thruk_totals,
)

# Semantic triage / analytics tools moved out of server.py (issue #259, parent
# #256). ``TRIAGE_REGISTRY`` is spliced into ``TOOL_REGISTRY`` below; the tool
# functions and ``_project_problem_counts`` are re-exported for backward compat.
from .tools.triage import TRIAGE_REGISTRY
from .tools.triage import (
    _project_problem_counts as _project_problem_counts,
)
from .tools.triage import (
    thruk_concurrent_failures as thruk_concurrent_failures,
)
from .tools.triage import (
    thruk_oldest_problems as thruk_oldest_problems,
)
from .tools.triage import (
    thruk_problem_counts as thruk_problem_counts,
)
from .tools.triage import (
    thruk_stale_acks as thruk_stale_acks,
)
from .tools.triage import (
    thruk_unacked_critical as thruk_unacked_critical,
)

__all__ = ["WRITE_TOOLS", "ThrukMCPServer", "build_server"]

# ``_NOISY_CAP_HINT`` moved to :mod:`thruk_mcp.constants` (issue #259) so it can
# be shared with ``tools/triage.py`` without a server <-> triage import cycle.
# Re-exported above via ``from .constants import _NOISY_CAP_HINT as ...``.

log = logging.getLogger("thruk_mcp.server")

# ``_RESOLVE_HOSTS_HARD_LIMIT`` moved to :mod:`thruk_mcp.helpers` (issue #258).

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


# ---------------------------------------------------------------------------
# Inventory / listing / availability tools - moved to tools/inventory.py
# (issue #258). The 17 read-only tools (thruk_list_hosts ... thruk_sites) and
# their private helpers now live in :mod:`thruk_mcp.tools.inventory` together
# with ``INVENTORY_REGISTRY``; re-exported above for backward compatibility.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Logs / history / trends tools moved to :mod:`thruk_mcp.tools.history`
# (issue #260, parent #256): thruk_top_noisy_hosts, thruk_top_noisy_services,
# thruk_flap_summary, thruk_alert_heatmap, thruk_recurring_problems,
# thruk_list_logs, thruk_list_alerts, thruk_list_notifications and
# thruk_recent_events, plus the private helpers _resolve_hosts_to_regex,
# _fetch_logs, _aggregate_alerts, _coerce_hours_to_since and the
# _BUCKET_SIZES / _DEFAULT_SINCE tables. They are re-imported at the top of
# this module and spliced into TOOL_REGISTRY via *HISTORY_TRENDS_REGISTRY and
# *HISTORY_LOGS_REGISTRY below.
# ---------------------------------------------------------------------------


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
# Semantic triage / analytics tools moved to :mod:`thruk_mcp.tools.triage`
# (issue #259, parent #256): thruk_oldest_problems, thruk_unacked_critical,
# thruk_stale_acks, thruk_problem_counts, thruk_concurrent_failures and the
# _project_problem_counts helper. They are re-imported at the top of this
# module and spliced into TOOL_REGISTRY via *TRIAGE_REGISTRY below.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# build_server: registers module-level functions into a fresh FastMCP instance
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Explicit JSON Schemas + ToolSpec — extracted to ``tools/base.py`` (issue #257).
# Re-exported above via ``from .tools.base import ...`` for backward compat:
# ``_s``, ``_str``, ``_int``, ``_bool``, ``_OPT_*``, ``_LOG_*``, ``_BACKENDS``,
# ``ToolSpec``.  No annotation introspection, no Pydantic.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TOOL_REGISTRY: one entry per tool (issue #85)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: list[ToolSpec] = [
    # ----------------------------------------------- noisy / flap / trends & history (issue #57)
    # Noisy / flap / trends ToolSpec entries live in
    # ``thruk_mcp.tools.history`` (issue #260); spliced here via
    # ``HISTORY_TRENDS_REGISTRY`` to preserve registration order.
    *HISTORY_TRENDS_REGISTRY,
    # ---------------------------------------------------------------- host / service listing
    # Inventory listing / availability / problems tools live in
    # ``thruk_mcp.tools.inventory`` (issue #258); their ToolSpec entries are
    # spliced in here via ``INVENTORY_REGISTRY`` to preserve registration order.
    *INVENTORY_REGISTRY,
    ToolSpec(
        name="thruk_get_downtime",
        fn=thruk_get_downtime,
        schema=_s("downtime_id", downtime_id=_int(), backends=_BACKENDS),
    ),
    # ---------------------------------------------------------------- log / alert / notification
    # Log / alert / notification ToolSpec entries live in
    # ``thruk_mcp.tools.history`` (issue #260); spliced here via
    # ``HISTORY_LOGS_REGISTRY`` to preserve registration order.
    *HISTORY_LOGS_REGISTRY,
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
    # Semantic triage / analytics tools (issues #52 / #54) — moved to
    # tools/triage.py (issue #259). Spliced here to preserve registration order.
    *TRIAGE_REGISTRY,
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
