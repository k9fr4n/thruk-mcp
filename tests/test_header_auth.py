"""Tests for header-auth multi-tenant mode (per-request Thruk credentials)."""

from __future__ import annotations

import asyncio

import pytest

from thruk_mcp.config import ThrukConfig
from thruk_mcp.helpers import _client_var, _get_cfg
from thruk_mcp.multitenant import ClientCache, HeaderAuthMiddleware, _client_key
from thruk_mcp.server import build_server

BASE = ThrukConfig(
    base_url="http://base/thruk",
    api_key="",  # header-auth: no fixed key at boot
    read_only=True,
    enabled_tools=("thruk_list_*",),
    audit_log=True,
)


# --------------------------------------------------------------------------- #
# ThrukConfig.from_headers / from_env(require_api_key=False)
# --------------------------------------------------------------------------- #


def test_from_headers_overrides_credential_fields() -> None:
    cfg = ThrukConfig.from_headers(
        BASE,
        {
            "x-thruk-auth-key": "tenant-key",
            "x-thruk-base-url": "https://tenant.example/thruk/",
            "x-thruk-auth-user": "alice",
            "x-thruk-backends": "be1, be2",
        },
    )
    assert cfg.api_key == "tenant-key"
    assert cfg.base_url == "https://tenant.example/thruk"  # trailing slash stripped
    assert cfg.auth_user == "alice"
    assert cfg.default_backends == ("be1", "be2")


def test_from_headers_inherits_security_knobs_from_base() -> None:
    """A tenant cannot grant itself write access or disable the audit log."""
    cfg = ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k"})
    assert cfg.read_only is True
    assert cfg.enabled_tools == ("thruk_list_*",)
    assert cfg.audit_log is True
    # base_url / auth_user fall back to base when no header supplied
    assert cfg.base_url == "http://base/thruk"
    assert cfg.auth_user == ""


def test_from_headers_missing_key_raises() -> None:
    with pytest.raises(ValueError, match="x-thruk-auth-key"):
        ThrukConfig.from_headers(BASE, {"x-thruk-base-url": "https://x/thruk"})


def test_from_headers_blank_key_raises() -> None:
    with pytest.raises(ValueError):
        ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "   "})


def test_from_headers_repr_redacts_key() -> None:
    cfg = ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "super-secret"})
    assert "super-secret" not in repr(cfg)
    assert "***" in repr(cfg)


def test_from_env_allows_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THRUK_API_KEY", raising=False)
    cfg = ThrukConfig.from_env(require_api_key=False)
    assert cfg.api_key == ""


def test_from_env_still_requires_key_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THRUK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="THRUK_API_KEY"):
        ThrukConfig.from_env()


# --------------------------------------------------------------------------- #
# build_server binds _cfg_var
# --------------------------------------------------------------------------- #


def test_build_server_binds_cfg_var() -> None:
    cfg = ThrukConfig(base_url="http://x/thruk", api_key="k")
    build_server(cfg)
    assert _get_cfg() is cfg


# --------------------------------------------------------------------------- #
# ClientCache
# --------------------------------------------------------------------------- #


def test_client_key_distinguishes_tenants() -> None:
    a = ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k1"})
    b = ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k2"})
    assert _client_key(a) != _client_key(b)


@pytest.mark.asyncio
async def test_cache_reuses_and_isolates() -> None:
    cache = ClientCache()
    try:
        a1 = await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k1"}))
        a2 = await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k1"}))
        b1 = await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k2"}))
        assert a1 is a2  # same tenant → reused
        assert a1 is not b1  # different tenant → distinct client
        assert len(cache) == 2
    finally:
        await cache.aclose_all()


@pytest.mark.asyncio
async def test_cache_evicts_and_closes_lru() -> None:
    cache = ClientCache(maxsize=2)
    try:
        c1 = await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k1"}))
        await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k2"}))
        # Inserting a 3rd evicts the LRU (k1) and closes its httpx client.
        await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k3"}))
        assert len(cache) == 2
        assert c1._client.is_closed
    finally:
        await cache.aclose_all()


@pytest.mark.asyncio
async def test_cache_aclose_all_closes_clients() -> None:
    cache = ClientCache()
    c = await cache.get(ThrukConfig.from_headers(BASE, {"x-thruk-auth-key": "k1"}))
    await cache.aclose_all()
    assert c._client.is_closed
    assert len(cache) == 0


# --------------------------------------------------------------------------- #
# HeaderAuthMiddleware (pure-ASGI)
# --------------------------------------------------------------------------- #


def _http_scope(headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }


async def _drive(mw: HeaderAuthMiddleware, scope: dict) -> tuple[int, list[dict]]:
    """Invoke the middleware once; return (status, sent-messages)."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await mw(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, sent


@pytest.mark.asyncio
async def test_middleware_missing_key_returns_401() -> None:
    cache = ClientCache()
    captured: list = []

    async def downstream(scope, receive, send):  # pragma: no cover - must not run
        captured.append(_get_cfg())

    mw = HeaderAuthMiddleware(downstream, base_cfg=BASE, cache=cache)
    try:
        status, _ = await _drive(mw, _http_scope({"content-type": "application/json"}))
        assert status == 401
        assert captured == []  # downstream never reached
    finally:
        await cache.aclose_all()


@pytest.mark.asyncio
async def test_middleware_binds_tenant_cfg_for_downstream(caplog: pytest.LogCaptureFixture) -> None:
    cache = ClientCache()
    seen: dict = {}

    async def downstream(scope, receive, send):
        cfg = _get_cfg()
        seen["api_key"] = cfg.api_key
        seen["client"] = _client_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = HeaderAuthMiddleware(downstream, base_cfg=BASE, cache=cache)
    try:
        status, _ = await _drive(
            mw,
            _http_scope({"x-thruk-auth-key": "secret-key", "x-thruk-auth-user": "bob"}),
        )
        assert status == 200
        assert seen["api_key"] == "secret-key"
        # the bound client is the cached tenant client
        assert seen["client"] is await cache.get(
            ThrukConfig.from_headers(
                BASE, {"x-thruk-auth-key": "secret-key", "x-thruk-auth-user": "bob"}
            )
        )
        # the key must never be logged
        assert "secret-key" not in caplog.text
    finally:
        await cache.aclose_all()


@pytest.mark.asyncio
async def test_middleware_isolates_concurrent_tenants() -> None:
    cache = ClientCache()
    results: dict[str, str] = {}

    def make_downstream(name: str):
        async def downstream(scope, receive, send):
            await asyncio.sleep(0.01)  # force interleaving
            results[name] = _get_cfg().api_key
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return downstream

    try:
        mw_a = HeaderAuthMiddleware(make_downstream("A"), base_cfg=BASE, cache=cache)
        mw_b = HeaderAuthMiddleware(make_downstream("B"), base_cfg=BASE, cache=cache)
        await asyncio.gather(
            _drive(mw_a, _http_scope({"x-thruk-auth-key": "key-A"})),
            _drive(mw_b, _http_scope({"x-thruk-auth-key": "key-B"})),
        )
        assert results == {"A": "key-A", "B": "key-B"}
    finally:
        await cache.aclose_all()


@pytest.mark.asyncio
async def test_middleware_passes_through_non_http() -> None:
    cache = ClientCache()
    hit = {"n": 0}

    async def downstream(scope, receive, send):
        hit["n"] += 1

    mw = HeaderAuthMiddleware(downstream, base_cfg=BASE, cache=cache)
    try:
        await mw({"type": "lifespan"}, None, None)
        assert hit["n"] == 1
    finally:
        await cache.aclose_all()
