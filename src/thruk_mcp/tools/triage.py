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
import re
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
    _s,
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
# Root-cause analysis via parent/child topology (issue #322)
# ---------------------------------------------------------------------------

#: Tight Livestatus column set for the topology map fetch.
_ROOT_CAUSE_TOPO_COLUMNS: str = "name,parents,groups,state"
#: Default cap on hosts scanned when building the topology map.
_ROOT_CAUSE_TOPO_LIMIT: int = 5000


async def _fetch_host_alert_states(
    since: str | None,
    until: str | None,
    filter: dict[str, Any] | None,
    backends: str | None,
) -> dict[str, Any]:
    """Fetch HOST ALERT log rows over ``[since, until]`` split by state.

    Returns a dict with ``down`` / ``unreachable`` (sets of host names that hit
    state 1 / state 2 in the window — a host may appear in both),
    ``total_events``, ``capped`` (log cap reached), ``errs`` (filter errors),
    ``host_truncated`` and ``warnings``. Mirrors ``thruk_concurrent_failures``'s
    hardened ``/logs`` fetch: ``class=1`` defence-in-depth (issues #176/#193),
    ``state[gte]=1`` to drop recoveries, and ``sort=-time`` so a hit cap keeps
    the most recent events (issue #250).
    """
    extra, errs, host_truncated = await _resolve_log_filter(filter, FIELDS_NOISY_HOSTS, backends)
    if errs:
        return {
            "down": set(),
            "unreachable": set(),
            "total_events": 0,
            "capped": False,
            "errs": errs,
            "host_truncated": host_truncated,
            "warnings": [],
        }

    extra["type[~]"] = "^HOST ALERT"
    extra["class"] = "1"
    extra["state[gte]"] = "1"
    if "time[gte]" not in extra and since:
        extra["time[gte]"] = _epoch_filter_value(since)
    if "time[lte]" not in extra and until:
        extra["time[lte]"] = _epoch_filter_value(until)

    params: dict[str, Any] = {
        "limit": _NOISY_MAX_ALERTS,
        "sort": "-time",
        "columns": "host_name,state,time",
        **extra,
    }
    data, warnings = await _get_client().get_with_fallback(
        "/logs", params=params, backends=_backends(backends), method="POST"
    )
    if not isinstance(data, list):
        data = []

    down: set[str] = set()
    unreachable: set[str] = set()
    for e in data:
        host = e.get("host_name")
        if not host:
            continue
        try:
            st = int(e.get("state"))
        except (TypeError, ValueError):
            continue
        if st == 1:
            down.add(str(host))
        elif st == 2:
            unreachable.add(str(host))

    return {
        "down": down,
        "unreachable": unreachable,
        "total_events": len(data),
        "capped": len(data) >= _NOISY_MAX_ALERTS,
        "errs": [],
        "host_truncated": host_truncated,
        "warnings": warnings,
    }


async def _fetch_topology(
    backends: str | None, limit: int
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, int], bool]:
    """Fetch the host ``parents`` topology map → ``(parents, groups, states, truncated)``.

    Fetched **unfiltered** on purpose: the root cause of a storm is often a core
    device in a different hostgroup than its victims, so any caller-supplied
    filter must not restrict topology resolution. ``truncated`` is ``True`` when
    ``limit`` hosts were scanned (map may be incomplete).
    """
    parents: dict[str, list[str]] = {}
    groups: dict[str, list[str]] = {}
    states: dict[str, int] = {}
    count = 0
    async for row in _get_client().get_all(
        "/hosts",
        params={"columns": _ROOT_CAUSE_TOPO_COLUMNS},
        backends=_backends(backends),
        page_size=500,
        hard_limit=limit,
    ):
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not name:
            continue
        name = str(name)
        ps = row.get("parents")
        parents[name] = [str(p) for p in ps] if isinstance(ps, list) else []
        gs = row.get("groups")
        groups[name] = [str(g) for g in gs] if isinstance(gs, list) else []
        raw_state = row.get("state")
        try:
            states[name] = int(raw_state) if raw_state is not None else -1
        except (TypeError, ValueError):
            states[name] = -1
        count += 1
    return parents, groups, states, count >= limit


