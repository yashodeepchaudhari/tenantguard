"""Brain configuration: everything comes from tenantguard.yaml (via tenantguard_common.config) or env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List
from urllib.parse import urlparse

from tenantguard_common.config import load_settings, load_tenants, worker_urls
from tenantguard_common.contracts import TenantConfig

from .decoder import DecoderParams
from .health import HealthParams
from .ladder import LadderParams
from .prover import ProverParams
from .verdicts import ActionBudget, VerdictParams


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
    baseline_half_life_s: float = 60.0
    victim_window_s: float = 5.0      # victim latency from per-tick p95 over this window ...
    victim_min_ticks: int = 4
    over_use: float = 1.25            # flagged only at >= this x its fair share
    victim_quantile: float = 0.3      # ... this quantile of it (a slowdown must cover ~70% of the window)
    victim_abs_ms: float = 8.0        # a victim is slower only if >= this many ms above its baseline
    default_mode: str = "tenantguard"
    control_token: str = "dev"
    ttl_max_s: float = 3600.0
    charge_s: float = 1.0
    verdicts: VerdictParams = field(default_factory=VerdictParams)
    action_budget: ActionBudget = field(default_factory=ActionBudget)
    prover: ProverParams = field(default_factory=ProverParams)
    prover_mode: str = "auto"         # auto | docker | null
    min_victim_impact: float = 0.2
    slow_peer_ratio: float = 1.6
    slow_abs_ms: float = 8.0
    fleet_servers: List[str] = field(default_factory=list)
    default_cores: float = 0.5        # worker.cpus, until a worker reports cpu_limit_cores
    default_window: int = 8           # gateway.limiter.initial_window, until telemetry reports it
    health: HealthParams = field(default_factory=HealthParams)
    decoder: DecoderParams = field(default_factory=DecoderParams)
    rescue_guard: float = 0.7
    rescue_ttl_s: float = 120.0
    canary_s: float = 1.0
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
        baseline_half_life_s=float(noisy.get("baseline_half_life_s", 60.0)),
        victim_window_s=float(noisy.get("victim_window_s", 5.0)),
        victim_min_ticks=int(noisy.get("victim_min_ticks", 4)),
        victim_quantile=float(noisy.get("victim_quantile", 0.3)),
        over_use=float(noisy.get("over_use", 1.25)),
        victim_abs_ms=float(noisy.get("victim_abs_ms", health.get("slow_abs_ms", 8))),
        default_mode=str(raw["gateway"].get("default_mode", "tenantguard")),
        control_token=os.environ.get("TENANTGUARD_CONTROL_TOKEN", "dev"),
        ttl_max_s=float(raw["gateway"].get("ttl_max_s", 3600)),
        charge_s=float(brain.get("charge_s", 1.0)),
        verdicts=VerdictParams.from_yaml(brain),
        action_budget=ActionBudget.from_yaml(brain),
        prover=ProverParams.from_yaml(brain),
        prover_mode=os.environ.get("TENANTGUARD_PROVER", str(brain.get("prover", "auto"))),
        min_victim_impact=float(noisy.get("min_victim_impact", 0.2)),
        slow_peer_ratio=float(health.get("slow_peer_ratio", 1.6)),
        slow_abs_ms=float(health.get("slow_abs_ms", 8)),
        fleet_servers=sorted(layout_servers),
        default_cores=float(raw.get("worker", {}).get("cpus", 0.5)),
        default_window=int(raw["gateway"].get("limiter", {}).get("initial_window", 8)),
        health=HealthParams.from_yaml(health, tick_s),
        decoder=DecoderParams.from_yaml(health, brain.get("decoder")),
        rescue_guard=float(brain.get("rescue_capacity_guard", 0.7)),
        rescue_ttl_s=float(brain.get("ttl_s", {}).get("rescue", brain.get("ttl_s", {}).get("relocate", 120))),
        canary_s=float(health.get("canary_s", 1.0)),
        host=os.environ.get("TENANTGUARD_BRAIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("TENANTGUARD_BRAIN_PORT", brain_url.port or 8200)),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )
