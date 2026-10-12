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
    max_reports: int = 200
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
            max_reports=int(n.get("max_reports", 200)),
            cors_origins=[o.strip() for o in cors.split(",") if o.strip()],
        )
