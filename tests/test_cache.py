from __future__ import annotations

import pytest

from thruk_mcp.cache import TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_set_and_get() -> None:
    c = TTLCache(default_ttl=10.0)
    await c.set("a", 1)
    assert await c.get("a") == 1


@pytest.mark.asyncio
async def test_expires_after_ttl() -> None:
    clock = FakeClock()
    c = TTLCache(default_ttl=10.0, clock=clock)
    await c.set("a", "v")
    clock.now += 9.9
    assert await c.get("a") == "v"
    clock.now += 0.2  # total 10.1
    assert await c.get("a") is None
    assert len(c) == 0


@pytest.mark.asyncio
async def test_per_entry_ttl_overrides_default() -> None:
    clock = FakeClock()
    c = TTLCache(default_ttl=10.0, clock=clock)
    await c.set("a", "v", ttl=2.0)
    clock.now += 3.0
    assert await c.get("a") is None


# ---------------------------------------------------------------------------
# maxsize / eviction tests (issue #91)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maxsize_default_is_512() -> None:
    """TTLCache must expose maxsize and default to 512."""
    c = TTLCache()
    assert c.maxsize == 512


@pytest.mark.asyncio
async def test_maxsize_cap_evicts_when_full() -> None:
    """Inserting beyond maxsize must evict one entry so len stays at maxsize.

    Bug (before fix): the store grew without bound — len(c) would exceed
    maxsize because set() performed no eviction on capacity.
    Fix asserted: len(c) == maxsize after inserting maxsize+1 distinct keys.
    """
    clock = FakeClock()
    cap = 4
    c = TTLCache(default_ttl=10.0, maxsize=cap, clock=clock)

    for i in range(cap + 1):
        await c.set(f"key-{i}", i)

    # Store must never exceed the cap.
    assert len(c) == cap


@pytest.mark.asyncio
async def test_maxsize_evicts_earliest_expiry() -> None:
    """When the cache is full, the entry whose expiry is earliest is evicted."""
    clock = FakeClock()
    cap = 3
    c = TTLCache(default_ttl=100.0, maxsize=cap, clock=clock)

    # "short" expires at clock.now + 1, "long-*" expire at clock.now + 100.
    await c.set("short", "will-be-evicted", ttl=1.0)
    await c.set("long-1", "keep", ttl=100.0)
    await c.set("long-2", "keep", ttl=100.0)

    # Cache is now full (3 entries). Adding a new key must evict "short".
    await c.set("new", "value", ttl=100.0)

    assert len(c) == cap
    assert await c.get("short") is None  # evicted
    assert await c.get("long-1") == "keep"
    assert await c.get("long-2") == "keep"
    assert await c.get("new") == "value"


@pytest.mark.asyncio
async def test_updating_existing_key_does_not_evict() -> None:
    """Overwriting an existing key must not trigger eviction."""
    clock = FakeClock()
    cap = 2
    c = TTLCache(default_ttl=10.0, maxsize=cap, clock=clock)

    await c.set("a", 1)
    await c.set("b", 2)

    # Both slots are taken; updating "a" must not evict "b".
    await c.set("a", 99)

    assert len(c) == cap
    assert await c.get("a") == 99
    assert await c.get("b") == 2


# ---------------------------------------------------------------------------
# OrderedDict-backed O(1) eviction (issue #148)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_marks_entry_as_recently_used() -> None:
    """A successful get() should move the entry to the tail so the next eviction
    drops the least-recently-used key instead of the freshly-touched one.

    Bug (before fix): eviction picked the entry with the smallest expiry
    timestamp via an O(n) min() scan; getting an entry did not influence
    eviction order. After switching to OrderedDict + move_to_end, get() now
    promotes the entry to the MRU position.
    """
    clock = FakeClock()
    cap = 2
    c = TTLCache(default_ttl=100.0, maxsize=cap, clock=clock)

    await c.set("a", 1)
    await c.set("b", 2)
    # Touch "a" so it becomes MRU; "b" is now the LRU candidate.
    assert await c.get("a") == 1

    # Inserting a new key must evict "b" (LRU), not "a".
    await c.set("c", 3)

    assert len(c) == cap
    assert await c.get("a") == 1
    assert await c.get("b") is None
    assert await c.get("c") == 3


@pytest.mark.asyncio
async def test_fifo_eviction_when_no_gets() -> None:
    """With no intervening get() calls, eviction follows insertion order (FIFO)."""
    clock = FakeClock()
    cap = 3
    c = TTLCache(default_ttl=100.0, maxsize=cap, clock=clock)

    await c.set("k1", 1)
    await c.set("k2", 2)
    await c.set("k3", 3)
    await c.set("k4", 4)  # evicts the head: "k1"

    assert len(c) == cap
    assert await c.get("k1") is None
    assert await c.get("k2") == 2
    assert await c.get("k3") == 3
    assert await c.get("k4") == 4


@pytest.mark.asyncio
async def test_uses_ordereddict_internal_store() -> None:
    """Pin the internal data structure choice: OrderedDict is required for the
    O(1) eviction path (popitem(last=False) and move_to_end)."""
    from collections import OrderedDict

    c = TTLCache()
    assert isinstance(c._store, OrderedDict)


@pytest.mark.asyncio
async def test_maxsize_zero_still_inserts_one_entry() -> None:
    """maxsize=0 is an edge case: every insert evicts the previous entry."""
    # This is not an expected real-world usage but must not crash.
    clock = FakeClock()
    c = TTLCache(default_ttl=10.0, maxsize=1, clock=clock)

    await c.set("first", "v1")
    await c.set("second", "v2")  # evicts "first"

    assert len(c) == 1
    assert await c.get("first") is None
    assert await c.get("second") == "v2"
