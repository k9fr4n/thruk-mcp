"""Header-auth multi-tenant support for the Streamable-HTTP transport.

In header-auth mode the server boots without a fixed ``THRUK_API_KEY`` and each
HTTP request supplies its own credentials via ``X-Thruk-*`` headers. A pure-ASGI
middleware derives a per-request :class:`ThrukConfig`, binds a (cached) per-tenant
:class:`ThrukClient` into the context vars that every tool resolves through, then
delegates to the app — so the existing context-var plumbing (issue #143) carries
the right tenant all the way to the tool coroutine.

Why a *pure* ASGI middleware (not Starlette's ``BaseHTTPMiddleware``): the latter
runs the downstream app in a separate anyio task, which severs context-var
propagation. A pure middleware sets the vars and awaits the app in the **same**
task, so the value is captured by ``copy_context()`` when the stateless session
manager starts its per-request server task.

Security model: only credential / endpoint fields come from headers. The server
owns ``read_only``, ``enabled_tools`` and ``audit_log`` (see
:meth:`ThrukConfig.from_headers`) — a tenant cannot escalate privileges or
silence the audit log. The API key is never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from .client import ThrukClient
from .config import ThrukConfig
from .helpers import _cfg_var, _client_var

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

log = logging.getLogger(__name__)

# Default upper bound on the number of distinct tenant clients kept alive. Each
# entry owns an httpx connection pool; the LRU evicts (and closes) the least
# recently used beyond this. Tunable via THRUK_CLIENT_CACHE_SIZE.
DEFAULT_CACHE_SIZE = 128


def _client_key(cfg: ThrukConfig) -> tuple[Any, ...]:
    """Identity of the httpx client a config needs — fields that change behaviour."""
    return (
        cfg.base_url,
        cfg.api_key,
        cfg.auth_user,
        cfg.verify_ssl,
        cfg.timeout,
        cfg.default_backends,
        cfg.max_connections,
        cfg.max_keepalive_connections,
    )


class ClientCache:
    """Bounded LRU cache of per-tenant ``ThrukClient`` instances (async-safe)."""

    def __init__(self, maxsize: int = DEFAULT_CACHE_SIZE) -> None:
        self._maxsize = max(1, maxsize)
        self._clients: OrderedDict[tuple[Any, ...], ThrukClient] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, cfg: ThrukConfig) -> ThrukClient:
        key = _client_key(cfg)
        evicted: list[ThrukClient] = []
        async with self._lock:
            client = self._clients.get(key)
            if client is not None:
                self._clients.move_to_end(key)
                return client
            client = ThrukClient(cfg)
            self._clients[key] = client
            while len(self._clients) > self._maxsize:
                _, old = self._clients.popitem(last=False)
                evicted.append(old)
        # Close evicted clients outside the lock (network I/O).
        for old in evicted:
            await _safe_aclose(old)
        return client

    async def aclose_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await _safe_aclose(client)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._clients)


async def _safe_aclose(client: ThrukClient) -> None:
    try:
        await client.aclose()
    except Exception:  # pragma: no cover - best-effort cleanup
        log.debug("error closing tenant client", exc_info=True)


def _lower_headers(scope: Scope) -> dict[str, str]:
    """Build a lower-cased header mapping from the raw ASGI scope.

    Duplicate header names are joined with ", " per RFC 7230. Header *values*
    are never logged — they may contain the API key.
    """
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        out[name] = f"{out[name]}, {value}" if name in out else value
    return out


class HeaderAuthMiddleware:
    """Pure-ASGI middleware that binds per-request Thruk credentials from headers."""

    def __init__(self, app: Any, base_cfg: ThrukConfig, cache: ClientCache) -> None:
        self.app = app
        self.base_cfg = base_cfg
        self.cache = cache

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _lower_headers(scope)
        try:
            cfg = ThrukConfig.from_headers(self.base_cfg, headers)
        except ValueError as exc:
            # exc message references only the header *name*, never a value.
            log.warning("header-auth: rejecting request: %s", exc)
            await self._unauthorized(scope, receive, send, str(exc))
            return

        client = await self.cache.get(cfg)
        # Same task as the downstream app → context propagates to the stateless
        # session manager's per-request server task (see module docstring).
        _cfg_var.set(cfg)
        _client_var.set(client)
        await self.app(scope, receive, send)

    @staticmethod
    async def _unauthorized(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        from starlette.responses import JSONResponse

        response = JSONResponse({"error": "unauthorized", "detail": detail}, status_code=401)
        await response(scope, receive, send)
