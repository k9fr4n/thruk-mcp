"""Entry point: `python -m thruk_mcp` or `thruk-mcp` console script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mcp.server.stdio import stdio_server

from .server import build_server


async def _run_stdio(log_level: str) -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def _run_http(port: int, host: str, log_level: str) -> None:
    """Run as SSE/Streamable-HTTP server."""
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    server = build_server()
    sse = SseServerTransport("/messages/")

    async def handle_sse(request):  # type: ignore[no-untyped-def]
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())
        return Response()

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level.lower())
    await uvicorn.Server(config).serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thruk-mcp", description="Thruk MCP server.")
    parser.add_argument(
        "--listen",
        type=int,
        metavar="PORT",
        help="Run as HTTP/SSE server on this port. Default: stdio mode.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for --listen mode.")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.listen:
        asyncio.run(_run_http(args.listen, args.host, args.log_level))
    else:
        asyncio.run(_run_stdio(args.log_level))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
