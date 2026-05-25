from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from thruk_mcp.client import _KEEPALIVE_EXPIRY, ThrukClient, ThrukError, _build_default_client
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


# ---------------------------------------------------------------------------
# Issue #144 — explicit httpx.Limits / httpx.Timeout on the shared AsyncClient
# ---------------------------------------------------------------------------


class TestBuildDefaultClient:
    """_build_default_client() must produce an AsyncClient with explicit
    httpx.Limits and a split httpx.Timeout.

    Pre-fix behaviour (documented here as a reminder):
        client = httpx.AsyncClient(
            transport=..., verify=..., timeout=30.0,  # ← bare float, no phase split
            headers=..., follow_redirects=True,       # ← no limits= kwarg
        )
        # → pool defaults to max_connections=100, max_keepalive_connections=20
        # → all timeout phases collapse to the same 30 s value, no connect cap

    Post-fix behaviour:
        limits = httpx.Limits(
            max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0
        )
        timeout = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0)
        client = httpx.AsyncClient(..., limits=limits, timeout=timeout)
    """

    def _cfg(self, **kwargs: object) -> ThrukConfig:
        return ThrukConfig(base_url="https://thruk.test", api_key="key", **kwargs)  # type: ignore[arg-type]

    def test_limits_use_config_values(self) -> None:
        """httpx.Limits must be constructed with max_connections / max_keepalive_connections."""
        cfg = self._cfg(max_connections=15, max_keepalive_connections=7)
        with (
            patch("thruk_mcp.client.httpx.Limits") as mock_limits,
            patch("thruk_mcp.client.httpx.AsyncClient") as mock_ac,
            patch("thruk_mcp.client.httpx.AsyncHTTPTransport"),
            patch("thruk_mcp.client.httpx.Timeout"),
        ):
            mock_ac.return_value = MagicMock()
            _build_default_client(cfg, max_retries=3)
        mock_limits.assert_called_once_with(
            max_connections=15,
            max_keepalive_connections=7,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        )

    def test_limits_defaults(self) -> None:
        """Default config must call httpx.Limits with 20 / 10 (not httpx defaults 100 / 20)."""
        cfg = self._cfg()
        with (
            patch("thruk_mcp.client.httpx.Limits") as mock_limits,
            patch("thruk_mcp.client.httpx.AsyncClient") as mock_ac,
            patch("thruk_mcp.client.httpx.AsyncHTTPTransport"),
            patch("thruk_mcp.client.httpx.Timeout"),
        ):
            mock_ac.return_value = MagicMock()
            _build_default_client(cfg, max_retries=3)
        mock_limits.assert_called_once_with(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        )

    def test_timeout_is_split(self) -> None:
        """Timeout must be an httpx.Timeout instance, not a bare float."""
        cfg = self._cfg(timeout=30.0)
        client = _build_default_client(cfg, max_retries=3)
        t = client.timeout
        assert isinstance(t, httpx.Timeout)
        assert t.read == 30.0
        assert t.write == 30.0
        assert t.pool == 30.0

    def test_connect_timeout_capped_at_5s(self) -> None:
        """connect timeout must be min(5.0, config.timeout) — always ≤ 5 s for long timeouts."""
        cfg = self._cfg(timeout=60.0)
        client = _build_default_client(cfg, max_retries=3)
        assert client.timeout.connect == 5.0  # type: ignore[attr-defined]

    def test_connect_timeout_uses_config_when_smaller(self) -> None:
        """When config.timeout < 5 s, connect timeout must use the smaller value."""
        cfg = self._cfg(timeout=2.0)
        client = _build_default_client(cfg, max_retries=3)
        assert client.timeout.connect == 2.0  # type: ignore[attr-defined]

    def test_default_client_in_thruk_client_uses_build_helper(self) -> None:
        """ThrukClient() without an injected client must call _build_default_client()."""
        sentinel = MagicMock(spec=httpx.AsyncClient)
        with patch("thruk_mcp.client._build_default_client", return_value=sentinel) as mock_build:
            tc = ThrukClient(CFG)
        mock_build.assert_called_once_with(CFG, 3)  # default max_retries=3
        assert tc._client is sentinel


class TestConfigPoolEnvVars:
    """THRUK_MAX_CONNECTIONS / THRUK_MAX_KEEPALIVE must propagate into ThrukConfig."""

    def test_max_connections_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "k")
        monkeypatch.setenv("THRUK_MAX_CONNECTIONS", "8")
        cfg = ThrukConfig.from_env()
        assert cfg.max_connections == 8

    def test_max_keepalive_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "k")
        monkeypatch.setenv("THRUK_MAX_KEEPALIVE", "4")
        cfg = ThrukConfig.from_env()
        assert cfg.max_keepalive_connections == 4

    def test_placeholder_max_connections_uses_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "k")
        monkeypatch.setenv("THRUK_MAX_CONNECTIONS", "<UNKNOWN>")
        cfg = ThrukConfig.from_env()
        assert cfg.max_connections == 20

    def test_placeholder_max_keepalive_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "k")
        monkeypatch.setenv("THRUK_MAX_KEEPALIVE", "<UNKNOWN>")
        cfg = ThrukConfig.from_env()
        assert cfg.max_keepalive_connections == 10

    def test_repr_includes_pool_fields(self) -> None:
        """__repr__ must mention max_connections and max_keepalive_connections."""
        cfg = ThrukConfig(
            base_url="https://t.example.com",
            api_key="secret",
            max_connections=8,
            max_keepalive_connections=4,
        )
        r = repr(cfg)
        assert "max_connections=8" in r
        assert "max_keepalive_connections=4" in r
