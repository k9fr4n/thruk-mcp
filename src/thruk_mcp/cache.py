"""Tiny in-memory TTL cache, async-safe.

Intended for use inside a single ThrukClient instance to absorb the burst of
identical calls an LLM agent typically issues (e.g. /sites, /hosts/stats called
from 5 different tools in one turn). Not a replacement for a real cache like
Redis — process-local, no eviction beyond TTL, no size cap.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

__all__ = ["TTLCache"]


class TTLCache:
    """Async-safe TTL cache keyed by an arbitrary hashable."""

    def __init__(self, default_ttl: float = 15.0, clock: Any = time.monotonic) -> None:
        self.default_ttl = default_ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._store: dict[Any, tuple[float, Any]] = {}

    async def get(self, key: Any) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    async def set(self, key: Any, value: Any, ttl: float | None = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        async with self._lock:
            self._store[key] = (self._clock() + ttl, value)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
