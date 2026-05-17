from __future__ import annotations

import httpx
import pytest
import respx

from thruk_mcp.client import ThrukClient, ThrukError
from thruk_mcp.config import ThrukConfig

CFG = ThrukConfig(base_url="https://thruk.test", api_key="k")


@pytest.mark.asyncio
async def test_get_all_paginates_until_short_page() -> None:
    async with respx.mock() as router:
        # 3 pages of 2 rows each, then a final short page of 1 → stop.
        router.get("https://thruk.test/r/hosts").mock(
            side_effect=[
                httpx.Response(200, json=[{"name": "h1"}, {"name": "h2"}]),
                httpx.Response(200, json=[{"name": "h3"}, {"name": "h4"}]),
                httpx.Response(200, json=[{"name": "h5"}]),
            ]
        )
        async with ThrukClient(CFG) as client:
            names = [r["name"] async for r in client.get_all("/hosts", page_size=2)]
        assert names == ["h1", "h2", "h3", "h4", "h5"]


@pytest.mark.asyncio
async def test_retry_on_503_then_success() -> None:
    async with respx.mock() as router:
        route = router.get("https://thruk.test/r/hosts").mock(
            side_effect=[
                httpx.Response(503, text="busy"),
                httpx.Response(503, text="busy"),
                httpx.Response(200, json=[{"name": "h1"}]),
            ]
        )
        async with ThrukClient(CFG, max_retries=3, backoff_base=0.0) as client:
            data = await client.get("/hosts")
        assert data == [{"name": "h1"}]
        assert route.call_count == 3


@pytest.mark.asyncio
async def test_cache_avoids_second_call_for_cacheable_path() -> None:
    async with respx.mock() as router:
        route = router.get("https://thruk.test/r/sites").mock(
            return_value=httpx.Response(200, json=[{"name": "s1"}])
        )
        async with ThrukClient(CFG) as client:
            a = await client.get("/sites")
            b = await client.get("/sites")
        assert a == b == [{"name": "s1"}]
        assert route.call_count == 1  # second call served from cache


@pytest.mark.asyncio
async def test_no_retry_on_4xx() -> None:
    async with respx.mock() as router:
        route = router.get("https://thruk.test/r/hosts/missing").mock(
            return_value=httpx.Response(404, text="nope")
        )
        async with ThrukClient(CFG, max_retries=3, backoff_base=0.0) as client:
            with pytest.raises(ThrukError):
                await client.get("/hosts/missing")
        assert route.call_count == 1
