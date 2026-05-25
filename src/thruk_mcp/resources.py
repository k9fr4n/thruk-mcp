"""MCP resource implementations (issue #147 — server.py split).

The ``thruk://...`` resources are URL-addressable JSON snapshots used by MCP
clients (Claude Desktop, the Docker MCP Gateway, ...) to fetch a single
object — host, service, hostgroup — or one of the two aggregate documents
(``problems``, ``stats``).

Functions are kept ``_``-prefixed because they are not directly callable
tools; they are dispatched by ``ThrukMCPServer.read_resource()``.
"""

from __future__ import annotations

import asyncio

from .constants import DEFAULT_HOST_COLUMNS, DEFAULT_SERVICE_COLUMNS
from .helpers import _get_client, _seg, _tool_response


async def _host_resource(name: str) -> str:
    """Single host as a JSON document, addressable as thruk://hosts/<name>."""
    data = await _get_client().get(f"/hosts/{_seg(name)}")
    return _tool_response(data)


async def _service_resource(host: str, service: str) -> str:
    """Single service as a JSON document (thruk://services/<host>/<service>)."""
    data = await _get_client().get(f"/services/{_seg(host)}/{_seg(service)}")
    return _tool_response(data)


async def _hostgroup_resource(name: str) -> str:
    """Host group config + members as JSON (thruk://hostgroups/<name>)."""
    data = await _get_client().get(f"/hostgroups/{_seg(name)}")
    return _tool_response(data)


async def _problems_resource() -> str:
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
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts", params=host_params),
        _get_client().get("/services", params=svc_params),
    )
    return _tool_response({"hosts": hosts, "services": services})


async def _stats_resource() -> str:
    """Aggregated host/service stats (cached ~15s)."""
    hosts, services = await asyncio.gather(
        _get_client().get("/hosts/stats"),
        _get_client().get("/services/stats"),
    )
    return _tool_response({"hosts": hosts, "services": services})
