from __future__ import annotations

import pytest

from thruk_mcp.config import ThrukConfig, _envbool, _float_env, _int_env, _str_env


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


# ---------------------------------------------------------------------------
# <UNKNOWN> placeholder handling (Docker MCP Gateway regression tests)
# ---------------------------------------------------------------------------


class TestUnknownPlaceholder:
    """Ensure every optional knob silently falls back to its default when
    the Docker MCP Gateway injects '<UNKNOWN>' for an unbound secret."""

    PLACEHOLDER = "<UNKNOWN>"

    def test_api_key_placeholder_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", self.PLACEHOLDER)
        with pytest.raises(RuntimeError, match="THRUK_API_KEY"):
            ThrukConfig.from_env()

    def test_max_concurrent_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "secret")
        monkeypatch.setenv("THRUK_MAX_CONCURRENT", self.PLACEHOLDER)
        cfg = ThrukConfig.from_env()
        assert cfg.max_concurrent == 0  # default

    def test_verify_ssl_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "secret")
        monkeypatch.setenv("THRUK_VERIFY_SSL", self.PLACEHOLDER)
        cfg = ThrukConfig.from_env()
        assert cfg.verify_ssl is True  # default

    def test_read_only_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "secret")
        monkeypatch.setenv("THRUK_READ_ONLY", self.PLACEHOLDER)
        cfg = ThrukConfig.from_env()
        assert cfg.read_only is False  # default

    def test_timeout_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "secret")
        monkeypatch.setenv("THRUK_TIMEOUT", self.PLACEHOLDER)
        cfg = ThrukConfig.from_env()
        assert cfg.timeout == 30.0  # default

    def test_enabled_tools_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "secret")
        monkeypatch.setenv("THRUK_ENABLED_TOOLS", self.PLACEHOLDER)
        cfg = ThrukConfig.from_env()
        assert cfg.enabled_tools == ()  # default = all tools

    def test_default_backends_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("THRUK_API_KEY", "secret")
        monkeypatch.setenv("THRUK_DEFAULT_BACKENDS", self.PLACEHOLDER)
        cfg = ThrukConfig.from_env()
        assert cfg.default_backends == ()  # default = all backends

    def test_all_optional_placeholders_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reproducer from issue #16: only mandatory secrets bound, all others <UNKNOWN>."""
        monkeypatch.setenv("THRUK_BASE_URL", "https://monitor.example.net/thruk")
        monkeypatch.setenv("THRUK_API_KEY", "dummy")
        for var in (
            "THRUK_AUTH_USER",
            "THRUK_VERIFY_SSL",
            "THRUK_TIMEOUT",
            "THRUK_MAX_CONCURRENT",
            "THRUK_READ_ONLY",
            "THRUK_ENABLED_TOOLS",
            "THRUK_DEFAULT_BACKENDS",
            "THRUK_AUDIT_LOG",
        ):
            monkeypatch.setenv(var, self.PLACEHOLDER)

        cfg = ThrukConfig.from_env()
        assert cfg.api_key == "dummy"
        assert cfg.max_concurrent == 0
        assert cfg.verify_ssl is True
        assert cfg.read_only is False
        assert cfg.timeout == 30.0
        assert cfg.enabled_tools == ()
        assert cfg.default_backends == ()
        assert cfg.audit_log is True


# ---------------------------------------------------------------------------
# Robustness: invalid (non-placeholder) values fall back to defaults too
# ---------------------------------------------------------------------------


def test_int_env_invalid_value_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THRUK_MAX_CONCURRENT", "not_a_number")
    assert _int_env("THRUK_MAX_CONCURRENT", 0) == 0


def test_float_env_invalid_value_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THRUK_TIMEOUT", "??")
    assert _float_env("THRUK_TIMEOUT", 30.0) == 30.0


def test_envbool_unknown_string_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THRUK_VERIFY_SSL", "maybe")
    assert _envbool("THRUK_VERIFY_SSL", True) is False  # not in {1,true,yes,on}


def test_str_env_placeholder_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THRUK_AUTH_USER", "<UNKNOWN>")
    assert _str_env("THRUK_AUTH_USER", "fallback") == "fallback"
