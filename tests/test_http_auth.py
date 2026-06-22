"""Transport-level HTTP auth: HttpAuthConfig, bearer gate, TrustedHost wiring,
and the fail-closed startup check in ``main()``.

No socket is ever bound: the bearer middleware is exercised at the ASGI level
with a recording downstream app, and ``main()`` is driven with the run helpers
patched out (mirroring ``test_transport.py``).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from thruk_mcp import __main__ as m
from thruk_mcp.config import HttpAuthConfig
from thruk_mcp.httpauth import BearerAuthMiddleware

# ---------------------------------------------------------------------------
# HttpAuthConfig.from_env
# ---------------------------------------------------------------------------

_HTTP_ENV_KEYS = (
    "MCP_HTTP_TOKEN",
    "MCP_HTTP_ALLOW_UNAUTHENTICATED",
    "MCP_HTTP_ALLOWED_HOSTS",
)


@pytest.fixture
def _clean_http_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _HTTP_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_from_env_defaults(_clean_http_env: None) -> None:
    cfg = HttpAuthConfig.from_env()
    assert cfg.token is None
    assert cfg.allow_unauthenticated is False
    assert cfg.allowed_hosts == ("localhost", "127.0.0.1", "[::1]")


def test_from_env_parses_all(_clean_http_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HTTP_TOKEN", "  s3cret  ")
    monkeypatch.setenv("MCP_HTTP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv("MCP_HTTP_ALLOWED_HOSTS", "mcp.example.com, 10.0.0.1 ")
    cfg = HttpAuthConfig.from_env()
    assert cfg.token == "s3cret"  # stripped
    assert cfg.allow_unauthenticated is True
    assert cfg.allowed_hosts == ("mcp.example.com", "10.0.0.1")


def test_empty_token_becomes_none(_clean_http_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HTTP_TOKEN", "   ")
    assert HttpAuthConfig.from_env().token is None


def test_repr_redacts_token() -> None:
    cfg = HttpAuthConfig(token="topsecret", allow_unauthenticated=False)
    text = repr(cfg)
    assert "topsecret" not in text
    assert "***" in text
    assert str(cfg) == repr(cfg)
    # No token → explicit None, not a redaction mark.
    assert "token=None" in repr(HttpAuthConfig())


# ---------------------------------------------------------------------------
# BearerAuthMiddleware (raw ASGI)
# ---------------------------------------------------------------------------


class _Recorder:
    """Downstream ASGI app that records whether it was reached."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(authorization: bytes | None) -> dict:
    headers = [(b"authorization", authorization)] if authorization is not None else []
    return {"type": "http", "headers": headers}


async def _drive(mw, scope) -> list[dict]:
    sent: list[dict] = []

    async def receive():  # pragma: no cover - never invoked in these paths
        return {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_bearer_accepts_correct_token() -> None:
    app = _Recorder()
    mw = BearerAuthMiddleware(app, token="abc")
    sent = await _drive(mw, _http_scope(b"Bearer abc"))
    assert app.called is True
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", [None, b"Bearer wrong", b"Basic abc", b"abc"])
async def test_bearer_rejects_bad_token(auth: bytes | None) -> None:
    app = _Recorder()
    mw = BearerAuthMiddleware(app, token="abc")
    sent = await _drive(mw, _http_scope(auth))
    assert app.called is False
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]


@pytest.mark.asyncio
async def test_bearer_passthrough_when_no_token() -> None:
    app = _Recorder()
    mw = BearerAuthMiddleware(app, token=None)
    sent = await _drive(mw, _http_scope(None))
    assert app.called is True
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_bearer_ignores_non_http_scope() -> None:
    app = _Recorder()
    mw = BearerAuthMiddleware(app, token="abc")
    # lifespan / websocket scopes must pass straight through, unauthenticated.
    await mw({"type": "lifespan"}, None, None)
    assert app.called is True


# ---------------------------------------------------------------------------
# App wiring — TrustedHost + Bearer attached only when http_auth is given
# ---------------------------------------------------------------------------


def _build(http_auth):
    with patch("mcp.server.streamable_http_manager.StreamableHTTPSessionManager"):
        return m._build_streamable_app(
            object(), stateless=True, json_response=False, http_auth=http_auth
        )


def _middleware_classes(app) -> list[str]:
    return [mw.cls.__name__ for mw in app.user_middleware]


def test_app_has_no_transport_middleware_without_http_auth() -> None:
    app = _build(None)
    names = _middleware_classes(app)
    assert "BearerAuthMiddleware" not in names
    assert "TrustedHostMiddleware" not in names


def test_app_wires_trustedhost_then_bearer() -> None:
    app = _build(HttpAuthConfig(token="t", allowed_hosts=("a.example.com",)))
    names = _middleware_classes(app)
    # Outermost first: TrustedHost must precede Bearer.
    assert names.index("TrustedHostMiddleware") < names.index("BearerAuthMiddleware")


# ---------------------------------------------------------------------------
# main() fail-closed gate for HTTP transport
# ---------------------------------------------------------------------------


def _run_main(argv, env):
    with (
        patch.dict(os.environ, env, clear=False),
        patch.object(m.asyncio, "run", side_effect=lambda coro: coro.close()),
        patch.object(m, "_run_stdio"),
        patch.object(m, "_run_streamable_http"),
    ):
        return m.main(argv)


def test_http_without_token_or_optout_refuses_to_start(
    _clean_http_env: None,
) -> None:
    with pytest.raises(SystemExit):
        _run_main(["--listen", "8001"], env={})


def test_http_with_token_starts(_clean_http_env: None) -> None:
    assert _run_main(["--listen", "8001"], env={"MCP_HTTP_TOKEN": "tok"}) == 0


def test_http_with_optout_starts(_clean_http_env: None) -> None:
    assert (
        _run_main(["--listen", "8001"], env={"MCP_HTTP_ALLOW_UNAUTHENTICATED": "true"}) == 0
    )


def test_stdio_unaffected_by_missing_token(_clean_http_env: None) -> None:
    # stdio never exposes a network socket → no bearer requirement.
    assert _run_main([], env={}) == 0
