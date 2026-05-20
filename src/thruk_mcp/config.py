"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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

    # --- Large-response spill (issue #49) ------------------------------------
    # Directory where large tool responses are written instead of being returned
    # inline. When None (default), all responses are returned inline.
    # Set THRUK_MCP_WORKDIR to enable (e.g. /tmp/thruk-report).
    workdir: Path | None = None

    # Payload size threshold in KB above which the response is spilled to disk
    # instead of returned inline. Default 256 KB — matches Dust's inline MCP
    # result cap. Lower this if your MCP client has a stricter limit.
    spill_threshold_kb: int = 256

    @classmethod
    def from_env(cls) -> ThrukConfig:
        # THRUK_API_KEY is mandatory — also reject the placeholder.
        api_key = _str_env("THRUK_API_KEY").strip()
        if not api_key:
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
            workdir=Path(_wd) if (_wd := _raw_env("THRUK_MCP_WORKDIR")) else None,
            spill_threshold_kb=_int_env("THRUK_SPILL_THRESHOLD_KB", 200),
        )

    def headers(self) -> dict[str, str]:
        h = {"X-Thruk-Auth-Key": self.api_key}
        if self.auth_user:
            h["X-Thruk-Auth-User"] = self.auth_user
        return h
