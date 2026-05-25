"""v0.6: read-only mode, allowlist, audit log, rate limit."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from thruk_mcp import audit
from thruk_mcp.__main__ import _run_stdio
from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import (
    _ALLOWED_METHODS,
    _REST_PATH_PREFIXES,
    WRITE_TOOLS,
    _is_auditable_write,
    build_server,
)

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


# ---------------------------------------------------------------------------
# Issue #73 — thruk_query POST/DELETE bypasses the audit log
# ---------------------------------------------------------------------------

# Pre-fix behaviour (documented as a reminder of the vulnerability):
#   thruk_query(path="/downtimes/42", method="DELETE") would execute the
#   deletion successfully but emit NO audit record, silently bypassing
#   the compliance mechanism. The audit gate checked `name in WRITE_TOOLS`
#   and "thruk_query" was absent from that set.
#
# The fix: _is_auditable_write() additionally checks the `method` argument
# when name == "thruk_query", treating any non-GET/HEAD method as a write.


def test_is_auditable_write_returns_false_for_get_thruk_query() -> None:
    """GET calls via thruk_query must NOT trigger the audit log."""
    assert not _is_auditable_write("thruk_query", {"method": "GET", "path": "/hosts"})
    assert not _is_auditable_write("thruk_query", {"path": "/hosts"})  # default GET
    assert not _is_auditable_write("thruk_query", {"method": "get", "path": "/hosts"})  # ci


def test_is_auditable_write_returns_true_for_post_delete_thruk_query() -> None:
    """POST/PUT/DELETE/PATCH calls via thruk_query must be flagged as writes."""
    for method in ("POST", "PUT", "DELETE", "PATCH", "post", "delete"):
        assert _is_auditable_write("thruk_query", {"method": method, "path": "/x"}), method
    assert not _is_auditable_write("thruk_query", {"method": "HEAD", "path": "/x"})


def test_is_auditable_write_returns_true_for_write_tools() -> None:
    """Existing WRITE_TOOLS must still be flagged regardless of arguments."""
    for tool in WRITE_TOOLS:
        assert _is_auditable_write(tool, {}), tool


def test_is_auditable_write_returns_false_for_read_tools() -> None:
    """Read-only tools must never be flagged."""
    assert not _is_auditable_write("thruk_list_hosts", {})
    assert not _is_auditable_write("thruk_get_host", {"host": "srv01"})
    assert not _is_auditable_write("thruk_stats", {})


@pytest.mark.asyncio
async def test_audit_log_fires_for_thruk_query_post(caplog) -> None:
    """A POST via thruk_query must be recorded in the audit log.

    Without the fix, `name in WRITE_TOOLS` was False for thruk_query and
    audit.log_call() was never reached — the write left no trace.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k", auth_user="bob")
    mcp = build_server(cfg)
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post(f"{BASE}/r/downtimes/42").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            await mcp.call_tool(
                "thruk_query", {"path": "/downtimes/42", "method": "POST", "data": {"x": "y"}}
            )
        audit_records = [r for r in caplog.records if r.name == "thruk_mcp.audit"]
        assert len(audit_records) == 1, "Expected exactly one audit record for POST thruk_query"
        payload = json.loads(audit_records[0].message)
        assert payload["tool"] == "thruk_query"
        assert payload["user"] == "bob"
        assert payload["status"] == "ok"
        assert "ts" in payload
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_fires_for_thruk_query_delete(caplog) -> None:
    """A DELETE via thruk_query must be recorded in the audit log."""
    cfg = ThrukConfig(base_url=BASE, api_key="k", auth_user="carol")
    mcp = build_server(cfg)
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.delete(f"{BASE}/r/downtimes/99").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            await mcp.call_tool("thruk_query", {"path": "/downtimes/99", "method": "DELETE"})
        audit_records = [r for r in caplog.records if r.name == "thruk_mcp.audit"]
        assert len(audit_records) == 1, "Expected exactly one audit record for DELETE thruk_query"
        payload = json.loads(audit_records[0].message)
        assert payload["tool"] == "thruk_query"
        assert payload["user"] == "carol"
        assert payload["status"] == "ok"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_not_fired_for_thruk_query_get(caplog) -> None:
    """A GET via thruk_query must NOT appear in the audit log."""
    cfg = ThrukConfig(base_url=BASE, api_key="k", auth_user="dave")
    mcp = build_server(cfg)
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.get(f"{BASE}/r/hosts").mock(
                return_value=httpx.Response(200, json=[{"name": "srv01"}])
            )
            await mcp.call_tool("thruk_query", {"path": "/hosts"})
        audit_records = [r for r in caplog.records if r.name == "thruk_mcp.audit"]
        assert len(audit_records) == 0, "GET thruk_query must not produce an audit record"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_audit_log_fires_for_thruk_query_post_error(caplog) -> None:
    """A failed POST via thruk_query must still be recorded with status=error."""
    cfg = ThrukConfig(base_url=BASE, api_key="k", auth_user="eve")
    mcp = build_server(cfg)
    mcp._thruk_client.max_retries = 0  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.INFO, logger="thruk_mcp.audit"), respx.mock() as router:
            router.post(f"{BASE}/r/hosts/srv01/cmd/do_something").mock(
                return_value=httpx.Response(500, text="server error")
            )
            result = await mcp.call_tool(
                "thruk_query",
                {"path": "/hosts/srv01/cmd/do_something", "method": "POST"},
            )
        assert result[0].text.startswith("Error:")
        audit_records = [r for r in caplog.records if r.name == "thruk_mcp.audit"]
        assert len(audit_records) == 1, "Expected error audit record for failed POST thruk_query"
        payload = json.loads(audit_records[0].message)
        assert payload["status"] == "error"
        assert payload["tool"] == "thruk_query"
        assert payload["error"]
    finally:
        await _close(mcp)


