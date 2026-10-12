"""Lab configuration: tenantguard.yaml (via tenantguard_common), lab/personas.yaml and env overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from tenantguard_common.config import load_settings, load_tenants, worker_urls
from tenantguard_common.contracts import TenantConfig

LAB_DIR = Path(__file__).resolve().parents[1]


@dataclass
class Endpoint:
    method: str
    path: str
    params: Dict[str, Any] = field(default_factory=dict)
    est_cpu_ms: float = 1.0


@dataclass
class Persona:
    tenant: TenantConfig
    rps: float
    mix: Dict[str, float]
    sla_p95_ms: float = 100.0
    end_users: int = 0
    note: str = ""

    @property
    def id(self) -> str:
        return self.tenant.id


@dataclass
class LabConfig:
    gateway_url: str
    brain_url: str
    lab_url: str
    ui_origins: List[str]
    results_dir: Path
    workers: Dict[str, str]
    worker_cpus: float
    personas: Dict[str, Persona]
    endpoints: Dict[str, Endpoint]
    attacks: Dict[str, Dict[str, Any]]
    run: Dict[str, Any]
    client: Dict[str, Any]
    rate_scale: float = 1.0
    autostart: bool = True
    default_attacker: str = "K"
    target_fleet_util: float = 0.30

    def persona(self, tenant: str) -> Persona:
        if tenant not in self.personas:
            raise KeyError(f"unknown tenant {tenant!r}")
        return self.personas[tenant]


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def load_config(personas_path: Optional[Path] = None) -> LabConfig:
    settings = load_settings()
    tenants = {t.id: t for t in load_tenants(settings)}
    path = Path(os.environ.get("TENANTGUARD_LAB_PERSONAS", personas_path or LAB_DIR / "personas.yaml"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    endpoints = {k: Endpoint(**v) for k, v in raw["endpoints"].items()}
    default_mix = raw["mix"]
    personas: Dict[str, Persona] = {}
    for tid, t in tenants.items():
        p = (raw.get("personas") or {}).get(tid, {})
        mix = p.get("mix", default_mix)
        unknown = set(mix) - set(endpoints)
        if unknown:
            raise ValueError(f"persona {tid}: unknown request kinds {sorted(unknown)}")
        personas[tid] = Persona(
            tenant=t, rps=float(p.get("rps", 5.0)), mix=dict(mix),
            sla_p95_ms=float(p.get("sla_p95_ms", 100.0)), end_users=int(p.get("end_users", 0)),
            note=p.get("note", ""),
        )

    services = settings.get("services", {})
    origins = os.environ.get("TENANTGUARD_UI_ORIGINS", "http://127.0.0.1:5173")
    return LabConfig(
        gateway_url=os.environ.get("TENANTGUARD_GATEWAY_URL", services.get("gateway", "http://127.0.0.1:8000")).rstrip("/"),
        brain_url=os.environ.get("TENANTGUARD_BRAIN_URL", services.get("brain", "http://127.0.0.1:8200")).rstrip("/"),
        lab_url=os.environ.get("TENANTGUARD_LAB_URL", services.get("lab", "http://127.0.0.1:8300")).rstrip("/"),
        ui_origins=[o.strip() for o in origins.split(",") if o.strip()],
        results_dir=Path(os.environ.get("TENANTGUARD_LAB_RESULTS", LAB_DIR / "results")),
        workers=worker_urls(settings),
        worker_cpus=float(settings.get("worker", {}).get("cpus", 0.5)),
        personas=personas,
        endpoints=endpoints,
        attacks=raw["attacks"],
        run=raw["run"],
        client=raw["client"],
        rate_scale=float(os.environ.get("TENANTGUARD_LAB_RATE_SCALE", raw.get("rate_scale", 1.0))),
        autostart=_env_bool("TENANTGUARD_LAB_AUTOSTART", bool(raw.get("autostart", True))),
        default_attacker=raw.get("default_attacker", "K"),
        target_fleet_util=float(raw.get("target_fleet_util", 0.30)),
    )


def estimated_fleet_cpu_ms_per_s(cfg: LabConfig) -> float:
    """Expected calm CPU-ms/s across the fleet, from est_cpu_ms (for printouts only)."""
    total = 0.0
    for p in cfg.personas.values():
        wsum = sum(p.mix.values()) or 1.0
        cost = sum(cfg.endpoints[k].est_cpu_ms * w / wsum for k, w in p.mix.items())
        total += p.rps * cfg.rate_scale * cost
    return total
