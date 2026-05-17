"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _envbool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
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

    @classmethod
    def from_env(cls) -> ThrukConfig:
        api_key = os.getenv("THRUK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "THRUK_API_KEY is required. Generate one from the Thruk user profile page "
                "(see https://www.thruk.org/documentation/rest.html#api-key)."
            )
        return cls(
            base_url=os.getenv("THRUK_BASE_URL", "http://localhost/thruk").rstrip("/"),
            api_key=api_key,
            auth_user=os.getenv("THRUK_AUTH_USER", "").strip(),
            verify_ssl=_envbool("THRUK_VERIFY_SSL", True),
            timeout=float(os.getenv("THRUK_TIMEOUT", "30")),
            default_backends=_split_csv(os.getenv("THRUK_DEFAULT_BACKENDS", "")),
            read_only=_envbool("THRUK_READ_ONLY", False),
            enabled_tools=_split_csv(os.getenv("THRUK_ENABLED_TOOLS", "")),
            audit_log=_envbool("THRUK_AUDIT_LOG", True),
            max_concurrent=int(os.getenv("THRUK_MAX_CONCURRENT", "0")),
        )

    def headers(self) -> dict[str, str]:
        h = {"X-Thruk-Auth-Key": self.api_key}
        if self.auth_user:
            h["X-Thruk-Auth-User"] = self.auth_user
        return h
