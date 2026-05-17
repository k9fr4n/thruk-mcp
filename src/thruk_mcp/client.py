"""Async HTTP client for the Thruk REST API."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .cache import TTLCache
from .config import ThrukConfig

log = logging.getLogger("thruk_mcp.client")

# Paths whose responses change slowly enough to be safely cached for a few
# seconds. Tweak as needed; this list is intentionally conservative.
CACHEABLE_PATHS: frozenset[str] = frozenset(
    {
        "/sites",
        "/processinfo",
        "/hosts/stats",
        "/hosts/totals",
        "/services/stats",
        "/services/totals",
        "/contacts",
        "/contactgroups",
        "/timeperiods",
        "/commands",
    }
)

# 5xx and 429 are retried. 4xx (except 429) are not — they are caller errors.
RETRY_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class ThrukError(RuntimeError):
    """Raised for any Thruk API error."""


class ThrukClient:
    """Thin async wrapper around the Thruk REST API (`/thruk/r/...`).

    Features:
    - native multi-backend URL building (`/r/sites/<a,b>/...`)
    - connection-level retries (`httpx.AsyncHTTPTransport(retries=N)`)
    - HTTP-level retries with exponential backoff + jitter for 5xx / 429
    - opt-in TTL cache for slow-moving endpoints
    - `get_all()` async paginator for unbounded queries
    - `run_background()` helper for Thruk long-running requests (?background=1)
    """

    def __init__(
        self,
        config: ThrukConfig,
        client: httpx.AsyncClient | None = None,
        cache: TTLCache | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.4,
        backoff_cap: float = 5.0,
    ) -> None:
        self.config = config
        self.cache = cache or TTLCache(default_ttl=15.0)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=max_retries),
            verify=config.verify_ssl,
            timeout=config.timeout,
            headers=config.headers(),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ThrukClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------- internals
    def _url(self, path: str, backends: tuple[str, ...] | None = None) -> str:
        path = path if path.startswith("/") else f"/{path}"
        backends = backends if backends is not None else self.config.default_backends
        prefix = f"/r/sites/{','.join(backends)}" if backends else "/r"
        return f"{self.config.base_url}{prefix}{path}"

    async def _backoff(self, attempt: int) -> None:
        delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
        delay += random.uniform(0, delay * 0.25)
        log.info("Thruk retry in %.2fs (attempt %d)", delay, attempt + 1)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------- requests
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        backends: tuple[str, ...] | None = None,
        cache_ttl: float | None = None,
    ) -> Any:
        url = self._url(path, backends=backends)
        cache_key: tuple[Any, ...] | None = None
        cacheable = method.upper() == "GET" and (cache_ttl is not None or path in CACHEABLE_PATHS)
        if cacheable:
            cache_key = (url, tuple(sorted((params or {}).items())))
            cached = await self.cache.get(cache_key)
            if cached is not None:
                log.debug("Thruk cache hit: %s", url)
                return cached

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                log.info("Thruk %s %s (try %d)", method, url, attempt + 1)
                resp = await self._client.request(method, url, params=params, data=data)
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await self._backoff(attempt)
                    continue
                raise ThrukError(f"Failed to reach Thruk at {url}: {exc}") from exc

            if resp.status_code in RETRY_STATUS and attempt < self.max_retries:
                log.warning("Thruk HTTP %d on %s, retrying", resp.status_code, url)
                await self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise ThrukError(
                    f"Thruk API returned HTTP {resp.status_code} for {method} {path}: "
                    f"{resp.text[:500]}"
                )
            if not resp.content:
                return None
            try:
                payload = resp.json()
            except ValueError as exc:
                raise ThrukError(f"Invalid JSON from Thruk: {exc}") from exc

            if cacheable and cache_key is not None:
                await self.cache.set(cache_key, payload, ttl=cache_ttl)
            return payload

        # Should be unreachable, but keep mypy happy.
        raise ThrukError(f"Failed to reach Thruk at {url}: {last_exc}")

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    # ----------------------------------------------------------- pagination
    async def get_all(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        backends: tuple[str, ...] | None = None,
        page_size: int = 500,
        hard_limit: int = 50_000,
    ) -> AsyncIterator[Any]:
        """Yield rows from a paginated Thruk list endpoint.

        Uses limit/offset under the hood. Stops when the server returns fewer
        rows than `page_size`, or when `hard_limit` rows have been yielded
        (safety net against runaway queries).
        """
        params = dict(params or {})
        params["limit"] = page_size
        offset = int(params.pop("offset", 0) or 0)
        yielded = 0
        while yielded < hard_limit:
            params["offset"] = offset
            page = await self.get(path, params=params, backends=backends)
            if not isinstance(page, list) or not page:
                return
            for row in page:
                yield row
                yielded += 1
                if yielded >= hard_limit:
                    log.warning("Thruk get_all hit hard_limit=%d", hard_limit)
                    return
            if len(page) < page_size:
                return
            offset += page_size

    # ------------------------------------------------------ long-running
    async def run_background(
        self,
        path: str,
        *,
        method: str = "POST",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        backends: tuple[str, ...] | None = None,
        poll_timeout: float = 300.0,
        poll_interval: float = 2.0,
    ) -> Any:
        """Run a Thruk request with `background=1` and poll the resulting job.

        Thruk returns `{job_id, result_url}` immediately. The result URL is
        served from the server root (`/thruk/jobs/<id>/output`) and emits HTTP
        302 every 30s while the job is still computing. We poll with
        follow_redirects=False so we can distinguish 'still running' (302)
        from 'done' (200 + JSON)."""
        from urllib.parse import urlparse

        params = dict(params or {})
        params["background"] = 1
        kicked = await self.request(method, path, params=params, data=data, backends=backends)
        if not isinstance(kicked, dict) or "job_id" not in kicked:
            # Not a background-capable endpoint, return as-is.
            return kicked
        job_id = kicked["job_id"]
        result_url = kicked.get("result_url") or f"/thruk/jobs/{job_id}/output"
        parsed = urlparse(self.config.base_url)
        full_url = f"{parsed.scheme}://{parsed.netloc}{result_url}"
        log.info("Thruk job %s submitted, polling %s", job_id, full_url)

        deadline = asyncio.get_event_loop().time() + poll_timeout
        while True:
            resp = await self._client.get(full_url, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                if asyncio.get_event_loop().time() >= deadline:
                    raise ThrukError(f"Thruk job {job_id} did not complete in {poll_timeout}s")
                await asyncio.sleep(poll_interval)
                continue
            if resp.status_code >= 400:
                raise ThrukError(
                    f"Thruk job {job_id} failed: HTTP {resp.status_code}: {resp.text[:500]}"
                )
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text
