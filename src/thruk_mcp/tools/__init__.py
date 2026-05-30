"""Tool sub-package (issue #147 — server.py split).

Each tool group lives in its own module (``inventory``, ``history``,
``triage``, ``commands``, ``escape``) together with a co-located
``*_REGISTRY: list[ToolSpec]`` describing the tools it owns.

Issue #262 (final step of parent #256): this package is now the single place
that aggregates every per-module registry into the global
``TOOL_REGISTRY``.  :mod:`thruk_mcp.server` imports ``TOOL_REGISTRY`` from here
and only derives ``_TOOL_DISPATCH`` / ``_TOOL_SCHEMAS`` / ``WRITE_TOOLS`` from
it.  Every tool symbol is re-exported here so existing
``from thruk_mcp.server import <name>`` / ``from thruk_mcp.tools import <name>``
imports keep working unchanged.

The splice order below is byte-for-byte identical to the original
``server.TOOL_REGISTRY`` ordering (the history block was non-contiguous: the
noisy/flap/trends tools came first, the log/alert/notification tools after the
inventory + read-command groups), so ``_TOOL_SCHEMAS`` and ``WRITE_TOOLS`` keep
the exact same keys and order.
"""

from __future__ import annotations

from .base import (
    _BACKENDS,
    _LOG_CUSTOM_VARS,
    _LOG_HOSTGROUP,
    _OPT_BOOL,
    _OPT_INT,
    _OPT_OBJ,
    _OPT_STR,
    ToolSpec,
    _bool,
    _int,
    _s,
    _str,
)
from .commands import (
    COMMANDS_READ_REGISTRY,
    COMMANDS_WRITE_REGISTRY,
    _delete_downtimes_by_host_comment,
    thruk_acknowledge,
    thruk_add_comment,
    thruk_bulk_acknowledge,
    thruk_checks,
    thruk_delete_active_downtimes,
    thruk_delete_comment,
    thruk_delete_downtime,
    thruk_delete_downtimes_by_filter,
    thruk_get_downtime,
    thruk_notifications,
    thruk_recheck,
    thruk_remove_acknowledgement,
    thruk_schedule_downtime,
    thruk_schedule_host_services_downtime,
    thruk_schedule_hostgroup_downtime,
    thruk_schedule_propagated_host_downtime,
    thruk_schedule_servicegroup_downtime,
)
from .escape import (
    _ALLOWED_METHODS,
    _REST_PATH_PREFIXES,
    ESCAPE_REGISTRY,
    _validate_rest_path,
    thruk_query,
    thruk_run_background_query,
)
from .history import (
    _BUCKET_SIZES,
    _DEFAULT_SINCE,
    HISTORY_LOGS_REGISTRY,
    HISTORY_REGISTRY,
    HISTORY_TRENDS_REGISTRY,
    _aggregate_alerts,
    _coerce_hours_to_since,
    _fetch_logs,
    _resolve_hosts_to_regex,
    thruk_alert_heatmap,
    thruk_flap_summary,
    thruk_list_alerts,
    thruk_list_logs,
    thruk_list_notifications,
    thruk_notification_summary,
    thruk_recent_events,
    thruk_recurring_problems,
    thruk_top_noisy_hosts,
    thruk_top_noisy_services,
)
from .inventory import (
    INVENTORY_REGISTRY,
    _collect_hostgroup_constraints,
    _ensure_columns_param,
    _row_matches_hostgroup_constraints,
    _strip_filter_field,
    thruk_get_contact,
    thruk_get_host,
    thruk_get_service,
    thruk_host_availability,
    thruk_hostgroup_availability,
    thruk_list_comments,
    thruk_list_contacts,
    thruk_list_downtimes,
    thruk_list_hostgroups,
    thruk_list_hosts,
    thruk_list_servicegroups,
    thruk_list_services,
    thruk_problems,
    thruk_service_availability,
    thruk_sites,
    thruk_stats,
    thruk_totals,
)
from .triage import (
    TRIAGE_REGISTRY,
    _project_problem_counts,
    thruk_concurrent_failures,
    thruk_oldest_problems,
    thruk_problem_counts,
    thruk_stale_acks,
    thruk_unacked_critical,
)

# ---------------------------------------------------------------------------
# TOOL_REGISTRY: aggregate of every per-module registry (issue #262).
# ---------------------------------------------------------------------------
# Order is byte-for-byte identical to the original ``server.TOOL_REGISTRY``:
#   1. noisy / flap / trends     (HISTORY_TRENDS_REGISTRY)
#   2. host / service listing    (INVENTORY_REGISTRY)
#   3. read-only commands        (COMMANDS_READ_REGISTRY)
#   4. log / alert / notification (HISTORY_LOGS_REGISTRY)
#   5. raw query (read + write)  (ESCAPE_REGISTRY)
#   6. write commands            (COMMANDS_WRITE_REGISTRY)
#   7. semantic triage / analytics (TRIAGE_REGISTRY)
TOOL_REGISTRY: list[ToolSpec] = [
    *HISTORY_TRENDS_REGISTRY,
    *INVENTORY_REGISTRY,
    *COMMANDS_READ_REGISTRY,
    *HISTORY_LOGS_REGISTRY,
    *ESCAPE_REGISTRY,
    *COMMANDS_WRITE_REGISTRY,
    *TRIAGE_REGISTRY,
]

__all__ = [
    "COMMANDS_READ_REGISTRY",
    "COMMANDS_WRITE_REGISTRY",
    "ESCAPE_REGISTRY",
    "HISTORY_LOGS_REGISTRY",
    "HISTORY_REGISTRY",
    "HISTORY_TRENDS_REGISTRY",
    "INVENTORY_REGISTRY",
    "TOOL_REGISTRY",
    "TRIAGE_REGISTRY",
    "_ALLOWED_METHODS",
    "_BACKENDS",
    "_BUCKET_SIZES",
    "_DEFAULT_SINCE",
    "_LOG_CUSTOM_VARS",
    "_LOG_HOSTGROUP",
    "_OPT_BOOL",
    "_OPT_INT",
    "_OPT_OBJ",
    "_OPT_STR",
    "_REST_PATH_PREFIXES",
    "ToolSpec",
    "_aggregate_alerts",
    "_bool",
    "_coerce_hours_to_since",
    "_collect_hostgroup_constraints",
    "_delete_downtimes_by_host_comment",
    "_ensure_columns_param",
    "_fetch_logs",
    "_int",
    "_project_problem_counts",
    "_resolve_hosts_to_regex",
    "_row_matches_hostgroup_constraints",
    "_s",
    "_str",
    "_strip_filter_field",
    "_validate_rest_path",
    "thruk_acknowledge",
    "thruk_add_comment",
    "thruk_alert_heatmap",
    "thruk_bulk_acknowledge",
    "thruk_checks",
    "thruk_concurrent_failures",
    "thruk_delete_active_downtimes",
    "thruk_delete_comment",
    "thruk_delete_downtime",
    "thruk_delete_downtimes_by_filter",
    "thruk_flap_summary",
    "thruk_get_contact",
    "thruk_get_downtime",
    "thruk_get_host",
    "thruk_get_service",
    "thruk_host_availability",
    "thruk_hostgroup_availability",
    "thruk_list_alerts",
    "thruk_list_comments",
    "thruk_list_contacts",
    "thruk_list_downtimes",
    "thruk_list_hostgroups",
    "thruk_list_hosts",
    "thruk_list_logs",
    "thruk_list_notifications",
    "thruk_list_servicegroups",
    "thruk_list_services",
    "thruk_notification_summary",
    "thruk_notifications",
    "thruk_oldest_problems",
    "thruk_problem_counts",
    "thruk_problems",
    "thruk_query",
    "thruk_recent_events",
    "thruk_recheck",
    "thruk_recurring_problems",
    "thruk_remove_acknowledgement",
    "thruk_run_background_query",
    "thruk_schedule_downtime",
    "thruk_schedule_host_services_downtime",
    "thruk_schedule_hostgroup_downtime",
    "thruk_schedule_propagated_host_downtime",
    "thruk_schedule_servicegroup_downtime",
    "thruk_service_availability",
    "thruk_sites",
    "thruk_stale_acks",
    "thruk_stats",
    "thruk_top_noisy_hosts",
    "thruk_top_noisy_services",
    "thruk_totals",
    "thruk_unacked_critical",
]
