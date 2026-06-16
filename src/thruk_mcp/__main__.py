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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thruk-mcp", description="Thruk MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default=None,
        help=("Transport to serve. Default: stdio, or streamable-http when --listen is given."),
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
        asyncio.run(
            _run_streamable_http(
                port,
                args.host,
                args.log_level,
                stateless=args.stateless,
                json_response=args.json_response,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
