"""v0.6: read-only mode, allowlist, audit log, rate limit."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import respx

from thruk_mcp import audit
from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import WRITE_TOOLS, build_server

BASE = "https://thruk.test"


async def _close(mcp) -> None:
    await mcp._thruk_client.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_only_strips_write_tools() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=True)
    mcp = build_server(cfg)
    try:
        tools = {t.name for t in await mcp.list_tools()}
        # No write tool should remain
        assert tools.isdisjoint(WRITE_TOOLS)
        # Read tools still present
        assert "thruk_list_hosts" in tools
        assert "thruk_sites" in tools
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_allowlist_filters_tools_with_wildcard() -> None:
    cfg = ThrukConfig(
        base_url=BASE,
        api_key="k",
        enabled_tools=("thruk_list_*", "thruk_problems"),
    )
    mcp = build_server(cfg)
    try:
        tools = {t.name for t in await mcp.list_tools()}
        assert "thruk_list_hosts" in tools
        assert "thruk_list_alerts" in tools
        assert "thruk_problems" in tools
        assert "thruk_acknowledge" not in tools
        assert "thruk_get_host" not in tools  # not in allowlist
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_read_only_overrides_allowlist_for_writes() -> None:
    """Even if a write tool matches the allowlist, read_only strips it."""
    cfg = ThrukConfig(
        base_url=BASE,
        api_key="k",
        read_only=True,
        enabled_tools=("thruk_*",),
    )
    mcp = build_server(cfg)
    try:
        tools = {t.name for t in await mcp.list_tools()}
        assert tools.isdisjoint(WRITE_TOOLS)
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_emits_json_line_on_write(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", auth_user="alice")
    mcp = build_server(cfg)
    try:
        # caplog captures via the root logger; we need to attach to thruk_mcp.audit
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post("https://thruk.test/r/hosts/srv01/cmd/acknowledge_host_problem").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            await mcp.call_tool("thruk_acknowledge", {"host": "srv01"})
        audit_records = [r for r in caplog.records if r.name == "thruk_mcp.audit"]
        assert len(audit_records) == 1
        payload = json.loads(audit_records[0].message)
        assert payload["tool"] == "thruk_acknowledge"
        assert payload["user"] == "alice"
        assert payload["target"] == "srv01"
        assert payload["status"] == "ok"
        assert "ts" in payload
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_disabled_emits_nothing(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", audit_log=False)
    mcp = build_server(cfg)
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_forced_host_check").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            await mcp.call_tool("thruk_recheck", {"host": "srv01"})
        assert not any(r.name == "thruk_mcp.audit" for r in caplog.records)
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_records_error_status(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    # Disable retries so a single 500 mock is enough
    mcp._thruk_client.max_retries = 0  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post("https://thruk.test/r/hosts/srv01/cmd/acknowledge_host_problem").mock(
                return_value=httpx.Response(500, text="boom")
            )
            result = await mcp.call_tool("thruk_acknowledge", {"host": "srv01"})
        # ThrukError is now returned as tool-level error content (not raised),
        # so the MCP client sees the actual Thruk message instead of a generic
        # "tool execution failed" protocol error.
        assert len(result) == 1
        assert result[0].text.startswith("Error:")
        rec = next(r for r in caplog.records if r.name == "thruk_mcp.audit")
        payload = json.loads(rec.message)
        assert payload["status"] == "error"
        assert payload["error"]  # any non-empty error message
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_max_concurrent_creates_semaphore() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", max_concurrent=4)
    client = ThrukClient(cfg)
    try:
        assert client._sem is not None
        assert isinstance(client._sem, asyncio.Semaphore)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_max_concurrent_zero_means_no_semaphore() -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", max_concurrent=0)
    client = ThrukClient(cfg)
    try:
        assert client._sem is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_audit_redacts_sensitive_keys() -> None:
    """Direct unit test on the redactor."""
    out = audit._redact({"host": "srv01", "api_key": "secret", "nested": {"token": "x"}})
    assert out == {"host": "srv01", "api_key": "***", "nested": {"token": "***"}}


# ---------------------------------------------------------------------------
# Issue #70 — Path injection: host/service names must be URL-encoded
# ---------------------------------------------------------------------------

# Pre-fix behaviour (documented as a reminder of the vulnerability):
#   endpoint = f"/hosts/{host}/cmd/schedule_host_downtime"
# A host value like "srv01/cmd/del_all_host_downtimes" would produce:
#   /hosts/srv01/cmd/del_all_host_downtimes/cmd/schedule_host_downtime
# which Thruk may route to the injected sub-path, executing a different command.
# The _seg() helper (urllib.parse.quote with safe="") prevents this.


@pytest.mark.asyncio
async def test_path_injection_host_name_slash_encoded() -> None:
    """A host name containing '/' must be percent-encoded before it reaches the wire.

    Without the fix the URL would contain a literal slash, corrupting the path.
    With the fix 'srv01/evil' becomes 'srv01%2Fevil' in the path.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    injected_host = "srv01/cmd/del_all_host_downtimes"
    try:
        with respx.mock() as router:
            # The mock must match the *encoded* URL — if _seg() is applied the
            # path will contain %2F rather than a raw slash.
            route = router.post(
                f"{BASE}/r/hosts/srv01%2Fcmd%2Fdel_all_host_downtimes/cmd/schedule_host_downtime"
            ).mock(return_value=httpx.Response(200, json={"rc": 0}))
            await mcp.call_tool(
                "thruk_schedule_downtime",
                {
                    "host": injected_host,
                    "comment": "test",
                    "start_time": "now",
                    "end_time": "+1h",
                },
            )
        assert route.called, (
            "Expected the encoded URL to be hit — path injection was not prevented."
        )
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_path_injection_service_name_slash_encoded() -> None:
    """A service description containing '/' must be percent-encoded in the path."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    injected_service = "HTTP/evil"
    try:
        with respx.mock() as router:
            route = router.post(
                f"{BASE}/r/services/srv01/HTTP%2Fevil/cmd/acknowledge_svc_problem"
            ).mock(return_value=httpx.Response(200, json={"rc": 0}))
            await mcp.call_tool(
                "thruk_acknowledge",
                {"host": "srv01", "service": injected_service},
            )
        assert route.called, (
            "Expected the encoded URL to be hit — service path injection was not prevented."
        )
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_path_injection_recheck_host_encoded() -> None:
    """thruk_recheck must encode the host name in the path."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    injected_host = "srv01/../admin"
    try:
        with respx.mock() as router:
            route = router.post(
                f"{BASE}/r/hosts/srv01%2F..%2Fadmin/cmd/schedule_forced_host_check"
            ).mock(return_value=httpx.Response(200, json={"rc": 0}))
            await mcp.call_tool("thruk_recheck", {"host": injected_host})
        assert route.called, "Expected encoded URL — path traversal was not prevented."
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_path_injection_get_host_encoded() -> None:
    """thruk_get_host must encode the host name in the read path."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    injected_host = "srv01/extra"
    try:
        with respx.mock() as router:
            route = router.get(f"{BASE}/r/hosts/srv01%2Fextra").mock(
                return_value=httpx.Response(200, json={"name": "srv01/extra"})
            )
            await mcp.call_tool("thruk_get_host", {"host": injected_host})
        assert route.called, "Expected encoded URL — read-path injection was not prevented."
    finally:
        await _close(mcp)


# ---------------------------------------------------------------------------
# Issue #72 — Path traversal in thruk_query / thruk_run_background_query
# ---------------------------------------------------------------------------

# Pre-fix behaviour (documented as a reminder of the vulnerability):
#   thruk_query(path="/../../cgi-bin/cmd.cgi?cmd_typ=14", ...)
#   would reach _url() which only prepends '/' when missing, so the raw '../..'
#   segments were forwarded to Thruk's HTTP client, potentially landing outside
#   the /thruk/r/ REST prefix on internal CGI or management endpoints.
#
# The fix: _validate_rest_path() rejects any path containing '..' before any
# HTTP request is attempted.


@pytest.mark.asyncio
async def test_thruk_query_rejects_dotdot_traversal() -> None:
    """thruk_query must return an error and make NO HTTP call for '..' paths.

    Without the fix the path would be forwarded verbatim to httpx, potentially
    reaching non-REST Thruk internals (CGI layer, management endpoints).
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    traversal_path = "/../../cgi-bin/cmd.cgi"
    try:
        with respx.mock() as router:
            # No route registered: any HTTP call would raise a NoMatchFound error,
            # proving the guard fired before making the request.
            result = await mcp.call_tool("thruk_query", {"path": traversal_path})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert ".." in payload["error"]
        # Ensure nothing was sent over the wire
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_rejects_relative_path() -> None:
    """thruk_query must reject paths that do not start with '/'."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool("thruk_query", {"path": "hosts"})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_allows_valid_path() -> None:
    """thruk_query must pass through normal REST paths unchanged."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            route = router.get(f"{BASE}/r/hosts").mock(
                return_value=httpx.Response(200, json=[{"name": "srv01"}])
            )
            result = await mcp.call_tool("thruk_query", {"path": "/hosts"})
        assert route.called
        payload = json.loads(result[0].text)
        assert payload[0]["name"] == "srv01"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_run_background_query_rejects_dotdot_traversal() -> None:
    """thruk_run_background_query must also reject '..' path traversal."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    traversal_path = "/hosts/../../../etc/passwd"
    try:
        with respx.mock() as router:
            result = await mcp.call_tool("thruk_run_background_query", {"path": traversal_path})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert ".." in payload["error"]
        assert len(router.calls) == 0
    finally:
        await _close(mcp)
