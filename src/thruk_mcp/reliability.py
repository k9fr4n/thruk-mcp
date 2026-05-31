"""Log-to-incident reducer: MTTR / MTBF / incident metrics (issue #286).

Turns raw Naemon/Thruk ``/logs`` rows into incident-level reliability metrics.
An uptime *percentage* (``*_availability``) cannot tell a service that crashed
14 times with a 38 min MTTR apart from one that had a single 11 h outage at the
same 99.2 %; this module recovers the missing dimension by reducing HARD state
transitions into discrete incidents and aggregating them.

Acceptance criteria (issue #286), all handled here:

1. **HARD states only.** SOFT rows are check-retry noise and are dropped.
2. **Only HOST/SERVICE ALERT rows.** ``* DOWNTIME ALERT`` / ``* FLAPPING
   ALERT`` / notifications are ignored.
3. **State numbering.** ``state == 0`` is the recovery for both object types
   (UP / OK); any non-zero HARD state is a problem (host 1=DOWN/2=UNREACHABLE,
   service 1=WARN/2=CRIT/3=UNKNOWN).
4. **Incident = first HARD non-OK -> next HARD OK.** Consecutive non-OK HARD
   states collapse into a single incident (WARN->CRIT is *not* a new incident).
5. **Ongoing incidents** (no recovery before ``window_end``) are flagged
   ``ongoing``, excluded from MTTR, but counted in ``incidents`` and
   ``total_downtime_seconds`` (clamped at ``window_end``).
6. **Window clamping.** A leading HARD recovery with no in-window problem-start
   implies an incident that began before ``window_start``; its downtime is
   clamped to ``window_start`` rather than dropped.
7. **Empty / single-event safety.** No incidents -> zeros (never an error);
   MTBF is ``None`` for fewer than 2 incidents.

The module is intentionally dependency-free (stdlib only) and pure/synchronous
-- all I/O and human-readable formatting live in the tool layer
(:mod:`thruk_mcp.tools.history`).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "extract_incidents",
    "summarize_reliability",
]

#: Log ``type`` values that represent a genuine state transition. Everything
#: else (``HOST DOWNTIME ALERT``, ``SERVICE FLAPPING ALERT``, notifications, ...)
#: is ignored (criterion 2).
_ALERT_TYPES: frozenset[str] = frozenset({"HOST ALERT", "SERVICE ALERT"})


def _is_hard(state_type: Any) -> bool:
    """Return ``True`` when a log row's ``state_type`` denotes a HARD state.

    Naemon writes ``"HARD"`` / ``"SOFT"`` strings in the ``/logs`` ``state_type``
    column, but some exports surface the numeric form (``1`` = HARD, ``0`` =
    SOFT). Both are accepted; anything else is treated as SOFT (criterion 1).
    """
    if isinstance(state_type, str):
        return state_type.strip().upper() == "HARD"
    if isinstance(state_type, bool):
        return False  # avoid True == 1 surprises; bools are never valid here
    if isinstance(state_type, int):
        return state_type == 1
    return False


def _coerce_int(value: Any) -> int | None:
    """Best-effort int coercion; returns ``None`` when not coercible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _relevant_rows(entries: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Filter to HARD HOST/SERVICE ALERT rows, returning ``(time, state)`` tuples.

    Rows missing a parseable ``time`` or ``state``, non-HARD rows, and
    non-alert rows are dropped. The result is sorted chronologically so the
    incident state machine can run in a single forward pass.
    """
    rows: list[tuple[int, int]] = []
    for entry in entries:
        if str(entry.get("type", "")).strip().upper() not in _ALERT_TYPES:
            continue
        if not _is_hard(entry.get("state_type")):
            continue
        t = _coerce_int(entry.get("time"))
        state = _coerce_int(entry.get("state"))
        if t is None or state is None:
            continue
        rows.append((t, state))
    rows.sort(key=lambda r: r[0])
    return rows


def extract_incidents(
    entries: list[dict[str, Any]],
    *,
    window_start: int | None,
    window_end: int,
) -> list[dict[str, Any]]:
    """Reduce one object's log rows into a list of incidents.

    *entries* are raw ``/logs`` rows (each a dict with ``time``, ``state``,
    ``state_type``, ``type``) for a **single** host or service. They need not be
    pre-sorted or pre-filtered -- :func:`_relevant_rows` keeps only HARD
    HOST/SERVICE ALERT rows in chronological order.

    Each returned incident is::

        {"start": int, "end": int | None, "ongoing": bool, "duration_seconds": int}

    *window_end* (now, or the resolved ``until``) clamps ongoing incidents.
    *window_start* (the resolved ``since``) clamps incidents that began before
    the window -- detected via a leading HARD recovery with no preceding
    in-window problem-start (criterion 6). When *window_start* is ``None`` the
    pre-window incident cannot be dated and is skipped.
    """
    incidents: list[dict[str, Any]] = []
    in_incident = False
    start: int | None = None
    first = True

    for t, state in _relevant_rows(entries):
        if state != 0:  # problem (criterion 3)
            if not in_incident:
                in_incident = True
                start = t
            # else: already inside an incident -> collapse (criterion 4)
        else:  # HARD recovery (state == 0)
            if in_incident and start is not None:
                incidents.append(_incident(start, t, window_end))
                in_incident = False
                start = None
            elif first and window_start is not None:
                # Leading recovery, no in-window problem-start: the incident
                # began before the window -> clamp to window_start (criterion 6).
                incidents.append(_incident(window_start, t, window_end))
        first = False

    if in_incident and start is not None:  # unrecovered -> ongoing (criterion 5)
        incidents.append(_incident(start, None, window_end))

    return incidents


def _incident(start: int, end: int | None, window_end: int) -> dict[str, Any]:
    """Build a single incident dict, clamping an ongoing one at *window_end*."""
    if end is None:
        return {
            "start": start,
            "end": None,
            "ongoing": True,
            "duration_seconds": max(0, window_end - start),
        }
    return {
        "start": start,
        "end": end,
        "ongoing": False,
        "duration_seconds": max(0, end - start),
    }


def summarize_reliability(
    entries: list[dict[str, Any]],
    *,
    window_start: int | None,
    window_end: int,
) -> dict[str, Any]:
    """Aggregate one object's incidents into reliability metrics (pure seconds).

    Returns a dict of integer-second metrics (the tool layer adds
    human-readable strings and the host/service identity):

    * ``incidents`` -- total incident count (recovered + ongoing).
    * ``mttr_seconds`` -- mean time to recovery over **recovered** incidents
      only (ongoing excluded, criterion 5); ``None`` when none recovered.
    * ``mtbf_seconds`` -- mean gap between consecutive incident *starts*;
      ``None`` for fewer than 2 incidents (criterion 7).
    * ``total_downtime_seconds`` -- sum of all incident durations (ongoing
      clamped at ``window_end``, pre-window clamped at ``window_start``).
    * ``longest_incident_seconds`` -- worst single incident duration.
    * ``ongoing`` -- ``True`` when the object is currently in an unrecovered
      incident.

    No incidents -> all-zero metrics with ``mttr``/``mtbf`` ``None`` (never an
    error, criterion 7).
    """
    incidents = extract_incidents(entries, window_start=window_start, window_end=window_end)
    count = len(incidents)
    recovered = [inc for inc in incidents if not inc["ongoing"]]

    mttr_seconds: int | None = None
    if recovered:
        mttr_seconds = round(sum(inc["duration_seconds"] for inc in recovered) / len(recovered))

    mtbf_seconds: int | None = None
    if count >= 2:
        starts = sorted(inc["start"] for inc in incidents)
        gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        mtbf_seconds = round(sum(gaps) / len(gaps))

    return {
        "incidents": count,
        "mttr_seconds": mttr_seconds,
        "mtbf_seconds": mtbf_seconds,
        "total_downtime_seconds": sum(inc["duration_seconds"] for inc in incidents),
        "longest_incident_seconds": max((inc["duration_seconds"] for inc in incidents), default=0),
        "ongoing": any(inc["ongoing"] for inc in incidents),
    }
