"""Load tenantguard.yaml: settings, the pool, and the seed projects and tenants (v2). Owned by the coordinator."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .contracts import PoolSpec, ProjectSpec, Quota, TenantConfig

ROOT = Path(os.environ.get("TENANTGUARD_ROOT", Path(__file__).resolve().parents[2]))


def load_settings() -> Dict[str, Any]:
    """Return tenantguard.yaml as a dict (path override: TENANTGUARD_CONFIG)."""
    path = Path(os.environ.get("TENANTGUARD_CONFIG", ROOT / "tenantguard.yaml"))
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_pool(settings: Dict[str, Any] | None = None) -> PoolSpec:
    """The pool seeded from the yaml (the admin API may change it at runtime)."""
    settings = settings or load_settings()
    return PoolSpec(**settings["pool"])


def load_projects(settings: Dict[str, Any] | None = None) -> List[ProjectSpec]:
    """Seed projects from the yaml."""
    settings = settings or load_settings()
    return [ProjectSpec(**p) for p in settings.get("projects") or []]


def tier_quota(settings: Dict[str, Any], tier: str) -> Quota:
    """Per-tenant request quota of a tier."""
    return Quota(**settings["tiers"][tier]["quota"])


def load_tenants(settings: Dict[str, Any] | None = None) -> List[TenantConfig]:
    """Seed tenants from the yaml, with tier and quota taken from their project."""
    settings = settings or load_settings()
    tier_of = {p.id: p.tier for p in load_projects(settings)}
    out = []
    for t in settings.get("tenants") or []:
        tier = tier_of[t["project"]]
        out.append(TenantConfig(tier=tier, quota=tier_quota(settings, tier), **t))
    return out
