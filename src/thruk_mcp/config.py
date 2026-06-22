"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = ["HttpAuthConfig", "ThrukConfig"]

# Default Host allowlist (anti-DNS-rebinding) when MCP_HTTP_ALLOWED_HOSTS is unset.
_DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")

# Inbound HTTP headers (lower-cased) that may override credential / endpoint
# fields per request in header-auth multi-tenant mode. Security knobs
# (read_only, enabled_tools, audit_log, pools) are deliberately NOT here — a
# client must never be able to escalate its own privileges via a header.
HEADER_AUTH_KEY = "x-thruk-auth-key"
HEADER_BASE_URL = "x-thruk-base-url"
HEADER_AUTH_USER = "x-thruk-auth-user"
HEADER_BACKENDS = "x-thruk-backends"

# Sentinel values injected by orchestrators (e.g. Docker MCP Gateway) when an
# optional secret is declared in the catalog but left unbound by the operator.
# We treat them as "not set" so that defaults are applied instead of crashing.
_PLACEHOLDERS: frozenset[str] = frozenset({"<UNKNOWN>"})


def _raw_env(name: str) -> str | None:
    """Return the raw env value, or None when absent / a known placeholder."""
    raw = os.getenv(name)
    if raw is None or raw in _PLACEHOLDERS:
        return None
    return raw


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _str_env(name: str, default: str = "") -> str:
    raw = _raw_env(name)
    return raw if raw is not None else default


def _int_env(name: str, default: int) -> int:
    raw = _raw_env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = _raw_env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _envbool(name: str, default: bool) -> bool:
    raw = _raw_env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ThrukConfig:
    base_url: str
    api_key: str
    auth_user: str = ""
    verify_ssl: bool = True
    timeout: float = 30.0
    default_backends: tuple[str, ...] = field(default_factory=tuple)

    # --- Security / multi-tenant knobs (v0.6) ----------------------------
    # When True, all write tools (acknowledge, schedule_*_downtime, recheck,
    # delete_*, run_background_query) are removed from the server registry.
    # Read tools and the GET-only `thruk_query` remain available.
    read_only: bool = False

    # Optional allowlist of tool names. Empty tuple = no filter (all enabled).
    # Glob-style wildcards via fnmatch (e.g. "thruk_list_*").
    enabled_tools: tuple[str, ...] = field(default_factory=tuple)

    # When True, every WRITE tool invocation is logged as one JSON line on
    # the `thruk_mcp.audit` logger (stderr by default).
    audit_log: bool = True

    # Max number of concurrent Thruk HTTP requests. 0 = unlimited.
    # Protects the Thruk core from an LLM that loops on tools.
    max_concurrent: int = 0

    # --- HTTP connection-pool knobs (v1.4) --------------------------------
    # Explicit httpx.Limits values. Defaults are far below the httpx default
    # (100 connections) to avoid saturating a single Thruk core under LLM
    # fan-out patterns. Exposed via env vars for operator tuning.
    max_connections: int = 20
    max_keepalive_connections: int = 10

    @classmethod
    def from_env(cls, *, require_api_key: bool = True) -> ThrukConfig:
        # THRUK_API_KEY is mandatory — also reject the placeholder — unless the
        # server runs in header-auth mode, where each request brings its own key
        # via the X-Thruk-Auth-Key header (require_api_key=False at boot).
        api_key = _str_env("THRUK_API_KEY").strip()
        if not api_key and require_api_key:
            raise RuntimeError(
                "THRUK_API_KEY is required. Generate one from the Thruk user profile page "
                "(see https://www.thruk.org/documentation/rest.html#api-key)."
            )
        return cls(
            base_url=_str_env("THRUK_BASE_URL", "http://localhost/thruk").rstrip("/"),
            api_key=api_key,
            auth_user=_str_env("THRUK_AUTH_USER").strip(),
            verify_ssl=_envbool("THRUK_VERIFY_SSL", True),
            timeout=_float_env("THRUK_TIMEOUT", 30.0),
            default_backends=_split_csv(_str_env("THRUK_DEFAULT_BACKENDS")),
            read_only=_envbool("THRUK_READ_ONLY", False),
            enabled_tools=_split_csv(_str_env("THRUK_ENABLED_TOOLS")),
            audit_log=_envbool("THRUK_AUDIT_LOG", True),
            max_concurrent=_int_env("THRUK_MAX_CONCURRENT", 0),
            max_connections=_int_env("THRUK_MAX_CONNECTIONS", 20),
            max_keepalive_connections=_int_env("THRUK_MAX_KEEPALIVE", 10),
        )

    def __repr__(self) -> str:
        """Return a safe representation that never exposes the api_key value.

        The auto-generated dataclass __repr__ would include api_key verbatim,
        which risks leaking credentials into log aggregators, tracebacks, and
        debug output.  We override it here to redact the key unconditionally.
        """
        return (
            f"ThrukConfig("
            f"base_url={self.base_url!r}, "
            f"api_key='***', "
            f"auth_user={self.auth_user!r}, "
            f"verify_ssl={self.verify_ssl!r}, "
            f"timeout={self.timeout!r}, "
            f"default_backends={self.default_backends!r}, "
            f"read_only={self.read_only!r}, "
            f"enabled_tools={self.enabled_tools!r}, "
            f"audit_log={self.audit_log!r}, "
            f"max_concurrent={self.max_concurrent!r}, "
            f"max_connections={self.max_connections!r}, "
            f"max_keepalive_connections={self.max_keepalive_connections!r})"
        )

    def __str__(self) -> str:
        """Delegate to __repr__ so f-string interpolation is equally safe."""
        return self.__repr__()

    def headers(self) -> dict[str, str]:
        h = {"X-Thruk-Auth-Key": self.api_key}
        if self.auth_user:
            h["X-Thruk-Auth-User"] = self.auth_user
        return h

    def with_overrides(self, **changes: Any) -> ThrukConfig:
        """Return a copy with the given fields replaced (frozen dataclass)."""
        return replace(self, **changes)

    @classmethod
    def from_headers(cls, base: ThrukConfig, headers: Mapping[str, str]) -> ThrukConfig:
        """Derive a per-request config from inbound ``X-Thruk-*`` headers.

        ``headers`` must be a mapping with **lower-cased** keys. Only the
        credential / endpoint fields are overridden; every security knob
        (``read_only``, ``enabled_tools``, ``audit_log``, pool sizes) is
        inherited from ``base`` so a client cannot grant itself write access or
        disable the audit log through a header.

        Raises ``ValueError`` when no ``X-Thruk-Auth-Key`` is supplied — the
        middleware turns that into an HTTP 401.
        """
        api_key = (headers.get(HEADER_AUTH_KEY) or "").strip()
        if not api_key:
            raise ValueError(f"missing required header {HEADER_AUTH_KEY!r}")
        overrides: dict[str, Any] = {"api_key": api_key}
        base_url = (headers.get(HEADER_BASE_URL) or "").strip()
        if base_url:
            overrides["base_url"] = base_url.rstrip("/")
        auth_user = (headers.get(HEADER_AUTH_USER) or "").strip()
        if auth_user:
            overrides["auth_user"] = auth_user
        backends = (headers.get(HEADER_BACKENDS) or "").strip()
        if backends:
            overrides["default_backends"] = _split_csv(backends)
        return base.with_overrides(**overrides)


