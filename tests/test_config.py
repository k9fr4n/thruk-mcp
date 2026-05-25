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


# ---------------------------------------------------------------------------
# Issue #122 — ThrukConfig.__repr__ must NOT expose api_key
# ---------------------------------------------------------------------------


class TestReprRedactsApiKey:
    """Regression tests for the security fix: api_key must never appear in
    repr() or str() output of ThrukConfig.

    Pre-fix behaviour (documented here as a reminder):
        >>> cfg = ThrukConfig(base_url="http://t.example.com/thruk", api_key="super-secret")
        >>> repr(cfg)
        "ThrukConfig(base_url='http://t.example.com/thruk', api_key='super-secret', ...)"
        # → key leaked verbatim into any log line that includes the config object.

    Post-fix behaviour:
        >>> repr(cfg)
        "ThrukConfig(base_url='http://t.example.com/thruk', api_key='***', ...)"
    """

    _SECRET = "super-secret-api-key-abc123"

    def _cfg(self) -> ThrukConfig:
        return ThrukConfig(base_url="https://t.example.com/thruk", api_key=self._SECRET)

    def test_repr_does_not_contain_api_key(self) -> None:
        """The actual key value must not appear anywhere in repr()."""
        assert self._SECRET not in repr(self._cfg())

    def test_repr_contains_redaction_placeholder(self) -> None:
        """repr() must contain '***' in place of the real key."""
        assert "api_key='***'" in repr(self._cfg())

    def test_str_does_not_contain_api_key(self) -> None:
        """str() (f-string interpolation) must also be safe."""
        cfg = self._cfg()
        assert self._SECRET not in str(cfg)
        assert self._SECRET not in f"{cfg}"

    def test_repr_includes_all_other_fields(self) -> None:
        """Diagnostic fields must still be visible so tracebacks are useful."""
        cfg = ThrukConfig(
            base_url="https://t.example.com/thruk",
            api_key=self._SECRET,
            auth_user="alice",
            verify_ssl=False,
            timeout=42.0,
            read_only=True,
            audit_log=False,
            max_concurrent=5,
        )
        r = repr(cfg)
        assert "https://t.example.com/thruk" in r
        assert "alice" in r
        assert "False" in r  # verify_ssl
        assert "42.0" in r  # timeout
        assert "True" in r  # read_only
        assert "5" in r  # max_concurrent

    def test_headers_still_returns_real_key(self) -> None:
        """The redaction must only affect repr/str — headers() must keep the
        real key so authentication continues to work."""
        cfg = self._cfg()
        assert cfg.headers()["X-Thruk-Auth-Key"] == self._SECRET

    def test_api_key_attribute_still_accessible(self) -> None:
        """The raw key must remain accessible on the object for code that
        explicitly reads cfg.api_key (e.g. headers())."""
        cfg = self._cfg()
        assert cfg.api_key == self._SECRET
