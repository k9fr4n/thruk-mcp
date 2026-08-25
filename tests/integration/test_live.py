"""End-to-end tests against a real Thruk instance.

Skipped by default. Run with:

    THRUK_BASE_URL=https://localhost:8443/demo/thruk \
    THRUK_VERIFY_SSL=false \
    THRUK_API_KEY=$(./scripts/get-test-api-key.sh) \
        pytest -m integration

Gate: `THRUK_API_KEY` must be set. Without it every test in this module is
skipped so the standard `pytest` invocation stays green.
"""

from __future__ import annotations

import os

import pytest

from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("THRUK_API_KEY"),
        reason="THRUK_API_KEY not set; live integration tests are opt-in.",
    ),
]


@pytest.fixture
async def live_client():
    cfg = ThrukConfig.from_env()
    async with ThrukClient(cfg) as client:
        yield client


@pytest.mark.asyncio
async def test_processinfo_reachable(live_client) -> None:
    """Smoke test: confirm we are talking to a real Thruk."""
    info = await live_client.get("/processinfo")
    assert info, "empty processinfo response"


@pytest.mark.asyncio
async def test_list_hosts_returns_rows(live_client) -> None:
    rows = await live_client.get("/hosts", params={"limit": 5})
    assert isinstance(rows, list)
    if rows:
        assert {"name"}.issubset(rows[0].keys())


@pytest.mark.asyncio
async def test_stats_have_state_buckets(live_client) -> None:
    stats = await live_client.get("/hosts/stats")
    # OMD demo always exposes these keys; a missing key is a regression.
    expected = {"total", "plain_up", "plain_down", "plain_unreachable"}
    assert expected.intersection(stats.keys()), f"unexpected stats shape: {list(stats)[:5]}"
