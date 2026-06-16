"""Semantic triage / analytics tools (issue #259 — server.py split).

Parent: #256. This module hosts the five read-only "triage" tools that turn
raw monitoring state into actionable problem summaries:

* ``thruk_oldest_problems``       — unhandled problems sorted by age.
* ``thruk_unacked_critical``      — CRITICAL/DOWN unacked for > N minutes.
* ``thruk_stale_acks``            — acknowledgements older than N days.
* ``thruk_problem_counts``        — flat unhealthy-state aggregate.
* ``thruk_concurrent_failures``   — sliding-window concurrent-failure bursts.

plus the private projection helper ``_project_problem_counts`` (and its
``_HOST_PROBLEM_KEYS`` / ``_SVC_PROBLEM_KEYS`` key tuples).

The co-located ``TRIAGE_REGISTRY: list[ToolSpec]`` keeps each tool name,
implementation and explicit JSON Schema in one place; ``server.py`` splices
it into the global ``TOOL_REGISTRY`` (preserving registration order) and
re-exports every symbol here for backward compatibility.

Shared infrastructure (time parsing, log-family host resolution, state maps,
the noisy-cap warning suffix) lives in :mod:`thruk_mcp.helpers` /
:mod:`thruk_mcp.constants` so this module never imports ``server``. The
``_strip_filter_field`` helper is imported from
:mod:`thruk_mcp.tools.inventory` (inventory never imports triage, so there is
no cycle).
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from typing import Any

from ..constants import (
    _NOISY_CAP_HINT,
    _NOISY_MAX_ALERTS,
    HOST_STATE_STR,
    LATENCY_SANITY_CAP_SECONDS,
    SVC_STATE_STR,
)
from ..filters import (
    FIELDS_NOISY_HOSTS,
    FIELDS_OLDEST_PROBLEMS,
    FIELDS_PROBLEM_COUNTS,
    FIELDS_STALE_ACKS,
    FIELDS_STALE_CHECKS,
    FIELDS_UNACKED,
    FilterError,
    build_tool_schema,
    compile_filter,
    rewrite_custom_var_to_host_custom_var,
    validate_filter,
)
from ..helpers import (
    _RESOLVE_HOSTS_HARD_LIMIT,
    _backends,
    _decode_form_value,
    _duration_human,
    _epoch_filter_value,
    _get_client,
    _now_utc_epoch,
    _resolve_log_filter,
    _sanitize_latency,
    _tool_response,
    _ts,
)
from .base import (
    _BACKENDS,
    _UNTIL,
    ToolSpec,
    _bool,
    _int,
)
from .inventory import _strip_filter_field

# State maps — sourced from constants.py (single source of truth, issue #81).
# Local aliases preserve the original ``server.py`` names used in the function
# bodies below, with no behaviour change.
HOST_STATES: dict[int, str] = HOST_STATE_STR
SERVICE_STATES: dict[int, str] = SVC_STATE_STR


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
        # Issue #244: host-level custom_var must map to _HOST{VAR} on /services.
        svc_params.update(compile_filter(rewrite_custom_var_to_host_custom_var(filter), "services"))
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
        # Issue #244: host-level custom_var must map to _HOST{VAR} on /services.
        svc_params.update(compile_filter(rewrite_custom_var_to_host_custom_var(filter), "services"))
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
    filter: dict[str, Any] | None = None,
    backends: str | None = None,
) -> str:
    """Acknowledgements older than N days (potentially forgotten ones).

    Queries ``/comments`` for ``entry_type=4`` (acknowledgements) and
    returns entries whose ``entry_time`` is older than ``min_days`` days
    (default 7). Useful for identifying problems that have been silenced
    but never actually fixed.

    Returns ``[{host, service, ack_author, ack_comment, ack_since_days}]``
    sorted by age descending (stalest first).

    Optional ``filter`` is a structured AND/OR tree scoping the review to a
    given perimeter. Supported fields (see issue #228): ``hostgroup`` and
    ``custom_var``. Because Thruk's ``/comments`` endpoint does not accept
    hostgroup / custom-variable filters directly, the matching host set is
    resolved via a concurrent ``/hosts`` query (``groups[gte]=`` /
    ``_VARNAME=``) and applied as a host-name intersection on the comments
    rows.
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

    scope_hosts: set[str] | None = None
    be = _backends(backends)
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_STALE_ACKS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})
        host_params: dict[str, Any] = {"columns": "name,peer_name"}
        host_params.update(compile_filter(filter, "hosts"))
        # Resolve scope and fetch comments concurrently to avoid extra latency.
        hosts_resp, data = await asyncio.gather(
            _get_client().get("/hosts", params=host_params, backends=be),
            _get_client().get("/comments", params=params, backends=be),
        )
        scope_hosts = {h.get("name", "") for h in (hosts_resp or []) if h.get("name")}
    else:
        data = await _get_client().get("/comments", params=params, backends=be)

    rows: list[dict[str, Any]] = []
    for c in data or []:
        host_name = c.get("host_name", "")
        if scope_hosts is not None and host_name not in scope_hosts:
            continue
        et = int(c.get("entry_time") or 0)
        rows.append(
            {
                "host": host_name,
                "service": c.get("service_description") or None,
                "ack_author": _decode_form_value(c.get("author", "")),
                "ack_comment": _decode_form_value(c.get("comment", "")),
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
        # Issue #244: host-level custom_var must map to _HOST{VAR} on /services.
        svc_params = compile_filter(rewrite_custom_var_to_host_custom_var(filter), "services")
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
        extra["time[gte]"] = _epoch_filter_value(since)
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = _epoch_filter_value(until)

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        # Newest-first (issue #250): when the cap is hit, keep the *most recent*
        # DOWN events instead of the oldest, so recent concurrent-failure
        # bursts are not dropped. The sliding-window scan re-sorts events
        # ascending client-side below, so DESC fetch order is safe here.
        "sort": "-time",
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
# Stale-checks detector (issue #287)
# ---------------------------------------------------------------------------

#: Tight Livestatus column set for the /services stale sweep. ``check_interval``
#: is in MINUTES in Livestatus (issue #287, criterion 1) and is converted to
#: seconds before any age comparison; ``check_type`` is 0=active / 1=passive.
_STALE_SVC_COLUMNS: str = (
    "host_name,description,last_check,next_check,latency,execution_time,"
    "check_interval,active_checks_enabled,has_been_checked,check_type,peer_name"
)
#: Same column shape for the /hosts host-check sweep (``name`` instead of
#: ``host_name`` + ``description``).
_STALE_HOST_COLUMNS: str = (
    "name,last_check,next_check,latency,execution_time,"
    "check_interval,active_checks_enabled,has_been_checked,check_type,peer_name"
)
#: Hard cap on rows scanned per object type (defence against runaway result sets).
_STALE_MAX_LIMIT: int = 5000


def _classify_check(
    row: dict[str, Any],
    *,
    now: int,
    staleness_factor: float,
    latency_threshold_s: float,
    grace_seconds: int,
    passive_max_age_s: int,
    include_disabled: bool,
) -> dict[str, Any] | None:
    """Classify a single host/service check row as healthy or abnormal.

    Pure function (no I/O) so the full classification matrix is unit-testable in
    isolation. Returns ``None`` when the check looks healthy, otherwise a result
    dict carrying the ``reason`` and the supporting metrics.

    A host row is detected by the absence of a ``description`` column; service
    rows carry ``host_name`` + ``description``.

    Reason precedence (issue #287):
    ``never_checked`` > ``disabled`` > ``stale`` / ``stale_passive`` > ``high_latency``.

    - ``check_interval`` is in MINUTES in Livestatus → converted to seconds
      (criterion 1).
    - Passive checks (``check_type == 1``) use a freshness threshold
      (``passive_max_age_s``) rather than the active interval, and are labelled
      ``stale_passive`` (criterion 2). Their ``active_checks_enabled == 0`` is
      expected and is **not** reported as ``disabled`` (criterion 3).
    - ``has_been_checked == 0`` → ``never_checked`` (criterion 4).
    - An absolute ``grace_seconds`` is added on top of ``interval * factor`` to
      avoid boundary flapping (criterion 5).
    """
    is_host = "description" not in row
    host = row.get("host_name") or row.get("name") or ""
    service = None if is_host else row.get("description", "")
    is_passive = int(row.get("check_type") or 0) == 1
    active_enabled = bool(int(row.get("active_checks_enabled") or 0))
    has_been_checked = bool(int(row.get("has_been_checked") or 0))
    last_check = int(row.get("last_check") or 0)
    # latency may be None after _sanitize_latency nulled a spurious value.
    latency = float(row.get("latency") or 0.0)
    exec_time = float(row.get("execution_time") or 0.0)
    interval_s = int(float(row.get("check_interval") or 0.0) * 60)  # minutes → seconds
    age_s: int | None = (now - last_check) if last_check else None

    def _result(reason: str) -> dict[str, Any]:
        return {
            "host": host,
            "service": service,
            "reason": reason,
            "last_check": _ts(last_check),
            "last_check_age_s": age_s,
            "check_interval_s": interval_s,
            "latency_s": round(latency, 3),
            "execution_time_s": round(exec_time, 3),
            "active_checks_enabled": active_enabled,
            "check_type": "passive" if is_passive else "active",
        }

    # 1. Never checked — distinct from stale, applies to any check type.
    if not has_been_checked:
        return _result("never_checked")

    # 2. Passive checks: freshness, not interval. active_checks_enabled == 0 is
    #    normal for them, so it is NOT treated as a fault.
    if is_passive:
        if age_s is not None and age_s > passive_max_age_s + grace_seconds:
            return _result("stale_passive")
        if latency > latency_threshold_s:
            return _result("high_latency")
        return None

    # 3. Active checks.
    #    a. disabled (separate category — intentional, not necessarily a fault).
    if not active_enabled:
        return _result("disabled") if include_disabled else None
    #    b. stale: overdue vs interval * factor (+ grace). interval_s <= 0 means
    #       no meaningful schedule to compare against, so staleness is skipped.
    if (
        interval_s > 0
        and age_s is not None
        and age_s > interval_s * staleness_factor + grace_seconds
    ):
        return _result("stale")
    #    c. high latency (scheduler backlog).
    if latency > latency_threshold_s:
        return _result("high_latency")
    return None


async def thruk_stale_checks(
    filter: dict[str, Any] | None = None,
    staleness_factor: float = 2.0,
    latency_threshold_s: float = 30.0,
    grace_seconds: int = 60,
    passive_max_age_s: int = 3600,
    include_hosts: bool = True,
    include_disabled: bool = True,
    limit: int = 500,
    backends: str | None = None,
) -> str:
    """Surface checks that stopped running (the dangerous "false green").

    A check that stops executing keeps displaying its last state (usually OK),
    so monitoring goes blind while the dashboard looks healthy. This tool scans
    ``/services`` (and ``/hosts`` when ``include_hosts``) and flags every check
    whose *execution* is abnormal, classified by ``reason``:

    * ``stale``         — active check overdue:
      ``now - last_check > check_interval*60 * staleness_factor + grace_seconds``.
    * ``stale_passive`` — passive check (``check_type == 1``) whose last result is
      older than ``passive_max_age_s`` (+ grace). Passive checks have no
      meaningful active interval, so freshness is used instead.
    * ``never_checked`` — ``has_been_checked == 0`` (never executed yet).
    * ``disabled``      — active checks turned off (``active_checks_enabled == 0``),
      reported separately (intentional, not necessarily a fault). Passive checks
      are NOT flagged here — their active checks are off by design.
    * ``high_latency``  — scheduler backlog: ``latency > latency_threshold_s``.

    Contract / notes:
    - ``check_interval`` is in MINUTES in Livestatus and converted to seconds
      before comparison.
    - Clock source: the MCP host's UTC clock (``datetime.now(UTC)``), consistent
      with the other triage tools. If absolute ages look off, suspect clock skew
      between the MCP host and the monitoring core.
    - Spurious ``latency`` values (a Naemon/Livestatus bug surfacing a Unix
      timestamp, issue #202) are sanitized to ``null`` before classification so
      they cannot masquerade as high latency.

    Optional ``filter`` is a structured AND/OR tree scoping both ``/services``
    and ``/hosts``. Supported fields: ``hostgroup`` and ``custom_var`` (the host
    custom var is rewritten to ``_HOST{VAR}`` on the services side). ``backends``
    selects sites.

    Returns a wrapped object: ``now``, the effective thresholds, ``counts``
    (per-reason tally) and ``results`` (sorted by ``last_check_age_s``
    descending — stalest/never-checked first).
    """
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_STALE_CHECKS)
        except FilterError as exc:
            return _tool_response({"error": str(exc)})

    capped = max(1, min(limit, _STALE_MAX_LIMIT))
    svc_params: dict[str, Any] = {"columns": _STALE_SVC_COLUMNS, "limit": capped}
    host_params: dict[str, Any] = {"columns": _STALE_HOST_COLUMNS, "limit": capped}
    if filter is not None:
        # Issue #244: host-level custom_var must map to _HOST{VAR} on /services.
        svc_params.update(compile_filter(rewrite_custom_var_to_host_custom_var(filter), "services"))
        host_params.update(compile_filter(filter, "hosts"))

    be = _backends(backends)
    if include_hosts:
        services, hosts = await asyncio.gather(
            _get_client().get("/services", params=svc_params, backends=be),
            _get_client().get("/hosts", params=host_params, backends=be),
        )
    else:
        services = await _get_client().get("/services", params=svc_params, backends=be)
        hosts = []

    svc_rows: list[Any] = services if isinstance(services, list) else []
    host_rows: list[Any] = hosts if isinstance(hosts, list) else []
    svc_rows, svc_warns = _sanitize_latency(svc_rows, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
    host_rows, host_warns = _sanitize_latency(host_rows, cap_seconds=LATENCY_SANITY_CAP_SECONDS)
    warnings: list[str] = [*svc_warns, *host_warns]

    now = _now_utc_epoch()
    results: list[dict[str, Any]] = []
    for row in [*svc_rows, *host_rows]:
        if not isinstance(row, dict):
            continue
        res = _classify_check(
            row,
            now=now,
            staleness_factor=staleness_factor,
            latency_threshold_s=latency_threshold_s,
            grace_seconds=grace_seconds,
            passive_max_age_s=passive_max_age_s,
            include_disabled=include_disabled,
        )
        if res is not None:
            results.append(res)

    counts: Counter[str] = Counter(r["reason"] for r in results)
    # Never-checked rows have no age (last_check == 0) — float('inf') floats them
    # to the top alongside the longest-overdue checks.
    results.sort(
        key=lambda r: r["last_check_age_s"] if r["last_check_age_s"] is not None else float("inf"),
        reverse=True,
    )

    payload: dict[str, Any] = {
        "now": _ts(now),
        "staleness_factor": staleness_factor,
        "latency_threshold_s": latency_threshold_s,
        "grace_seconds": grace_seconds,
        "passive_max_age_s": passive_max_age_s,
        "include_hosts": include_hosts,
        "counts": dict(counts),
        "results": results,
    }
    if len(svc_rows) >= capped or (include_hosts and len(host_rows) >= capped):
        payload["_warning"] = (
            f"Result set reached the per-object cap ({capped}); detection may be "
            "incomplete. Narrow the scope with filter= or raise limit."
        )
    return _tool_response(payload, warnings or None)


# ---------------------------------------------------------------------------
# TRIAGE_REGISTRY: co-located tool specs (spliced into server.TOOL_REGISTRY)
# ---------------------------------------------------------------------------

TRIAGE_REGISTRY: list[ToolSpec] = [
    # -------------------------------------------------------- semantic problem tools (issue #52)
    ToolSpec(
        name="thruk_oldest_problems",
        fn=thruk_oldest_problems,
        schema=build_tool_schema(
            FIELDS_OLDEST_PROBLEMS,
            limit=_int("Maximum number of results (default 20).", default=20),
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
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_stale_acks",
        fn=thruk_stale_acks,
        schema=build_tool_schema(
            FIELDS_STALE_ACKS,
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
            backends=_BACKENDS,
        ),
    ),
    # -------------------------------------------------- concurrent failure detection (issue #54)
    ToolSpec(
        name="thruk_concurrent_failures",
        fn=thruk_concurrent_failures,
        schema=build_tool_schema(
            FIELDS_NOISY_HOSTS,
            since={
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": "-1h",
                "description": (
                    'Start of analysis window. Thruk relative time ("-2h", "-7d") '
                    'or ISO datetime ("2026-05-20 14:00:00"). Default: last 1 hour.'
                ),
            },
            until=_UNTIL,
            window_minutes=_int("Sliding window width in minutes.", default=5),
            min_hosts=_int(
                "Minimum number of distinct hosts failing in a window to be reported.",
                default=3,
            ),
            backends=_BACKENDS,
        ),
    ),
    # ----------------------------------------------------- stale-checks detector (issue #287)
    ToolSpec(
        name="thruk_stale_checks",
        fn=thruk_stale_checks,
        schema=build_tool_schema(
            FIELDS_STALE_CHECKS,
            staleness_factor={
                "type": "number",
                "default": 2.0,
                "description": (
                    "Multiplier on the active check_interval before a check is considered "
                    "stale (now - last_check > interval*factor + grace_seconds)."
                ),
            },
            latency_threshold_s={
                "type": "number",
                "default": 30.0,
                "description": (
                    "Latency in seconds above which a check is flagged with reason=high_latency."
                ),
            },
            grace_seconds=_int(
                "Absolute grace (seconds) added on top of interval*factor / passive_max_age_s "
                "to avoid boundary flapping (default 60).",
                default=60,
            ),
            passive_max_age_s=_int(
                "Max age in seconds of a passive check's last result before reason=stale_passive "
                "(default 3600).",
                default=3600,
            ),
            include_hosts=_bool(
                "Also run the same logic over /hosts host checks (default true).", default=True
            ),
            include_disabled=_bool(
                "Report active checks with active_checks_enabled=0 as reason=disabled "
                "(default true).",
                default=True,
            ),
            limit=_int(
                "Maximum number of rows scanned per object type (max 5000, default 500).",
                default=500,
            ),
            backends=_BACKENDS,
        ),
    ),
]


__all__ = [
    "TRIAGE_REGISTRY",
    "_classify_check",
    "_project_problem_counts",
    "thruk_concurrent_failures",
    "thruk_oldest_problems",
    "thruk_problem_counts",
    "thruk_stale_acks",
    "thruk_stale_checks",
    "thruk_unacked_critical",
]
