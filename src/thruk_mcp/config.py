"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ThrukConfig:
    base_url: str
    api_key: str
    auth_user: str = ""
    verify_ssl: bool = True
    timeout: float = 30.0
    default_backends: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> ThrukConfig:
        api_key = os.getenv("THRUK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "THRUK_API_KEY is required. Generate one from the Thruk user profile page "
                "(see https://www.thruk.org/documentation/rest.html#api-key)."
            )
        backends = tuple(
            b.strip() for b in os.getenv("THRUK_DEFAULT_BACKENDS", "").split(",") if b.strip()
        )
        return cls(
            base_url=os.getenv("THRUK_BASE_URL", "http://localhost/thruk").rstrip("/"),
            api_key=api_key,
            auth_user=os.getenv("THRUK_AUTH_USER", "").strip(),
            verify_ssl=os.getenv("THRUK_VERIFY_SSL", "true").lower() != "false",
            timeout=float(os.getenv("THRUK_TIMEOUT", "30")),
            default_backends=backends,
        )

    def headers(self) -> dict[str, str]:
        h = {"X-Thruk-Auth-Key": self.api_key}
        if self.auth_user:
            h["X-Thruk-Auth-User"] = self.auth_user
        return h
