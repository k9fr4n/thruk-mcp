"""Entry point: `python -m thruk_mcp` or `thruk-mcp` console script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

from mcp.server.stdio import stdio_server

from .server import build_server

if TYPE_CHECKING:
    from starlette.applications import Starlette

log = logging.getLogger(__name__)

DEFAULT_HTTP_PORT = 8001

_SSL_WARNING = (
    "SECURITY WARNING: THRUK_VERIFY_SSL=false — TLS certificate verification is DISABLED. "
    "All HTTPS connections to Thruk are vulnerable to MITM attacks. "
    "Set THRUK_VERIFY_SSL=true (or remove the variable) for production use."
)

_SSE_DEPRECATION = (
    "DEPRECATION: the SSE transport (/sse) is deprecated in the MCP spec "
    "(superseded by Streamable HTTP since revision 2025-03-26). "
    "Prefer --transport streamable-http (endpoint /mcp). "
    "SSE support may be removed in a future release."
)


async def _run_stdio(log_level: str) -> None:
    server = build_server()
    if not server._cfg.verify_ssl:
        log.warning(_SSL_WARNING)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _build_streamable_app(server: Any, *, stateless: bool, json_response: bool) -> Starlette:
    """Build the Starlette app exposing the Streamable-HTTP endpoint at /mcp."""
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=json_response,
        stateless=stateless,
    )

    async def handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> Any:
        async with session_manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)


def _build_sse_app(server: Any) -> Starlette:
    """Build the Starlette app for the (deprecated) SSE transport."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Any) -> Response:  # starlette Request — avoid heavy import
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
        return Response()

    return Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )


async def _serve(app: Starlette, host: str, port: int, log_level: str) -> None:
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level.lower())
    await uvicorn.Server(config).serve()


async def _run_streamable_http(
    port: int, host: str, log_level: str, *, stateless: bool, json_response: bool
) -> None:
    """Run as a Streamable-HTTP server (endpoint /mcp)."""
    server = build_server()
    if not server._cfg.verify_ssl:
        log.warning(_SSL_WARNING)
    app = _build_streamable_app(server, stateless=stateless, json_response=json_response)
    await _serve(app, host, port, log_level)


async def _run_sse(port: int, host: str, log_level: str) -> None:
    """Run as a (deprecated) SSE server (endpoints /sse + /messages/)."""
    server = build_server()
    if not server._cfg.verify_ssl:
        log.warning(_SSL_WARNING)
    log.warning(_SSE_DEPRECATION)
    app = _build_sse_app(server)
    await _serve(app, host, port, log_level)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thruk-mcp", description="Thruk MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=None,
        help=(
            "Transport to serve. Default: stdio, or streamable-http when --listen "
            "is given. 'sse' is deprecated."
        ),
    )
    parser.add_argument(
        "--listen",
        type=int,
        metavar="PORT",
        help=(
            f"Run as an HTTP server on this port (implies --transport streamable-http "
            f"unless overridden). Default HTTP port: {DEFAULT_HTTP_PORT}."
        ),
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for HTTP transports.")
    parser.add_argument(
        "--stateless",
        action="store_true",
        help=(
            "Streamable-HTTP only: keep no per-session state. Recommended behind a load "
            "balancer / with multiple replicas (no sticky sessions required)."
        ),
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        help=(
            "Streamable-HTTP only: reply with a single JSON response instead of an "
            "SSE stream. Simpler for plain request/response clients."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    transport = args.transport
    if transport is None:
        transport = "streamable-http" if args.listen is not None else "stdio"

    if (args.stateless or args.json_response) and transport != "streamable-http":
        parser.error(
            "--stateless / --json-response are only valid with --transport streamable-http"
        )

    if transport == "stdio":
        if args.listen is not None:
            parser.error("--listen is not valid with --transport stdio")
        asyncio.run(_run_stdio(args.log_level))
    else:
        port = args.listen if args.listen is not None else DEFAULT_HTTP_PORT
        if transport == "streamable-http":
            asyncio.run(
                _run_streamable_http(
                    port,
                    args.host,
                    args.log_level,
                    stateless=args.stateless,
                    json_response=args.json_response,
                )
            )
        else:  # sse
            asyncio.run(_run_sse(port, args.host, args.log_level))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