def _attribute_root_causes(
    down: set[str],
    unreachable: set[str],
    parents: dict[str, list[str]],
) -> tuple[dict[str, set[str]], list[str]]:
    """Attribute each affected host to its root-cause ancestor (pure function).

    Returns ``(clusters, unattributed)``. ``clusters`` maps each root-cause host
    — a DOWN host with no DOWN ancestor above it — to the set of affected hosts
    (including the root) attributed to it. ``unattributed`` lists UNREACHABLE
    hosts whose upward ``parents`` walk reaches no DOWN host (the cause fell
    outside the window or its parent is unmonitored), sorted.

    A host is attributed to the **topmost** DOWN ancestor on its parent chain
    (ties broken by sorted name). All walks are cycle-guarded against
    mis-configured parent loops.
    """
    affected = down | unreachable

    def _down_ancestors(host: str) -> set[str]:
        """Upward closure of ``host`` (inclusive) restricted to DOWN members."""
        out: set[str] = set()
        seen: set[str] = set()
        stack = [host]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in down:
                out.add(node)
            stack.extend(parents.get(node, []))
        return out

    has_down_above_cache: dict[str, bool] = {}

    def _has_down_above(host: str) -> bool:
        """True if a DOWN host sits strictly above ``host`` on the parent chain."""
        if host in has_down_above_cache:
            return has_down_above_cache[host]
        seen: set[str] = set()
        stack = list(parents.get(host, []))
        result = False
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in down:
                result = True
                break
            stack.extend(parents.get(node, []))
        has_down_above_cache[host] = result
        return result

    root_cache: dict[str, str | None] = {}

    def _root_of(host: str) -> str | None:
        if host in root_cache:
            return root_cache[host]
        anc = _down_ancestors(host)
        if not anc:
            root_cache[host] = None
            return None
        tops = sorted(d for d in anc if not _has_down_above(d))
        root = tops[0] if tops else sorted(anc)[0]
        root_cache[host] = root
        return root

    clusters: dict[str, set[str]] = {}
    unattributed: list[str] = []
    for host in affected:
        root = _root_of(host)
        if root is None:
            unattributed.append(host)
        else:
            clusters.setdefault(root, set()).add(host)
    return clusters, sorted(unattributed)


def _root_cause_confidence(impacted_count: int) -> str:
    """Confidence tier for a cluster: high (>=3 impacted), medium (2), low (1)."""
    if impacted_count >= 3:
        return "high"
    if impacted_count == 2:
        return "medium"
    return "low"


async def thruk_root_cause(
    since: str | None = "-1h",
    until: str | None = None,
    filter: dict[str, Any] | None = None,
    limit: int = _ROOT_CAUSE_TOPO_LIMIT,
    sample_limit: int = 50,
    backends: str | None = None,
) -> str:
    """Collapse a DOWN/UNREACHABLE storm into its root cause(s) via parent topology.

    During a mass outage the actionable signal is the common cause (the root
    network device), not the hundreds of UNREACHABLE victims. This tool:

    1. fetches ``HOST ALERT`` DOWN (state=1) / UNREACHABLE (state=2) log entries
       over ``[since, until]`` (Thruk relative ``"-2h"`` / ISO datetime; default
       last 1 hour),
    2. fetches the host ``parents`` topology (unfiltered — the root cause is
       often in a different hostgroup than its victims),
    3. walks each affected host up its ``parents`` chain to the topmost DOWN
       ancestor, and
    4. clusters victims under that root.

    ``filter`` (AND/OR tree, fields ``host`` / ``hostgroup`` / ``custom_var``)
    scopes only the *affected* set, never topology resolution. ``limit`` caps the
    topology scan (default 5000); ``sample_limit`` caps each per-cluster
    ``impacted_hosts`` sample (default 50).

    Returns ``{since, until, total_affected_hosts, down_count,
    unreachable_count, root_causes, unattributed_unreachable}``. Each entry of
    ``root_causes`` (sorted by ``impacted_count`` descending) carries
    ``{root_cause_host, root_cause_state, impacted_count, impacted_hosts,
    impacted_hosts_truncated, impacted_hostgroups, confidence}``. ``confidence``
    is ``high`` (>=3 impacted), ``medium`` (2) or ``low`` (1 — isolated failure).

    On a flat estate (no ``parents`` configured) every DOWN host is its own
    low-confidence root and UNREACHABLE hosts with no known DOWN ancestor land in
    ``unattributed_unreachable``.
    """
    res = await _fetch_host_alert_states(since, until, filter, backends)
    if res["errs"]:
        return _tool_response({"error": res["errs"][0]})
    down, unreachable = res["down"], res["unreachable"]
    affected = down | unreachable

    parents, groups, states, topo_truncated = await _fetch_topology(backends, max(1, limit))
    clusters, unattributed = _attribute_root_causes(down, unreachable, parents)

    root_causes: list[dict[str, Any]] = []
    for root, members in clusters.items():
        impacted = sorted(members)
        hgs = sorted({g for m in members for g in groups.get(m, [])})
        root_causes.append(
            {
                "root_cause_host": root,
                "root_cause_state": HOST_STATES.get(states.get(root, -1), "DOWN"),
                "impacted_count": len(members),
                "impacted_hosts": impacted[:sample_limit],
                "impacted_hosts_truncated": len(impacted) > sample_limit,
                "impacted_hostgroups": hgs,
                "confidence": _root_cause_confidence(len(members)),
            }
        )
    root_causes.sort(key=lambda r: (-r["impacted_count"], r["root_cause_host"]))

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "total_affected_hosts": len(affected),
        "down_count": len(down),
        "unreachable_count": len(unreachable),
        "root_causes": root_causes,
        "unattributed_unreachable": unattributed[:sample_limit],
    }
    if res["host_truncated"]:
        payload["_warning"] = (
            f"Host filter list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "affected set may be incomplete."
        )
    elif res["capped"]:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; analysis may be incomplete."
            + _NOISY_CAP_HINT
        )
    elif topo_truncated:
        payload["_warning"] = (
            f"Topology scan capped at {limit} hosts; parent resolution may be incomplete."
        )
    if res["warnings"]:
        payload["_warnings"] = res["warnings"]
    return _tool_response(payload)


