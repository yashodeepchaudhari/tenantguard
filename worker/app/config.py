"""Worker settings: tenantguard.yaml `worker:` section, overridden by env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from tenantguard_common.config import load_settings, load_tenants

# Defaults used only if tenantguard.yaml lacks the key (proposed yaml keys: worker.cache_mb,
# worker.cache_guaranteed_share, worker.bg_threads, worker.seed_rows).
_DEFAULTS = {
    "cpus": 0.5,
    "mem_limit_mb": 256,
    "body_limit_bytes": 1_048_576,
    "tenant_mem_budget_mb": 32,
    "cache_mb": 8,
    "cache_guaranteed_share": 0.125,
    "bg_threads": 4,
    "seed_rows": 1000,
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
    admin_token: str
    cors_origins: List[str]
    cpu_limit_env: Optional[float]
    tenants: List[str] = field(default_factory=list)


def _default_data_dir(server: str) -> Path:
    # Docker sets WORKER_DATA_DIR=/data; locally use ./data/<server>/.
    return Path("data") / server


def load_config(settings: Optional[Dict[str, Any]] = None, **overrides: Any) -> WorkerConfig:
    settings = settings or load_settings()
    w = {**_DEFAULTS, **(settings.get("worker") or {})}
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
        admin_token=os.environ.get("TENANTGUARD_ADMIN_TOKEN", "dev"),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
        cpu_limit_env=float(cpu_env) if cpu_env else None,
        tenants=[t.id for t in load_tenants(settings)],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg
