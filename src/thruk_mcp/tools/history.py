"""Logs / history / trends tools (issue #260 — server.py split).

Parent: #256. This module hosts the nine read-only "history" tools that turn
the Naemon/Thruk ``/logs`` table into trends, heatmaps and raw event listings:

* ``thruk_top_noisy_hosts``     — top hosts by HOST ALERT count.
* ``thruk_top_noisy_services``  — top services by SERVICE ALERT count.
* ``thruk_flap_summary``        — objects with the most state transitions.
* ``thruk_alert_heatmap``       — alert counts bucketed over a time window.
* ``thruk_notification_heatmap``— notification counts bucketed over a window.
* ``thruk_recurring_problems``  — chronic objects above an alert threshold.
* ``thruk_list_logs``           — raw Livestatus log entries.
* ``thruk_list_alerts``         — HOST/SERVICE ALERT entries.
* ``thruk_list_notifications``  — notification entries (class=3).
* ``thruk_recent_events``       — most recent events from the last N hours.

plus the private helpers ``_resolve_hosts_to_regex`` (hostgroup/custom-var ->
``host_name[regex]`` via a /hosts lookup), ``_fetch_logs`` (log-family fetch
with per-backend fallback), ``_aggregate_alerts`` (shared noisy-host/service
aggregation) and ``_coerce_hours_to_since`` (legacy ``hours`` shim), together
with the ``_BUCKET_SIZES`` / ``_DEFAULT_SINCE`` tables.

The co-located ``HISTORY_TRENDS_REGISTRY`` / ``HISTORY_LOGS_REGISTRY`` lists
(``list[ToolSpec]``) keep each tool name, implementation and explicit JSON
Schema in one place; ``server.py`` splices them into the global
``TOOL_REGISTRY`` at their original (non-contiguous) positions — preserving
registration order — and re-exports every symbol here for backward
compatibility.

Shared infrastructure (time parsing, log-family host resolution, state maps,
the noisy-cap warning suffix) lives in :mod:`thruk_mcp.helpers` /
:mod:`thruk_mcp.constants` so this module never imports ``server``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import warnings
from datetime import datetime, timezone
from typing import Any

from ..client import ThrukError
from ..constants import (
    _NOISY_CAP_HINT,
    _NOISY_MAX_ALERTS,
    DEFAULT_LOG_COLUMNS,
    DEFAULT_NOTIFICATION_COLUMNS,
    HOST_STATE_STR,
    SVC_STATE_STR,
)
from ..filters import (
    FIELDS_ALERTS,
    FIELDS_LOGS,
    FIELDS_NOISY_HOSTS,
    FIELDS_NOISY_SERVICES,
    FIELDS_NOTIFICATIONS,
    build_tool_schema,
    infer_alert_type_regex,
)
from ..helpers import (
    _RESOLVE_HOSTS_HARD_LIMIT,
    _backends,
    _build_cv_params,
    _duration_human,
    _epoch_filter_value,
    _format_state_label,
    _get_client,
    _list_params,
    _now_utc_epoch,
    _parse_thruk_time,
    _regroup_records_by_group,
    _resolve_log_filter,
    _tool_response,
    _ts,
)
from ..reliability import extract_incidents, summarize_reliability
from .base import (
    _BACKENDS,
    _COLUMNS,
    _SINCE,
    _UNTIL,
    ToolSpec,
    _bool,
    _int,
    _sort,
)

# State maps — sourced from constants.py (single source of truth, issue #81).
# Local aliases preserve the original ``server.py`` names used in the function
# bodies below, with no behaviour change.
HOST_STATES: dict[int, str] = HOST_STATE_STR
SERVICE_STATES: dict[int, str] = SVC_STATE_STR

# Issue #312: ``thruk_flap_summary`` needs the ordered event *sequence* to count
# consecutive state transitions, so it cannot be fully pushed server-side. We
# instead use server-side aggregation to find candidate objects (those with at
# least ``min_transitions`` alerts — a necessary condition, since transitions
# can never exceed the raw alert count) and then fetch raw rows scoped to just
# those hosts. This bounds the raw fetch to the flapping candidates instead of
# every alert in the window. Cap the candidate host set so a pathological window
# cannot rebuild an unbounded ``host_name[regex]``.
_FLAP_CANDIDATE_CAP = 500

# Valid ``group_by`` dimensions for the alert-aggregation tools (issue #318).
# ``host`` / ``service`` are the identity (per-object) groupings preserving the
# historical behaviour; ``hostgroup`` / ``servicegroup`` fan the per-object
# counts out across each object's group membership (a "ventilation par client"
# where one hostgroup == one client). /logs has no group column, so the latter
# are resolved via a /hosts or /services lookup (see _regroup_records_by_group).
_NOISY_HOSTS_GROUP_BY: tuple[str, ...] = ("host", "hostgroup")
_NOISY_SERVICES_GROUP_BY: tuple[str, ...] = ("service", "host", "hostgroup", "servicegroup")

# Group-map lookups (/hosts, /services) are bounded; surface incompleteness.
_REGROUP_TRUNC_WARNING = "Group membership lookup truncated; per-group totals may be incomplete."


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
        params["time[gte]"] = _epoch_filter_value(since)
    if until:
        params["time[lte]"] = _epoch_filter_value(until)
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
    extra_params: dict[str, Any],
    backends: str | None,
    *,
    exclude_recovery: bool = True,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Aggregate alert log entries **server-side** via Thruk's /logs GROUP BY.

    Shared helper for :func:`thruk_top_noisy_hosts`,
    :func:`thruk_top_noisy_services` and :func:`thruk_recurring_problems`.
    Callers build ``extra_params`` (``time[gte]`` / ``time[lte]`` and any
    resolved ``host_name`` / ``host_name[regex]`` filter) and pass the key
    fields identifying a unique entity (``("host_name",)`` for hosts,
    ``("host_name", "service_description")`` for services).

    Historically (issue #312) this fetched up to ``_NOISY_MAX_ALERTS`` *raw*
    log rows (``sort=-time``) and counted them in Python.  On busy/federated
    instances a 24 h window holds **far** more than 10 000 alert rows, so the
    cap returned only the most recent slice and every ranking was skewed and
    inconsistent between tools.  We now push the aggregation to Thruk:

        columns = <key_fields>,state,count(*):cnt,min(time):first_t,max(time):last_t
        sort    = -cnt        class = 1        type[~] = <type_regex>

    Thruk re-aggregates group keys **across federated backends**, so counts are
    exact and the response is one tiny row per ``(key, state)`` combination.
    Grouping additionally by ``state`` lets us recover ``last_state`` (the state
    of the most recent event) in the same query — per key we keep the state of
    the substate row with the greatest ``max(time)``.

    ``class=1`` scopes the query to genuine HOST/SERVICE ALERT rows (issues
    #176 / #193 / #248): it excludes ``type=NULL`` system/command/current-state
    rows that leak past ``type[~]`` alone, so the former client-side type
    re-check is no longer needed.

    ``exclude_recovery`` adds ``state[!=]=0`` to drop RECOVERY (UP/OK) events
    server-side (noisy/recurring rank non-recovery alerts); flap analysis sets
    it ``False`` because transitions *to* OK still count.

    Returns ``(rows, warnings, hit_cap)`` where each row carries the key fields,
    ``alert_count`` (summed across states), ``first_ts``, ``last_ts`` and
    ``last_state_int`` (caller formats via :func:`_format_state_label`).  Rows
    are sorted by ``alert_count`` descending (not yet sliced to limit).
    ``hit_cap`` is ``True`` only when the number of distinct ``(key, state)``
    groups reached ``_NOISY_MAX_ALERTS`` — practically never, but surfaced as a
    cap warning by callers for parity with the old contract.
    """
    group_cols = [*key_fields, "state"]
    columns = ",".join([*group_cols, "count(*):cnt", "min(time):first_t", "max(time):last_t"])
    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "-cnt",
        "columns": columns,
        **extra_params,
        "type[~]": type_regex,  # always override: callers must not change the log type
        "class": "1",  # genuine HOST/SERVICE ALERT rows only (drops type=NULL leaks)
    }
    if exclude_recovery:
        params["state[!=]"] = "0"  # drop RECOVERY (UP/OK) events server-side
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    # Re-aggregate per key. Grouping by (key, state) means a single entity spans
    # several rows; we sum counts and track the state at the latest timestamp.
    # This loop also re-merges duplicate keys that the per-backend fallback path
    # of ``get_with_fallback`` concatenates without merging (client.py) — a
    # no-op on the normal path where Thruk already merged across backends.
    counts: dict[tuple[str, ...], dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            cnt = int(entry.get("cnt") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt <= 0:
            continue
        key = tuple(str(entry.get(f) or "") for f in key_fields)
        state = entry.get("state", -1)
        first_t = entry.get("first_t")
        last_t = int(entry.get("last_t") or 0)
        rec = counts.setdefault(
            key,
            {"alert_count": 0, "first_ts": None, "last_ts": 0, "last_state_int": state},
        )
        rec["alert_count"] += cnt
        if first_t is not None:
            ft = int(first_t)
            if rec["first_ts"] is None or ft < rec["first_ts"]:
                rec["first_ts"] = ft
        if last_t >= rec["last_ts"]:
            rec["last_ts"] = last_t
            rec["last_state_int"] = state

    rows = sorted(
        [
            {
                **dict(zip(key_fields, k, strict=False)),
                "alert_count": v["alert_count"],
                "first_ts": v["first_ts"],
                "last_ts": v["last_ts"],
                "last_state_int": v["last_state_int"],
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
    group_by: str = "host",
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

    ``group_by`` selects the aggregation dimension: ``host`` (default, one row
    per host) or ``hostgroup`` (a "ventilation par client" — counts fanned out
    across each host's hostgroup membership, issue #318). A host in several
    hostgroups contributes its full count to each; hosts in none land in a
    ``"(none)"`` bucket.

    ``filter`` fields: ``host`` (eq/regex), ``hostgroup``, ``custom_var``
    (host-level Nagios variable, resolved via /hosts lookup).

    ``hours`` is **deprecated** (issue #191 backward-compat shim): clients
    still using the legacy ``hours: int`` schema have it translated to
    ``since="-{hours}h"``. Prefer ``since`` / ``until`` for new code.

    Returns a wrapped object:
    ``since``, ``until``, ``group_by``, ``total_alerts_in_window`` (after
    RECOVERY exclusion), ``results`` sorted by ``alert_count`` desc. Per-host
    entries carry ``host``, ``alert_count``, ``last_state``, ``last_alert_time``;
    per-hostgroup entries carry ``hostgroup``, ``alert_count``,
    ``hosts_affected``, ``last_alert_time``.
    """
    if group_by not in _NOISY_HOSTS_GROUP_BY:
        return _tool_response(
            {"error": f"Invalid group_by {group_by!r}. Allowed: {', '.join(_NOISY_HOSTS_GROUP_BY)}"}
        )
    since = _coerce_hours_to_since(hours, since, "thruk_top_noisy_hosts")
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_HOSTS, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    if since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if until:
        extra["time[lte]"] = _epoch_filter_value(until)

    rows, warnings, hit_limit = await _aggregate_alerts(
        type_regex="^HOST ALERT",
        key_fields=("host_name",),
        extra_params=extra,
        backends=backends,
    )
    total = sum(r["alert_count"] for r in rows)
    regroup_trunc = False
    if group_by == "hostgroup":
        grouped, regroup_trunc = await _regroup_records_by_group(
            rows, "hostgroup", count_key="alert_count", backends=backends
        )
        results = [
            {
                "hostgroup": r["hostgroup"],
                "alert_count": r["alert_count"],
                "hosts_affected": r["hosts_affected"],
                "last_alert_time": _ts(r["last_ts"]),
            }
            for r in grouped[:limit]
        ]
    else:
        results = [
            {
                "host": r["host_name"],
                "alert_count": r["alert_count"],
                "last_state": _format_state_label(r["last_state_int"], HOST_STATES),
                "last_alert_time": _ts(r["last_ts"]),
            }
            for r in rows[:limit]
        ]

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "group_by": group_by,
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
            f"Result capped at {_NOISY_MAX_ALERTS} distinct groups; "
            "aggregation may be incomplete." + _NOISY_CAP_HINT
        )
    elif regroup_trunc:
        payload["_warning"] = _REGROUP_TRUNC_WARNING
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


async def thruk_top_noisy_services(
    since: str | None = _DEFAULT_SINCE,
    until: str | None = None,
    limit: int = 10,
    group_by: str = "service",
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

    ``group_by`` selects the aggregation dimension (issue #318): ``service``
    (default, one row per host+service), ``host`` (service alerts rolled up per
    host), ``hostgroup`` or ``servicegroup`` (a "ventilation par client" —
    counts fanned out across group membership). An object in several groups
    contributes its full count to each; objects in none land in a ``"(none)"``
    bucket.

    ``filter`` fields: ``host`` (eq/regex), ``service`` (eq/regex),
    ``hostgroup``, ``custom_var`` (host-level Nagios variable, resolved via
    /hosts lookup).

    ``hours`` is **deprecated** (issue #191 backward-compat shim): clients
    still using the legacy ``hours: int`` schema have it translated to
    ``since="-{hours}h"``. Prefer ``since`` / ``until`` for new code.

    Returns a wrapped object: ``since``, ``until``, ``group_by``,
    ``total_alerts_in_window`` (after RECOVERY exclusion) and ``results`` sorted
    by ``alert_count`` desc. Per-service entries carry ``host``, ``service``,
    ``alert_count``, ``last_state``, ``last_alert_time``; per-host entries carry
    ``host``, ``alert_count``, ``services_affected``, ``last_alert_time``;
    per-(host|service)group entries carry the group name, ``alert_count``,
    ``hosts_affected`` (and ``services_affected`` for servicegroup) and
    ``last_alert_time``.
    """
    if group_by not in _NOISY_SERVICES_GROUP_BY:
        return _tool_response(
            {
                "error": f"Invalid group_by {group_by!r}. "
                f"Allowed: {', '.join(_NOISY_SERVICES_GROUP_BY)}"
            }
        )
    since = _coerce_hours_to_since(hours, since, "thruk_top_noisy_services")
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    if since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if until:
        extra["time[lte]"] = _epoch_filter_value(until)

    rows, warnings, hit_limit = await _aggregate_alerts(
        type_regex="^SERVICE ALERT",
        key_fields=("host_name", "service_description"),
        extra_params=extra,
        backends=backends,
    )
    total = sum(r["alert_count"] for r in rows)
    regroup_trunc = False
    if group_by in ("hostgroup", "servicegroup"):
        grouped, regroup_trunc = await _regroup_records_by_group(
            rows, group_by, count_key="alert_count", backends=backends
        )
        results = []
        for r in grouped[:limit]:
            entry: dict[str, Any] = {
                group_by: r[group_by],
                "alert_count": r["alert_count"],
                "hosts_affected": r["hosts_affected"],
            }
            if group_by == "servicegroup":
                entry["services_affected"] = r["services_affected"]
            entry["last_alert_time"] = _ts(r["last_ts"])
            results.append(entry)
    elif group_by == "host":
        # Roll service alerts up per host (no topology lookup needed).
        per_host: dict[str, dict[str, Any]] = {}
        for r in rows:
            b = per_host.setdefault(
                r["host_name"], {"alert_count": 0, "services": set(), "last_ts": 0}
            )
            b["alert_count"] += r["alert_count"]
            b["services"].add(r["service_description"])
            if r["last_ts"] > b["last_ts"]:
                b["last_ts"] = r["last_ts"]
        ranked = sorted(per_host.items(), key=lambda kv: kv[1]["alert_count"], reverse=True)
        results = [
            {
                "host": h,
                "alert_count": b["alert_count"],
                "services_affected": len(b["services"]),
                "last_alert_time": _ts(b["last_ts"]),
            }
            for h, b in ranked[:limit]
        ]
    else:
        results = [
            {
                "host": r["host_name"],
                "service": r["service_description"],
                "alert_count": r["alert_count"],
                "last_state": _format_state_label(r["last_state_int"], SERVICE_STATES),
                "last_alert_time": _ts(r["last_ts"]),
            }
            for r in rows[:limit]
        ]

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "group_by": group_by,
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
            f"Result capped at {_NOISY_MAX_ALERTS} distinct groups; "
            "aggregation may be incomplete." + _NOISY_CAP_HINT
        )
    elif regroup_trunc:
        payload["_warning"] = _REGROUP_TRUNC_WARNING
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

    if since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if until:
        extra["time[lte]"] = _epoch_filter_value(until)

    # Step 1 (issue #312): find candidate objects via server-side aggregation.
    # An object that fired fewer than ``min_transitions`` alerts cannot possibly
    # have ``min_transitions`` state changes, so ``alert_count >= min_transitions``
    # is a sound necessary condition. ``exclude_recovery=False`` because a
    # transition *to* OK/UP is still a transition. Counts are exact (no cap skew).
    cand_rows, warnings, _cand_cap = await _aggregate_alerts(
        type_regex="^(HOST|SERVICE) ALERT",
        key_fields=("host_name", "service_description"),
        extra_params=extra,
        backends=backends,
        exclude_recovery=False,
    )
    candidates = [r for r in cand_rows if r["alert_count"] >= min_transitions]
    candidates_capped = len(candidates) > _FLAP_CANDIDATE_CAP
    candidates = candidates[:_FLAP_CANDIDATE_CAP]  # already sorted by alert_count desc

    # Step 2: fetch the raw ordered rows for the candidate hosts only, so the
    # transition count is exact while the fetch stays bounded to flapping hosts.
    data: list[dict[str, Any]] = []
    if candidates:
        cand_hosts = sorted({r["host_name"] for r in candidates})
        raw_extra = dict(extra)
        # Narrow to candidate hosts; a candidate set is always a subset of any
        # hostgroup/custom-var-derived host_name[regex], so overriding is safe.
        raw_extra["host_name[regex]"] = "^(" + "|".join(re.escape(h) for h in cand_hosts) + ")$"
        params: dict[str, Any] = {
            "limit": _NOISY_MAX_ALERTS,
            "sort": "time",  # ascending: chronological order required for transition counting
            "columns": "host_name,service_description,state,time",
            **raw_extra,
            "type[~]": "^(HOST|SERVICE) ALERT",
            # class=1 drops type=NULL leaks (issues #176 / #193) that would
            # otherwise be counted as spurious transitions.
            "class": "1",
        }
        raw_data, raw_warnings = await _get_client().get_with_fallback(
            "/logs", params=params, backends=_backends(backends), method="POST"
        )
        if isinstance(raw_data, list):
            data = raw_data
        warnings = [*warnings, *raw_warnings]

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
        # Issue #245: route through _format_state_label so unmapped ints
        # (e.g. host state=3 from a stray Naemon log row) render as
        # "UNKNOWN(<n>)" instead of a bare integer string.
        states_seen = sorted({_format_state_label(e.get("state", -1), state_map) for e in entries})
        results_raw.append(
            {
                "host": h,
                "service": svc or None,
                "transition_count": transitions,
                "states_seen": states_seen,
                "last_state": _format_state_label(last_state_int, state_map),
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
    elif candidates_capped:
        payload["_warning"] = (
            f"More than {_FLAP_CANDIDATE_CAP} candidate hosts matched; analysed the "
            f"{_FLAP_CANDIDATE_CAP} with the most alerts. Narrow the filter or window "
            "for complete coverage."
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

# ``_now_utc_epoch`` / ``_parse_thruk_time`` (+ the ``_THRUK_REL_*`` table)
# live in :mod:`thruk_mcp.helpers` (issue #258); imported at the top of this module.

# Upper bound on the number of time buckets a single heatmap call will enumerate.
# Each bucket is one server-side count(*) query (issue #312), so this also caps
# the request fan-out. e.g. 24 h / 1 h = 24, 7 d / 1 h = 168 — all well within.
_HEATMAP_MAX_BUCKETS = 500


def _bucket_iso(epoch: int) -> str:
    """Format a bucket-start epoch as a UTC ISO-8601 ``...Z`` string."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sum_cnt(rows: Any) -> int:
    """Sum the ``count(*):cnt`` column across aggregated /logs rows.

    A bare ``count(*)`` query with no ``GROUP BY`` collapses to a **single
    object** on Thruk's normal (federated) path — ``{"cnt": N}`` rather than a
    one-element list (issue #312 regression: the heatmap and the
    reliability-report ``total_events`` both silently read 0). The per-backend
    fallback path of ``get_with_fallback`` instead concatenates one such object
    per backend into a **list**. Normalise both shapes to a list of rows, then
    sum, skipping any non-numeric or malformed row.
    """
    total = 0
    items = rows if isinstance(rows, list) else [rows]
    for r in items:
        if isinstance(r, dict):
            with contextlib.suppress(TypeError, ValueError):
                total += int(r.get("cnt") or 0)
    return total


async def _bucketed_log_counts(
    extra: dict[str, Any],
    since: str | None,
    until: str | None,
    bucket_secs: int,
    backends: str | None,
) -> tuple[list[dict[str, Any]], int, list[str], str | None]:
    """Count /logs rows per time bucket with one server-side ``count(*)`` query each.

    Shared by :func:`thruk_alert_heatmap` and :func:`thruk_notification_heatmap`.
    ``extra`` already carries the type/class scoping and any resolved host
    filter; it must **not** be reused afterwards (time bounds are popped here).

    Replaces the former "fetch up to 10 000 raw rows then bucket in Python"
    approach (issue #312), which silently dropped the oldest buckets on busy
    windows. Each bucket is an exact, federated ``count(*)`` over
    ``[bucket_start, bucket_start + bucket_secs)``; queries run concurrently.

    Returns ``(results, total, warnings, error)``. On a usable window ``error``
    is ``None`` and ``results`` is a continuous, chronologically ordered list of
    ``{bucket_start, count}`` (empty buckets included as ``count=0``).
    """
    # A filter-supplied time bound (notifications expose since/until as filter
    # leaves) takes precedence over the since/until arguments.
    gte = extra.pop("time[gte]", None) or since
    lte = extra.pop("time[lte]", None) or until
    ts_since = _parse_thruk_time(gte) if gte else None
    ts_until = _parse_thruk_time(lte) if lte else _now_utc_epoch()
    if ts_since is None or ts_until is None:
        return (
            [],
            0,
            [],
            "Cannot determine a bounded time window; pass an explicit since/until "
            "(relative like '-24h' or absolute 'YYYY-MM-DD HH:MM:SS').",
        )

    first_b = (ts_since // bucket_secs) * bucket_secs
    last_b = (ts_until // bucket_secs) * bucket_secs
    n_buckets = (last_b - first_b) // bucket_secs + 1
    if n_buckets > _HEATMAP_MAX_BUCKETS:
        return (
            [],
            0,
            [],
            f"Window spans {n_buckets} buckets (max {_HEATMAP_MAX_BUCKETS}); "
            "use a larger bucket or a shorter window.",
        )
    starts = [first_b + i * bucket_secs for i in range(int(n_buckets))]

    async def _count_bucket(b: int) -> tuple[int, list[str]]:
        params: dict[str, Any] = {
            "columns": "count(*):cnt",
            "limit": 1,
            **extra,
            "time[gte]": str(b),
            "time[lte]": str(b + bucket_secs - 1),  # inclusive end == [b, b+secs)
        }
        rows, w = await _get_client().get_with_fallback(
            "/logs", params=params, backends=_backends(backends), method="POST"
        )
        return _sum_cnt(rows), w

    pairs = await asyncio.gather(*[_count_bucket(b) for b in starts])

    results: list[dict[str, Any]] = []
    total = 0
    seen: set[str] = set()
    warnings_out: list[str] = []
    for b, (cnt, w) in zip(starts, pairs, strict=False):
        results.append({"bucket_start": _bucket_iso(b), "count": cnt})
        total += cnt
        for msg in w:
            if msg not in seen:
                seen.add(msg)
                warnings_out.append(msg)
    return results, total, warnings_out, None


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

    Each bucket count is an exact, federated server-side ``count(*)`` (issue
    #312), so there is no truncation and counts are consistent with the other
    trend tools. A window spanning more than ``_HEATMAP_MAX_BUCKETS`` buckets
    returns an ``error`` asking for a larger bucket or a shorter window.
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

    # Issue #312: one exact server-side count(*) per bucket instead of fetching
    # raw rows and bucketing client-side (which truncated busy windows).
    results, total, agg_warnings, error = await _bucketed_log_counts(
        extra, since, until, bucket_secs, backends
    )
    if error:
        return _tool_response({"error": error})

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
    if agg_warnings:
        payload["_warnings"] = agg_warnings
    return _tool_response(payload)


async def thruk_notification_heatmap(
    since: str | None = "-24h",
    until: str | None = None,
    bucket: str = "1h",
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Return notification counts grouped by time bucket over a window.

    The notification (``class=3``) counterpart of :func:`thruk_alert_heatmap`:
    useful for spotting notification/mail storms, quiet periods and recurring
    paging patterns. The LLM can use the returned list as a sparkline.

    ``bucket`` controls bucket width: ``"15m"``, ``"30m"``, ``"1h"`` (default),
    ``"6h"``, ``"1d"``. Buckets with zero notifications are included so the
    output can be rendered as a continuous timeline.

    ``since`` / ``until`` accept Thruk relative (``"-24h"``, ``"-7d"``) or
    absolute (``"2026-05-21 14:00:00"``) values. Default window: last 24 h.

    ``filter`` fields: ``host``, ``service``, ``contact``, ``state``,
    ``since`` / ``until``, ``hostgroup`` and ``custom_var`` (host-level,
    resolved via /hosts lookup) — identical to
    :func:`thruk_list_notifications`.

    Returns a wrapped object: ``since``, ``until``, ``bucket``,
    ``total_notifications``, ``results`` list of ``{bucket_start, count}``
    ordered chronologically. Empty buckets are filled with ``count=0``.

    Each bucket count is an exact, federated server-side ``count(*)`` (issue
    #312), so there is no truncation. A window spanning more than
    ``_HEATMAP_MAX_BUCKETS`` buckets returns an ``error`` asking for a larger
    bucket or a shorter window.
    """
    bucket_secs = _BUCKET_SIZES.get(bucket)
    if bucket_secs is None:
        return _tool_response(
            {"error": f"Invalid bucket {bucket!r}. Allowed: {', '.join(_BUCKET_SIZES)}"}
        )

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOTIFICATIONS, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    # Notification entries are class=3 (client-side alias expansion — the
    # /notifications endpoint is broken on some Thruk versions).
    extra["class"] = "3"

    # Issue #312: one exact server-side count(*) per bucket. ``_bucketed_log_counts``
    # honours a filter-supplied time[gte]/time[lte] (FIELDS_NOTIFICATIONS exposes
    # since/until as leaves) over the since/until arguments.
    results, total, agg_warnings, error = await _bucketed_log_counts(
        extra, since, until, bucket_secs, backends
    )
    if error:
        return _tool_response({"error": error})

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "bucket": bucket,
        "total_notifications": total,
        "results": results,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    if agg_warnings:
        payload["_warnings"] = agg_warnings
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

    if since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if until:
        extra["time[lte]"] = _epoch_filter_value(until)

    # Server-side aggregation (issue #312): exact per-object counts across all
    # federated backends, no 10 000-row truncation. ``state[!=]=0`` (recovery
    # exclusion) and ``class=1`` are enforced inside the helper.
    rows, warnings, hit_cap = await _aggregate_alerts(
        type_regex="^(HOST|SERVICE) ALERT",
        key_fields=("host_name", "service_description"),
        extra_params=extra,
        backends=backends,
    )

    # rows are already sorted by alert_count descending.
    above = [
        {
            "host": r["host_name"],
            "service": r["service_description"] or None,
            "alert_count": r["alert_count"],
            "first_seen": _ts(r["first_ts"]),
            "last_seen": _ts(r["last_ts"]),
            # Issue #245: friendly "UNKNOWN(<n>)" fallback instead of raw int.
            "last_state": _format_state_label(
                r["last_state_int"],
                HOST_STATES if not r["service_description"] else SERVICE_STATES,
            ),
        }
        for r in rows
        if r["alert_count"] >= min_alerts
    ]

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
    elif hit_cap:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} distinct groups; "
            "aggregation may be incomplete." + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


def _enrich_reliability(metrics: dict[str, Any]) -> dict[str, Any]:
    """Add human-readable ``*_human`` strings to a pure-seconds metrics dict.

    The reducer (:mod:`thruk_mcp.reliability`) returns integer seconds only; the
    tool layer owns presentation. ``None`` seconds (no recovery for MTTR, < 2
    incidents for MTBF) map to ``None`` human strings.
    """

    def _h(seconds: int | None) -> str | None:
        return _duration_human(seconds) if seconds is not None else None

    return {
        "incidents": metrics["incidents"],
        "mttr_seconds": metrics["mttr_seconds"],
        "mttr_human": _h(metrics["mttr_seconds"]),
        "mtbf_seconds": metrics["mtbf_seconds"],
        "mtbf_human": _h(metrics["mtbf_seconds"]),
        "total_downtime_seconds": metrics["total_downtime_seconds"],
        "total_downtime_human": _h(metrics["total_downtime_seconds"]),
        "longest_incident_seconds": metrics["longest_incident_seconds"],
        "longest_incident_human": _h(metrics["longest_incident_seconds"]),
        "ongoing": metrics["ongoing"],
    }


async def thruk_reliability_report(
    since: str | None = "-30d",
    until: str | None = None,
    limit: int = 50,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Per host/service reliability metrics (MTTR / MTBF / incidents) from the log.

    Where ``*_availability`` gives only an uptime *percentage*, this turns raw
    HARD alert transitions into incident-level metrics, so a service at 99.2 %
    that crashed 14 times (MTTR 38 m) is distinguishable from one with a single
    11 h outage at the same percentage.

    Incidents are reconstructed from HARD ``HOST ALERT`` / ``SERVICE ALERT``
    transitions only — SOFT (check-retry) rows, ``* DOWNTIME ALERT`` /
    ``* FLAPPING ALERT`` and notifications are ignored. An incident runs from
    the first HARD non-OK state to the next HARD OK; consecutive non-OK HARD
    states (e.g. WARN -> CRIT) collapse into one incident. An incident with no
    recovery in the window is ``ongoing`` (excluded from MTTR but counted in
    ``incidents`` / ``total_downtime``, clamped at ``until``); a leading HARD
    recovery clamps a pre-window incident's downtime to ``since``.

    ``since`` / ``until`` accept Thruk relative (``"-30d"``, ``"-7d"``) or
    absolute (``"2026-05-21 14:00:00"``) values. Default window: last 30 days.
    ``limit`` caps the number of host/service objects returned (busiest first).
    ``filter`` fields: ``host``, ``service``, ``hostgroup``, ``custom_var``
    (host-level, resolved via /hosts lookup).

    Returns a wrapped object: ``since``, ``until``, ``total_objects``,
    ``results`` — each ``{host, service, incidents, mttr_seconds, mttr_human,
    mtbf_seconds, mtbf_human, total_downtime_seconds, total_downtime_human,
    longest_incident_seconds, longest_incident_human, ongoing}`` — sorted by
    ``total_downtime_seconds`` descending. MTTR is ``null`` when nothing
    recovered; MTBF is ``null`` for fewer than 2 incidents.
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    extra["type[~]"] = "^(HOST|SERVICE) ALERT"
    # Defence-in-depth (issues #176 / #193): pair the type regex with class=1 so
    # class=0 system messages (type=NULL) cannot leak past the regex filter.
    extra["class"] = "1"
    if since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if until:
        extra["time[lte]"] = _epoch_filter_value(until)

    # Pre-check (issue #312): incident reconstruction needs the ordered HARD-state
    # sequence, so this tool cannot be aggregated server-side. A busy window can
    # still exceed the _NOISY_MAX_ALERTS raw-fetch cap; a cheap server-side
    # count(*) lets us report the true event volume so the operator knows when
    # the metrics are partial. Narrowing by alert count is unsafe here — a single
    # long outage produces few alerts but dominates downtime — so we keep full
    # scope and only surface an accurate warning.
    count_rows, _count_warnings = await _get_client().get_with_fallback(
        "/logs",
        params={"columns": "count(*):cnt", "limit": 1, **extra},
        backends=_backends(backends),
        method="POST",
    )
    total_events = _sum_cnt(count_rows)

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "time",  # ascending: incident reconstruction needs chronological order
        "columns": "host_name,service_description,state,state_type,time,type",
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    # Resolve the window bounds once: ongoing incidents clamp at window_end
    # (until, or now); pre-window incidents clamp at window_start (since).
    window_start = _parse_thruk_time(since) if since else None
    window_end = _parse_thruk_time(until) if until else None
    if window_end is None:
        window_end = _now_utc_epoch()

    # Group rows per (host, service); host-level alerts have service="".
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("host_name") or "", entry.get("service_description") or "")
        groups.setdefault(key, []).append(entry)

    results: list[dict[str, Any]] = []
    for (host, svc), entries in groups.items():
        metrics = summarize_reliability(entries, window_start=window_start, window_end=window_end)
        if metrics["incidents"] == 0:
            continue
        results.append({"host": host, "service": svc or None, **_enrich_reliability(metrics)})

    results.sort(key=lambda r: r["total_downtime_seconds"], reverse=True)

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "total_objects": len(results),
        "results": results[:limit],
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif total_events > _NOISY_MAX_ALERTS:
        payload["_warning"] = (
            f"Window holds {total_events} alert log entries but only {_NOISY_MAX_ALERTS} "
            "were analysed; metrics are partial. Narrow the window or filter." + _NOISY_CAP_HINT
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


# ---------------------------------------------------------------------------
# Incident timeline (issue #321)
#
# Where ``thruk_reliability_report`` collapses a window into *aggregate* metrics
# (MTTR / MTBF / incident count), an operator writing a post-mortem needs the
# ordered *déroulé*: every state change, notification, downtime, flap and ack
# for one object (host / service / hostgroup), in chronological order. This is
# reconstructed straight from ``/logs`` and reuses the :mod:`thruk_mcp.reliability`
# incident reducer for the summary block.
# ---------------------------------------------------------------------------

#: ``/logs`` columns the timeline needs (per-event detail, not just counts).
_TIMELINE_COLUMNS = (
    "host_name,service_description,state,state_type,time,type,plugin_output,contact_name,message"
)

#: One regex spanning every ``type`` we surface. It anchors on the known
#: HOST/SERVICE alert-family + notification names, so ``type=NULL`` system rows
#: and ``EXTERNAL COMMAND`` entries are excluded without a ``class`` filter
#: (we span class 1 + 3, so a single ``class=`` value cannot scope the query).
_TIMELINE_TYPE_REGEX = (
    "^(HOST|SERVICE) (ALERT|NOTIFICATION|DOWNTIME ALERT|FLAPPING ALERT|ACKNOWLEDGE ALERT)"
)

#: ``type`` suffix -> timeline event category.
_TIMELINE_TYPE_MAP: tuple[tuple[str, str], ...] = (
    ("DOWNTIME ALERT", "downtime"),
    ("FLAPPING ALERT", "flap"),
    ("ACKNOWLEDGE ALERT", "ack"),
    ("NOTIFICATION", "notification"),
    ("ALERT", "state_change"),  # plain HOST/SERVICE ALERT — must be checked last
)

#: STARTED / STOPPED / CANCELLED markers carried in the ``state`` (string) or
#: ``message`` column of downtime / flapping / ack rows.
_TIMELINE_DETAIL_RE = re.compile(r"\b(STARTED|STOPPED|CANCELLED|EXPIRED)\b", re.IGNORECASE)


def _classify_timeline_type(type_str: Any) -> str:
    """Map a Naemon ``/logs`` ``type`` value to a timeline event category.

    Returns one of ``state_change`` / ``notification`` / ``downtime`` / ``flap``
    / ``ack`` / ``other``. The order of :data:`_TIMELINE_TYPE_MAP` matters:
    ``DOWNTIME ALERT`` / ``FLAPPING ALERT`` / ``ACKNOWLEDGE ALERT`` all contain
    the substring ``ALERT`` and must be matched before the bare alert fallback.
    """
    t = str(type_str or "").strip().upper()
    for needle, category in _TIMELINE_TYPE_MAP:
        if needle in t:
            return category
    return "other"


def _timeline_detail(entry: dict[str, Any]) -> str | None:
    """Extract a STARTED/STOPPED/CANCELLED marker for downtime/flap/ack rows.

    Naemon writes the marker into the ``state`` column (as a string) for these
    alert types, but some exports keep it only in ``message``. We scan both.
    """
    for field in ("state", "plugin_output", "message"):
        m = _TIMELINE_DETAIL_RE.search(str(entry.get(field) or ""))
        if m:
            return m.group(1).upper()
    return None


def _build_timeline(rows: list[Any]) -> list[dict[str, Any]]:
    """Reduce raw ``/logs`` rows into an ordered list of timeline events.

    Rows are sorted chronologically and walked once, tracking the last seen
    state per ``(host, service)`` so each ``state_change`` event can carry its
    ``from_state`` / ``to_state`` and ``duration_in_state`` (seconds the object
    spent in the previous state — ``None`` for the first transition seen).

    Host- vs service-level rows are distinguished by an empty
    ``service_description`` so the correct state vocabulary is used.
    """
    events: list[dict[str, Any]] = []
    # Per (host, service): (last_state_int, last_state_change_epoch).
    last_seen: dict[tuple[str, str], tuple[int, int]] = {}
    ordered = sorted(
        (r for r in rows if isinstance(r, dict)),
        key=lambda r: int(r.get("time") or 0),
    )
    for entry in ordered:
        epoch = int(entry.get("time") or 0)
        host = entry.get("host_name") or ""
        svc = entry.get("service_description") or ""
        category = _classify_timeline_type(entry.get("type"))
        state_map = SERVICE_STATES if svc else HOST_STATES
        event: dict[str, Any] = {
            "time": _ts(epoch),
            "epoch": epoch,
            "host": host,
            "service": svc or None,
            "type": category,
        }
        if category == "state_change":
            try:
                to_state_int = int(entry.get("state", -1))
            except (TypeError, ValueError):
                to_state_int = -1
            prev = last_seen.get((host, svc))
            event["from_state"] = (
                _format_state_label(prev[0], state_map) if prev is not None else None
            )
            event["to_state"] = _format_state_label(to_state_int, state_map)
            event["soft_hard"] = str(entry.get("state_type") or "").upper() or None
            dur = epoch - prev[1] if prev is not None else None
            event["duration_in_state"] = dur
            event["duration_in_state_human"] = _duration_human(dur) if dur is not None else None
            last_seen[(host, svc)] = (to_state_int, epoch)
        elif category == "notification":
            event["state"] = _format_state_label(entry.get("state"), state_map)
            event["contact"] = entry.get("contact_name") or None
        else:
            detail = _timeline_detail(entry)
            if detail:
                event["detail"] = detail
        plugin = entry.get("plugin_output")
        if plugin:
            event["plugin_output"] = plugin
        events.append(event)
    return events


def _timeline_summary(
    rows: list[Any],
    events: list[dict[str, Any]],
    window_start: int | None,
    window_end: int,
) -> dict[str, Any]:
    """Aggregate the post-mortem summary block from the fetched rows + events.

    Incident-level metrics (count / total downtime / MTTR) reuse the
    :mod:`thruk_mcp.reliability` reducer per ``(host, service)`` and are summed
    across objects, so a hostgroup timeline reconciles every member's incidents.
    """
    type_counts: dict[str, int] = {}
    for ev in events:
        type_counts[ev["type"]] = type_counts.get(ev["type"], 0) + 1
    hard_transitions = sum(
        1 for ev in events if ev["type"] == "state_change" and ev.get("soft_hard") == "HARD"
    )

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("host_name") or "", entry.get("service_description") or "")
        groups.setdefault(key, []).append(entry)

    incidents: list[dict[str, Any]] = []
    for entries in groups.values():
        incidents.extend(
            extract_incidents(entries, window_start=window_start, window_end=window_end)
        )
    recovered = [inc for inc in incidents if not inc["ongoing"]]
    total_downtime = sum(inc["duration_seconds"] for inc in incidents)
    longest = max((inc["duration_seconds"] for inc in incidents), default=0)
    mttr: int | None = None
    if recovered:
        mttr = round(sum(inc["duration_seconds"] for inc in recovered) / len(recovered))

    epochs = [ev["epoch"] for ev in events if ev.get("epoch")]
    return {
        "events": len(events),
        "state_changes": type_counts.get("state_change", 0),
        "hard_transitions": hard_transitions,
        "notifications": type_counts.get("notification", 0),
        "downtimes": type_counts.get("downtime", 0),
        "flaps": type_counts.get("flap", 0),
        "acks": type_counts.get("ack", 0),
        "incidents": len(incidents),
        "ongoing": any(inc["ongoing"] for inc in incidents),
        "total_downtime_seconds": total_downtime,
        "total_downtime_human": _duration_human(total_downtime),
        "longest_incident_seconds": longest,
        "longest_incident_human": _duration_human(longest),
        "mttr_seconds": mttr,
        "mttr_human": _duration_human(mttr) if mttr is not None else None,
        "first_event": _ts(min(epochs)) if epochs else None,
        "last_event": _ts(max(epochs)) if epochs else None,
    }


async def thruk_incident_timeline(
    filter: dict[str, Any] | None = None,
    since: str | None = _DEFAULT_SINCE,
    until: str | None = None,
    limit: int = 500,
    backends: str | None = None,
) -> str:
    """Ordered event chronology for a host / service / hostgroup (post-mortem "déroulé").

    Where ``thruk_reliability_report`` returns only *aggregate* MTTR / incident
    metrics, this reconstructs the full **ordered sequence** of events from the
    ``/logs`` table — every state change, notification, downtime, flap and
    acknowledgement — so it can be dropped straight into the chronology section
    of a post-mortem, per client or per host.

    A scoping ``filter`` is **required** (``host``, ``service``, ``hostgroup``
    or ``custom_var``): an unscoped parc-wide timeline is unbounded and
    meaningless. ``hostgroup`` / ``custom_var`` are resolved to a
    ``host_name[regex]`` via a ``/hosts`` lookup (AND-only), like the other
    log-family tools.

    ``since`` / ``until`` accept Thruk relative (``"-24h"``, ``"-7d"``) or
    absolute (``"2026-05-21 14:00:00"``) values. Default window: last 24 h.
    ``limit`` caps the number of timeline events returned (earliest first); the
    summary is always computed over the full fetched set.

    Each timeline event carries ``time`` / ``epoch``, ``host``, ``service``
    (``null`` for host-level), ``type`` (``state_change`` / ``notification`` /
    ``downtime`` / ``flap`` / ``ack``) and ``plugin_output`` when present.
    ``state_change`` events add ``from_state`` / ``to_state`` / ``soft_hard``
    and ``duration_in_state`` (seconds in the previous state, ``null`` for the
    first); ``notification`` events add ``contact`` and ``state``;
    downtime/flap/ack events add a ``detail`` (``STARTED`` / ``STOPPED`` / …).

    The ``summary`` block reports event/transition counts, ``incidents``,
    ``total_downtime``, ``longest_incident``, ``mttr`` and first/last event —
    incident metrics reuse the same HARD-transition reducer as
    ``thruk_reliability_report`` (consecutive non-OK HARD states collapse into
    one incident; ongoing incidents clamp at ``until``).

    Note: acknowledgements that the monitoring core logs only as a class-2
    ``EXTERNAL COMMAND`` (rather than a native ``* ACKNOWLEDGE ALERT`` row) do
    not appear in the timeline.

    Returns a wrapped object: ``since``, ``until``, ``total_events``,
    ``summary``, ``timeline`` (sorted ascending by time).
    """
    if not filter:
        return _tool_response(
            {
                "error": (
                    "thruk_incident_timeline requires a 'filter' scoping the timeline to "
                    "a host, service, hostgroup or custom_var."
                )
            }
        )
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    extra["type[~]"] = _TIMELINE_TYPE_REGEX
    if since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if until:
        extra["time[lte]"] = _epoch_filter_value(until)

    # Cheap server-side count(*) so we can warn when the window holds more
    # events than the raw-fetch cap and the timeline is therefore partial
    # (mirrors thruk_reliability_report, issue #312).
    count_rows, _count_warnings = await _get_client().get_with_fallback(
        "/logs",
        params={"columns": "count(*):cnt", "limit": 1, **extra},
        backends=_backends(backends),
        method="POST",
    )
    total_events = _sum_cnt(count_rows)

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "time",  # ascending: a timeline must be chronological
        "columns": _TIMELINE_COLUMNS,
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    window_start = _parse_thruk_time(since) if since else None
    window_end = _parse_thruk_time(until) if until else None
    if window_end is None:
        window_end = _now_utc_epoch()

    events = _build_timeline(data)
    summary = _timeline_summary(data, events, window_start, window_end)

    truncated = len(events) > limit
    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "total_events": total_events,
        "summary": summary,
        "timeline": events[:limit],
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    elif total_events > _NOISY_MAX_ALERTS:
        payload["_warning"] = (
            f"Window holds {total_events} log entries but only {_NOISY_MAX_ALERTS} "
            "were analysed; the timeline is partial. Narrow the window or filter." + _NOISY_CAP_HINT
        )
    elif truncated:
        payload["_warning"] = (
            f"Timeline truncated to the earliest {limit} of {len(events)} events; "
            "the summary covers all of them. Raise 'limit' or narrow the window."
        )
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


# ---------------------------------------------------------------------------
# thruk_state_at / thruk_state_diff (issue #324)
# ---------------------------------------------------------------------------
# Point-in-time reconstruction of the parc state at a *past* timestamp, replayed
# from ``/logs`` — answers "qu'est-ce qui était DOWN à 15h45 ?" independently of
# the current (often already-recovered) state. Reuses the timeline classifier /
# detail extractor; the reducer is pure so ``thruk_state_diff`` can fetch the log
# window once and reconstruct at two distinct cut-off instants.
# ---------------------------------------------------------------------------

#: One regex spanning the alert families we replay to rebuild state. HOST/SERVICE
#: ALERT carries the state + state_type; DOWNTIME/ACKNOWLEDGE ALERT carry the
#: in_downtime / acknowledged flags via their STARTED/STOPPED detail.
_STATE_AT_TYPE_REGEX = "^(HOST|SERVICE) (ALERT|DOWNTIME ALERT|ACKNOWLEDGE ALERT)"

#: ``/logs`` columns needed to reconstruct each object's state.
_STATE_AT_COLUMNS = "host_name,service_description,state,state_type,time,type,plugin_output,message"


def _reconstruct_state_at(rows: list[Any], at_epoch: int) -> dict[tuple[str, str], dict[str, Any]]:
    """Replay ``/logs`` rows to rebuild each object's state as of ``at_epoch``.

    Walks the rows chronologically, ignoring any with ``time > at_epoch`` (this
    cut-off is what lets :func:`thruk_state_diff` fetch one window and rebuild at
    two instants). Per ``(host, service)`` we track:

    * ``state`` / ``state_type`` — from the last HOST/SERVICE ALERT row. The row
      time becomes ``since``; a run of same-state rows (e.g. SOFT then HARD of the
      same code) keeps the *earliest* entry, so ``since`` marks when the object
      entered that state.
    * ``in_downtime`` — toggled by DOWNTIME ALERT STARTED vs STOPPED/CANCELLED/EXPIRED.
    * ``acknowledged`` — set by ACKNOWLEDGE ALERT, cleared on a recovery (state
      back to OK/UP) or an explicit STOPPED/CANCELLED/EXPIRED.

    Host- vs service-level rows are distinguished by an empty ``service_description``.
    Objects with no replayable row in the fetched window simply do not appear —
    an inherent limit of log replay, surfaced via the caller's cap ``_warning``.
    """
    state: dict[tuple[str, str], dict[str, Any]] = {}
    ordered = sorted(
        (r for r in rows if isinstance(r, dict) and int(r.get("time") or 0) <= at_epoch),
        key=lambda r: int(r.get("time") or 0),
    )
    for entry in ordered:
        epoch = int(entry.get("time") or 0)
        host = entry.get("host_name") or ""
        svc = entry.get("service_description") or ""
        key = (host, svc)
        rec = state.setdefault(
            key,
            {
                "state": None,
                "state_type": None,
                "since": None,
                "in_downtime": False,
                "acknowledged": False,
                "plugin_output": None,
            },
        )
        category = _classify_timeline_type(entry.get("type"))
        if category == "state_change":
            try:
                s = int(entry.get("state", -1))
            except (TypeError, ValueError):
                s = -1
            if s != rec["state"]:
                rec["since"] = epoch
            rec["state"] = s
            rec["state_type"] = str(entry.get("state_type") or "").upper() or None
            rec["plugin_output"] = entry.get("plugin_output") or None
            if s == 0:  # recovery (OK/UP) clears any standing acknowledgement
                rec["acknowledged"] = False
        elif category == "downtime":
            detail = _timeline_detail(entry)
            if detail == "STARTED":
                rec["in_downtime"] = True
            elif detail in ("STOPPED", "CANCELLED", "EXPIRED"):
                rec["in_downtime"] = False
        elif category == "ack":
            detail = _timeline_detail(entry)
            if detail in ("STOPPED", "CANCELLED", "EXPIRED"):
                rec["acknowledged"] = False
            else:
                # STARTED — or no explicit marker, which Naemon omits on the
                # initial ACKNOWLEDGE ALERT row: presence means it is acked.
                rec["acknowledged"] = True
    return state


def _state_object(host: str, svc: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Format one reconstructed ``(host, service)`` record into an output object."""
    state_map = SERVICE_STATES if svc else HOST_STATES
    since = rec["since"]
    return {
        "host": host,
        "service": svc or None,
        "state": _format_state_label(rec["state"], state_map),
        "state_type": rec["state_type"],
        "since": _ts(since) if since else None,
        "since_human": _duration_human(_now_utc_epoch() - since) if since else None,
        "in_downtime": rec["in_downtime"],
        "acknowledged": rec["acknowledged"],
        "plugin_output": rec["plugin_output"],
    }


def _is_problem(rec: dict[str, Any]) -> bool:
    """True when the reconstructed state is a non-OK/UP problem (state code > 0)."""
    s = rec.get("state")
    return isinstance(s, int) and s > 0


def _state_snapshot(
    state: dict[tuple[str, str], dict[str, Any]],
    problems_only: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Turn a reconstructed state map into a sorted object list + summary.

    Objects are sorted problems-first, then by host/service. The ``summary``
    counts total / ok / problems and a ``by_state`` breakdown over the full
    reconstructed set (independent of ``problems_only``, which only filters the
    returned object list).
    """
    by_state: dict[str, int] = {}
    problems = 0
    for (_host, svc), rec in state.items():
        label = _format_state_label(rec["state"], SERVICE_STATES if svc else HOST_STATES)
        by_state[label] = by_state.get(label, 0) + 1
        if _is_problem(rec):
            problems += 1
    summary = {
        "total": len(state),
        "ok": len(state) - problems,
        "problems": problems,
        "by_state": dict(sorted(by_state.items())),
    }
    items = [
        ((host, svc), rec)
        for (host, svc), rec in state.items()
        if not problems_only or _is_problem(rec)
    ]
    items.sort(key=lambda kv: (not _is_problem(kv[1]), kv[0][0], kv[0][1]))
    objects = [_state_object(host, svc, rec) for (host, svc), rec in items]
    return objects, summary


def _diff_states(
    before: dict[tuple[str, str], dict[str, Any]],
    after: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Diff two reconstructed state maps, one change object per object that moved.

    Each change carries the object identity plus, for the dimensions that
    actually moved, ``*_change`` blocks. ``category`` summarises the move:
    ``new_problem`` (OK→non-OK), ``recovered`` (non-OK→OK), ``state_changed``
    (non-OK→different non-OK), ``downtime_changed`` / ``ack_changed`` (only the
    flag moved). Objects present in only one snapshot use ``None`` as the missing
    side's state. Objects identical on every tracked dimension are omitted.
    """
    changes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    empty = {"state": None, "state_type": None, "in_downtime": False, "acknowledged": False}
    for key in sorted(before.keys() | after.keys()):
        host, svc = key
        b = before.get(key, empty)
        a = after.get(key, empty)
        state_moved = b["state"] != a["state"]
        dt_moved = b["in_downtime"] != a["in_downtime"]
        ack_moved = b["acknowledged"] != a["acknowledged"]
        if not (state_moved or dt_moved or ack_moved):
            continue
        if state_moved:
            if not _is_problem(b) and _is_problem(a):
                category = "new_problem"
            elif _is_problem(b) and not _is_problem(a):
                category = "recovered"
            else:
                category = "state_changed"
        elif dt_moved:
            category = "downtime_changed"
        else:
            category = "ack_changed"
        counts[category] = counts.get(category, 0) + 1
        state_map = SERVICE_STATES if svc else HOST_STATES
        change: dict[str, Any] = {
            "host": host,
            "service": svc or None,
            "category": category,
        }
        if state_moved:
            change["from_state"] = _format_state_label(b["state"], state_map)
            change["to_state"] = _format_state_label(a["state"], state_map)
        if dt_moved:
            change["in_downtime"] = {"from": b["in_downtime"], "to": a["in_downtime"]}
        if ack_moved:
            change["acknowledged"] = {"from": b["acknowledged"], "to": a["acknowledged"]}
        changes.append(change)
    summary = {"changed": len(changes), "by_category": dict(sorted(counts.items()))}
    return changes, summary


async def _fetch_state_logs(
    extra: dict[str, Any], upto_epoch: int, backends: str | None
) -> tuple[list[Any], int, list[str]]:
    """Fetch the ``/logs`` window up to ``upto_epoch`` for state reconstruction.

    Returns ``(rows, total, warnings)``. ``total`` is a server-side ``count(*)``
    used to warn when the pre-cut-off history exceeds ``_NOISY_MAX_ALERTS`` (an
    object whose last change predates the fetched slice would be missed). Mirrors
    the bounded-fetch + count pattern of :func:`thruk_incident_timeline`.
    """
    scoped = {**extra, "type[~]": _STATE_AT_TYPE_REGEX, "time[lte]": str(upto_epoch)}
    count_rows, _count_warnings = await _get_client().get_with_fallback(
        "/logs",
        params={"columns": "count(*):cnt", "limit": 1, **scoped},
        backends=_backends(backends),
        method="POST",
    )
    total = _sum_cnt(count_rows)
    data, warnings = await _get_client().get_with_fallback(
        "/logs",
        params={
            "limit": _NOISY_MAX_ALERTS,
            "sort": "-time",
            "columns": _STATE_AT_COLUMNS,
            **scoped,
        },
        backends=_backends(backends),
        method="POST",
    )
    if not isinstance(data, list):
        data = []
    return data, total, warnings


def _state_cap_warning(total: int) -> str | None:
    """Warn when the replayed window exceeded the raw-fetch cap (partial state)."""
    if total > _NOISY_MAX_ALERTS:
        return (
            f"Window holds {total} log entries but only {_NOISY_MAX_ALERTS} were "
            "replayed; objects whose last change predates that slice are missing. "
            "Narrow the filter." + _NOISY_CAP_HINT
        )
    return None


async def thruk_state_at(
    timestamp: str | None = None,
    filter: dict[str, Any] | None = None,
    problems_only: bool = False,
    backends: str | None = None,
) -> str:
    """Reconstruct the parc state at a **past** instant from ``/logs`` (post-mortem snapshot).

    Answers "qu'est-ce qui était DOWN à 15h45 ?" — the state of every host /
    service at ``timestamp``, **independent of the current state** (which has
    often already recovered by the time the analysis runs). State is replayed
    from the ``/logs`` table: each object's state is the last HOST/SERVICE ALERT
    with ``time <= timestamp``; ``in_downtime`` / ``acknowledged`` are derived
    from DOWNTIME / ACKNOWLEDGE ALERT rows over the same window.

    ``timestamp`` is required (Thruk relative ``"-2h"`` or absolute
    ``"2026-06-16 15:45:00"`` — interpreted as UTC). A scoping ``filter``
    (``host`` / ``service`` / ``hostgroup`` / ``custom_var``) is also **required**:
    an unscoped parc-wide replay is unbounded. ``hostgroup`` / ``custom_var`` are
    resolved to a ``host_name[regex]`` via a ``/hosts`` lookup, like the other
    log-family tools. Set ``problems_only=true`` to drop OK/UP objects.

    Each returned object carries ``host``, ``service`` (``null`` for host-level),
    ``state`` + ``state_type`` (SOFT/HARD — soft states are transient and shown
    explicitly), ``since`` / ``since_human`` (when the object entered that state),
    ``in_downtime``, ``acknowledged`` and ``plugin_output``. The ``summary`` block
    reports ``total`` / ``ok`` / ``problems`` and a ``by_state`` breakdown over
    the full reconstructed set.

    Limits: only objects with at least one log row in the fetched window appear
    (objects unchanged for a long time before ``timestamp`` may be missing — a
    ``_warning`` is set when the window exceeds the replay cap). Acknowledgements
    that the core logs only as a class-2 ``EXTERNAL COMMAND`` (rather than a
    native ``* ACKNOWLEDGE ALERT`` row) are not reflected in ``acknowledged``.
    """
    if not timestamp:
        return _tool_response(
            {"error": "thruk_state_at requires a 'timestamp' (the past instant to reconstruct)."}
        )
    if not filter:
        return _tool_response(
            {
                "error": (
                    "thruk_state_at requires a 'filter' scoping the snapshot to a host, "
                    "service, hostgroup or custom_var."
                )
            }
        )
    at_epoch = _parse_thruk_time(timestamp)
    if at_epoch is None:
        return _tool_response({"error": f"Could not parse timestamp: {timestamp!r}."})

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    rows, total, warnings = await _fetch_state_logs(extra, at_epoch, backends)
    state = _reconstruct_state_at(rows, at_epoch)
    objects, summary = _state_snapshot(state, problems_only)

    payload: dict[str, Any] = {
        "timestamp": _ts(at_epoch),
        "problems_only": problems_only,
        "summary": summary,
        "objects": objects,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    else:
        cap = _state_cap_warning(total)
        if cap:
            payload["_warning"] = cap
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


async def thruk_state_diff(
    t1: str | None = None,
    t2: str | None = None,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """List what changed between two **past** instants ``t1`` → ``t2`` (replayed from ``/logs``).

    Reconstructs the parc state at ``t1`` and at ``t2`` (one ``/logs`` fetch up to
    the later instant, replayed at both cut-offs) and reports every object whose
    state, downtime flag or acknowledgement moved — the "diff of two photos" for a
    report, e.g. what broke between the start and the peak of an incident.

    Both ``t1`` and ``t2`` are required (Thruk relative ``"-2h"`` or absolute
    ``"2026-06-16 15:45:00"`` — UTC); order is normalised so ``t1`` is the
    earlier instant. A scoping ``filter`` (``host`` / ``service`` / ``hostgroup``
    / ``custom_var``) is **required**, as for :func:`thruk_state_at`.

    Each change carries ``host``, ``service``, a ``category`` (``new_problem`` =
    OK→non-OK, ``recovered`` = non-OK→OK, ``state_changed`` = non-OK→other non-OK,
    ``downtime_changed`` / ``ack_changed``) and, for the dimensions that moved,
    ``from_state`` / ``to_state`` and/or ``in_downtime`` / ``acknowledged``
    ``{from, to}`` blocks. Objects identical at both instants are omitted; the
    ``summary`` reports the total changed count and a ``by_category`` breakdown.

    Same log-replay limits and acknowledgement caveat as :func:`thruk_state_at`.
    """
    if not t1 or not t2:
        return _tool_response(
            {"error": "thruk_state_diff requires both 't1' and 't2' (the two instants to compare)."}
        )
    if not filter:
        return _tool_response(
            {
                "error": (
                    "thruk_state_diff requires a 'filter' scoping the diff to a host, "
                    "service, hostgroup or custom_var."
                )
            }
        )
    e1, e2 = _parse_thruk_time(t1), _parse_thruk_time(t2)
    if e1 is None:
        return _tool_response({"error": f"Could not parse t1: {t1!r}."})
    if e2 is None:
        return _tool_response({"error": f"Could not parse t2: {t2!r}."})
    if e1 > e2:  # normalise so t1 is the earlier instant
        t1, t2, e1, e2 = t2, t1, e2, e1

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_SERVICES, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    rows, total, warnings = await _fetch_state_logs(extra, e2, backends)
    before = _reconstruct_state_at(rows, e1)
    after = _reconstruct_state_at(rows, e2)
    changes, summary = _diff_states(before, after)

    payload: dict[str, Any] = {
        "t1": _ts(e1),
        "t2": _ts(e2),
        "summary": summary,
        "changes": changes,
    }
    if host_truncated:
        payload["_warning"] = (
            f"Host list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "results may be incomplete."
        )
    else:
        cap = _state_cap_warning(total)
        if cap:
            payload["_warning"] = cap
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


# ``_resolve_log_filter`` / ``_resolve_hosts_to_regex_from_params`` live in
# :mod:`thruk_mcp.helpers` (issue #258); ``_resolve_log_filter`` is imported above.


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
        extra["time[gte]"] = _epoch_filter_value(since)
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = _epoch_filter_value(until)
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
        extra["time[gte]"] = _epoch_filter_value(since)
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = _epoch_filter_value(until)
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
        extra["time[gte]"] = _epoch_filter_value(since)
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = _epoch_filter_value(until)
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


#: Maps the ``group_by`` dimension to its Naemon ``/logs`` column. For
#: ``state`` the raw code is mapped to a human-readable label *per row*
#: (issue #282): host- and service-notification state vocabularies differ, so
#: the context is derived from ``service_description`` (empty => host
#: notification => HOST_STATES, otherwise SERVICE_STATES).
_NOTIF_GROUP_FIELDS: dict[str, str] = {
    "contact": "contact_name",
    "host": "host_name",
    "service": "service_description",
    "state": "state",
    "command": "command_name",
}

#: ``group_by`` dimensions that have no /logs column and are instead resolved by
#: aggregating per host/(host,service) and fanning out across group membership
#: via a /hosts or /services lookup (issue #318).
_NOTIF_TOPOLOGY_GROUP_BY: tuple[str, ...] = ("hostgroup", "servicegroup")


async def thruk_notification_summary(
    group_by: str = "contact",
    since: str | None = "-24h",
    until: str | None = None,
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Count notifications grouped by a single dimension over a time window.

    Aggregates ``/logs`` ``class=3`` (notification) entries and returns the
    per-group counts sorted by ``count`` descending — the notification
    equivalent of :func:`thruk_top_noisy_hosts` for alerts.

    ``group_by`` selects the dimension: ``contact`` (default), ``host``,
    ``service``, ``state``, ``command``, ``hostgroup`` or ``servicegroup``.
    The last two are a "ventilation par client" (issue #318): counts are
    aggregated per host/(host,service) and then fanned out across each object's
    group membership (an object in N groups contributes to each; objects in
    none land in a ``"(none)"`` bucket).

    ``since`` / ``until`` accept Thruk relative (``"-24h"``, ``"-7d"``) or
    absolute (``"2026-05-21 14:00:00"``) values. Default window: last 24 h.

    ``filter`` fields: ``host``, ``service``, ``contact``, ``state``,
    ``since`` / ``until``, ``hostgroup`` and ``custom_var`` (AND-only, /hosts
    lookup) — identical to :func:`thruk_list_notifications`.

    Returns a wrapped object: ``since``, ``until``, ``group_by``, ``total``
    (notifications counted in the window) and ``results`` sorted by ``count``
    desc. Most dimensions yield ``{<group_by>, count, last_time}``; the
    ``hostgroup`` / ``servicegroup`` dimensions additionally carry
    ``hosts_affected`` (and ``services_affected`` for servicegroup).

    The fetch is issued newest-first (``sort="-time"``) so that when the
    ``_NOISY_MAX_ALERTS`` cap is hit it is the *oldest* notifications that are
    dropped, keeping the most recent ones counted.
    """
    is_topology = group_by in _NOTIF_TOPOLOGY_GROUP_BY
    group_field = _NOTIF_GROUP_FIELDS.get(group_by)
    if group_field is None and not is_topology:
        allowed = [*_NOTIF_GROUP_FIELDS, *_NOTIF_TOPOLOGY_GROUP_BY]
        return _tool_response(
            {"error": f"Invalid group_by {group_by!r}. Allowed: {', '.join(allowed)}"}
        )

    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOTIFICATIONS, backends)
    if errs:
        return _tool_response({"error": errs[0]})

    extra["class"] = "3"
    if "time[gte]" not in extra and since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = _epoch_filter_value(until)

    # Columns: topology grouping needs host_name (+ service_description for
    # servicegroup) to map onto groups; group_by=state needs service_description
    # to tell a host notification from a service one (issue #282).
    wanted_cols = {"time"}
    if is_topology:
        wanted_cols.add("host_name")
        if group_by == "servicegroup":
            wanted_cols.add("service_description")
    else:
        assert group_field is not None  # narrowed by the validation above
        wanted_cols.add(group_field)
        if group_by == "state":
            wanted_cols.add("service_description")
    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        # Newest-first (mirrors thruk_alert_heatmap, issue #250): when the cap
        # is hit, drop the *oldest* entries so recent notifications stay counted.
        "sort": "-time",
        "columns": ",".join(sorted(wanted_cols)),
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    total = 0
    regroup_trunc = False
    if is_topology:
        # Count per (host[,service]) then fan out across group membership.
        need_service = group_by == "servicegroup"
        per_obj: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            host = str(entry.get("host_name") or "")
            svc = str(entry.get("service_description") or "") if need_service else ""
            rec = per_obj.setdefault(
                (host, svc),
                {"host_name": host, "service_description": svc, "count": 0, "last_ts": 0},
            )
            rec["count"] += 1
            total += 1
            t = int(entry.get("time") or 0)
            if t > rec["last_ts"]:
                rec["last_ts"] = t
        grouped, regroup_trunc = await _regroup_records_by_group(
            list(per_obj.values()), group_by, count_key="count", backends=backends
        )
        results = [
            {
                group_by: r[group_by],
                "count": r["count"],
                "hosts_affected": r["hosts_affected"],
                **({"services_affected": r["services_affected"]} if need_service else {}),
                "last_time": _ts(r["last_ts"]),
            }
            for r in grouped
        ]
    else:
        counts: dict[str, dict[str, Any]] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if group_by == "state":
                # Derive host-vs-service context per row, then map the raw code
                # to a label. Routing through _format_state_label means state 0
                # maps to "UP"/"OK" (no more bare "" bucket from `0 or ""`) and
                # unmapped codes surface as "UNKNOWN(<n>)" (issue #282).
                state_map = SERVICE_STATES if entry.get("service_description") else HOST_STATES
                key = _format_state_label(entry.get("state"), state_map)
            else:
                key = str(entry.get(group_field) or "")
            rec = counts.setdefault(key, {"count": 0, "_last_ts": 0, "last_time": None})
            rec["count"] += 1
            total += 1
            t = entry.get("time") or 0
            if t > rec["_last_ts"]:
                rec["_last_ts"] = t
                rec["last_time"] = _ts(t)
        results = sorted(
            (
                {group_by: key, "count": v["count"], "last_time": v["last_time"]}
                for key, v in counts.items()
            ),
            key=lambda x: x["count"],
            reverse=True,
        )

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "group_by": group_by,
        "total": total,
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
            + _NOISY_CAP_HINT
        )
    elif regroup_trunc:
        payload["_warning"] = _REGROUP_TRUNC_WARNING
    if warnings:
        payload["_warnings"] = warnings
    return _tool_response(payload)


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
        extra["time[gte]"] = _epoch_filter_value(f"-{hours}h")
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
# Co-located tool specs (spliced into server.TOOL_REGISTRY, preserving order)
# ---------------------------------------------------------------------------
# The history-family tools were registered in two non-contiguous blocks in the
# original ``server.py`` TOOL_REGISTRY (the noisy/flap/trends block came before
# the inventory tools; the log/alert/notification block came after them). To
# preserve registration order byte-for-byte, they are exposed as two slices and
# spliced back at their respective positions in ``server.py``.

_SINCE_WINDOW = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": "-24h",
    "description": (
        'Start of analysis window. Thruk relative time ("-2h", "-7d") '
        'or ISO datetime ("2026-05-21 14:00:00"). Default: last 24 h.'
    ),
}

#: Reliability report defaults to a longer 30-day window (issue #286): MTTR /
#: MTBF only become meaningful once several incidents have accumulated.
_SINCE_WINDOW_30D = {
    "anyOf": [{"type": "string"}, {"type": "null"}],
    "default": "-30d",
    "description": (
        'Start of analysis window. Thruk relative time ("-30d", "-7d") '
        'or ISO datetime ("2026-05-21 14:00:00"). Default: last 30 days.'
    ),
}

HISTORY_TRENDS_REGISTRY: list[ToolSpec] = [
    # ---------------------------------------------------------------- noisy / flap
    ToolSpec(
        name="thruk_top_noisy_hosts",
        fn=thruk_top_noisy_hosts,
        schema=build_tool_schema(
            FIELDS_NOISY_HOSTS,
            since=_SINCE_WINDOW,
            until=_UNTIL,
            limit=_int(default=10),
            group_by={
                "type": "string",
                "default": "host",
                "description": (
                    "Aggregation dimension: 'host' (default, one row per host) or "
                    "'hostgroup' (ventilation par client — counts fanned out across "
                    "hostgroup membership)."
                ),
                "enum": ["host", "hostgroup"],
            },
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_top_noisy_services",
        fn=thruk_top_noisy_services,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            since=_SINCE_WINDOW,
            until=_UNTIL,
            limit=_int(default=10),
            group_by={
                "type": "string",
                "default": "service",
                "description": (
                    "Aggregation dimension: 'service' (default, one row per "
                    "host+service), 'host' (rolled up per host), 'hostgroup' or "
                    "'servicegroup' (ventilation par client)."
                ),
                "enum": ["service", "host", "hostgroup", "servicegroup"],
            },
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_flap_summary",
        fn=thruk_flap_summary,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            since=_SINCE_WINDOW,
            until=_UNTIL,
            limit=_int(default=10),
            min_transitions=_int(default=3),
            backends=_BACKENDS,
        ),
    ),
    # ----------------------------------------------------- trends & history (issue #57)
    ToolSpec(
        name="thruk_alert_heatmap",
        fn=thruk_alert_heatmap,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            since=_SINCE_WINDOW,
            until=_UNTIL,
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
        name="thruk_notification_heatmap",
        fn=thruk_notification_heatmap,
        schema=build_tool_schema(
            FIELDS_NOTIFICATIONS,
            since=_SINCE_WINDOW,
            until=_UNTIL,
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
            since=_SINCE_WINDOW,
            until=_UNTIL,
            min_alerts=_int(
                "Minimum number of non-recovery alert events to be included (default 5).",
                default=5,
            ),
            limit=_int("Maximum number of results (default 10).", default=10),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_reliability_report",
        fn=thruk_reliability_report,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            since=_SINCE_WINDOW_30D,
            until=_UNTIL,
            limit=_int(
                "Maximum number of host/service objects to return (busiest first, default 50).",
                default=50,
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_incident_timeline",
        fn=thruk_incident_timeline,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            since=_SINCE_WINDOW,
            until=_UNTIL,
            limit=_int(
                "Maximum number of timeline events to return, earliest first "
                "(default 500). The summary always covers the full window.",
                default=500,
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_state_at",
        fn=thruk_state_at,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            timestamp={
                "type": "string",
                "description": (
                    'Past instant to reconstruct (required). Thruk relative ("-2h") or '
                    'absolute ("2026-06-16 15:45:00", interpreted as UTC).'
                ),
            },
            problems_only=_bool(
                "Return only non-OK/UP objects (drops the OK/UP rows). Default false.",
                default=False,
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_state_diff",
        fn=thruk_state_diff,
        schema=build_tool_schema(
            FIELDS_NOISY_SERVICES,
            t1={
                "type": "string",
                "description": (
                    'First instant (required). Thruk relative ("-2h") or absolute '
                    '("2026-06-16 15:45:00", UTC). Order is normalised to earlier→later.'
                ),
            },
            t2={
                "type": "string",
                "description": "Second instant (required, same formats as t1).",
            },
            backends=_BACKENDS,
        ),
    ),
]

HISTORY_LOGS_REGISTRY: list[ToolSpec] = [
    # ---------------------------------------------------------------- log / alert / notification
    ToolSpec(
        name="thruk_list_logs",
        fn=thruk_list_logs,
        schema=build_tool_schema(
            FIELDS_LOGS,
            since=_SINCE,
            until=_UNTIL,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_sort("-time"),
            columns=_COLUMNS,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_alerts",
        fn=thruk_list_alerts,
        schema=build_tool_schema(
            FIELDS_ALERTS,
            since=_SINCE,
            until=_UNTIL,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_sort("-time"),
            columns=_COLUMNS,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_list_notifications",
        fn=thruk_list_notifications,
        schema=build_tool_schema(
            FIELDS_NOTIFICATIONS,
            since=_SINCE,
            until=_UNTIL,
            limit=_int(default=100),
            offset=_int(default=0),
            sort=_sort("-time"),
            columns=_COLUMNS,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_notification_summary",
        fn=thruk_notification_summary,
        schema=build_tool_schema(
            FIELDS_NOTIFICATIONS,
            group_by={
                "type": "string",
                "default": "contact",
                "description": (
                    "Dimension to group notification counts by: 'contact' "
                    "(default), 'host', 'service', 'state', 'command', "
                    "'hostgroup' or 'servicegroup' (the last two: ventilation "
                    "par client)."
                ),
                "enum": [
                    "contact",
                    "host",
                    "service",
                    "state",
                    "command",
                    "hostgroup",
                    "servicegroup",
                ],
            },
            since=_SINCE_WINDOW,
            until=_UNTIL,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_recent_events",
        fn=thruk_recent_events,
        schema=build_tool_schema(
            FIELDS_LOGS,
            hours=_int(default=1),
            only_alerts=_bool(default=False),
            limit=_int(default=100),
            offset=_int(default=0),
            columns=_COLUMNS,
            backends=_BACKENDS,
        ),
    ),
]

# Convenience aggregate (registration order: trends block, then logs block).
HISTORY_REGISTRY: list[ToolSpec] = [*HISTORY_TRENDS_REGISTRY, *HISTORY_LOGS_REGISTRY]


__all__ = [
    "HISTORY_LOGS_REGISTRY",
    "HISTORY_REGISTRY",
    "HISTORY_TRENDS_REGISTRY",
    "_BUCKET_SIZES",
    "_DEFAULT_SINCE",
    "_aggregate_alerts",
    "_coerce_hours_to_since",
    "_fetch_logs",
    "_resolve_hosts_to_regex",
    "thruk_alert_heatmap",
    "thruk_flap_summary",
    "thruk_incident_timeline",
    "thruk_list_alerts",
    "thruk_list_logs",
    "thruk_list_notifications",
    "thruk_notification_heatmap",
    "thruk_notification_summary",
    "thruk_recent_events",
    "thruk_recurring_problems",
    "thruk_reliability_report",
    "thruk_state_at",
    "thruk_state_diff",
    "thruk_top_noisy_hosts",
    "thruk_top_noisy_services",
]
