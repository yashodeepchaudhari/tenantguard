"""Narrator settings: tenantguard.yaml (narrator, services) + env for deployment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List
from urllib.parse import urlparse


@dataclass
class NarratorSettings:
    brain_url: str = "http://127.0.0.1:8200"
    port: int = 8700
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_tokens: int = 4096
    timeout_s: float = 30.0
    ai_enabled: bool = True             # False when no API key is configured (template only)
    cors_origins: List[str] = field(default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"])

    @classmethod
    def from_dict(cls, s: Dict[str, Any]) -> "NarratorSettings":
        n = s.get("narrator", {}) or {}
        svc = s.get("services", {}) or {}
        env = os.environ.get
        cors = env("TENANTGUARD_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
        return cls(
            brain_url=env("TENANTGUARD_BRAIN_URL", svc.get("brain", "http://127.0.0.1:8200")),
            port=urlparse(svc.get("narrator", "http://127.0.0.1:8700")).port or 8700,
            model=env("NARRATOR_MODEL", n.get("model", "claude-opus-5-5")),
            effort=str(n.get("effort", "low")),
            max_tokens=int(n.get("max_tokens", 4096)),
            timeout_s=float(n.get("timeout_s", 30)),
            ai_enabled=bool(env("ANTHROPIC_API_KEY")) and env("NARRATOR_AI", "1") != "0",
            cors_origins=[o.strip() for o in cors.split(",") if o.strip()],
        )
