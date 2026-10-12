"""Worker settings: tenantguard.yaml `worker:` section, overridden by env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tenantguard_common.config import load_settings, load_tenants

# Defaults used only if tenantguard.yaml lacks the key.
_DEFAULTS = {
    "cpus": 0.5,
    "mem_limit_mb": 256,
    "body_limit_bytes": 1_048_576,
    "tenant_mem_budget_mb": 32,
    "cache_mb": 8,
    "cache_guaranteed_share": 0.125,
    "bg_threads": 4,
    "seed_rows": 1000,
    "blackbox": {"cpu_ms_per_doc": 2.0, "queue_max": 5000},
    "max_request_s": 30,
    "bg_pending": {"per_tenant": 200, "total": 1000},
    "mem_baseline_mb": 80,
    "memory_trim_s": 30,
    "max_classes_per_tenant": 256,
}


@dataclass
class WorkerConfig:
    server: str
    data_dir: Path
    cpus: float
    mem_limit_mb: float
    body_limit_bytes: int
    tenant_mem_budget_mb: float
    cache_mb: float
    cache_guaranteed_share: float
    bg_threads: int
    seed_rows: int
    blackbox_cpu_ms_per_doc: float
    blackbox_queue_max: int
    max_request_s: float
    bg_pending_per_tenant: int
    bg_pending_total: int
    mem_baseline_mb: float
    memory_trim_s: float
    max_classes_per_tenant: int
    admin_token: str
    cors_origins: List[str]
    cpu_limit_env: Optional[float]
    tenants: List[str] = field(default_factory=list)


def _default_data_dir(server: str) -> Path:
    # Docker sets WORKER_DATA_DIR=/data; locally use ./data/<server>/.
    return Path("data") / server


def _tenants(settings: Dict[str, Any]) -> List[str]:
    """The tenants this replica serves: TENANTGUARD_TENANTS (set by the brain per project), else every seed tenant."""
    env = os.environ.get("TENANTGUARD_TENANTS")
    if env is not None:
        return [t for t in (x.strip() for x in env.split(",")) if t]
    return [t.id for t in load_tenants(settings)]


def load_config(settings: Optional[Dict[str, Any]] = None, **overrides: Any) -> WorkerConfig:
    settings = settings or load_settings()
    w = {**_DEFAULTS, **(settings.get("worker") or {})}
    bb = {**_DEFAULTS["blackbox"], **(w.get("blackbox") or {})}
    bgp = {**_DEFAULTS["bg_pending"], **(w.get("bg_pending") or {})}
    server = overrides.pop("server", None) or os.environ.get("SERVER_NAME", "w0")
    data_dir = overrides.pop("data_dir", None) or os.environ.get("WORKER_DATA_DIR") or _default_data_dir(server)
    cpu_env = os.environ.get("CPU_LIMIT_CORES")
    origins = os.environ.get("TENANTGUARD_UI_ORIGINS", "http://127.0.0.1:5173")
    cfg = WorkerConfig(
        server=server,
        data_dir=Path(data_dir),
        cpus=float(w["cpus"]),
        mem_limit_mb=float(w["mem_limit_mb"]),
        body_limit_bytes=int(w["body_limit_bytes"]),
        tenant_mem_budget_mb=float(w["tenant_mem_budget_mb"]),
        cache_mb=float(w["cache_mb"]),
        cache_guaranteed_share=float(w["cache_guaranteed_share"]),
        bg_threads=int(w["bg_threads"]),
        seed_rows=int(w["seed_rows"]),
        blackbox_cpu_ms_per_doc=float(bb["cpu_ms_per_doc"]),
        blackbox_queue_max=int(bb["queue_max"]),
        max_request_s=float(w["max_request_s"]),
        bg_pending_per_tenant=int(bgp["per_tenant"]),
        bg_pending_total=int(bgp["total"]),
        mem_baseline_mb=float(w["mem_baseline_mb"]),
        memory_trim_s=float(w["memory_trim_s"]),
        max_classes_per_tenant=int(w["max_classes_per_tenant"]),
        admin_token=os.environ.get("TENANTGUARD_ADMIN_TOKEN", "dev"),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
        cpu_limit_env=float(cpu_env) if cpu_env else None,
        tenants=_tenants(settings),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg
