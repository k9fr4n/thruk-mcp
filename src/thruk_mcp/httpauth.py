"""Transport-level HTTP auth for the Streamable-HTTP endpoint.

Gates the ``/mcp`` endpoint *before* any Thruk credential resolution happens:

* :class:`BearerAuthMiddleware` — requires ``Authorization: Bearer <token>``
  (constant-time compare). Pass-through when no token is configured; the
  unauthenticated opt-in is enforced at startup in :mod:`__main__`.
* Anti-DNS-rebinding is handled by Starlette's stock ``TrustedHostMiddleware``
  (wired in ``__main__._build_streamable_app``), not re-implemented here.

These are orthogonal to :mod:`multitenant`: the bearer gate controls *access to
the endpoint*; header-auth selects *which* Thruk credentials a request uses.
Both can be enabled together (recommended for a multi-tenant server exposed
over the network).
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send


class BearerAuthMiddleware:
    """Pure-ASGI bearer-token gate for the HTTP transport.

    Implemented as raw ASGI (not Starlette's ``BaseHTTPMiddleware``) so it does
    not buffer or break the SSE streaming response. When ``token`` is ``None``
    the gate is a pass-through (unauthenticated mode, guarded by an explicit
    opt-in upstream).
    """

    def __init__(self, app: Any, *, token: str | None) -> None:
        self.app = app
        self._expected = b"Bearer " + token.encode() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._expected is None or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        provided = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(provided, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"401 Unauthorized"})
            return
        await self.app(scope, receive, send)
