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


def daily_health_report(hostgroup: str | None = None) -> str:
    scope = f"the hostgroup '{hostgroup}'" if hostgroup else "the whole estate"
    flt = f" Pass `hostgroup='{hostgroup}'` to every tool that accepts it." if hostgroup else ""
    return (
        f"You are the on-call SRE assistant producing the morning health report "
        f"for {scope}.{flt} Build a concise, scannable summary:\n\n"
        "1. Overall counts with `thruk_totals` (hosts up/down, services "
        "ok/warning/critical/unknown).\n"
        "2. Unacknowledged CRITICAL/DOWN problems via `thruk_unacked_critical` "
        "(threshold_minutes=60) — these need attention first.\n"
        "3. Silent failures: run `thruk_stale_checks` to surface checks that "
        "stopped reporting (the dangerous 'false green').\n"
        "4. Oldest lingering problems with `thruk_oldest_problems` (limit=10).\n"
        "5. Noisiest objects over the last 24h with `thruk_top_noisy_hosts` and "
        "`thruk_top_noisy_services` (limit=5 each).\n\n"
        "Present the result as a short bulleted digest, worst-first, with one "
        "line per finding. Do not modify any monitoring state — this is a "
        "read-only report."
    )


def incident_triage(hostgroup: str | None = None) -> str:
    scope = f"the hostgroup '{hostgroup}'" if hostgroup else "all backends"
    flt = f" Pass `hostgroup='{hostgroup}'` to every tool that accepts it." if hostgroup else ""
    return (
        f"A major incident is in progress on {scope}. Act as the triage lead and "
        f"prioritise ruthlessly.{flt}\n\n"
        "1. Get the blast radius with `thruk_problem_counts` (how many "
        "hosts/services are down vs critical vs warning).\n"
        "2. Look for a common cause: `thruk_concurrent_failures` "
        "(window_minutes=5, min_hosts=3) to detect hosts that failed together — "
        "a spike points to a shared dependency (network, hypervisor, storage).\n"
        "3. `thruk_oldest_problems` to find when the incident actually started.\n"
        "4. `thruk_unacked_critical` (threshold_minutes=0) for the full list of "
        "unhandled critical problems still needing an owner.\n\n"
        "Then output: (a) a one-line severity assessment, (b) the most likely "
        "common cause with supporting evidence, (c) a prioritised action list. "
        "Do not acknowledge or schedule downtime without explicit confirmation."
    )


def capacity_review(hostgroup: str | None = None, within_percent: int = 10) -> str:
    scope = f"the hostgroup '{hostgroup}'" if hostgroup else "the monitored estate"
    flt = f"hostgroup='{hostgroup}', " if hostgroup else ""
    return (
        f"Perform a capacity / saturation review for {scope}, catching metrics "
        "before they breach.\n\n"
        f"1. Run `thruk_perfdata_near_threshold` ({flt}within_percent="
        f"{within_percent}) to list every metric within {within_percent}% of its "
        "warn/crit range (or already breached with zero headroom).\n"
        f"2. For the worst offenders, pull `thruk_perfdata_snapshot` ({flt}"
        "limit=200) to read current values and trends (disk %, memory, CPU, "
        "queue depth, ...).\n"
        "3. Group findings by resource type and rank by smallest headroom.\n\n"
        "Output a prioritised list of at-risk metrics with current value, "
        "threshold, and remaining headroom, plus a short recommendation per "
        "item (capacity add, cleanup, threshold review). Read-only — do not "
        "change state."
    )


def sla_report(target: str, kind: str = "host", timeperiod: str = "last7days") -> str:
    kind = kind.lower()
    if kind not in {"host", "service", "hostgroup"}:
        kind = "host"
    tool_map = {
        "host": "thruk_host_availability",
        "service": "thruk_service_availability",
        "hostgroup": "thruk_hostgroup_availability",
    }
    note = (
        " For a service, target must be 'host/service' — split it and pass both "
        "`host` and `service` arguments."
        if kind == "service"
        else ""
    )
    return (
        f"Produce an availability / SLA report for the {kind} '{target}' over "
        f"the period '{timeperiod}'.\n\n"
        f"1. Call `{tool_map[kind]}` with timeperiod='{timeperiod}'.{note}\n"
        "2. Report the uptime percentage (time_up_percent for hosts, "
        "time_ok_percent for services) to two decimals.\n"
        "3. Break down the unavailable time: scheduled downtime vs unplanned "
        "outage — rerun with `with_downtimes=True` to separate the two.\n"
        "4. State whether the result meets a 99.9% SLA target and by what "
        "margin (in minutes of allowed downtime).\n\n"
        "Present a clean report: period, measured availability, downtime budget "
        "consumed, and pass/fail verdict. Read-only."
    )


def noise_review(since: str = "-24h") -> str:
    return (
        "Perform a monitoring-noise hygiene review to reduce alert fatigue. "
        f"Use the window since='{since}' on every history/analytics tool.\n\n"
        "1. `thruk_top_noisy_hosts` and `thruk_top_noisy_services` (limit=10) — "
        "the objects generating the most alerts.\n"
        "2. `thruk_flap_summary` (min_transitions=3) to find objects oscillating "
        "between states.\n"
        "3. `thruk_recurring_problems` (min_alerts=5) for issues that keep coming "
        "back without ever being fixed.\n"
        "4. `thruk_alert_heatmap` (bucket='1h') to see whether noise clusters at "
        "specific times (cron jobs, backups, batch windows).\n\n"
        "For each noisy object, classify the likely root cause (threshold too "
        "tight, flapping, recurring real fault, scheduled-job collision) and "
        "recommend a concrete fix (retune thresholds, raise "
        "max_check_attempts, add a recurring downtime, or fix the underlying "
        "issue). Read-only — propose, do not apply."
    )
