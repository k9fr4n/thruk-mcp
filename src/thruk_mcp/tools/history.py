"""Logs / history / trends tools (issue #260 — server.py split).

Parent: #256. This module hosts the nine read-only "history" tools that turn
the Naemon/Thruk ``/logs`` table into trends, heatmaps and raw event listings:

* ``thruk_top_noisy_hosts``     — top hosts by HOST ALERT count.
* ``thruk_top_noisy_services``  — top services by SERVICE ALERT count.
* ``thruk_flap_summary``        — objects with the most state transitions.
* ``thruk_alert_heatmap``       — alert counts bucketed over a time window.
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
    filter_schema_property,
    infer_alert_type_regex,
)
from ..helpers import (
    _RESOLVE_HOSTS_HARD_LIMIT,
    _backends,
    _build_cv_params,
    _format_state_label,
    _get_client,
    _list_params,
    _now_utc_epoch,
    _parse_thruk_time,
    _resolve_log_filter,
    _tool_response,
    _ts,
)
from .base import (
    _BACKENDS,
    _OPT_STR,
    ToolSpec,
    _bool,
    _int,
    _str,
)

# State maps — sourced from constants.py (single source of truth, issue #81).
# Local aliases preserve the original ``server.py`` names used in the function
# bodies below, with no behaviour change.
HOST_STATES: dict[int, str] = HOST_STATE_STR
SERVICE_STATES: dict[int, str] = SVC_STATE_STR


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
    # Issue #248: request ``type`` so we can re-verify each row client-side.
    columns_set = {"host_name", "state", "time", "type"} | set(key_fields)
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

    # Issue #248: defensive client-side type guard. Thruk already scopes the
    # query via ``type[~]`` server-side, but we re-verify each row's ``type``
    # against the same pattern so a SERVICE ALERT (service state vocabulary,
    # e.g. state=3 UNKNOWN) can never leak into HOST aggregation -- and vice
    # versa. Rows that omit ``type`` (older backends / fixtures) are kept.
    type_matcher = re.compile(type_regex)
    cross_type_dropped = 0

    counts: dict[tuple[str, ...], dict[str, Any]] = {}
    for entry in data:
        etype = entry.get("type")
        if isinstance(etype, str) and etype and not type_matcher.match(etype):
            cross_type_dropped += 1
            continue
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
                # Issue #245: render unmapped ints (Naemon log rows occasionally
                # carry host state=3) as "UNKNOWN(<n>)" rather than a raw string.
                "last_state": _format_state_label(v["last_state_int"], state_map),
                "last_alert_time": v["last_alert_time"],
            }
            for k, v in counts.items()
        ],
        key=lambda x: x["alert_count"],
        reverse=True,
    )
    if cross_type_dropped:
        warnings = [
            *warnings,
            f"Ignored {cross_type_dropped} cross-type log row(s) not matching "
            f"{type_regex!r}; host/service state vocabularies were not mixed.",
        ]
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
    response also carries ``truncated_before`` (ISO-UTC timestamp of the
    *earliest* fetched event) and every bucket ending *before* the bucket that
    contains that event is marked as ``{"count": null, "truncated": true}`` so
    the consumer can distinguish "no alerts in this bucket" from "bucket not
    covered by the capped fetch".

    The fetch is issued newest-first (``sort="-time"``) so that when the cap is
    hit it is the *oldest* entries that are dropped, keeping the most recent
    buckets — the relevant ones for incident analytics — fully populated
    (issue #250).
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
        # Newest-first (issue #250): when the cap is hit, drop the *oldest*
        # entries so recent buckets stay populated. Bucket counting below is
        # order-independent, so DESC fetch order does not affect aggregation.
        "sort": "-time",
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

    # When the log cap is hit, sort=-time (newest first) means we got the
    # *most recent* entries only — buckets before the earliest fetched
    # timestamp would silently show count=0. Mark them as null+truncated so
    # consumers (LLM or human) do not confuse "missing data" with "quiet
    # period" (issue #250).
    log_capped = len(data) >= _NOISY_MAX_ALERTS
    if log_capped and raw_counts:
        first_ts = min(int(e["time"]) for e in data if e.get("time"))
        first_bucket = (first_ts // bucket_secs) * bucket_secs
        truncated_before_iso = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        payload["truncated_before"] = truncated_before_iso
        for bucket_obj in results:
            bs_str = bucket_obj["bucket_start"]
            bs_epoch = int(
                datetime.strptime(bs_str, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            if bs_epoch < first_bucket:
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
            "Buckets before 'truncated_before' are reported as count=null (data not fetched)."
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
            # Issue #245: friendly "UNKNOWN(<n>)" fallback instead of raw int.
            "last_state": _format_state_label(
                v["last_state_int"], HOST_STATES if not svc else SERVICE_STATES
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

HISTORY_TRENDS_REGISTRY: list[ToolSpec] = [
    # ---------------------------------------------------------------- noisy / flap
    ToolSpec(
        name="thruk_top_noisy_hosts",
        fn=thruk_top_noisy_hosts,
        schema=build_tool_schema(
            FIELDS_NOISY_HOSTS,
            filter=filter_schema_property(FIELDS_NOISY_HOSTS),
            since=_SINCE_WINDOW,
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
            since=_SINCE_WINDOW,
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
            since=_SINCE_WINDOW,
            until=_OPT_STR,
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
            filter=filter_schema_property(FIELDS_NOISY_SERVICES),
            since=_SINCE_WINDOW,
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
            since=_SINCE_WINDOW,
            until=_OPT_STR,
            min_alerts=_int(
                "Minimum number of non-recovery alert events to be included (default 5).",
                default=5,
            ),
            limit=_int("Maximum number of results (default 10).", default=10),
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
    "thruk_list_alerts",
    "thruk_list_logs",
    "thruk_list_notifications",
    "thruk_recent_events",
    "thruk_recurring_problems",
    "thruk_top_noisy_hosts",
    "thruk_top_noisy_services",
]
