from __future__ import annotations

import httpx
import pytest
import respx

from thruk_mcp.client import ThrukClient, ThrukError
from thruk_mcp.config import ThrukConfig

CFG = ThrukConfig(base_url="https://thruk.test", api_key="k")


# ---------------------------------------------------------------------------
# HTTP 429 retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_on_429_then_success() -> None:
    """429 is in RETRY_STATUS; the client must retry and eventually succeed.

    Bug reproduced (before fix): 429 was listed in RETRY_STATUS but untested —
    any regression removing 429 from that set would silently break.
    """
    async with respx.mock() as router:
        route = router.get("https://thruk.test/r/hosts").mock(
            side_effect=[
                httpx.Response(429, text="rate limited"),
                httpx.Response(200, json=[{"name": "h1"}]),
            ]
        )
        async with ThrukClient(CFG, max_retries=2, backoff_base=0.0) as client:
            data = await client.get("/hosts")
        assert data == [{"name": "h1"}]
        assert route.call_count == 2  # first call → 429, second → 200


# ---------------------------------------------------------------------------
# httpx.TimeoutException retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_retry_then_success() -> None:
    """httpx.TimeoutException is a subclass of RequestError; retry logic must kick in.

    The client should retry up to max_retries times on transient network errors
    (RequestError subclasses) and succeed if the server eventually responds.
    """
    async with respx.mock() as router:
        route = router.get("https://thruk.test/r/hosts").mock(
            side_effect=[
                httpx.TimeoutException("timed out"),
                httpx.Response(200, json=[{"name": "h1"}]),
            ]
        )
        async with ThrukClient(CFG, max_retries=2, backoff_base=0.0) as client:
            data = await client.get("/hosts")
        assert data == [{"name": "h1"}]
        assert route.call_count == 2


@pytest.mark.asyncio
async def test_timeout_exhausts_retries_raises_thruk_error() -> None:
    """When every attempt raises TimeoutException the client raises ThrukError."""
    async with respx.mock() as router:
        router.get("https://thruk.test/r/hosts").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        async with ThrukClient(CFG, max_retries=1, backoff_base=0.0) as client:
            with pytest.raises(ThrukError, match="Failed to reach Thruk"):
                await client.get("/hosts")


# ---------------------------------------------------------------------------
# get_all() hard_limit safety net
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_stops_at_hard_limit() -> None:
    """get_all must stop yielding rows once hard_limit is reached.

    The safety net prevents runaway queries from flooding the LLM context.
    """
    async with respx.mock() as router:
        # Each page returns 10 rows; without hard_limit the loop would run forever.
        router.get("https://thruk.test/r/hosts").mock(
            return_value=httpx.Response(200, json=[{"name": f"h{i}"} for i in range(10)])
        )
        async with ThrukClient(CFG) as client:
            names = [r async for r in client.get_all("/hosts", page_size=10, hard_limit=5)]
    assert len(names) == 5
    assert names == [{"name": f"h{i}"} for i in range(5)]


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