async def thruk_unreachable_vs_down(
    since: str | None = "-1h",
    until: str | None = None,
    filter: dict[str, Any] | None = None,
    sample_limit: int = 100,
    backends: str | None = None,
) -> str:
    """Split a host outage window into DOWN (cause) vs UNREACHABLE (consequence).

    The lightweight companion to ``thruk_root_cause``: fetches ``HOST ALERT`` log
    entries over ``[since, until]`` and separates hosts that went DOWN (state=1 —
    the actual failures) from those that went UNREACHABLE (state=2 — downstream
    victims cut off by a DOWN parent). No topology walk.

    ``since`` / ``until`` accept Thruk relative times or ISO datetimes (default
    last 1 hour). ``filter`` (fields ``host`` / ``hostgroup`` / ``custom_var``)
    scopes the set; ``sample_limit`` caps each returned host list (default 100).

    Returns ``{since, until, down_count, unreachable_count, both_count,
    down_hosts, unreachable_hosts}`` (``both_count`` = hosts that hit both states
    in the window). Host lists are sorted and capped at ``sample_limit``.
    """
    res = await _fetch_host_alert_states(since, until, filter, backends)
    if res["errs"]:
        return _tool_response({"error": res["errs"][0]})
    down, unreachable = res["down"], res["unreachable"]
    both = down & unreachable

    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "down_count": len(down),
        "unreachable_count": len(unreachable),
        "both_count": len(both),
        "down_hosts": sorted(down)[:sample_limit],
        "unreachable_hosts": sorted(unreachable)[:sample_limit],
    }
    if res["host_truncated"]:
        payload["_warning"] = (
            f"Host filter list truncated at {_RESOLVE_HOSTS_HARD_LIMIT} entries; "
            "result may be incomplete."
        )
    elif res["capped"]:
        payload["_warning"] = (
            f"Result capped at {_NOISY_MAX_ALERTS} log entries; result may be incomplete."
            + _NOISY_CAP_HINT
        )
    if res["warnings"]:
        payload["_warnings"] = res["warnings"]
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
# thruk_worker_health — mod-gearman worker/queue supervision-artefact scan (#320)
# ---------------------------------------------------------------------------
# Thruk's REST API exposes no mod-gearman endpoint, and the Livestatus ``status``
# latency/queue columns come back null through LMD, so true queue depth / worker
# liveness is not available here. What *is* observable — and what distinguishes a
# real outage from a supervision blind spot — are the worker-failure signatures
# mod-gearman leaves in plugin output, which Thruk *can* filter server-side via
# the q-language (``plugin_output ~~ "<regex>"``). This tool scans hosts/services
# for those signatures, attributes them per backend and per gearman queue, and
# reports backend connectivity from /sites.

