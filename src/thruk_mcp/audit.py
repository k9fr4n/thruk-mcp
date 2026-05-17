"""Structured audit logging for write operations.

Every successful invocation of a WRITE tool emits one JSON line on the
`thruk_mcp.audit` logger. Default destination is stderr, which makes it
trivial to ship to Loki / Cloudwatch / journald via the container runtime.

The payload contains:
- ts:      ISO-8601 timestamp (UTC, second precision)
- tool:    MCP tool name
- user:    auth_user from ThrukConfig (server-level operator identity)
- args:    redacted call arguments (sensitive keys stripped)
- target:  best-effort "host/service" reconstruction for grep-ability
- status:  "ok" | "error"
- error:   exception class (only when status=error)
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from functools import wraps
from typing import Any

log = logging.getLogger("thruk_mcp.audit")

# Sensitive keys we never log (none today, but cheap to enumerate for safety).
_REDACT: frozenset[str] = frozenset({"api_key", "password", "token"})


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k in _REDACT else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _target(args: dict[str, Any]) -> str | None:
    host = args.get("host")
    service = args.get("service")
    hg = args.get("hostgroup")
    sg = args.get("servicegroup")
    if host and service:
        return f"{host}/{service}"
    if host:
        return host
    if hg:
        return f"hostgroup:{hg}"
    if sg:
        return f"servicegroup:{sg}"
    return None


def configure(enabled: bool = True) -> None:
    """Idempotently configure the audit logger to emit one JSON line per record
    on stderr. Calling with enabled=False silences it."""
    log.handlers.clear()
    if not enabled:
        log.addHandler(logging.NullHandler())
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    # propagate=True so testing harnesses like pytest's caplog can intercept.
    # The Python root logger is unconfigured by default, so this does not
    # cause duplicate output in production.
    log.propagate = True


def audited(
    tool_name: str, user: str = ""
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator: wrap an async tool function with audit logging."""

    def deco(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            record = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": tool_name,
                "user": user,
                "args": _redact(kwargs),
                "target": _target(kwargs),
            }
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                record["status"] = "error"
                record["error"] = f"{type(exc).__name__}: {exc}"
                log.info(json.dumps(record, default=str))
                raise
            record["status"] = "ok"
            log.info(json.dumps(record, default=str))
            return result

        return wrapper

    return deco
