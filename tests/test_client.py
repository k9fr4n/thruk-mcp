from __future__ import annotations

import httpx
import pytest
import respx

from thruk_mcp.client import ThrukClient, ThrukError
from thruk_mcp.config import ThrukConfig

CFG = ThrukConfig(base_url="https://thruk.test", api_key="secret")


@pytest.mark.asyncio
async def test_get_hosts_builds_correct_url() -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get("https://thruk.test/r/hosts").mock(
            return_value=httpx.Response(200, json=[{"name": "srv01"}])
        )
        async with ThrukClient(CFG) as client:
            data = await client.get("/hosts", params={"limit": 10})
        assert data == [{"name": "srv01"}]
        assert route.calls.last.request.headers["X-Thruk-Auth-Key"] == "secret"
        assert dict(route.calls.last.request.url.params) == {"limit": "10"}


@pytest.mark.asyncio
async def test_backends_prefix_added() -> None:
    cfg = ThrukConfig(base_url="https://thruk.test", api_key="k", default_backends=("prod",))
    async with respx.mock(assert_all_called=True) as router:
        router.get("https://thruk.test/r/sites/prod/hosts").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with ThrukClient(cfg) as client:
            await client.get("/hosts")


@pytest.mark.asyncio
async def test_http_error_raises_thruk_error() -> None:
    async with respx.mock() as router:
        router.get("https://thruk.test/r/hosts").mock(
            return_value=httpx.Response(500, text="boom")
        )
        async with ThrukClient(CFG) as client:
            with pytest.raises(ThrukError):
                await client.get("/hosts")
