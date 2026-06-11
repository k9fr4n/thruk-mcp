"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest_asyncio
import respx
from mcp.types import TextContent

from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import _TOOL_DISPATCH, build_server

BASE = "https://thruk.test"
CFG = ThrukConfig(base_url=BASE, api_key="k")


class _ServerProxy:
    """Thin wrapper around the low-level Server that provides the same
    ``call_tool(name, args)`` interface the tests expect.

    Also exposes ``read_resource`` as a direct call to the module-level
    resource functions (resources are not part of the low-level Server
    in the same way as FastMCP).
    """

    def __init__(self, server, client: ThrukClient) -> None:
        self._server = server
        self._thruk_client = client

    async def call_tool(self, name: str, args: dict) -> list[TextContent]:
        fn = _TOOL_DISPATCH.get(name)
        if fn is None:
            raise ValueError(f"Unknown tool: {name!r}")
        result = await fn(**args)
        return [TextContent(type="text", text=result)]

    async def read_resource(self, uri) -> list:
        """Delegate to ThrukMCPServer.read_resource (issue #145: tests the real path)."""
        return await self._server.read_resource(uri)


@pytest_asyncio.fixture
async def mocked_server():
    """Low-level MCP Server + respx router intercepting all outbound traffic.

    Yields a tuple ``(proxy, respx_router)``. Tests register HTTP expectations
    on the router and invoke tools via ``await proxy.call_tool(name, args)``.
    """
    with respx.mock(assert_all_called=False) as router:
        server = build_server(CFG)
        client: ThrukClient = server._thruk_client  # type: ignore[attr-defined]
        proxy = _ServerProxy(server, client)
        try:
            yield proxy, router
        finally:
            await client.aclose()


def ok(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ---------------------------------------------------------------------------
# Server-side aggregation simulation helpers (issue #312)
#
# The trend/noisy tools now push GROUP BY / count(*) down to Thruk instead of
# fetching raw rows and counting client-side. These helpers reproduce what
# Thruk's aggregated /logs responses look like so tests can keep expressing
# fixtures as plain raw log rows.
# ---------------------------------------------------------------------------

from urllib.parse import parse_qs  # noqa: E402


def body_params(request) -> dict[str, str]:
    """Parse a form-encoded POST body into a flat {key: value} dict."""
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def agg_rows(raw, key_fields=("host_name",), *, exclude_recovery=True) -> list[dict]:
    """Simulate Thruk's ``GROUP BY <key_fields>,state`` ``count(*)`` response.

    Mirrors the query ``_aggregate_alerts`` issues: one row per
    ``(*key_fields, state)`` combination carrying ``cnt`` / ``first_t`` /
    ``last_t``, sorted by ``cnt`` descending. ``exclude_recovery`` drops
    ``state=0`` rows the way the server-side ``state[!=]=0`` filter does.
    """
    groups: dict[tuple, dict] = {}
    for e in raw:
        st = e.get("state", -1)
        if exclude_recovery and st == 0:
            continue
        kv = tuple(e.get(f) or "" for f in key_fields)
        t = int(e.get("time") or 0)
        g = groups.setdefault((*kv, st), {"cnt": 0, "first_t": t, "last_t": t})
        g["cnt"] += 1
        g["first_t"] = min(g["first_t"], t)
        g["last_t"] = max(g["last_t"], t)
    out: list[dict] = []
    for key, g in groups.items():
        *kv, st = key
        row = dict(zip(key_fields, kv, strict=False))
        row.update(state=st, cnt=g["cnt"], first_t=g["first_t"], last_t=g["last_t"])
        out.append(row)
    out.sort(key=lambda r: r["cnt"], reverse=True)
    return out


def count_side_effect(events):
    """respx side_effect answering per-bucket ``count(*)`` heatmap queries.

    Counts how many ``events`` (each a dict with a ``time`` key) fall within the
    bucket window ``[time[gte], time[lte]]`` of each request, exactly as a
    server-side ``count(*)`` over that window would.

    An ungrouped ``count(*)`` collapses to a **single object** ``{"cnt": N}`` on
    Thruk's normal path — *not* a one-element list. Returning the dict shape
    here keeps the heatmap tests honest: the issue-#312 regression where
    ``_sum_cnt`` only summed lists (so every bucket read 0) was invisible while
    this mock wrapped the count in a list.
    """

    def _se(request):
        p = body_params(request)
        if p.get("columns") != "count(*):cnt":
            return ok([])
        gte, lte = int(p["time[gte]"]), int(p["time[lte]"])
        n = sum(1 for e in events if gte <= int(e["time"]) <= lte)
        return ok({"cnt": n})

    return _se


def flap_side_effect(raw):
    """respx side_effect for ``thruk_flap_summary`` (two /logs queries).

    The candidate-discovery query (aggregated, ``count(*):cnt`` in columns) gets
    the simulated GROUP BY response; the scoped chronological fetch gets the raw
    rows back unchanged so transitions can be counted.
    """
    agg = agg_rows(raw, ("host_name", "service_description"), exclude_recovery=False)

    def _se(request):
        p = body_params(request)
        if "count(*):cnt" in p.get("columns", ""):
            return ok(agg)
        return ok(raw)

    return _se
