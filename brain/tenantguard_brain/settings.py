"""Brain configuration: everything comes from tenantguard.yaml (via tenantguard_common.config) or env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List
from urllib.parse import urlparse

from tenantguard_common.config import load_settings, load_tenants, worker_urls
from tenantguard_common.contracts import TenantConfig

from .ladder import LadderParams


@dataclass
class BrainSettings:
    tick_s: float
    stats_poll_s: float
    heartbeat_s: float
    gateway_url: str
    worker_urls: Dict[str, str]
    tenants: List[TenantConfig]
    ladder: LadderParams
    noisy: dict
    probe_window_s: float          # detection window for victim p95
    view_window_s: float = 5.0     # TenantView window (contracts: "last 5 s")
    server_window_s: float = 2.0   # ServerView window (contracts: "last 2 s")
    history_s: float = 60.0        # rolling state kept in memory
    baseline_alpha: float = 0.05
    min_victim_impact: float = 0.2
    slow_peer_ratio: float = 1.6
    slow_abs_ms: float = 8.0
    fleet_servers: List[str] = field(default_factory=list)
    default_cores: float = 0.5        # worker.cpus, until a worker reports cpu_limit_cores
    default_window: int = 8           # gateway.limiter.initial_window, until telemetry reports it
    host: str = "0.0.0.0"
    port: int = 8200
    cors_origins: List[str] = field(default_factory=list)


def load(raw: dict | None = None) -> BrainSettings:
    raw = raw or load_settings()
    brain = raw["brain"]
    noisy = dict(brain["noisy"])
    health = brain.get("health", {})
    tick_s = brain["tick_ms"] / 1000.0
    gateway = os.environ.get("TENANTGUARD_GATEWAY_URL", raw["services"]["gateway"]).rstrip("/")
    brain_url = urlparse(raw["services"]["brain"])
    origins = os.environ.get("TENANTGUARD_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    layout_servers: List[str] = []
    tenants = load_tenants(raw)
    for t in tenants:
        for s in t.servers:
            if s not in layout_servers:
                layout_servers.append(s)
    return BrainSettings(
        tick_s=tick_s,
        stats_poll_s=float(brain["stats_poll_s"]),
        # 4 heartbeats inside the gateway's fail-static window (2 s -> 500 ms).
        heartbeat_s=float(raw["gateway"]["fail_static_after_s"]) / 4.0,
        gateway_url=gateway,
        worker_urls={k: v.rstrip("/") for k, v in worker_urls(raw).items()},
        tenants=tenants,
        ladder=LadderParams.from_yaml(noisy, brain.get("ttl_s", {})),
        noisy=noisy,
        probe_window_s=health.get("probe_window_ticks", 8) * tick_s,
        baseline_alpha=float(noisy.get("baseline_alpha", 0.05)),
        min_victim_impact=float(noisy.get("min_victim_impact", 0.2)),
        slow_peer_ratio=float(health.get("slow_peer_ratio", 1.6)),
        slow_abs_ms=float(health.get("slow_abs_ms", 8)),
        fleet_servers=sorted(layout_servers),
        default_cores=float(raw.get("worker", {}).get("cpus", 0.5)),
        default_window=int(raw["gateway"].get("limiter", {}).get("initial_window", 8)),
        host=os.environ.get("TENANTGUARD_BRAIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("TENANTGUARD_BRAIN_PORT", brain_url.port or 8200)),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )
