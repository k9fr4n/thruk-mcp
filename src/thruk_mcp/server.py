"""MCP server definition: tools mapped to Thruk REST endpoints."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ThrukClient
from .config import ThrukConfig

log = logging.getLogger("thruk_mcp.server")

HOST_STATES = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}
SERVICE_STATES = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}
HOST_STATE_MAP = {"up": 0, "down": 1, "unreachable": 2}
SVC_STATE_MAP = {"ok": 0, "warning": 1, "critical": 2, "unknown": 3}


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
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List monitored hosts. Optional filters: hostgroup, state (up/down/unreachable),
        name_regex (case-insensitive regex on host name), columns (comma list)."""
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if columns:
            params["columns"] = columns
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
        columns: str | None = None,
        backends: str | None = None,
    ) -> str:
        """List monitored services. Filters: host, servicegroup, state
        (ok/warning/critical/unknown), description_regex."""
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if columns:
            params["columns"] = columns
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
    async def thruk_list_hostgroups(limit: int = 100, backends: str | None = None) -> str:
        """List host groups."""
        data = await client.get(
            "/hostgroups", params={"limit": limit}, backends=_backends(backends)
        )
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_servicegroups(limit: int = 100, backends: str | None = None) -> str:
        """List service groups."""
        data = await client.get(
            "/servicegroups", params={"limit": limit}, backends=_backends(backends)
        )
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_problems(limit: int = 100, backends: str | None = None) -> str:
        """List all current unhandled host/service problems (not acknowledged, not in downtime)."""
        host_params = {"limit": limit, "state": 1, "acknowledged": 0, "scheduled_downtime_depth": 0}
        svc_params = {
            "limit": limit,
            "state[gte]": 1,
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
        }
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
        backends: str | None = None,
    ) -> str:
        """List scheduled downtimes."""
        params: dict[str, Any] = {"limit": limit}
        if host:
            params["host_name"] = host
        if active_only:
            now = int(datetime.now().timestamp())
            params["start_time[lte]"] = now
            params["end_time[gte]"] = now
        data = await client.get("/downtimes", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_list_comments(host: str | None = None, limit: int = 100,
                                   backends: str | None = None) -> str:
        """List comments (acknowledgements appear here too)."""
        params: dict[str, Any] = {"limit": limit}
        if host:
            params["host_name"] = host
        data = await client.get("/comments", params=params, backends=_backends(backends))
        return json.dumps(data, indent=2, default=str)

    @mcp.tool()
    async def thruk_sites() -> str:
        """List configured Thruk backends (sites)."""
        return json.dumps(await client.get("/sites"), indent=2, default=str)

    @mcp.tool()
    async def thruk_query(
        path: str,
        method: str = "GET",
        params_json: str | None = None,
        data_json: str | None = None,
        backends: str | None = None,
    ) -> str:
        """Escape hatch: call any Thruk REST endpoint. `path` is everything after `/thruk/r`
        (e.g. `/hosts/srv01/services`). Use `params_json` for query string, `data_json` for body.
        See https://www.thruk.org/documentation/rest.html for the full catalogue."""
        params = json.loads(params_json) if params_json else None
        data = json.loads(data_json) if data_json else None
        result = await client.request(
            method.upper(), path, params=params, data=data, backends=_backends(backends),
        )
        return json.dumps(result, indent=2, default=str)

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
            indent=2, default=str,
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
            "sticky": "1" if sticky else "0",
            "notify": "1" if notify else "0",
            "persistent": "1" if persistent else "0",
        }
        return json.dumps(
            await client.post(endpoint, data=payload, backends=_backends(backends)),
            indent=2, default=str,
        )

    @mcp.tool()
    async def thruk_remove_acknowledgement(host: str, service: str | None = None,
                                            backends: str | None = None) -> str:
        """Remove an acknowledgement."""
        endpoint = (
            f"/services/{host}/{service}/cmd/remove_svc_acknowledgement"
            if service
            else f"/hosts/{host}/cmd/remove_host_acknowledgement"
        )
        return json.dumps(
            await client.post(endpoint, backends=_backends(backends)),
            indent=2, default=str,
        )

    @mcp.tool()
    async def thruk_recheck(host: str, service: str | None = None,
                             forced: bool = True, backends: str | None = None) -> str:
        """Schedule an immediate (re)check for a host or service."""
        if service:
            cmd = "schedule_forced_svc_check" if forced else "schedule_svc_check"
            endpoint = f"/services/{host}/{service}/cmd/{cmd}"
        else:
            cmd = "schedule_forced_host_check" if forced else "schedule_host_check"
            endpoint = f"/hosts/{host}/cmd/{cmd}"
        return json.dumps(
            await client.post(endpoint, data={"start_time": "now"}, backends=_backends(backends)),
            indent=2, default=str,
        )

    @mcp.tool()
    async def thruk_delete_downtime(downtime_id: int, host: str,
                                     service: str | None = None,
                                     backends: str | None = None) -> str:
        """Delete a host or service downtime by its id."""
        endpoint = (
            f"/services/{host}/{service}/cmd/del_downtime"
            if service
            else f"/hosts/{host}/cmd/del_downtime"
        )
        return json.dumps(
            await client.post(endpoint, data={"downtime_id": str(downtime_id)},
                              backends=_backends(backends)),
            indent=2, default=str,
        )

    # store for graceful shutdown if caller wants it
    mcp._thruk_client = client  # type: ignore[attr-defined]
    return mcp
