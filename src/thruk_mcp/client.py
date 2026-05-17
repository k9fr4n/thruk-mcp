"""Async HTTP client for the Thruk REST API."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import ThrukConfig

log = logging.getLogger("thruk_mcp.client")


class ThrukError(RuntimeError):
    """Raised for any Thruk API error."""


class ThrukClient:
    """Thin async wrapper around the Thruk REST API (`/thruk/r/...`)."""

    def __init__(self, config: ThrukConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(
            verify=config.verify_ssl,
            timeout=config.timeout,
            headers=config.headers(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ThrukClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _url(self, path: str, backends: tuple[str, ...] | None = None) -> str:
        path = path if path.startswith("/") else f"/{path}"
        backends = backends if backends is not None else self.config.default_backends
        prefix = f"/r/sites/{','.join(backends)}" if backends else "/r"
        return f"{self.config.base_url}{prefix}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        backends: tuple[str, ...] | None = None,
    ) -> Any:
        url = self._url(path, backends=backends)
        log.info("Thruk %s %s", method, url)
        try:
            resp = await self._client.request(method, url, params=params, data=data)
        except httpx.RequestError as exc:
            raise ThrukError(f"Failed to reach Thruk at {url}: {exc}") from exc

        if resp.status_code >= 400:
            raise ThrukError(
                f"Thruk API returned HTTP {resp.status_code} for {method} {path}: "
                f"{resp.text[:500]}"
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ThrukError(f"Invalid JSON from Thruk: {exc}") from exc

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)
