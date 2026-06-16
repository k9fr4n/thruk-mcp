"""Transport selection + HTTP app wiring (stdio / streamable-http / sse).

Covers the CLI routing in ``main()`` and the Starlette app builders, without
ever binding a socket: ``_serve`` (uvicorn) and the run helpers are patched.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from thruk_mcp import __main__ as m
from thruk_mcp.config import ThrukConfig
from thruk_mcp.server import build_server

BASE = "https://thruk.test"


def _route_paths(app) -> list[str]:
    return [getattr(r, "path", None) for r in app.routes]


# ---------------------------------------------------------------------------
# main() — transport selection
# ---------------------------------------------------------------------------


def _dispatch(argv):
    """Run main() with all run helpers + asyncio.run patched out.

    Returns the three run-helper mocks: (stdio, streamable_http, sse).
    """
    # The run helpers are async, so patch() replaces them with AsyncMocks that
    # return un-awaited coroutines. asyncio.run is stubbed to close that
    # coroutine (instead of running it), keeping the test off the event loop and
    # silencing "coroutine was never awaited" warnings.
    with (
        patch.object(m.asyncio, "run", side_effect=lambda coro: coro.close()),
        patch.object(m, "_run_stdio") as stdio,
        patch.object(m, "_run_streamable_http") as shttp,
        patch.object(m, "_run_sse") as sse,
    ):
        rc = m.main(argv)
    assert rc == 0
    return stdio, shttp, sse


def test_no_args_runs_stdio() -> None:
    stdio, shttp, sse = _dispatch([])
    stdio.assert_called_once()
    shttp.assert_not_called()
    sse.assert_not_called()


def test_transport_stdio_explicit() -> None:
    stdio, shttp, sse = _dispatch(["--transport", "stdio"])
    stdio.assert_called_once()
    shttp.assert_not_called()
    sse.assert_not_called()


def test_listen_alone_promotes_to_streamable_http() -> None:
    """--listen without --transport now serves Streamable-HTTP (was SSE)."""
    stdio, shttp, sse = _dispatch(["--listen", "8001"])
    stdio.assert_not_called()
    sse.assert_not_called()
    shttp.assert_called_once_with(8001, "0.0.0.0", "INFO", stateless=False, json_response=False)


def test_streamable_http_default_port() -> None:
    _stdio, shttp, _sse = _dispatch(["--transport", "streamable-http"])
    shttp.assert_called_once_with(
        m.DEFAULT_HTTP_PORT, "0.0.0.0", "INFO", stateless=False, json_response=False
    )


def test_streamable_http_flags_and_host() -> None:
    _stdio, shttp, _sse = _dispatch(
        [
            "--transport",
            "streamable-http",
            "--listen",
            "9000",
            "--host",
            "127.0.0.1",
            "--stateless",
            "--json-response",
        ]
    )
    shttp.assert_called_once_with(9000, "127.0.0.1", "INFO", stateless=True, json_response=True)


def test_transport_sse_default_port() -> None:
    _stdio, _shttp, sse = _dispatch(["--transport", "sse"])
    sse.assert_called_once_with(m.DEFAULT_HTTP_PORT, "0.0.0.0", "INFO")


def test_transport_sse_with_listen() -> None:
    _stdio, _shttp, sse = _dispatch(["--transport", "sse", "--listen", "9000"])
    sse.assert_called_once_with(9000, "0.0.0.0", "INFO")


def test_listen_with_stdio_is_error() -> None:
    with pytest.raises(SystemExit):
        m.main(["--transport", "stdio", "--listen", "8001"])


@pytest.mark.parametrize("flag", ["--stateless", "--json-response"])
def test_streamable_flags_rejected_for_sse(flag) -> None:
    with pytest.raises(SystemExit):
        m.main(["--transport", "sse", flag])


@pytest.mark.parametrize("flag", ["--stateless", "--json-response"])
def test_streamable_flags_rejected_for_stdio(flag) -> None:
    with pytest.raises(SystemExit):
        m.main([flag])  # stdio is the implicit default


# ---------------------------------------------------------------------------
# App builders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stateless", "json_response"),
    [(False, False), (True, True)],
)
def test_build_streamable_app_mounts_mcp(stateless, json_response) -> None:
    with patch("mcp.server.streamable_http_manager.StreamableHTTPSessionManager") as Manager:
        sentinel_server = object()
        app = m._build_streamable_app(
            sentinel_server, stateless=stateless, json_response=json_response
        )
    Manager.assert_called_once_with(
        app=sentinel_server, json_response=json_response, stateless=stateless
    )
    assert "/mcp" in _route_paths(app)


def test_build_sse_app_has_sse_and_messages_routes() -> None:
    app = m._build_sse_app(object())
    paths = _route_paths(app)
    assert "/sse" in paths
    # Mount path is normalised without the trailing slash.
    assert "/messages" in paths


# ---------------------------------------------------------------------------
# Run helpers — SSL + deprecation warnings (no socket bound)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_streamable_http_passes_flags_and_serves(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", verify_ssl=False)
    fake_server = build_server(cfg)
    fake_app = object()

    with (
        patch("thruk_mcp.__main__.build_server", return_value=fake_server),
        patch("thruk_mcp.__main__._build_streamable_app", return_value=fake_app) as build_app,
        patch("thruk_mcp.__main__._serve", new=AsyncMock()) as serve,
        caplog.at_level(logging.WARNING, logger="thruk_mcp.__main__"),
    ):
        await m._run_streamable_http(8001, "0.0.0.0", "INFO", stateless=True, json_response=True)

    build_app.assert_called_once_with(fake_server, stateless=True, json_response=True)
    serve.assert_awaited_once_with(fake_app, "0.0.0.0", 8001, "INFO")
    # verify_ssl=False must surface the SSL warning.
    msgs = [r.message for r in caplog.records]
    assert any("THRUK_VERIFY_SSL=false" in msg for msg in msgs)
    await fake_server._thruk_client.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_sse_logs_deprecation(caplog) -> None:
    cfg = ThrukConfig(base_url=BASE, api_key="k", verify_ssl=True)
    fake_server = build_server(cfg)

    with (
        patch("thruk_mcp.__main__.build_server", return_value=fake_server),
        patch("thruk_mcp.__main__._build_sse_app", return_value=object()) as build_app,
        patch("thruk_mcp.__main__._serve", new=AsyncMock()) as serve,
        caplog.at_level(logging.WARNING, logger="thruk_mcp.__main__"),
    ):
        await m._run_sse(8001, "0.0.0.0", "INFO")

    build_app.assert_called_once_with(fake_server)
    serve.assert_awaited_once()
    msgs = [r.message for r in caplog.records]
    assert any("deprecated" in msg.lower() for msg in msgs)
    await fake_server._thruk_client.aclose()  # type: ignore[attr-defined]