# ---------------------------------------------------------------------------
# Issue #74 — No warning emitted at startup when THRUK_VERIFY_SSL=false
# ---------------------------------------------------------------------------

# Pre-fix behaviour (documented as a reminder of the vulnerability):
#   build_server() would silently create an httpx.AsyncClient with verify=False
#   when THRUK_VERIFY_SSL=false. Neither _run_stdio() nor _run_http() emitted
#   any warning, making this dangerous configuration invisible to operators.
#
# The fix: both entry points call log.warning(_SSL_WARNING) immediately after
# build_server() when server._cfg.verify_ssl is False.


@pytest.mark.asyncio
async def test_ssl_warning_emitted_in_stdio_when_verify_ssl_false(caplog) -> None:
    """_run_stdio must log a security warning when verify_ssl=False.

    Without the fix the warning was never emitted and operators had no
    indication that TLS verification had been silently disabled.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k", verify_ssl=False)

    # Patch build_server to inject our insecure config, and stdio_server to
    # avoid blocking on actual stdio streams.
    read_mock = AsyncMock()
    write_mock = AsyncMock()

    class _FakeCtx:
        async def __aenter__(self):
            return (read_mock, write_mock)

        async def __aexit__(self, *_):
            pass

    fake_server = build_server(cfg)
    fake_server.run = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("thruk_mcp.__main__.build_server", return_value=fake_server),
        patch("thruk_mcp.__main__.stdio_server", return_value=_FakeCtx()),
        caplog.at_level(logging.WARNING, logger="thruk_mcp.__main__"),
    ):
        await _run_stdio("INFO")

    warning_records = [
        r for r in caplog.records if r.name == "thruk_mcp.__main__" and r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "THRUK_VERIFY_SSL=false" in warning_records[0].message
    assert "MITM" in warning_records[0].message


@pytest.mark.asyncio
async def test_no_ssl_warning_in_stdio_when_verify_ssl_true(caplog) -> None:
    """_run_stdio must NOT log a security warning when verify_ssl=True (default)."""
    cfg = ThrukConfig(base_url=BASE, api_key="k", verify_ssl=True)

    read_mock = AsyncMock()
    write_mock = AsyncMock()

    class _FakeCtx:
        async def __aenter__(self):
            return (read_mock, write_mock)

        async def __aexit__(self, *_):
            pass

    fake_server = build_server(cfg)
    fake_server.run = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("thruk_mcp.__main__.build_server", return_value=fake_server),
        patch("thruk_mcp.__main__.stdio_server", return_value=_FakeCtx()),
        caplog.at_level(logging.WARNING, logger="thruk_mcp.__main__"),
    ):
        await _run_stdio("INFO")

    warning_records = [
        r for r in caplog.records if r.name == "thruk_mcp.__main__" and r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 0, "No SSL warning expected when verify_ssl=True"


# ---------------------------------------------------------------------------
# Issue #123 — thruk_query: validate HTTP method and restrict path to prefix
# ---------------------------------------------------------------------------

# Pre-fix behaviour (documented as a reminder of the vulnerability):
#   thruk_query(path="/cgi-bin/cmd.cgi", method="TRACE") would forward the
#   request verbatim to httpx: TRACE can leak Authorization headers (HTTP TRACE
#   attack) and /cgi-bin/cmd.cgi bypasses Thruk's REST authentication layer,
#   potentially reaching CGI management endpoints directly.
#
# The fix:
#   1. _ALLOWED_METHODS allowlist rejects any verb not in
#      {GET, HEAD, POST, PUT, PATCH, DELETE} before any I/O.
#   2. _validate_rest_path() gains a third guard: the path must start with one
#      of the entries in _REST_PATH_PREFIXES (the known Thruk REST collections).


def test_allowed_methods_constant_contains_standard_verbs() -> None:
    """_ALLOWED_METHODS must include all standard REST verbs and exclude dangerous ones."""
    assert {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"} == _ALLOWED_METHODS
    assert "TRACE" not in _ALLOWED_METHODS
    assert "CONNECT" not in _ALLOWED_METHODS
    assert "OPTIONS" not in _ALLOWED_METHODS


def test_rest_path_prefixes_includes_core_resources() -> None:
    """_REST_PATH_PREFIXES must cover the core Thruk REST collections."""
    for prefix in ("/hosts", "/services", "/downtimes", "/comments", "/logs", "/sites"):
        assert prefix in _REST_PATH_PREFIXES, f"Missing expected prefix: {prefix}"


@pytest.mark.asyncio
async def test_thruk_query_rejects_trace_method() -> None:
    """thruk_query must return an error and make NO HTTP call for TRACE method.

    Without the fix, method.upper() == 'TRACE' was forwarded to httpx, which
    could leak Authorization headers to a Thruk endpoint (HTTP TRACE attack).
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool("thruk_query", {"path": "/hosts", "method": "TRACE"})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "TRACE" in payload["error"]
        assert "Allowed" in payload["error"]
        assert len(router.calls) == 0, "No HTTP call must be made for a rejected method"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_rejects_connect_method() -> None:
    """thruk_query must reject the CONNECT verb (proxy tunnelling, no REST use-case)."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool("thruk_query", {"path": "/hosts", "method": "CONNECT"})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_rejects_invented_method() -> None:
    """thruk_query must reject completely invented HTTP method strings."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool("thruk_query", {"path": "/hosts", "method": "HAXVERB"})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_rejects_cgi_path() -> None:
    """thruk_query must reject paths outside the known REST prefixes.

    Without the fix, a path like /cgi-bin/cmd.cgi would be forwarded to httpx
    after passing the only two guards (starts-with-/ and no-..), potentially
    bypassing Thruk's REST authentication layer and reaching the CGI layer.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool(
                "thruk_query", {"path": "/cgi-bin/cmd.cgi", "method": "GET"}
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "cgi-bin" in payload["error"] or "prefix" in payload["error"].lower()
        assert len(router.calls) == 0, "No HTTP call must be made for a rejected path"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_rejects_unknown_path_prefix() -> None:
    """Any path not starting with a known REST prefix must be rejected."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool(
                "thruk_query", {"path": "/internal/reset", "method": "POST"}
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_allows_all_valid_methods_on_valid_path() -> None:
    """All methods in _ALLOWED_METHODS must be accepted on a valid path."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        for verb in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            with respx.mock() as router:
                # Register a catch-all for the verb so respx does not raise NoMatchFound.
                router.route(method=verb, url__regex=r".*/r/hosts.*").mock(
                    return_value=httpx.Response(200, json=[])
                )
                result = await mcp.call_tool("thruk_query", {"path": "/hosts", "method": verb})
            payload = json.loads(result[0].text)
            assert "error" not in payload, f"Valid method {verb!r} was incorrectly rejected"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_run_background_query_rejects_trace_method() -> None:
    """thruk_run_background_query must also reject disallowed HTTP methods."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool(
                "thruk_run_background_query", {"path": "/hosts", "method": "TRACE"}
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "TRACE" in payload["error"]
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_run_background_query_rejects_unknown_path_prefix() -> None:
    """thruk_run_background_query must also reject paths outside the REST prefix."""
    cfg = ThrukConfig(base_url=BASE, api_key="k")
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool(
                "thruk_run_background_query", {"path": "/admin/reset", "method": "POST"}
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


# ---------------------------------------------------------------------------
# Issue #138 — thruk_query must block non-GET/HEAD methods in read-only mode
# ---------------------------------------------------------------------------

# Pre-fix behaviour (the vulnerability):
#   thruk_query accepted any method in _ALLOWED_METHODS even when read_only=True,
#   because it was kept out of WRITE_TOOLS intentionally (it also handles GET).
#   An LLM could therefore call thruk_query(method="POST", path="/hosts/srv01/cmd/...")
#   to mutate monitoring state while THRUK_READ_ONLY=true, completely bypassing
#   the WRITE_TOOLS allowlist.  The fix adds an explicit guard inside the function.


@pytest.mark.asyncio
async def test_thruk_query_read_only_blocks_post() -> None:
    """thruk_query with method=POST must be rejected when THRUK_READ_ONLY=true.

    This is the primary regression test for issue #138.
    Without the fix, a POST call would be forwarded to Thruk despite read_only=True.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=True)
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool(
                "thruk_query",
                {"path": "/hosts/srv01/cmd/acknowledge_host_problem", "method": "POST"},
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "THRUK_READ_ONLY" in payload["error"]
        assert "POST" in payload["error"]
        assert len(router.calls) == 0, "No HTTP request must be made when blocked by read-only"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_read_only_blocks_delete() -> None:
    """thruk_query with method=DELETE must be rejected when THRUK_READ_ONLY=true."""
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=True)
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            result = await mcp.call_tool(
                "thruk_query",
                {"path": "/downtimes/42", "method": "DELETE"},
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "THRUK_READ_ONLY" in payload["error"]
        assert len(router.calls) == 0
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_read_only_allows_get() -> None:
    """thruk_query with method=GET must still work when THRUK_READ_ONLY=true.

    The whole point of keeping thruk_query available in read-only mode is to
    allow read operations — this test verifies GET is not blocked.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=True)
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            route = router.route(method="GET", url__regex=r".*/r/hosts$").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = await mcp.call_tool("thruk_query", {"path": "/hosts", "method": "GET"})
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" not in payload, f"GET should not be blocked in read-only mode: {payload}"
        assert route.called, "GET must be forwarded to Thruk even in read-only mode"
    finally:
        await _close(mcp)


