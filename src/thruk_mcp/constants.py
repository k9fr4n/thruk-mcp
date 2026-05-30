"""Shared monitoring constants: state maps used across server.py and filters.py.

Centralising these here avoids silent divergence when a new Naemon state is
added: a single edit to this file propagates everywhere.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Host state maps
# ---------------------------------------------------------------------------

#: int -> human-readable label  (e.g. 0 -> "UP")
HOST_STATE_STR: dict[int, str] = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}

#: string / numeric-string -> int  (e.g. "down" -> 1, "1" -> 1)
HOST_STATE_INT: dict[str, int] = {
    "up": 0,
    "down": 1,
    "unreachable": 2,
    "0": 0,
    "1": 1,
    "2": 2,
}

# ---------------------------------------------------------------------------
# Service state maps
# ---------------------------------------------------------------------------

#: int -> human-readable label  (e.g. 2 -> "CRITICAL")
SVC_STATE_STR: dict[int, str] = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}

#: string / numeric-string -> int  (e.g. "critical" -> 2, "2" -> 2)
SVC_STATE_INT: dict[str, int] = {
    "ok": 0,
    "warning": 1,
    "critical": 2,
    "unknown": 3,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
}

# ---------------------------------------------------------------------------
# Default columns for list endpoints
# ---------------------------------------------------------------------------
# Tight by design to minimise LLM token usage.  A typical Thruk host row has
# ~80 attributes; returning all would blow the context for no reason.  Callers
# can always override via the ``columns`` argument or use ``thruk_query``.

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
DEFAULT_CONTACT_COLUMNS = (
    "name,alias,email,pager,host_notifications_enabled,service_notifications_enabled"
)

# ---------------------------------------------------------------------------
# Analysis constants
# ---------------------------------------------------------------------------

# Maximum number of raw log entries fetched for noisy-* aggregation queries.
# Beyond this cap the aggregation may be incomplete; a _warning key is added.
#
# Operators can override the cap via the THRUK_NOISY_MAX_ALERTS env var (e.g.
# large infrastructures with > 10 000 alert events in their typical analysis
# window).  A defensive lower bound is enforced so an operator typo (e.g. "0"
# or "5") cannot silently defeat aggregation entirely.

#: Lower bound for the noisy-aggregation cap.  Anything below this would
#: produce useless rankings; we coerce to this minimum and keep going rather
#: than crash the server.
_NOISY_MAX_ALERTS_MIN: int = 100

#: Default value when THRUK_NOISY_MAX_ALERTS is unset / invalid.
_NOISY_MAX_ALERTS_DEFAULT: int = 10_000


def _load_noisy_max_alerts(
    raw: str | None = None,
    *,
    default: int = _NOISY_MAX_ALERTS_DEFAULT,
    minimum: int = _NOISY_MAX_ALERTS_MIN,
) -> int:
    """Resolve the noisy-aggregation cap from an env-style string.

    Parameters
    ----------
    raw:
        Raw value as read from the environment.  ``None`` (or any non-int
        string) falls back to *default* — this is intentional: a server-side
        operator typo should not crash startup.
    default:
        Value returned when *raw* is missing or unparseable.
    minimum:
        Floor enforced on the parsed value (defensive: a too-small cap would
        make the analytics tools useless without producing any error).

    Returns
    -------
    int
        The effective cap, guaranteed >= *minimum*.
    """
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


_NOISY_MAX_ALERTS: int = _load_noisy_max_alerts(os.getenv("THRUK_NOISY_MAX_ALERTS"))

# Actionable suffix appended to every "Result capped at ..." warning so that
# users / LLMs immediately know how to mitigate the truncation (issue #201).
# Lives here (rather than in server.py) so both the noisy/flap analytics tools
# in ``server.py`` and the relocated ``thruk_concurrent_failures`` in
# ``tools/triage.py`` can share it without a server <-> triage import cycle
# (issue #259).
_NOISY_CAP_HINT: str = (
    " Narrow the time window (e.g. since='-2h') or raise the cap by setting "
    "the THRUK_NOISY_MAX_ALERTS env var (default 10000)."
)


# ---------------------------------------------------------------------------
# Latency sanity cap (issue #202)
# ---------------------------------------------------------------------------
# Naemon/Livestatus occasionally writes a Unix-timestamp-shaped value
# (~1.7e9) into the host ``latency`` column instead of a real latency in
# seconds.  Any value above this cap is treated as spurious and nullified
# before reaching the LLM client (see ``helpers._sanitize_latency``).
#
# The threshold is intentionally generous: real-world Livestatus latencies
# top out in the tens of seconds even on heavily loaded poller hosts, so
# a 1-hour cap will only ever match the buggy data, never a legitimate
# slow check.  Operators with truly pathological setups can raise it via
# ``THRUK_LATENCY_CAP_SECONDS``.

#: Default ceiling (seconds) above which a latency value is considered
#: corrupt (likely a Unix timestamp leaked from another column).
_LATENCY_SANITY_CAP_DEFAULT: float = 3600.0


def _load_latency_cap(
    raw: str | None = None,
    *,
    default: float = _LATENCY_SANITY_CAP_DEFAULT,
) -> float:
    """Resolve the latency-sanity cap from an env-style string.

    Returns *default* on missing or unparseable input (operator typos
    should not crash startup).  A non-positive value is also coerced to
    *default* — disabling the sanitizer entirely would re-expose the
    Naemon bug to LLM clients.
    """
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


LATENCY_SANITY_CAP_SECONDS: float = _load_latency_cap(os.getenv("THRUK_LATENCY_CAP_SECONDS"))
