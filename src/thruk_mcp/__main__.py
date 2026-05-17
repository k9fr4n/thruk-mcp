"""Entry point: `python -m thruk_mcp` or `thruk-mcp` console script."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thruk-mcp", description="Thruk MCP server.")
    parser.add_argument(
        "--listen",
        type=int,
        metavar="PORT",
        help="Run as HTTP (Streamable-HTTP) server on this port. Default: stdio mode.",
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

    mcp = build_server()

    if args.listen:
        uvicorn.run(
            mcp.streamable_http_app,
            host=args.host,
            port=args.listen,
            log_level=args.log_level.lower(),
        )
    else:
        mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