@pytest.mark.asyncio
async def test_thruk_query_non_read_only_allows_post() -> None:
    """thruk_query with method=POST must still work when THRUK_READ_ONLY=false (default)."""
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=False)
    mcp = build_server(cfg)
    try:
        with respx.mock() as router:
            router.post(f"{BASE}/r/hosts/srv01/cmd/acknowledge_host_problem").mock(
                return_value=httpx.Response(200, json={"rc": 0})
            )
            result = await mcp.call_tool(
                "thruk_query",
                {"path": "/hosts/srv01/cmd/acknowledge_host_problem", "method": "POST"},
            )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" not in payload
    finally:
        await _close(mcp)


# ---------------------------------------------------------------------------
# Issue #143 — ContextVar isolation: two build_server() calls must not
# clobber each other's ThrukClient reference.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_build_server_calls_independent_clients() -> None:
    """Two build_server() calls in the same process must each use their own client.

    Bug (before fix): build_server() set a module-level ``_client`` global.
    The second call would silently overwrite it, so the first server would
    start routing requests through the second server's ThrukClient.

    Fix: _client_var (ContextVar) is set per call; each ThrukMCPServer stores
    its own client in ``._thruk_client`` and tools read ``_client_var.get()``
    which reflects the last set() in the current context.

    This test verifies that both servers hold distinct ThrukClient instances
    and that the module-level ContextVar was updated by each build_server() call.
    """
    from thruk_mcp.server import _client_var

    cfg1 = ThrukConfig(base_url="https://thruk-a.test", api_key="key-a")
    cfg2 = ThrukConfig(base_url="https://thruk-b.test", api_key="key-b")

    mcp1 = build_server(cfg1)
    mcp2 = build_server(cfg2)

    try:
        client1 = mcp1._thruk_client  # type: ignore[attr-defined]
        client2 = mcp2._thruk_client  # type: ignore[attr-defined]

        # Each server must hold a distinct client instance.
        assert client1 is not client2, (
            "build_server() must create independent ThrukClient instances"
        )

        # Each client must point to its own base URL (no clobber).
        assert str(client1.config.base_url) == "https://thruk-a.test"
        assert str(client2.config.base_url) == "https://thruk-b.test"

        # The ContextVar holds the most-recently set client (last writer wins
        # in a flat context — that is the expected behaviour for the main-thread
        # single-server use case).
        assert _client_var.get() is client2
    finally:
        await client1.aclose()
        await client2.aclose()


@pytest.mark.asyncio
async def test_thruk_run_background_query_read_only_blocks_post() -> None:
    """thruk_run_background_query with method=POST must be blocked when THRUK_READ_ONLY=true.

    thruk_run_background_query is already removed from the tool registry in read-only mode
    (is_write=True), but the function body guard provides defense-in-depth.
    """
    cfg = ThrukConfig(base_url=BASE, api_key="k", read_only=True)
    mcp = build_server(cfg)
    try:
        # Call the underlying function directly to test the in-body guard
        # (the tool is stripped from the registry in read-only mode)
        from thruk_mcp.server import thruk_run_background_query

        with respx.mock() as router:
            raw = await thruk_run_background_query(
                path="/hosts/srv01/cmd/schedule_host_check",
                method="POST",
            )
        payload = json.loads(raw)
        assert "error" in payload
        assert "THRUK_READ_ONLY" in payload["error"]
        assert len(router.calls) == 0
    finally:
        await _close(mcp)
