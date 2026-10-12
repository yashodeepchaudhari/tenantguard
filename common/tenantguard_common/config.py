"""Load tenantguard.yaml and the tenant layout. Owned by the coordinator."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .contracts import Quota, TenantConfig

ROOT = Path(os.environ.get("TENANTGUARD_ROOT", Path(__file__).resolve().parents[2]))


def load_settings() -> Dict[str, Any]:
    """Return tenantguard.yaml as a dict (path override: TENANTGUARD_CONFIG)."""
    path = Path(os.environ.get("TENANTGUARD_CONFIG", ROOT / "tenantguard.yaml"))
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_tenants(settings: Dict[str, Any] | None = None) -> List[TenantConfig]:
    """Return tenants from the layout file with tier quota and weight filled in."""
    settings = settings or load_settings()
    path = Path(os.environ.get("TENANTGUARD_LAYOUT", ROOT / settings["layout"]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    tiers = settings["tiers"]
    out = []
    for t in raw["tenants"]:
        tier = tiers[t["tier"]]
        out.append(TenantConfig(weight=tier["weight"], quota=Quota(**tier["quota"]), **t))
    return out


def worker_urls(settings: Dict[str, Any] | None = None) -> Dict[str, str]:
    """Return server name -> base URL (env TENANTGUARD_WORKER_URLS='w0=http://..,w1=..' overrides)."""
    settings = settings or load_settings()
    env = os.environ.get("TENANTGUARD_WORKER_URLS")
    if env:
        return dict(kv.split("=", 1) for kv in env.split(","))
    return dict(settings["workers"])