# Signature -> case-insensitive regex matched against plugin_output. The orphaned
# message names the gearman queue ("... worker on queue 'service' running?"), so
# ``_WORKER_QUEUE_RE`` extracts it. First match wins, in declared order.
_WORKER_SIGNATURES: dict[str, re.Pattern[str]] = {
    "orphaned": re.compile(r"orphaned", re.IGNORECASE),
    "worker_timeout": re.compile(r"Timed Out On Worker", re.IGNORECASE),
    "address_undef": re.compile(r"Invalid hostname/address\s*-\s*undef", re.IGNORECASE),
}
_WORKER_QUEUE_RE = re.compile(r"queue '([^']+)'", re.IGNORECASE)

# Combined alternation handed to Thruk as a single quoted q-language regex so the
# candidate set is filtered server-side (one /services + one /hosts fetch). The
# q-parser treats a bare "(" as logical grouping and splits on unquoted
# whitespace, so the value MUST be double-quoted and MUST NOT wrap the
# alternation in parentheses.
_WORKER_Q_REGEX = "orphaned|Timed Out On Worker|Invalid hostname/address - undef"

_WORKER_MAX_LIMIT = 5000
_WORKER_SVC_COLUMNS = "host_name,description,plugin_output,peer_name,state,last_check"
_WORKER_HOST_COLUMNS = "name,plugin_output,peer_name,state,last_check"


def _classify_worker_artefact(plugin_output: str) -> tuple[str | None, str | None]:
    """Classify a plugin-output line as a mod-gearman worker artefact.

    Returns ``(signature, queue)`` where ``signature`` is one of the
    ``_WORKER_SIGNATURES`` keys (or ``None`` when nothing matched) and ``queue``
    is the gearman queue name parsed from an orphaned message (or ``None``). The
    first matching signature wins, in ``_WORKER_SIGNATURES`` declaration order.
    """
    text = plugin_output or ""
    for signature, pattern in _WORKER_SIGNATURES.items():
        if pattern.search(text):
            m = _WORKER_QUEUE_RE.search(text)
            return signature, (m.group(1) if m else None)
    return None, None


