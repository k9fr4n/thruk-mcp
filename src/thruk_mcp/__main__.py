"""Entry point: `python -m thruk_mcp` or `thruk-mcp` console script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

from mcp.server.stdio import stdio_server

from .config import HttpAuthConfig, _envbool, _raw_env
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


def _build_streamable_app(
    server: Any,
    *,
    stateless: bool,
    json_response: bool,
    header_auth: bool = False,
    http_auth: HttpAuthConfig | None = None,
) -> Starlette:
    """Build the Starlette app exposing the Streamable-HTTP endpoint at /mcp.

    When ``header_auth`` is set, the /mcp mount is wrapped in
    :class:`HeaderAuthMiddleware` so each request's ``X-Thruk-*`` headers select
    the tenant credentials, and the per-tenant client cache is closed on shutdown.

    When ``http_auth`` is given, the app is fronted by two transport-level
    middlewares (outermost first): ``TrustedHostMiddleware`` (anti-DNS-rebinding)
    then :class:`BearerAuthMiddleware`. They run *before* header-auth, so the
    final chain is TrustedHost → Bearer → HeaderAuth → handle_mcp.
    """
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

    mount_app: Any = handle_mcp
    cache = None
    if header_auth:
        from .multitenant import ClientCache, HeaderAuthMiddleware

        cache = ClientCache()
        mount_app = HeaderAuthMiddleware(handle_mcp, base_cfg=server._cfg, cache=cache)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> Any:
        async with session_manager.run():
            try:
                yield
            finally:
                if cache is not None:
                    await cache.aclose_all()

    middleware = []
    if http_auth is not None:
        from starlette.middleware import Middleware
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        from .httpauth import BearerAuthMiddleware

        middleware = [
            Middleware(TrustedHostMiddleware, allowed_hosts=list(http_auth.allowed_hosts)),
            Middleware(BearerAuthMiddleware, token=http_auth.token),
        ]

    return Starlette(
        routes=[Mount("/mcp", app=mount_app)], middleware=middleware, lifespan=lifespan
    )


async def _serve(app: Starlette, host: str, port: int, log_level: str) -> None:
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level.lower())
    await uvicorn.Server(config).serve()


async def _run_streamable_http(
    port: int,
    host: str,
    log_level: str,
    *,
    stateless: bool,
    json_response: bool,
    header_auth: bool = False,
) -> None:
    """Run as a Streamable-HTTP server (endpoint /mcp)."""
    # In header-auth mode the server boots without THRUK_API_KEY; each request
    # brings its own via headers. The base config still supplies the shared,
    # server-owned knobs (read_only, enabled_tools, audit_log, pools).
    server = build_server(require_api_key=not header_auth)
    if not server._cfg.verify_ssl:
        log.warning(_SSL_WARNING)
    if header_auth:
        log.warning(
            "header-auth mode: Thruk credentials are read from per-request headers. "
            "Serve only over TLS — credentials travel in X-Thruk-Auth-Key."
        )
    http_auth = HttpAuthConfig.from_env()
    if http_auth.token is None and http_auth.allow_unauthenticated:
        log.warning(
            "HTTP transport is running WITHOUT bearer authentication "
            "(MCP_HTTP_ALLOW_UNAUTHENTICATED=true). Ensure a TLS + auth reverse proxy "
            "sits in front of /mcp before exposing it."
        )
    app = _build_streamable_app(
        server,
        stateless=stateless,
        json_response=json_response,
        header_auth=header_auth,
        http_auth=http_auth,
    )
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
        "--header-auth",
        action="store_true",
        default=_envbool("THRUK_HTTP_HEADER_AUTH", False),
        help=(
            "Multi-tenant Streamable-HTTP: take Thruk credentials per request from "
            "X-Thruk-Auth-Key / X-Thruk-Base-Url / X-Thruk-Auth-User / X-Thruk-Backends "
            "headers instead of THRUK_API_KEY. Requires --stateless. TLS is mandatory "
            "(credentials travel in headers). Env: THRUK_HTTP_HEADER_AUTH=1."
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

    if args.header_auth:
        if transport != "streamable-http":
            parser.error("--header-auth requires --transport streamable-http")
        if not args.stateless:
            # Headers are only reliably present per request in stateless mode;
            # in stateful mode they bind once at session init (see plan/issue).
            parser.error("--header-auth requires --stateless")

    if transport == "streamable-http":
        # Fail closed: serving over HTTP exposes token-bearing monitoring tools
        # (and, in header-auth mode, an open credential-relay endpoint) on the
        # network. Require a bearer token unless the operator explicitly opts out
        # (e.g. when fronting the server with their own auth proxy).
        if _raw_env("MCP_HTTP_TOKEN") is None and not _envbool(
            "MCP_HTTP_ALLOW_UNAUTHENTICATED", False
        ):
            parser.error(
                "HTTP transport requires MCP_HTTP_TOKEN to be set (clients must send "
                "'Authorization: Bearer <token>'). To run unauthenticated behind your own "
                "auth proxy, set MCP_HTTP_ALLOW_UNAUTHENTICATED=true."
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
                header_auth=args.header_auth,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
