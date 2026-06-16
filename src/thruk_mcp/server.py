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

import fnmatch
import logging
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
from .helpers import (
    _build_cv_params as _build_cv_params,
)
from .helpers import (
    _cfg_var as _cfg_var,
)

# Shared helpers + inventory tools moved out of server.py (issue #258).
# Re-exported so ``from thruk_mcp.server import <name>`` keeps working.
from .helpers import (
    _client_var,
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
    _epoch_filter_value as _epoch_filter_value,
)
from .helpers import (
    _format_state_label as _format_state_label,
)
from .helpers import (
    _get_cfg as _get_cfg,
)
from .helpers import (
    _list_params as _list_params,
)
from .helpers import (
    _now_utc_epoch as _now_utc_epoch,
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
from .prompts import (
    capacity_review,
    daily_health_report,
    diagnose_flapping,
    incident_triage,
    investigate_alert,
    noise_review,
    schedule_maintenance,
    sla_report,
)
from .resources import (
    _host_resource,
    _hostgroup_resource,
    _problems_resource,
    _service_resource,
    _stats_resource,
)
from .tools import (
    _ALLOWED_METHODS as _ALLOWED_METHODS,
)
from .tools import (
    _BACKENDS as _BACKENDS,
)
from .tools import (
    _BUCKET_SIZES as _BUCKET_SIZES,
)
from .tools import (
    _COLUMNS as _COLUMNS,
)
from .tools import (
    _DEFAULT_SINCE as _DEFAULT_SINCE,
)
from .tools import (
    _LOG_CUSTOM_VARS as _LOG_CUSTOM_VARS,
)
from .tools import (
    _LOG_HOSTGROUP as _LOG_HOSTGROUP,
)
from .tools import (
    _OPT_BOOL as _OPT_BOOL,
)
from .tools import (
    _OPT_INT as _OPT_INT,
)
from .tools import (
    _OPT_OBJ as _OPT_OBJ,
)
from .tools import (
    _OPT_STR as _OPT_STR,
)
from .tools import (
    _REST_PATH_PREFIXES as _REST_PATH_PREFIXES,
)
from .tools import (
    _SINCE as _SINCE,
)
from .tools import (
    _UNTIL as _UNTIL,
)

# All tool functions, co-located ToolSpec registries and the aggregated
# ``TOOL_REGISTRY`` now live in :mod:`thruk_mcp.tools` (issue #262, parent
# #256). They are re-exported here so existing
# ``from thruk_mcp.server import <name>`` imports keep working unchanged.
from .tools import (
    COMMANDS_READ_REGISTRY as COMMANDS_READ_REGISTRY,
)
from .tools import (
    COMMANDS_WRITE_REGISTRY as COMMANDS_WRITE_REGISTRY,
)
from .tools import (
    ESCAPE_REGISTRY as ESCAPE_REGISTRY,
)
from .tools import (
    HISTORY_LOGS_REGISTRY as HISTORY_LOGS_REGISTRY,
)
from .tools import (
    HISTORY_REGISTRY as HISTORY_REGISTRY,
)
from .tools import (
    HISTORY_TRENDS_REGISTRY as HISTORY_TRENDS_REGISTRY,
)
from .tools import (
    INVENTORY_REGISTRY as INVENTORY_REGISTRY,
)
from .tools import (
    TOOL_REGISTRY as TOOL_REGISTRY,
)
from .tools import (
    TRIAGE_REGISTRY as TRIAGE_REGISTRY,
)
from .tools import (
    ToolSpec as ToolSpec,
)
from .tools import (
    _aggregate_alerts as _aggregate_alerts,
)
from .tools import (
    _bool as _bool,
)
from .tools import (
    _coerce_hours_to_since as _coerce_hours_to_since,
)
from .tools import (
    _collect_hostgroup_constraints as _collect_hostgroup_constraints,
)
from .tools import (
    _delete_downtimes_by_host_comment as _delete_downtimes_by_host_comment,
)
from .tools import (
    _ensure_columns_param as _ensure_columns_param,
)
from .tools import (
    _fetch_logs as _fetch_logs,
)
from .tools import (
    _int as _int,
)
from .tools import (
    _project_problem_counts as _project_problem_counts,
)
from .tools import (
    _resolve_hosts_to_regex as _resolve_hosts_to_regex,
)
from .tools import (
    _row_matches_hostgroup_constraints as _row_matches_hostgroup_constraints,
)
from .tools import (
    _s as _s,
)
from .tools import (
    _sort as _sort,
)
from .tools import (
    _str as _str,
)
from .tools import (
    _strip_filter_field as _strip_filter_field,
)
from .tools import (
    _validate_rest_path as _validate_rest_path,
)
from .tools import (
    thruk_acknowledge as thruk_acknowledge,
)
from .tools import (
    thruk_add_comment as thruk_add_comment,
)
from .tools import (
    thruk_alert_heatmap as thruk_alert_heatmap,
)
from .tools import (
    thruk_bulk_acknowledge as thruk_bulk_acknowledge,
)
from .tools import (
    thruk_checks as thruk_checks,
)
from .tools import (
    thruk_concurrent_failures as thruk_concurrent_failures,
)
from .tools import (
    thruk_delete_active_downtimes as thruk_delete_active_downtimes,
)
from .tools import (
    thruk_delete_comment as thruk_delete_comment,
)
from .tools import (
    thruk_delete_downtime as thruk_delete_downtime,
)
from .tools import (
    thruk_delete_downtimes_by_filter as thruk_delete_downtimes_by_filter,
)
from .tools import (
    thruk_flap_summary as thruk_flap_summary,
)
from .tools import (
    thruk_get_contact as thruk_get_contact,
)
from .tools import (
    thruk_get_downtime as thruk_get_downtime,
)
from .tools import (
    thruk_get_host as thruk_get_host,
)
from .tools import (
    thruk_get_service as thruk_get_service,
)
from .tools import (
    thruk_host_availability as thruk_host_availability,
)
from .tools import (
    thruk_hostgroup_availability as thruk_hostgroup_availability,
)
from .tools import (
    thruk_list_alerts as thruk_list_alerts,
)
from .tools import (
    thruk_list_comments as thruk_list_comments,
)
from .tools import (
    thruk_list_contacts as thruk_list_contacts,
)
from .tools import (
    thruk_list_downtimes as thruk_list_downtimes,
)
from .tools import (
    thruk_list_hostgroups as thruk_list_hostgroups,
)
from .tools import (
    thruk_list_hosts as thruk_list_hosts,
)
from .tools import (
    thruk_list_logs as thruk_list_logs,
)
from .tools import (
    thruk_list_notifications as thruk_list_notifications,
)
from .tools import (
    thruk_list_servicegroups as thruk_list_servicegroups,
)
from .tools import (
    thruk_list_services as thruk_list_services,
)
from .tools import (
    thruk_notification_heatmap as thruk_notification_heatmap,
)
from .tools import (
    thruk_notification_summary as thruk_notification_summary,
)
from .tools import (
    thruk_notifications as thruk_notifications,
)
from .tools import (
    thruk_oldest_problems as thruk_oldest_problems,
)
from .tools import (
    thruk_problem_counts as thruk_problem_counts,
)
from .tools import (
    thruk_problems as thruk_problems,
)
from .tools import (
    thruk_query as thruk_query,
)
from .tools import (
    thruk_recent_events as thruk_recent_events,
)
from .tools import (
    thruk_recheck as thruk_recheck,
)
from .tools import (
    thruk_recurring_problems as thruk_recurring_problems,
)
from .tools import (
    thruk_reliability_report as thruk_reliability_report,
)
from .tools import (
    thruk_remove_acknowledgement as thruk_remove_acknowledgement,
)
from .tools import (
    thruk_run_background_query as thruk_run_background_query,
)
from .tools import (
    thruk_schedule_downtime as thruk_schedule_downtime,
)
from .tools import (
    thruk_schedule_host_services_downtime as thruk_schedule_host_services_downtime,
)
from .tools import (
    thruk_schedule_hostgroup_downtime as thruk_schedule_hostgroup_downtime,
)
from .tools import (
    thruk_schedule_propagated_host_downtime as thruk_schedule_propagated_host_downtime,
)
from .tools import (
    thruk_schedule_servicegroup_downtime as thruk_schedule_servicegroup_downtime,
)
from .tools import (
    thruk_service_availability as thruk_service_availability,
)
from .tools import (
    thruk_sites as thruk_sites,
)
from .tools import (
    thruk_stale_acks as thruk_stale_acks,
)
from .tools import (
    thruk_stale_checks as thruk_stale_checks,
)
from .tools import (
    thruk_stats as thruk_stats,
)
from .tools import (
    thruk_top_noisy_hosts as thruk_top_noisy_hosts,
)
from .tools import (
    thruk_top_noisy_services as thruk_top_noisy_services,
)
from .tools import (
    thruk_totals as thruk_totals,
)
from .tools import (
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


# Write / command tool implementations moved to
# :mod:`thruk_mcp.tools.commands` (issue #261, parent #256). They are
# re-imported at the top of this module and spliced into TOOL_REGISTRY
# via *COMMANDS_READ_REGISTRY / *COMMANDS_WRITE_REGISTRY below.


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
# TOOL_REGISTRY: aggregated in :mod:`thruk_mcp.tools` (issue #262, parent #256)
# ---------------------------------------------------------------------------
# The single ``TOOL_REGISTRY: list[ToolSpec]`` (one entry per tool, issue #85)
# is now assembled from the per-module registries in ``tools/__init__.py`` and
# imported above.  ``server.py`` only derives the dispatch / schema / write-set
# structures from it below.

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
        # Audit attribution uses the per-request config (the calling tenant's
        # auth_user in header-auth mode); falls back to the server's base
        # config everywhere else. Whether to audit at all stays a server-level
        # decision (self._cfg.audit_log) — a tenant cannot disable it.
        cfg = _get_cfg(self._cfg) or self._cfg
        try:
            result = await fn(**arguments)
        except TypeError as exc:
            if self._cfg.audit_log and _is_auditable_write(name, arguments):
                audit.log_call(name, arguments, user=cfg.auth_user, status="error", error=str(exc))
            raise ValueError(f"Invalid arguments for {name!r}: {exc}") from exc
        except (ThrukError, ValueError) as exc:
            if self._cfg.audit_log and _is_auditable_write(name, arguments):
                audit.log_call(name, arguments, user=cfg.auth_user, status="error", error=str(exc))
            # Return as tool-level error content instead of raising.
            # Raising here causes the low-level MCP SDK to emit a protocol-level
            # McpError(-32603) which the client shows as the generic
            # "tool execution failed" message, discarding the actual Thruk error.
            # ValueError is included as a defensive catch: tools that raise it
            # (e.g. validation guards before the fix for issue #71) must not
            # escape to the MCP protocol layer as an unhandled exception.
            return [TextContent(type="text", text=audit.scrub(f"Error: {exc}"))]
        if self._cfg.audit_log and _is_auditable_write(name, arguments):
            audit.log_call(name, arguments, user=cfg.auth_user, status="ok")
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
        """Return the Thruk prompt templates."""
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
            Prompt(
                name="daily_health_report",
                description="Morning health digest for the estate or a hostgroup",
                arguments=[
                    PromptArgument(
                        name="hostgroup",
                        description="Restrict to a hostgroup (optional)",
                        required=False,
                    ),
                ],
            ),
            Prompt(
                name="incident_triage",
                description="Prioritise and find the common cause during a major incident",
                arguments=[
                    PromptArgument(
                        name="hostgroup",
                        description="Restrict to a hostgroup (optional)",
                        required=False,
                    ),
                ],
            ),
            Prompt(
                name="capacity_review",
                description="Surface metrics approaching their warn/crit thresholds",
                arguments=[
                    PromptArgument(
                        name="hostgroup",
                        description="Restrict to a hostgroup (optional)",
                        required=False,
                    ),
                    PromptArgument(
                        name="within_percent",
                        description="Proximity threshold in percent (default 10)",
                        required=False,
                    ),
                ],
            ),
            Prompt(
                name="sla_report",
                description="Availability / SLA report for a host, service or hostgroup",
                arguments=[
                    PromptArgument(
                        name="target", description="Host/service/hostgroup name", required=True
                    ),
                    PromptArgument(
                        name="kind",
                        description="host, service or hostgroup (default host)",
                        required=False,
                    ),
                    PromptArgument(
                        name="timeperiod",
                        description=(
                            "Thruk period shortcut, e.g. last7days, lastmonth (default last7days)"
                        ),
                        required=False,
                    ),
                ],
            ),
            Prompt(
                name="noise_review",
                description="Monitoring-noise hygiene review to reduce alert fatigue",
                arguments=[
                    PromptArgument(
                        name="since",
                        description="Analysis window start, e.g. -24h, -7d (default -24h)",
                        required=False,
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
        elif name == "daily_health_report":
            text = daily_health_report(hostgroup=args.get("hostgroup") or None)
        elif name == "incident_triage":
            text = incident_triage(hostgroup=args.get("hostgroup") or None)
        elif name == "capacity_review":
            raw_pct = args.get("within_percent", "10")
            within = int(raw_pct) if str(raw_pct).isdigit() else 10
            text = capacity_review(
                hostgroup=args.get("hostgroup") or None,
                within_percent=within,
            )
        elif name == "sla_report":
            text = sla_report(
                target=args.get("target", ""),
                kind=args.get("kind", "host"),
                timeperiod=args.get("timeperiod", "last7days"),
            )
        elif name == "noise_review":
            text = noise_review(since=args.get("since", "-24h"))
        else:
            raise ValueError(f"Unknown prompt: {name!r}")
        return GetPromptResult(
            messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))]
        )


def build_server(
    config: ThrukConfig | None = None, *, require_api_key: bool = True
) -> ThrukMCPServer:
    """Build the MCP server with all Thruk tools registered.

    Uses mcp.server.Server directly (not FastMCP) so that:
    - inputSchema is defined explicitly — no annotation introspection
    - arguments arrive as a raw dict in call_tool — no Pydantic model
    - the Docker MCP Gateway cannot silently strip arguments

    ``require_api_key=False`` lets the server boot without ``THRUK_API_KEY`` in
    header-auth multi-tenant mode, where each request supplies its own key.
    """
    cfg = config or ThrukConfig.from_env(require_api_key=require_api_key)
    client = ThrukClient(cfg)
    # Bind the new client + config to the current context so that all tool
    # coroutines spawned from this event-loop context reach the right instance.
    # Each build_server() call operates independently — no shared module-level
    # state.  In header-auth mode the middleware overrides both vars per request.
    _client_var.set(client)
    _cfg_var.set(cfg)

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