async def thruk_worker_health(
    include_hosts: bool = True,
    include_services: bool = True,
    limit: int = 500,
    sample_limit: int = 20,
    backends: str | None = None,
) -> str:
    """Distinguish a real outage from a mod-gearman supervision blind spot.

    During a large incident a share of the DOWN/CRITICAL states are **artefacts
    of the check-execution layer** (mod-gearman), not real failures: saturated
    queues, dead workers, orphaned checks, ``undef`` addresses. Thruk's REST API
    exposes **no** mod-gearman endpoint and the Livestatus ``status`` latency /
    queue columns return null through LMD, so true queue depth and worker
    liveness are **not** available here — use ``gearman_top`` / ``gearadmin
    --status`` on the host for those.

    What this tool *can* do over REST is surface the worker-failure **signatures**
    mod-gearman leaves in plugin output and attribute them per backend and per
    gearman queue. It scans ``/services`` (and ``/hosts`` when ``include_hosts``)
    with a server-side ``plugin_output`` regex filter, classifying each match:

    * ``orphaned``       — ``(... check orphaned, is the mod-gearman worker on
      queue 'X' running?)`` — the queue name ``X`` is extracted into ``by_queue``.
    * ``worker_timeout`` — ``... Timed Out On Worker ...``.
    * ``address_undef``  — ``Invalid hostname/address - undef`` (worker handed an
      unresolved target).

    It also reads ``/sites`` for backend connectivity — a disconnected backend
    means its whole worker layer is a blind spot regardless of artefact counts.

    ``limit`` caps rows scanned per object type (max 5000); ``sample_limit`` caps
    the returned ``samples``. ``backends`` selects sites.

    Returns ``now``, ``patterns`` (the signatures used), ``total_artefacts``,
    ``artefact_counts`` (per signature), ``by_queue``, ``by_backend``,
    ``backends`` (connectivity), ``samples`` and a factual ``assessment``. A
    ``_warning`` is added when a per-object cap is reached (detection may be
    incomplete).
    """
    now = _now_utc_epoch()
    capped = max(1, min(limit, _WORKER_MAX_LIMIT))
    q_expr = f'plugin_output ~~ "{_WORKER_Q_REGEX}"'
    svc_params: dict[str, Any] = {"q": q_expr, "columns": _WORKER_SVC_COLUMNS, "limit": capped}
    host_params: dict[str, Any] = {"q": q_expr, "columns": _WORKER_HOST_COLUMNS, "limit": capped}
    be = _backends(backends)

    sites, services, hosts = await asyncio.gather(
        _get_client().get("/sites", backends=be),
        _get_client().get("/services", params=svc_params, backends=be)
        if include_services
        else asyncio.sleep(0, result=[]),
        _get_client().get("/hosts", params=host_params, backends=be)
        if include_hosts
        else asyncio.sleep(0, result=[]),
    )

    sites_rows: list[Any] = sites if isinstance(sites, list) else []
    svc_rows: list[Any] = services if isinstance(services, list) else []
    host_rows: list[Any] = hosts if isinstance(hosts, list) else []

    # Backend connectivity from /sites: connected==1 and status==0 means healthy.
    connected_n = 0
    disconnected: list[dict[str, Any]] = []
    for s in sites_rows:
        if not isinstance(s, dict):
            continue
        if int(s.get("connected") or 0) == 1 and int(s.get("status") or 0) == 0:
            connected_n += 1
        else:
            disconnected.append({"name": s.get("name", ""), "last_error": s.get("last_error", "")})

    artefact_counts: Counter[str] = Counter()
    by_queue: Counter[str] = Counter()
    by_backend: dict[str, Counter[str]] = {}
    samples: list[dict[str, Any]] = []

    def _record(
        obj_type: str,
        host: str,
        service: str | None,
        backend: str,
        sig: str,
        queue: str | None,
        output: str,
    ) -> None:
        artefact_counts[sig] += 1
        if queue:
            by_queue[queue] += 1
        by_backend.setdefault(backend or "(unknown)", Counter())[sig] += 1
        if len(samples) < sample_limit:
            sample: dict[str, Any] = {
                "object_type": obj_type,
                "host": host,
                "backend": backend,
                "signature": sig,
                "plugin_output": output,
            }
            if service is not None:
                sample["service"] = service
            if queue:
                sample["queue"] = queue
            samples.append(sample)

    for row in svc_rows:
        if not isinstance(row, dict):
            continue
        output = row.get("plugin_output", "")
        sig, queue = _classify_worker_artefact(output)
        if sig is None:
            continue
        _record(
            "service",
            row.get("host_name", ""),
            row.get("description", ""),
            row.get("peer_name", ""),
            sig,
            queue,
            output,
        )

    for row in host_rows:
        if not isinstance(row, dict):
            continue
        output = row.get("plugin_output", "")
        sig, queue = _classify_worker_artefact(output)
        if sig is None:
            continue
        _record("host", row.get("name", ""), None, row.get("peer_name", ""), sig, queue, output)

    total = sum(artefact_counts.values())
    if total == 0 and not disconnected:
        assessment = "No mod-gearman worker artefacts detected and all backends connected."
    else:
        parts: list[str] = []
        if total:
            parts.append(
                f"{total} worker artefact(s) across {len(by_queue)} queue(s) — these "
                "non-OK states are supervision artefacts (dead/saturated workers, "
                "orphaned checks, unresolved addresses), not necessarily real outages."
            )
        if disconnected:
            parts.append(
                f"{len(disconnected)} backend(s) disconnected — their worker layer is a blind spot."
            )
        assessment = " ".join(parts)

    payload: dict[str, Any] = {
        "now": _ts(now),
        "patterns": {k: v.pattern for k, v in _WORKER_SIGNATURES.items()},
        "total_artefacts": total,
        "artefact_counts": dict(artefact_counts),
        "by_queue": dict(by_queue.most_common()),
        "by_backend": {k: dict(v) for k, v in sorted(by_backend.items())},
        "backends": {
            "connected": connected_n,
            "disconnected": len(disconnected),
            "disconnected_sites": disconnected,
        },
        "samples": samples,
        "assessment": assessment,
    }
    svc_capped = include_services and len(svc_rows) >= capped
    host_capped = include_hosts and len(host_rows) >= capped
    if svc_capped or host_capped:
        payload["_warning"] = (
            f"Result set reached the per-object cap ({capped}); detection may be "
            "incomplete. Narrow with backends= or raise limit."
        )
    return _tool_response(payload)


