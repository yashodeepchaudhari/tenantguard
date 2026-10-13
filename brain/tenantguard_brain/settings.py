"""Brain configuration: everything comes from tenantguard.yaml (via tenantguard_common.config) or env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

from tenantguard_common.config import ROOT, load_pool, load_projects, load_settings, load_tenants, tier_quota
from tenantguard_common.contracts import PoolSpec, ProjectSpec, Quota, TenantConfig

from .ladder import LadderParams
from .scaler import ScaleCfg


@dataclass
class DetectCfg:
    unattributed_ratio: float = 0.25        # estimate
    unattributed_min_ms: float = 50.0       # estimate: ms/s; an idle replica's own runtime never triggers a split
    unattributed_hold_s: float = 10.0       # estimate
    split_settle_s: float = 2.0
    split_sample_s: float = 6.0
    contention_ratio: float = 1.6
    contention_open_s: float = 4.0
    overhead_ms_per_request: float = 6.6    # max of process - handler - bg - black-box CPU in worker/calibration.json
    blame_ttl_s: float = 300.0              # how long a split-test culprit is charged the unexplained CPU

    @classmethod
    def from_yaml(cls, d: dict) -> "DetectCfg":
        split, cont, x = d.get("split", {}), d.get("contention", {}), cls()
        return cls(unattributed_ratio=float(d.get("unattributed_ratio", x.unattributed_ratio)),
                   unattributed_min_ms=float(d.get("unattributed_min_ms", x.unattributed_min_ms)),
                   unattributed_hold_s=float(d.get("unattributed_hold_s", x.unattributed_hold_s)),
                   split_settle_s=float(split.get("settle_s", x.split_settle_s)),
                   split_sample_s=float(split.get("sample_s", x.split_sample_s)),
                   contention_ratio=float(cont.get("ratio", x.contention_ratio)),
                   contention_open_s=float(cont.get("open_s", x.contention_open_s)),
                   overhead_ms_per_request=float(d.get("overhead_ms_per_request", x.overhead_ms_per_request)),
                   blame_ttl_s=float(d.get("blame_ttl_s", x.blame_ttl_s)))


@dataclass
class ControlCfg:
    tick_s: float = 1.0
    demand_alpha: float = 0.5
    backend: str = "docker"                 # docker | process
    docker_url: str = "unix:///var/run/docker.sock"
    docker_api: str = "v1.41"
    worker_image: str = "tenantguard-worker:latest"
    docker_network: str = ""
    cgroup_parent: str = ""
    process_base_port: int = 8100
    start_timeout_s: float = 30.0
    telemetry_stale_s: float = 2.0
    wake_hold_s: float = 5.0                # a wake keeps a project's demand above idle this long
    registry_path: Path = Path("brain/.state/registry.json")
    events_path: Path = Path("brain/.state/events.jsonl")
    scale: ScaleCfg = field(default_factory=ScaleCfg)

    @classmethod
    def from_yaml(cls, c: dict, root: Path) -> "ControlCfg":
        x = cls()
        return cls(tick_s=float(c.get("tick_s", x.tick_s)), demand_alpha=float(c.get("demand_alpha", x.demand_alpha)),
                   backend=os.environ.get("TENANTGUARD_BACKEND", c.get("backend", x.backend)),
                   docker_url=c.get("docker_url", x.docker_url), docker_api=c.get("docker_api", x.docker_api),
                   worker_image=os.environ.get("TENANTGUARD_WORKER_IMAGE", c.get("worker_image", x.worker_image)),
                   docker_network=os.environ.get("TENANTGUARD_DOCKER_NETWORK", c.get("docker_network", "")),
                   cgroup_parent=c.get("cgroup_parent", ""),
                   process_base_port=int(os.environ.get("TENANTGUARD_PROCESS_BASE_PORT",
                                                        c.get("process_base_port", x.process_base_port))),
                   start_timeout_s=float(c.get("start_timeout_s", x.start_timeout_s)),
                   telemetry_stale_s=float(c.get("telemetry_stale_s", x.telemetry_stale_s)),
                   registry_path=root / os.environ.get("TENANTGUARD_REGISTRY", c.get("registry_path", x.registry_path)),
                   events_path=root / c.get("events_path", x.events_path),
                   scale=ScaleCfg.from_yaml(c))


@dataclass
class BrainSettings:
    tick_s: float
    stats_poll_s: float
    gateway_url: str
    ladder: LadderParams
    noisy: dict
    detect_window_s: float = 2.0      # window for victim p95 and usage shares
    view_window_s: float = 5.0        # TenantView window (contracts: "last 5 s")
    server_window_s: float = 2.0      # ServerView window (contracts: "last 2 s")
    history_s: float = 60.0           # rolling state kept in memory
    baseline_half_life_s: float = 60.0
    default_mode: str = "tenantguard"
    control_token: str = "dev"
    ttl_max_s: float = 3600.0
    min_victim_impact: float = 0.2
    slow_abs_ms: float = 8.0
    default_cores: float = 0.5        # worker.cpus, until a replica reports cpu_limit_cores
    default_window: int = 8           # gateway.limiter.initial_window, until telemetry reports it
    tier_weights: Dict[str, float] = field(default_factory=dict)
    tier_quotas: Dict[str, Quota] = field(default_factory=dict)
    seed_pool: PoolSpec = field(default_factory=lambda: PoolSpec(cpu_total=1.0, mem_total_mb=1024))
    seed_projects: List[ProjectSpec] = field(default_factory=list)
    seed_tenants: List[TenantConfig] = field(default_factory=list)
    control: ControlCfg = field(default_factory=ControlCfg)
    detect: DetectCfg = field(default_factory=DetectCfg)
    stack: str = "tenantguard"        # compose project: scopes the replica containers the brain may touch
    root: Path = ROOT
    host: str = "0.0.0.0"
    port: int = 8200
    cors_origins: List[str] = field(default_factory=list)


def load(raw: dict | None = None) -> BrainSettings:
    raw = raw or load_settings()
    brain = raw["brain"]
    noisy = dict(brain["noisy"])
    gateway = os.environ.get("TENANTGUARD_GATEWAY_URL", raw["services"]["gateway"]).rstrip("/")
    brain_url = urlparse(raw["services"]["brain"])
    origins = os.environ.get("TENANTGUARD_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    return BrainSettings(
        tick_s=brain["tick_ms"] / 1000.0,
        stats_poll_s=float(brain["stats_poll_s"]),
        gateway_url=gateway,
        ladder=LadderParams.from_yaml(noisy, brain.get("ttl_s", {})),
        noisy=noisy,
        detect_window_s=float(brain.get("detect_window_s", 2.0)),
        baseline_half_life_s=float(noisy.get("baseline_half_life_s", 60.0)),
        default_mode=str(raw["gateway"].get("default_mode", "tenantguard")),
        control_token=os.environ.get("TENANTGUARD_CONTROL_TOKEN", "dev"),
        ttl_max_s=float(raw["gateway"].get("ttl_max_s", 3600)),
        min_victim_impact=float(noisy.get("min_victim_impact", 0.2)),
        slow_abs_ms=float(noisy.get("slow_abs_ms", 8)),
        default_cores=float(raw.get("worker", {}).get("cpus", 0.5)),
        default_window=int(raw["gateway"].get("limiter", {}).get("initial_window", 8)),
        tier_weights={k: float(v["weight"]) for k, v in raw["tiers"].items()},
        tier_quotas={k: tier_quota(raw, k) for k in raw["tiers"]},
        seed_pool=load_pool(raw), seed_projects=load_projects(raw), seed_tenants=load_tenants(raw),
        control=ControlCfg.from_yaml(raw.get("control", {}), ROOT),
        detect=DetectCfg.from_yaml(raw.get("detector", {})),
        stack=os.environ.get("COMPOSE_PROJECT", "tenantguard"),
        host=os.environ.get("TENANTGUARD_BRAIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("TENANTGUARD_BRAIN_PORT", brain_url.port or 8200)),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )
