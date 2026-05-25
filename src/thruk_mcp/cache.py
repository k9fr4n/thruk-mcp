"""Tiny in-memory TTL cache, async-safe.

Intended for use inside a single ThrukClient instance to absorb the burst of
identical calls an LLM agent typically issues (e.g. /sites, /hosts/stats called
from 5 different tools in one turn). Not a replacement for a real cache like
Redis — process-local, bounded by ``maxsize`` with O(1) FIFO/LRU-style eviction.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

__all__ = ["TTLCache"]


class TTLCache:
    """Async-safe TTL cache keyed by an arbitrary hashable.

    Backed by an :class:`~collections.OrderedDict` so eviction is O(1). When the
    number of stored entries reaches ``maxsize``, inserting a new key evicts the
    least-recently-used entry (the head of the order). Successful ``get`` calls
    and updates of an existing key move the entry to the tail, so frequently
    accessed keys are retained. Since insertions happen in monotonic time and
    typical workloads use a uniform TTL, FIFO order also tracks expiry order in
    practice.
    """

    def __init__(
        self,
        default_ttl: float = 15.0,
        maxsize: int = 512,
        clock: Any = time.monotonic,
    ) -> None:
        self.default_ttl = default_ttl
        self.maxsize = maxsize
        self._clock = clock
        self._lock = asyncio.Lock()
        self._store: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    async def get(self, key: Any) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                self._store.pop(key, None)
                return None
            # Mark as most-recently-used so it survives subsequent evictions.
            self._store.move_to_end(key)
            return value

    async def set(self, key: Any, value: Any, ttl: float | None = None) -> None:
        ttl = self.default_ttl if ttl is None else ttl
        async with self._lock:
            if key in self._store:
                # Existing key: refresh order, no eviction needed.
                self._store.move_to_end(key)
            elif len(self._store) >= self.maxsize:
                # O(1) eviction of the least-recently-used entry.
                self._store.popitem(last=False)
            self._store[key] = (self._clock() + ttl, value)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