# ---------------------------------------------------------------------------
# Backend health (issue #323) — Livestatus latency / replication lag per site
# ---------------------------------------------------------------------------
# /sites is the mandatory connectivity baseline (name/connected/status/
# last_error). The richer per-peer metrics live in two *optional* endpoints
# that not every Thruk deployment exposes: /lmd/sites (LMD only — response_time
# latency, last_online/last_update freshness, queries, bytes) and /processinfo
# (program_start, accept_passive_*_checks, cached). The tool merges whichever
# answered, indexing peers by every identity key Thruk might emit.
_BACKEND_PEER_KEYS: tuple[str, ...] = ("peer_key", "key", "id", "name", "peer_name")


def _backend_num(value: Any) -> float | None:
    """Coerce a Livestatus numeric field to float, or None when absent/garbage."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _backend_bool(value: Any) -> bool | None:
    """Coerce a Naemon 0/1 flag (int or string) to bool, or None when absent."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return None


def _index_peer_rows(rows: Any) -> dict[str, dict[str, Any]]:
    """Index /lmd/sites or /processinfo rows by every identity key they expose.

    Thruk emits the peer identity under different keys across builds/endpoints
    (``peer_key``/``key``/``id`` and ``name``/``peer_name``); indexing under all
    of them lets a /sites row be matched by whichever the backend populated. A
    bare dict (single-backend /processinfo) is treated as one row.
    """
    index: dict[str, dict[str, Any]] = {}
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return index
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in _BACKEND_PEER_KEYS:
            val = row.get(key)
            if val:
                index.setdefault(str(val), row)
    return index


