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
        router.get("https://thruk.test/r/hosts").mock(return_value=httpx.Response(500, text="boom"))
        async with ThrukClient(CFG) as client:
            with pytest.raises(ThrukError):
                await client.get("/hosts")


# ---------------------------------------------------------------------------
# get_with_fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_with_fallback_success_no_fallback() -> None:
    """Happy path: all-backends request succeeds → no fallback, no warnings."""
    async with respx.mock(assert_all_called=True) as router:
        router.get("https://thruk.test/r/logs").mock(
            return_value=httpx.Response(200, json=[{"time": 1}])
        )
        async with ThrukClient(CFG) as client:
            data, warnings = await client.get_with_fallback("/logs")
    assert data == [{"time": 1}]
    assert warnings == []


@pytest.mark.asyncio
async def test_get_with_fallback_triggers_per_backend_on_500() -> None:
    """On all-backends 500, falls back to per-backend queries using /sites."""
    sites = [
        {"id": "prod", "name": "prod", "connected": 1},
        {"id": "dr", "name": "dr", "connected": 1},
        {"id": "broken", "name": "broken", "connected": 0},
    ]
    async with respx.mock() as router:
        # All-backends /logs → 500
        router.get("https://thruk.test/r/logs").mock(
            return_value=httpx.Response(500, text="federation error")
        )
        # /sites → list with 2 connected + 1 disconnected
        router.get("https://thruk.test/r/sites").mock(return_value=httpx.Response(200, json=sites))
        # Per-backend queries (only connected ones)
        router.get("https://thruk.test/r/sites/prod/logs").mock(
            return_value=httpx.Response(200, json=[{"time": 2, "peer_name": "prod"}])
        )
        router.get("https://thruk.test/r/sites/dr/logs").mock(
            return_value=httpx.Response(200, json=[{"time": 1, "peer_name": "dr"}])
        )
        async with ThrukClient(CFG) as client:
            data, warnings = await client.get_with_fallback("/logs")

    assert warnings == []
    assert len(data) == 2
    peer_names = {r["peer_name"] for r in data}
    assert peer_names == {"prod", "dr"}


@pytest.mark.asyncio
async def test_get_with_fallback_partial_backend_failure_produces_warning() -> None:
    """When one per-backend query fails, its id appears in warnings and others succeed."""
    sites = [
        {"id": "ok-site", "name": "ok-site", "connected": 1},
        {"id": "flaky", "name": "flaky", "connected": 1},
    ]
    async with respx.mock() as router:
        router.get("https://thruk.test/r/logs").mock(return_value=httpx.Response(500, text="boom"))
        router.get("https://thruk.test/r/sites").mock(return_value=httpx.Response(200, json=sites))
        router.get("https://thruk.test/r/sites/ok-site/logs").mock(
            return_value=httpx.Response(200, json=[{"time": 99}])
        )
        router.get("https://thruk.test/r/sites/flaky/logs").mock(
            return_value=httpx.Response(500, text="empty response")
        )
        async with ThrukClient(CFG) as client:
            data, warnings = await client.get_with_fallback("/logs")

    assert data == [{"time": 99}]
    assert len(warnings) == 1
    assert "flaky" in warnings[0]


@pytest.mark.asyncio
async def test_get_with_fallback_explicit_backends_reraises() -> None:
    """When backends is set explicitly, errors are NOT swallowed by the fallback."""
    async with respx.mock() as router:
        router.get("https://thruk.test/r/sites/prod/logs").mock(
            return_value=httpx.Response(500, text="boom")
        )
        async with ThrukClient(CFG) as client:
            with pytest.raises(ThrukError):
                await client.get_with_fallback("/logs", backends=("prod",))