@dataclass(frozen=True)
class HttpAuthConfig:
    """Transport-level auth for the Streamable-HTTP endpoint (provider-agnostic).

    These knobs gate *access to* the ``/mcp`` endpoint, independently of which
    Thruk credentials a request ultimately uses (those are ``THRUK_*`` /
    header-auth). Hence the ``MCP_HTTP_*`` prefix rather than ``THRUK_*``.

    - ``token`` (``MCP_HTTP_TOKEN``): bearer token required in the
      ``Authorization: Bearer <token>`` header. ``None`` = no bearer gate, which
      is only reachable via the explicit ``allow_unauthenticated`` opt-in
      (enforced at startup in ``__main__``).
    - ``allow_unauthenticated`` (``MCP_HTTP_ALLOW_UNAUTHENTICATED``): opt out of
      the bearer requirement, e.g. when fronting the server with an auth proxy.
    - ``allowed_hosts`` (``MCP_HTTP_ALLOWED_HOSTS``): ``Host`` header allowlist
      (anti-DNS-rebinding). Defaults to loopback names.
    """

    token: str | None = None
    allow_unauthenticated: bool = False
    allowed_hosts: tuple[str, ...] = _DEFAULT_ALLOWED_HOSTS

    @classmethod
    def from_env(cls) -> HttpAuthConfig:
        raw_token = _str_env("MCP_HTTP_TOKEN").strip()
        hosts = _split_csv(_str_env("MCP_HTTP_ALLOWED_HOSTS"))
        return cls(
            token=raw_token or None,
            allow_unauthenticated=_envbool("MCP_HTTP_ALLOW_UNAUTHENTICATED", False),
            allowed_hosts=hosts or _DEFAULT_ALLOWED_HOSTS,
        )

    def __repr__(self) -> str:
        """Redact the token so it never lands in logs / tracebacks."""
        return (
            f"HttpAuthConfig("
            f"token={'***' if self.token else None!r}, "
            f"allow_unauthenticated={self.allow_unauthenticated!r}, "
            f"allowed_hosts={self.allowed_hosts!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()