def _lookup_peer(site: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Find the lmd/processinfo row for a /sites entry by any shared identity key."""
    for key in ("id", "peer_key", "key", "name", "peer_name"):
        val = site.get(key)
        if val and str(val) in index:
            return index[str(val)]
    return {}


def _backend_site_report(
    site: dict[str, Any],
    lmd: dict[str, Any],
    proc: dict[str, Any],
    *,
    now: int,
    latency_warn: float,
    lag_warn: int,
    latency_cap: float,
) -> dict[str, Any]:
    """Merge the three per-peer rows into one health report (pure / testable)."""
    name = site.get("name") or site.get("peer_name") or site.get("id") or "<unknown>"
    connected = _backend_bool(site.get("connected"))
    status = int(_backend_num(site.get("status")) or 0) if site.get("status") is not None else None
    last_error = str(site.get("last_error") or lmd.get("last_error") or "").strip()

    report: dict[str, Any] = {
        "name": name,
        "id": site.get("id") or site.get("peer_key"),
        "section": site.get("section"),
        "type": site.get("type"),
        "addr": site.get("addr"),
        "connected": bool(connected),
        "status": status,
    }
    reasons: list[str] = []

    # Disconnected = the blind spot the issue is about: not connected, or the
    # peer reports a non-OK Livestatus status.
    if connected is not True or (status is not None and status != 0):
        report["health"] = "disconnected"
        report["reasons"] = ["backend disconnected"]
        if last_error:
            report["last_error"] = last_error
        return report

    # --- connected: enrich with latency / freshness when the optional endpoints
    #     answered, and decide ok vs degraded.
    latency = _backend_num(lmd.get("response_time"))
    if latency is not None and 0 <= latency <= latency_cap:
        report["latency_seconds"] = round(latency, 3)
        if latency > latency_warn:
            reasons.append(f"latency {report['latency_seconds']}s > {latency_warn}s")

    last_online = _backend_num(lmd.get("last_online"))
    if last_online:
        report["last_online"] = int(last_online)
        report["last_online_human"] = _ts(int(last_online))

    # Replication / cache lag: how stale the served data is, measured against the
    # backend host's own clock (localtime) when present to dodge clock skew.
    ref = _backend_num(site.get("localtime")) or float(now)
    fresh_src = _backend_num(lmd.get("last_update")) or last_online
    if fresh_src:
        age = max(0, int(ref - fresh_src))
        report["data_age_seconds"] = age
        report["data_age_human"] = _duration_human(age)
        if age > lag_warn:
            reasons.append(f"data age {report['data_age_human']} > {lag_warn}s (stale cache / lag)")

    for k in ("queries", "bytes_send", "bytes_received"):
        v = _backend_num(lmd.get(k))
        if v is not None:
            report[k] = int(v)
    idling = _backend_bool(lmd.get("idling"))
    if idling is not None:
        report["idling"] = idling

    program_start = _backend_num(proc.get("program_start"))
    if program_start:
        report["program_start"] = int(program_start)
        report["uptime_human"] = _duration_human(max(0, now - int(program_start)))
    if proc.get("program_version"):
        report["program_version"] = proc.get("program_version")
    cached = _backend_bool(proc.get("cached"))
    if cached is not None:
        report["cached"] = cached
    for flag, label in (
        ("accept_passive_host_checks", "passive host checks disabled"),
        ("accept_passive_service_checks", "passive service checks disabled"),
    ):
        accepts = _backend_bool(proc.get(flag))
        if accepts is not None:
            report[flag] = accepts
            if accepts is False:
                reasons.append(label)

    if last_error:
        report["last_error"] = last_error
        reasons.append(f"backend reports error: {last_error}")

    report["health"] = "degraded" if reasons else "ok"
    report["reasons"] = reasons
    return report


_BACKEND_HEALTH_RANK = {"disconnected": 0, "degraded": 1, "ok": 2}


async def thruk_backend_health(
    latency_warn_seconds: float = 5.0,
    lag_warn_seconds: int = 120,
    backends: str | None = None,
) -> str:
    """Per-site supervision-backend health: latency, replication lag, blind spots.

    ``thruk_sites`` only reports connected/disconnected. During an incident that
    is not enough: a **muted or lagging collector** turns its whole perimeter
    into a green-looking blind spot, so you must distinguish "the estate is down"
    from "the supervision backend is blind/late". This tool enriches the /sites
    baseline with per-peer Livestatus **latency** and data **freshness**.

    It merges three endpoints (concurrently, degrading gracefully — an optional
    one that errors never sinks the call):

    * ``/sites`` — mandatory: ``connected``, ``status``, ``last_error``, ``addr``.
    * ``/lmd/sites`` — *optional* (LMD only): ``response_time`` (latency),
      ``last_online`` / ``last_update`` (freshness), ``queries``, byte counters.
    * ``/processinfo`` — *optional*: ``program_start`` (uptime),
      ``accept_passive_*_checks``, ``cached``.

    Each site is classified ``disconnected`` (not connected / non-OK status — a
    blind spot, carries ``last_error``), ``degraded`` (connected but latency
    ``> latency_warn_seconds``, data age ``> lag_warn_seconds``, passive checks
    off, or a non-empty error), or ``ok``. Returns ``{now, summary, sites
    (worst-first), degraded_sites, disconnected_sites, lmd_available,
    processinfo_available, assessment}``. When neither optional endpoint answers
    the report is connectivity-only (latency/lag unavailable) and says so.
    """
    now = _now_utc_epoch()
    be = _backends(backends)
    # Index (rather than tuple-unpack) the gather result: unpacking its overloaded
    # return under return_exceptions trips mypy [has-type].
    results = await asyncio.gather(
        _get_client().get("/sites", backends=be),
        _get_client().get("/lmd/sites", backends=be),
        _get_client().get("/processinfo", backends=be),
        return_exceptions=True,
    )
    sites_res = results[0]
    lmd_res = results[1]
    proc_res = results[2]
    # /sites is mandatory — surface its ThrukError verbatim (per conventions).
    if isinstance(sites_res, BaseException):
        raise sites_res

    sites_rows = sites_res if isinstance(sites_res, list) else []
    lmd_available = not isinstance(lmd_res, BaseException)
    processinfo_available = not isinstance(proc_res, BaseException)
    lmd_index = _index_peer_rows(lmd_res) if lmd_available else {}
    proc_index = _index_peer_rows(proc_res) if processinfo_available else {}

    reports: list[dict[str, Any]] = []
    for site in sites_rows:
        if not isinstance(site, dict):
            continue
        reports.append(
            _backend_site_report(
                site,
                _lookup_peer(site, lmd_index),
                _lookup_peer(site, proc_index),
                now=now,
                latency_warn=latency_warn_seconds,
                lag_warn=lag_warn_seconds,
                latency_cap=LATENCY_SANITY_CAP_SECONDS,
            )
        )

    reports.sort(key=lambda r: (_BACKEND_HEALTH_RANK.get(r["health"], 3), str(r["name"])))
    ok_n = sum(1 for r in reports if r["health"] == "ok")
    degraded = [r["name"] for r in reports if r["health"] == "degraded"]
    disconnected = [
        {"name": r["name"], "last_error": r.get("last_error", "")}
        for r in reports
        if r["health"] == "disconnected"
    ]

    total = len(reports)
    if total == 0:
        assessment = "No backends configured."
    elif not degraded and not disconnected:
        assessment = f"All {total} backend(s) connected and healthy."
    else:
        parts = [f"{len(degraded) + len(disconnected)}/{total} backend(s) unhealthy"]
        if disconnected:
            parts.append(f"{len(disconnected)} disconnected (blind spot)")
        if degraded:
            parts.append(f"{len(degraded)} degraded (latency/lag)")
        assessment = (
            ": ".join([parts[0], ", ".join(parts[1:])])
            + " — analyses over their perimeter may be incomplete."
        )
    if not lmd_available and not processinfo_available:
        assessment += (
            " Latency / replication-lag metrics unavailable (no LMD and "
            "/processinfo did not answer); report is connectivity-only."
        )

    payload: dict[str, Any] = {
        "now": _ts(now),
        "summary": {
            "total": total,
            "ok": ok_n,
            "degraded": len(degraded),
            "disconnected": len(disconnected),
        },
        "sites": reports,
        "degraded_sites": degraded,
        "disconnected_sites": disconnected,
        "lmd_available": lmd_available,
        "processinfo_available": processinfo_available,
        "assessment": assessment,
    }
    return _tool_response(payload)


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
    # --------------------------------------------- mod-gearman worker health (issue #320)
    ToolSpec(
        name="thruk_worker_health",
        fn=thruk_worker_health,
        schema=_s(
            include_hosts=_bool(
                "Also scan /hosts host checks for worker artefacts (default true).",
                default=True,
            ),
            include_services=_bool(
                "Scan /services for worker artefacts (default true).", default=True
            ),
            limit=_int(
                "Maximum rows scanned per object type (max 5000, default 500).", default=500
            ),
            sample_limit=_int(
                "Maximum example rows returned in 'samples' (default 20).", default=20
            ),
            backends=_BACKENDS,
        ),
    ),
    # ----------------------------------------- root-cause topology analysis (issue #322)
    ToolSpec(
        name="thruk_root_cause",
        fn=thruk_root_cause,
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
            limit=_int(
                "Maximum hosts scanned when building the parent topology map (default 5000).",
                default=_ROOT_CAUSE_TOPO_LIMIT,
            ),
            sample_limit=_int(
                "Maximum hosts listed per cluster in 'impacted_hosts' (default 50).",
                default=50,
            ),
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_unreachable_vs_down",
        fn=thruk_unreachable_vs_down,
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
            sample_limit=_int(
                "Maximum hosts listed in each returned host array (default 100).",
                default=100,
            ),
            backends=_BACKENDS,
        ),
    ),
    # ------------------------------------------ backend health / blind spots (issue #323)
    ToolSpec(
        name="thruk_backend_health",
        fn=thruk_backend_health,
        schema=_s(
            latency_warn_seconds={
                "type": "number",
                "default": 5.0,
                "description": (
                    "Livestatus response time (seconds) above which a connected "
                    "backend is flagged 'degraded' (default 5.0)."
                ),
            },
            lag_warn_seconds=_int(
                "Data-freshness age (seconds) above which a connected backend is "
                "flagged 'degraded' for stale cache / replication lag (default 120).",
                default=120,
            ),
            backends=_BACKENDS,
        ),
    ),
]


__all__ = [
    "TRIAGE_REGISTRY",
    "_attribute_root_causes",
    "_backend_site_report",
    "_classify_check",
    "_classify_worker_artefact",
    "_index_peer_rows",
    "_project_problem_counts",
    "thruk_backend_health",
    "thruk_concurrent_failures",
    "thruk_oldest_problems",
    "thruk_problem_counts",
    "thruk_root_cause",
    "thruk_stale_acks",
    "thruk_stale_checks",
    "thruk_unacked_critical",
    "thruk_unreachable_vs_down",
    "thruk_worker_health",
]
