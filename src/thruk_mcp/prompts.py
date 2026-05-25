"""MCP prompt templates (issue #147 — server.py split).

Each function returns the prompt body that the MCP client renders for the
user.  Dispatched by ``ThrukMCPServer.get_prompt()``.
"""

from __future__ import annotations


def investigate_alert(host: str, service: str | None = None) -> str:
    target = f"host '{host}'" if not service else f"service '{service}' on host '{host}'"
    steps = "\n".join(
        [
            f"1. Fetch the current state of {target} using `thruk_get_host`"
            + ("/`thruk_get_service`" if service else ""),
            "2. Pull the recent alert history via `thruk_list_alerts` (last 6h)",
            "3. Check notifications sent via `thruk_list_notifications`",
            "4. Inspect related comments and acknowledgements with `thruk_list_comments`",
            "5. Verify there is no active downtime via `thruk_list_downtimes`",
            "6. Summarise root-cause hypotheses and propose 2-3 remediation steps",
            "7. If the operator confirms, acknowledge with `thruk_acknowledge` "
            "and/or trigger a forced recheck with `thruk_recheck`.",
        ]
    )
    return (
        f"You are the on-call SRE assistant. The user wants to investigate the "
        f"current alert on {target}. Proceed methodically:\n\n{steps}\n\n"
        "Do not modify the monitoring state without explicit user confirmation."
    )


def schedule_maintenance(target: str, duration_minutes: int = 120, kind: str = "hostgroup") -> str:
    kind = kind.lower()
    if kind not in {"host", "service", "hostgroup", "servicegroup"}:
        kind = "hostgroup"
    tool_map = {
        "host": "thruk_schedule_downtime",
        "service": "thruk_schedule_downtime",
        "hostgroup": "thruk_schedule_hostgroup_downtime",
        "servicegroup": "thruk_schedule_servicegroup_downtime",
    }
    return (
        f"The user wants to schedule {duration_minutes} minutes of maintenance "
        f"on the {kind} '{target}'.\n\n"
        f"1. Confirm the {kind} exists by listing it (e.g. `thruk_list_{kind}s` "
        "or `thruk_get_host`).\n"
        "2. Show the user the list of impacted hosts/services.\n"
        "3. Ask explicit confirmation before applying.\n"
        f"4. On 'yes', call `{tool_map[kind]}` with "
        f"duration_minutes={duration_minutes} and a clear comment explaining the reason.\n"
        "5. Verify the downtime is active via `thruk_list_downtimes`.\n"
    )


def diagnose_flapping(host: str, service: str) -> str:
    return (
        f"The user reports that service '{service}' on host '{host}' is flapping. "
        "Carry out a focused investigation:\n\n"
        "1. `thruk_get_service` to confirm state and current `is_flapping` flag.\n"
        "2. `thruk_list_alerts` for the same host/service over the last 24h, "
        "sorted -time, to count state transitions.\n"
        "3. `thruk_list_logs` filtered on `message_regex='flapp'` to confirm "
        "flap-detection events.\n"
        "4. If perf-data is available in the service row, inspect the metric "
        "that is oscillating (rta, latency, queue depth, ...).\n"
        "5. Summarise likely causes (network jitter, threshold too tight, "
        "passive check freshness, ...).\n"
        "6. Propose remediation: widen warning/critical thresholds, increase "
        "max_check_attempts, disable flap detection if intentional, or add a "
        "downtime while a fix is rolled out.\n"
        "7. Do not change Thruk state without confirmation."
    )
