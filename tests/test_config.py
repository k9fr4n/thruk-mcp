from __future__ import annotations

import pytest

from thruk_mcp.config import ThrukConfig


def test_from_env_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THRUK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="THRUK_API_KEY"):
        ThrukConfig.from_env()


def test_from_env_parses_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THRUK_API_KEY", "secret")
    monkeypatch.setenv("THRUK_BASE_URL", "https://t.example.com/thruk/")
    monkeypatch.setenv("THRUK_AUTH_USER", "alice")
    monkeypatch.setenv("THRUK_VERIFY_SSL", "false")
    monkeypatch.setenv("THRUK_TIMEOUT", "42")
    monkeypatch.setenv("THRUK_DEFAULT_BACKENDS", "prod, dr ,")
    cfg = ThrukConfig.from_env()
    assert cfg.api_key == "secret"
    assert cfg.base_url == "https://t.example.com/thruk"  # trailing slash stripped
    assert cfg.auth_user == "alice"
    assert cfg.verify_ssl is False
    assert cfg.timeout == 42.0
    assert cfg.default_backends == ("prod", "dr")


def test_headers_include_auth_user_when_set() -> None:
    cfg = ThrukConfig(base_url="x", api_key="k", auth_user="alice")
    h = cfg.headers()
    assert h["X-Thruk-Auth-Key"] == "k"
    assert h["X-Thruk-Auth-User"] == "alice"


def test_headers_omit_auth_user_when_empty() -> None:
    cfg = ThrukConfig(base_url="x", api_key="k")
    assert "X-Thruk-Auth-User" not in cfg.headers()
