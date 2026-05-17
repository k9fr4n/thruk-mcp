"""MCP server definition: tools mapped to Thruk REST endpoints."""

import fnmatch
import json
import logging
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import audit
from .client import ThrukClient
from .config import ThrukConfig

log = logging.getLogger("thruk_mcp.server")

# Tools that mutate the monitoring state. Used by:
# - read_only mode: removed entirely from the registry
# - audit log: wrapped to emit a JSON line per invocation
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "thruk_schedule_downtime",
        "thruk_schedule_host_services_downtime",
        "thruk_schedule_propagated_host_downtime",
        "thruk_schedule_hostgroup_downtime",
        "thruk_schedule_servicegroup_downtime",
        "thruk_delete_downtime",
        "thruk_delete_active_downtimes",
        "thruk_delete_downtimes_by_filter",
        "thruk_acknowledge",
        "thruk_remove_acknowledgement",
        "thruk_recheck",
        "thruk_run_background_query",
    }
)

HOST_STATES = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}
SERVICE_STATES = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
HOST_STATE_MAP = {"up": 0, "down": 1, "unreachable": 2}
SVC_STATE_MAP = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}

# Default columns: tight by design to minimize LLM token usage. A typical
# Thruk host row has ~80 attributes (custom vars, perf_data expansions, ...);
# returning them all blows the context for no reason. Callers can always
# override via the `columns` argument or use `thruk_query` for the raw row.
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
DEFAULT_DOWNTIME_COLUMNS = (
    "id,host_name,service_description,author,comment,"
    "start_time,end_time,fixed,duration,triggered_by,peer_name"
)
DEFAULT_COMMENT_COLUMNS = (
    "id,host_name,service_description,author,comment,entry_time,entry_type,persistent,peer_name"
)


def _list_params(
    limit: int,
    offset: int,
    sort: str | None,
    columns: str | None,
    default_columns: str | None,
    *,
    max_limit: int = 1000,
) -> dict[str, Any]:
    """Build the common limit/offset/sort/columns query params for list endpoints.

    `columns=''` (empty string) means "return all columns" — explicit opt-out
    from the token-saving default. `columns=None` falls back to default_columns.
    """
    p: dict[str, Any] = {"limit": max(1, min(limit, max_limit))}
    if offset > 0:
        p["offset"] = offset
    if sort:
        p["sort"] = sort
    effective = default_columns if columns is None else columns
    if effective:  # non-empty string
        p["columns"] = effective
    return p


def _ts(value: Any) -> str:
    if not value:
        return "N/A"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


def _backends(backends: str | None) -> tuple[str, ...] | None:
    if backends is None:
        return None
    parts = tuple(b.strip() for b in backends.split(",") if b.strip())
    return parts or None


def build_server(config: ThrukConfig | None = None) -> FastMCP:
    """Build the FastMCP server with all Thruk tools registered."""
    cfg = config or ThrukConfig.from_env()
    mcp = FastMCP("thruk-mcp")
    client = ThrukClient(cfg)

    # ------------------------------------------------------------------ Read
    @mcp.tool()
    async def thruk_list_hosts(
        hostgroup: str | None = None,
        state: str | None = None,
        name_regex: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "name",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List monitored hosts.

        Filters: `hostgroup`, `state` (up/down/unreachable), `name_regex` (CI regex).
        Pagination: `limit` (max 1000), `offset`. Sort: `sort` (e.g. 'name', '-state').
        Columns: by default a tight subset is returned to save tokens. Pass an empty
        string `columns=''` to return ALL columns, or a custom comma list.
        """
        params = _list_params(limit, offset, sort, columns, DEFAULT_HOST_COLUMNS)
        if hostgroup:
            params["groups[gte]"] = hostgroup
        if state and state.lower() in HOST_STATE_MAP:
            params["state"] = HOST_STATE_MAP[state.lower()]
        if name_regex:
            params["name[regex]"] = name_regex
        data = await client.get("/hosts", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_get_host(host: str, backends: str | None = None) -> str:
        """Get a single host by name."""
        data = await client.get(f"/hosts/{host}", backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_services(
        host: str | None = None,
        servicegroup: str | None = None,
        state: str | None = None,
        description_regex: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "host_name,description",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List monitored services.

        Filters: `host`, `servicegroup`, `state` (ok/warning/critical/unknown),
        `description_regex`. Pagination via `limit`/`offset`, sort via `sort`
        (e.g. '-last_state_change'). Default columns are a tight subset to save
        tokens; pass `columns=''` for all columns or a custom comma list.
        """
        params = _list_params(limit, offset, sort, columns, DEFAULT_SERVICE_COLUMNS)
        if host:
            params["host_name"] = host
        if servicegroup:
            params["groups[gte]"] = servicegroup
        if state and state.lower() in SVC_STATE_MAP:
            params["state"] = SVC_STATE_MAP[state.lower()]
        if description_regex:
            params["description[regex]"] = description_regex
        data = await client.get("/services", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_get_service(host: str, service: str, backends: str | None = None) -> str:
        """Get a single service by host and description."""
        data = await client.get(f"/services/{host}/{service}", backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_hostgroups(
        limit: int = 100,
        offset: int = 0,
        sort: str = "name",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List host groups. Default columns return name/alias and host/service counts only."""
        params = _list_params(limit, offset, sort, columns, DEFAULT_GROUP_COLUMNS)
        data = await client.get("/hostgroups", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_servicegroups(
        limit: int = 100,
        offset: int = 0,
        sort: str = "name",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List service groups. Default columns return name/alias and counts only."""
        params = _list_params(limit, offset, sort, columns, DEFAULT_GROUP_COLUMNS)
        data = await client.get("/servicegroups", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_problems(
        limit: int = 100,
        offset: int = 0,
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List all current unhandled host/service problems (not acknowledged, not in downtime).

        Sorted by worst state first. Default columns are tight; pass `columns=''` for all."""
        host_params = _list_params(limit, offset, "-state,name", columns, DEFAULT_HOST_COLUMNS)
        host_params.update({"state": 1, "acknowledged": 0, "scheduled_downtime_depth": 0})
        svc_params = _list_params(
            limit, offset, "-state,host_name,description", columns, DEFAULT_SERVICE_COLUMNS
        )
        svc_params.update({"state[gte]": 1, "acknowledged": 0, "scheduled_downtime_depth": 0})
        hosts = await client.get("/hosts", params=host_params, backends=_backends(backends))
        services = await client.get("/services", params=svc_params, backends=_backends(backends))
        return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)

    @mcp.tool()
    async def thruk_stats(backends: str | None = None) -> str:
        """Aggregated host/service statistics."""
        hosts = await client.get("/hosts/stats", backends=_backends(backends))
        services = await client.get("/services/stats", backends=_backends(backends))
        return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_downtimes(
        host: str | None = None,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
        sort: str = "-start_time",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List scheduled downtimes."""
        params = _list_params(limit, offset, sort, columns, DEFAULT_DOWNTIME_COLUMNS)
        if host:
            params["host_name"] = host
        if active_only:
            now = int(datetime.now().timestamp())
            params["start_time[lte]"] = now
            params["end_time[gte]"] = now
        data = await client.get("/downtimes", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_comments(
        host: str | None = None,
        limit: int = 100,
        offset: int = 0,
        sort: str = "-entry_time",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List comments (acknowledgements appear here too)."""
        params = _list_params(limit, offset, sort, columns, DEFAULT_COMMENT_COLUMNS)
        if host:
            params["host_name"] = host
        data = await client.get("/comments", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_sites() -> str:
        """List configured Thruk backends (sites)."""
        return json.dumps(await client.get("/sites"), indent=2, default=str)

    # ------------------------------------------------------ Logs / history
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
    ) -> Any:
        params = _list_params(limit, offset, sort, columns, DEFAULT_LOG_COLUMNS)
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
        if extra:
            params.update(extra)
        return await client.get(path, params=params, backends=_backends(backends))

    @mcp.tool()
    async def thruk_list_logs(
        host: str | None = None,
        service: str | None = None,
        since: str | None = "-24h",
        until: str | None = None,
        message_regex: str | None = None,
        limit: int = 100,
        offset: int = 0,
        sort: str = "-time",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """Query raw Livestatus log entries (/logs).

        Time arguments accept Thruk relative timestamps (e.g. '-24h', '-7d', '-30m')
        or absolute unix epoch. Default window: last 24h. Sort '-time' = newest first.
        Pagination via `limit`/`offset`. Default columns are a tight subset;
        pass `columns=''` for all columns."""
        data = await _fetch_logs(
            "/logs",
            host,
            service,
            since,
            until,
            message_regex,
            limit,
            offset,
            sort,
            columns,
            backends,
        )
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_alerts(
        host: str | None = None,
        service: str | None = None,
        state: str | None = None,
        since: str | None = "-24h",
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
        sort: str = "-time",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List HOST/SERVICE ALERT entries from the log (/alerts).

        Optional `state` filters alert state: up/down/unreachable for hosts,
        ok/warning/critical/unknown for services."""
        extra: dict[str, Any] = {}
        if state:
            s = state.lower()
            if s in HOST_STATE_MAP:
                extra["state"] = HOST_STATE_MAP[s]
            elif s in SVC_STATE_MAP:
                extra["state"] = SVC_STATE_MAP[s]
        data = await _fetch_logs(
            "/alerts",
            host,
            service,
            since,
            until,
            None,
            limit,
            offset,
            sort,
            columns,
            backends,
            extra=extra,
        )
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_notifications(
        host: str | None = None,
        service: str | None = None,
        contact: str | None = None,
        since: str | None = "-24h",
        until: str | None = None,
        limit: int = 100,
        offset: int = 0,
        sort: str = "-time",
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List notification entries from the log (/notifications, class=3).

        Optional `contact` filters notifications sent to a specific contact name."""
        extra: dict[str, Any] = {}
        if contact:
            extra["contact_name"] = contact
        data = await _fetch_logs(
            "/notifications",
            host,
            service,
            since,
            until,
            None,
            limit,
            offset,
            sort,
            columns,
            backends,
            extra=extra,
        )
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_recent_events(
        hours: int = 1,
        host: str | None = None,
        service: str | None = None,
        only_alerts: bool = False,
        limit: int = 100,
        offset: int = 0,
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """Return the most recent monitoring events from the last N hours
        (default 1h). Defaults to all log classes; set `only_alerts=True` to
        restrict to HOST/SERVICE ALERT entries."""
        path = "/alerts" if only_alerts else "/logs"
        data = await _fetch_logs(
            path, host, service, f"-{hours}h", None, None, limit, offset, "-time", columns, backends
        )
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_query(
        path: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        backends: str | None = None,
    ) -> str:
        """Escape hatch: call any Thruk REST endpoint. `path` is everything after `/thruk/r`
        (e.g. `/hosts/srv01/services`). `params` is the query string, `data` the form body.
        See https://www.thruk.org/documentation/rest.html for the full catalogue."""
        result = await client.request(
            method.upper(),
            path,
            params=params,
            data=data,
            backends=_backends(backends),
        )
        return json.dumps(result, indent=2, default=str)

    @mcp.tool()
    async def thruk_run_background_query(
        path: str,
        method: str = "POST",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        backends: str | None = None,
        poll_timeout: float = 300.0,
    ) -> str:
        """Run a potentially long Thruk REST request via the `background=1`
        mechanism. The server returns a job id immediately, then we poll
        `/thruk/jobs/<id>/output` until completion (default 5 min timeout).

        Use this for expensive queries: full config dumps, large availability
        reports, recursive config checks. Same `path` semantics as
        `thruk_query`."""
        result = await client.run_background(
            path,
            method=method.upper(),
            params=params,
            data=data,
            backends=_backends(backends),
            poll_timeout=poll_timeout,
        )
        return json.dumps(result, indent=2, default=str)

    # =================================================== MCP Resources
    # Resources let an MCP client "open" a Thruk object as if it were a file.
    # Useful for clients that have a resource browser (Claude Desktop, VS Code).
    @mcp.resource("thruk://hosts/{name}")
    async def host_resource(name: str) -> str:
        """Single host as a JSON document, addressable as thruk://hosts/<name>."""
        data = await client.get(f"/hosts/{name}")
        return json.dumps(data, indent=2, default=str)

    @mcp.resource("thruk://services/{host}/{service}")
    async def service_resource(host: str, service: str) -> str:
        """Single service as a JSON document (thruk://services/<host>/<service>)."""
        data = await client.get(f"/services/{host}/{service}")
        return json.dumps(data, indent=2, default=str)

    @mcp.resource("thruk://hostgroups/{name}")
    async def hostgroup_resource(name: str) -> str:
        """Host group config + members as JSON (thruk://hostgroups/<name>)."""
        data = await client.get(f"/hostgroups/{name}")
        return json.dumps(data, indent=2, default=str)

    @mcp.resource("thruk://problems")
    async def problems_resource() -> str:
        """Current unhandled host/service problems as a JSON document."""
        host_params = {
            "state": 1,
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
            "columns": DEFAULT_HOST_COLUMNS,
            "limit": 500,
        }
        svc_params = {
            "state[gte]": 1,
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
            "columns": DEFAULT_SERVICE_COLUMNS,
            "limit": 500,
        }
        hosts = await client.get("/hosts", params=host_params)
        services = await client.get("/services", params=svc_params)
        return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)

    @mcp.resource("thruk://stats")
    async def stats_resource() -> str:
        """Aggregated host/service stats (cached ~15s)."""
        hosts = await client.get("/hosts/stats")
        services = await client.get("/services/stats")
        return json.dumps({"hosts": hosts, "services": services}, indent=2, default=str)

    # ===================================================== MCP Prompts
    # Prompts are pre-canned workflows the LLM client can offer to the user
    # (slash-commands in Claude Desktop, etc.). They return a structured
    # conversation the model is expected to start with.
    @mcp.prompt(
        title="Investigate alert",
        description="Walk through a host/service alert: state, recent logs, "
        "notifications, and suggested next steps.",
    )
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

    @mcp.prompt(
        title="Schedule maintenance",
        description="Schedule downtime for a hostgroup, host or service for a given duration.",
    )
    def schedule_maintenance(
        target: str,
        duration_minutes: int = 120,
        kind: str = "hostgroup",
    ) -> str:
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
            f"duration_minutes={duration_minutes} and a clear comment "
            "explaining the reason.\n"
            "5. Verify the downtime is active via `thruk_list_downtimes`.\n"
        )

    @mcp.prompt(
        title="Why is service flapping?",
        description="Diagnose a flapping service: state-change history, recent logs, "
        "perf-data trends, and recommendations.",
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

    # ----------------------------------------------------------------- Write
    @mcp.tool()
    async def thruk_schedule_downtime(
        host: str,
        service: str | None = None,
        comment: str = "requested via MCP",
        author: str = "thruk-mcp",
        start_time: str = "now",
        end_time: str = "+2h",
        duration_minutes: int | None = None,
        fixed: bool = True,
        backends: str | None = None,
    ) -> str:
        """Schedule a host or service downtime. Time accepts 'now', relative ('+2h', '+30m')
        or ISO 8601. If `duration_minutes` is set it overrides `end_time`."""
        if duration_minutes:
            end_time = f"+{duration_minutes}m"
        endpoint = (
            f"/services/{host}/{service}/cmd/schedule_svc_downtime"
            if service
            else f"/hosts/{host}/cmd/schedule_host_downtime"
        )
        payload = {
            "start_time": start_time,
            "end_time": end_time,
            "comment_data": comment,
            "comment_author": author,
            "fixed": "1" if fixed else "0",
        }
        return json.dumps(
            await client.post(endpoint, data=payload, backends=_backends(backends)),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_acknowledge(
        host: str,
        service: str | None = None,
        comment: str = "acknowledged via MCP",
        author: str = "thruk-mcp",
        sticky: bool = True,
        notify: bool = True,
        persistent: bool = False,
        backends: str | None = None,
    ) -> str:
        """Acknowledge a host or service problem."""
        endpoint = (
            f"/services/{host}/{service}/cmd/acknowledge_svc_problem"
            if service
            else f"/hosts/{host}/cmd/acknowledge_host_problem"
        )
        payload = {
            "comment_data": comment,
            "comment_author": author,
            "sticky_ack": "1" if sticky else "0",
            "send_notification": "1" if notify else "0",
            "persistent_comment": "1" if persistent else "0",
        }
        return json.dumps(
            await client.post(endpoint, data=payload, backends=_backends(backends)),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_remove_acknowledgement(
        host: str, service: str | None = None, backends: str | None = None
    ) -> str:
        """Remove an acknowledgement."""
        endpoint = (
            f"/services/{host}/{service}/cmd/remove_svc_acknowledgement"
            if service
            else f"/hosts/{host}/cmd/remove_host_acknowledgement"
        )
        return json.dumps(
            await client.post(endpoint, backends=_backends(backends)),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_recheck(
        host: str, service: str | None = None, forced: bool = True, backends: str | None = None
    ) -> str:
        """Schedule an immediate (re)check for a host or service."""
        if service:
            cmd = "schedule_forced_svc_check" if forced else "schedule_svc_check"
            endpoint = f"/services/{host}/{service}/cmd/{cmd}"
        else:
            cmd = "schedule_forced_host_check" if forced else "schedule_host_check"
            endpoint = f"/hosts/{host}/cmd/{cmd}"
        return json.dumps(
            await client.post(endpoint, data={"start_time": "now"}, backends=_backends(backends)),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_delete_downtime(
        downtime_id: int, host: str, service: str | None = None, backends: str | None = None
    ) -> str:
        """Delete a host or service downtime by its id."""
        endpoint = (
            f"/services/{host}/{service}/cmd/del_downtime"
            if service
            else f"/hosts/{host}/cmd/del_downtime"
        )
        return json.dumps(
            await client.post(
                endpoint, data={"downtime_id": str(downtime_id)}, backends=_backends(backends)
            ),
            indent=2,
            default=str,
        )

    # ----------------------------------------------------- Downtime mgmt
    def _downtime_payload(
        comment: str,
        author: str,
        start_time: str,
        end_time: str,
        duration_minutes: int | None,
        fixed: bool,
        triggered_by: int,
    ) -> dict[str, str]:
        if duration_minutes:
            end_time = f"+{duration_minutes}m"
        return {
            "start_time": start_time,
            "end_time": end_time,
            "comment_data": comment,
            "comment_author": author,
            "fixed": "1" if fixed else "0",
            "triggered_by": str(triggered_by),
        }

    @mcp.tool()
    async def thruk_get_downtime(downtime_id: int, backends: str | None = None) -> str:
        """Get a single downtime by id."""
        data = await client.get(f"/downtimes/{downtime_id}", backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_schedule_host_services_downtime(
        host: str,
        comment: str = "requested via MCP",
        author: str = "thruk-mcp",
        start_time: str = "now",
        end_time: str = "+2h",
        duration_minutes: int | None = None,
        fixed: bool = True,
        backends: str | None = None,
    ) -> str:
        """Schedule a downtime on ALL services of the given host
        (schedule_host_svc_downtime). Use thruk_schedule_downtime for the host
        itself or for one specific service."""
        payload = _downtime_payload(
            comment, author, start_time, end_time, duration_minutes, fixed, 0
        )
        return json.dumps(
            await client.post(
                f"/hosts/{host}/cmd/schedule_host_svc_downtime",
                data=payload,
                backends=_backends(backends),
            ),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_schedule_propagated_host_downtime(
        host: str,
        triggered: bool = False,
        comment: str = "requested via MCP",
        author: str = "thruk-mcp",
        start_time: str = "now",
        end_time: str = "+2h",
        duration_minutes: int | None = None,
        fixed: bool = True,
        backends: str | None = None,
    ) -> str:
        """Schedule a downtime on a host and propagate to all child hosts.
        If `triggered=True`, child downtimes are triggered by the parent (start
        when the parent enters its downtime). Useful for a parent network
        device whose children should automatically follow."""
        cmd = (
            "schedule_and_propagate_triggered_host_downtime"
            if triggered
            else "schedule_and_propagate_host_downtime"
        )
        payload = _downtime_payload(
            comment, author, start_time, end_time, duration_minutes, fixed, 0
        )
        return json.dumps(
            await client.post(
                f"/hosts/{host}/cmd/{cmd}",
                data=payload,
                backends=_backends(backends),
            ),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_schedule_hostgroup_downtime(
        hostgroup: str,
        target: str = "hosts",
        comment: str = "requested via MCP",
        author: str = "thruk-mcp",
        start_time: str = "now",
        end_time: str = "+2h",
        duration_minutes: int | None = None,
        fixed: bool = True,
        backends: str | None = None,
    ) -> str:
        """Schedule a downtime for every host (`target='hosts'`, default) or
        every service (`target='services'`) of a hostgroup."""
        cmd = (
            "schedule_hostgroup_svc_downtime"
            if target == "services"
            else "schedule_hostgroup_host_downtime"
        )
        payload = _downtime_payload(
            comment, author, start_time, end_time, duration_minutes, fixed, 0
        )
        return json.dumps(
            await client.post(
                f"/hostgroups/{hostgroup}/cmd/{cmd}",
                data=payload,
                backends=_backends(backends),
            ),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_schedule_servicegroup_downtime(
        servicegroup: str,
        target: str = "services",
        comment: str = "requested via MCP",
        author: str = "thruk-mcp",
        start_time: str = "now",
        end_time: str = "+2h",
        duration_minutes: int | None = None,
        fixed: bool = True,
        backends: str | None = None,
    ) -> str:
        """Schedule a downtime on a servicegroup. `target='services'` (default)
        targets all services in the group; `target='hosts'` targets the hosts
        owning those services."""
        cmd = (
            "schedule_servicegroup_host_downtime"
            if target == "hosts"
            else "schedule_servicegroup_svc_downtime"
        )
        payload = _downtime_payload(
            comment, author, start_time, end_time, duration_minutes, fixed, 0
        )
        return json.dumps(
            await client.post(
                f"/servicegroups/{servicegroup}/cmd/{cmd}",
                data=payload,
                backends=_backends(backends),
            ),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_delete_active_downtimes(
        host: str,
        service: str | None = None,
        backends: str | None = None,
    ) -> str:
        """Remove ALL currently active downtimes for a host (or one specific
        service when `service` is given). No need to know individual ids."""
        endpoint = (
            f"/services/{host}/{service}/cmd/del_active_service_downtimes"
            if service
            else f"/hosts/{host}/cmd/del_active_host_downtimes"
        )
        return json.dumps(
            await client.post(endpoint, backends=_backends(backends)),
            indent=2,
            default=str,
        )

    @mcp.tool()
    async def thruk_delete_downtimes_by_filter(
        host: str | None = None,
        hostgroup: str | None = None,
        service: str | None = None,
        start_time: str | None = None,
        comment: str | None = None,
        backends: str | None = None,
    ) -> str:
        """Bulk-delete downtimes matching arbitrary filters via the system
        commands `del_downtime_by_host_name`, `del_downtime_by_hostgroup_name`
        or `del_downtime_by_start_time_comment`. At least one filter must be
        provided; the most specific endpoint is selected automatically."""
        payload: dict[str, str] = {}
        if host:
            payload["hostname"] = host
        if hostgroup:
            payload["hostgroup_name"] = hostgroup
        if service:
            payload["service_desc"] = service
        if start_time:
            payload["start_time"] = start_time
        if comment:
            payload["comment"] = comment
        if not payload:
            raise ValueError(
                "Provide at least one of host, hostgroup, service, start_time, comment."
            )
        if hostgroup:
            cmd = "del_downtime_by_hostgroup_name"
        elif host:
            cmd = "del_downtime_by_host_name"
        else:
            cmd = "del_downtime_by_start_time_comment"
        return json.dumps(
            await client.post(
                f"/system/cmd/{cmd}",
                data=payload,
                backends=_backends(backends),
            ),
            indent=2,
            default=str,
        )

    # ===================== Security: filter & wrap registered tools =====
    audit.configure(enabled=cfg.audit_log)
    _apply_security_filters(mcp, cfg)

    # store for graceful shutdown if caller wants it
    mcp._thruk_client = client  # type: ignore[attr-defined]
    return mcp


def _apply_security_filters(mcp: FastMCP, cfg: ThrukConfig) -> None:
    """Remove disallowed tools and wrap write tools with audit logging.

    Three orthogonal knobs:
    - `cfg.read_only`: drop every tool in WRITE_TOOLS.
    - `cfg.enabled_tools`: if non-empty, drop every tool not matched by any
      of its fnmatch patterns. Always evaluated AFTER read_only.
    - `cfg.audit_log`: wrap remaining write tools to emit JSON audit lines.
    """
    tool_mgr = mcp._tool_manager
    registry: dict[str, Any] = tool_mgr._tools
    for name in list(registry):
        # 1) read_only mode strips writes outright
        if cfg.read_only and name in WRITE_TOOLS:
            mcp.remove_tool(name)
            log.info("read_only: removed tool %s", name)
            continue
        # 2) allowlist (if provided) takes precedence over everything else
        if cfg.enabled_tools and not any(
            fnmatch.fnmatchcase(name, pat) for pat in cfg.enabled_tools
        ):
            mcp.remove_tool(name)
            log.info("allowlist: removed tool %s", name)
            continue
        # 3) wrap remaining writes for audit
        if cfg.audit_log and name in WRITE_TOOLS:
            tool = registry[name]
            tool.fn = audit.audited(name, user=cfg.auth_user)(tool.fn)
