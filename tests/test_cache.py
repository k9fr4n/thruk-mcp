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
