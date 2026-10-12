"""Gateway settings, read from tenantguard.yaml (via tenantguard_common.config) plus env for deployment-only values."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

# Brief: the DRR cost of a class nobody has measured yet (CPU ms), and the base quantum per weight unit.
DEFAULT_CLASS_COST_MS = 5.0
# EWMA smoothing for a (tenant, class) average cost.
CLASS_COST_ALPHA = 0.2
# Upstream request timeout (s). Bounded-wait covers queueing; this only guards hung workers.
DEFAULT_UPSTREAM_TIMEOUT_S = 30.0
DEFAULT_CORS = "http://127.0.0.1:5173,http://localhost:5173"


@dataclass
class GatewaySettings:
    default_mode: str = "tenantguard"
    tick_ms: int = 250
    max_wait_ms: Dict[str, float] = field(default_factory=lambda: {"interactive": 1000.0, "batch": 5000.0})
    cost_bucket_s: float = 2.0
    express_cost_ms: float = 5.0
    fail_static_after_s: float = 2.0
    initial_window: int = 8
    body_limit_bytes: int = 1048576
    batch_routes: List[str] = field(default_factory=lambda: ["/api/export"])
    cors_origins: List[str] = field(default_factory=lambda: DEFAULT_CORS.split(","))
    upstream_timeout_s: float = DEFAULT_UPSTREAM_TIMEOUT_S

    @classmethod
    def from_dict(cls, s: Dict[str, Any]) -> "GatewaySettings":
        g = s.get("gateway", {}) or {}
        lim = g.get("limiter", {}) or {}
        w = s.get("worker", {}) or {}
        cors = os.environ.get("TENANTGUARD_CORS_ORIGINS", DEFAULT_CORS)
        return cls(
            default_mode=g.get("default_mode", "tenantguard"),
            tick_ms=int(g.get("tick_ms", 250)),
            max_wait_ms={k: float(v) for k, v in (g.get("max_queue_wait_ms") or {"interactive": 1000, "batch": 5000}).items()},
            cost_bucket_s=float(g.get("cost_bucket_s", 2)),
            express_cost_ms=float(g.get("express_cost_ms", 5)),
            fail_static_after_s=float(g.get("fail_static_after_s", 2)),
            initial_window=int(lim.get("initial_window", 8)),
            body_limit_bytes=int(w.get("body_limit_bytes", 1048576)),
            batch_routes=list(g.get("batch_routes", ["/api/export"])),
            cors_origins=[o.strip() for o in cors.split(",") if o.strip()],
            upstream_timeout_s=float(os.environ.get("TENANTGUARD_UPSTREAM_TIMEOUT_S", DEFAULT_UPSTREAM_TIMEOUT_S)),
        )
