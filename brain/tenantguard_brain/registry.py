"""Who exists: the pool, projects and tenants (pure, no I/O). Every change is validated by check_capacity.

Invariants (docs/v2/02_ARCHITECTURE.md §2):
  I1 a tenant belongs to exactly one project (ids unique, case-insensitively: replicas keep one SQLite file each)
  I2 sum of floors <= usable CPU; sum of reserved replica memory <= usable memory
  I3 0 < floor <= cap <= cpu_total for every project
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Dict, List, Mapping, Tuple

from pydantic import BaseModel

from tenantguard_common.contracts import (PoolSpec, ProjectPatch, ProjectSpec, Quota, SnapTenant, TenantConfig,
                                          TenantCreate)

from .scaler import ScaleCfg, own_replicas, replica_memory_mb

PROJECT_ID = re.compile(r"^[a-z0-9-]{1,32}$")
TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")    # same rule as the worker (tenancy.TENANT_RE)


class Invalid(ValueError):
    """The change would break an invariant (HTTP 422)."""


class NotFound(KeyError):
    """Unknown project or tenant (HTTP 404)."""


class Conflict(ValueError):
    """Duplicate id or key, or a project that still has tenants (HTTP 409)."""


class TenantRecord(BaseModel):
    id: str
    name: str
    project: str
    key_sha256: str
    monthly_revenue_rs: int = 0
    protected: bool = False
    sla_p95_ms: float = 100.0


class Registry(BaseModel):
    pool: PoolSpec
    projects: Dict[str, ProjectSpec]
    tenants: Dict[str, TenantRecord]
    version: int = 0


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def usable(pool: PoolSpec) -> Tuple[float, int]:
    """(usable cores, usable MB) after headroom."""
    keep = 1.0 - pool.headroom_ratio
    return pool.cpu_total * keep, int(pool.mem_total_mb * keep)


def tenants_of(reg: Registry, project: str) -> List[str]:
    return sorted(t.id for t in reg.tenants.values() if t.project == project)


def check_capacity(reg: Registry, cfg: ScaleCfg) -> List[str]:
    """Every invariant the registry breaks (empty = valid)."""
    errs = []
    pool = reg.pool
    if not (pool.cpu_total > 0 and pool.mem_total_mb > 0 and 0 <= pool.headroom_ratio < 1):
        errs.append("pool needs cpu_total > 0, mem_total_mb > 0 and 0 <= headroom_ratio < 1")
    cpu, mem = usable(pool)
    for p in reg.projects.values():
        if not (0 < p.floor_cpu <= p.cap_cpu <= pool.cpu_total):
            errs.append(f"project {p.id}: need 0 < floor_cpu <= cap_cpu <= cpu_total ({pool.cpu_total:g})")
        if p.warm_min < 0:
            errs.append(f"project {p.id}: warm_min must be >= 0")
    floors = sum(p.floor_cpu for p in reg.projects.values())
    if floors > cpu + 1e-9:
        errs.append(f"sum of floors {floors:g} cores > usable {cpu:g} cores")
    reserved = sum(own_replicas(p, cfg) * replica_memory_mb(len(tenants_of(reg, p.id)), cfg)
                   for p in reg.projects.values())
    if reserved > mem:
        errs.append(f"reserved replica memory {reserved} MB > usable {mem} MB")
    return errs


def _checked(reg: Registry, cfg: ScaleCfg) -> Registry:
    errs = check_capacity(reg, cfg)
    if errs:
        raise Invalid("; ".join(errs))
    return reg.model_copy(update={"version": reg.version + 1})


def set_pool(reg: Registry, pool: PoolSpec, cfg: ScaleCfg) -> Registry:
    return _checked(reg.model_copy(update={"pool": pool}), cfg)


def create_project(reg: Registry, spec: ProjectSpec, cfg: ScaleCfg) -> Registry:
    if not PROJECT_ID.match(spec.id):
        raise Invalid("project id must match ^[a-z0-9-]{1,32}$")
    if spec.id in reg.projects:
        raise Conflict(f"project {spec.id} already exists")
    return _checked(reg.model_copy(update={"projects": {**reg.projects, spec.id: spec}}), cfg)


def update_project(reg: Registry, pid: str, patch: ProjectPatch, cfg: ScaleCfg) -> Registry:
    if pid not in reg.projects:
        raise NotFound(pid)
    new = reg.projects[pid].model_copy(update=patch.model_dump(exclude_none=True))
    return _checked(reg.model_copy(update={"projects": {**reg.projects, pid: new}}), cfg)


def remove_project(reg: Registry, pid: str) -> Registry:
    if pid not in reg.projects:
        raise NotFound(pid)
    if tenants_of(reg, pid):
        raise Conflict(f"project {pid} still has tenants")
    rest = {k: v for k, v in reg.projects.items() if k != pid}
    return reg.model_copy(update={"projects": rest, "version": reg.version + 1})


def add_tenant(reg: Registry, pid: str, req: TenantCreate, cfg: ScaleCfg) -> Tuple[Registry, str]:
    """Bind a new tenant to a project (I1). Returns the registry and the plaintext key (shown once)."""
    if pid not in reg.projects:
        raise NotFound(pid)
    if not TENANT_ID.match(req.id):
        raise Invalid("tenant id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
    if req.id.lower() in {t.lower() for t in reg.tenants}:
        raise Conflict(f"tenant {req.id} already exists")
    key = req.api_key or "tk_" + secrets.token_urlsafe(32)
    if hash_key(key) in {t.key_sha256 for t in reg.tenants.values()}:
        raise Conflict("api_key already in use")
    rec = TenantRecord(id=req.id, name=req.name, project=pid, key_sha256=hash_key(key),
                       monthly_revenue_rs=req.monthly_revenue_rs, protected=req.protected, sla_p95_ms=req.sla_p95_ms)
    return _checked(reg.model_copy(update={"tenants": {**reg.tenants, req.id: rec}}), cfg), key


def remove_tenant(reg: Registry, tid: str) -> Registry:
    if tid not in reg.tenants:
        raise NotFound(tid)
    rest = {k: v for k, v in reg.tenants.items() if k != tid}
    return reg.model_copy(update={"tenants": rest, "version": reg.version + 1})


def seed(pool: PoolSpec, projects: List[ProjectSpec], tenants: List[TenantConfig], cfg: ScaleCfg) -> Registry:
    """The registry from tenantguard.yaml (first start, no registry file yet)."""
    recs = {t.id: TenantRecord(id=t.id, name=t.name, project=t.project, key_sha256=hash_key(t.api_key),
                               monthly_revenue_rs=t.monthly_revenue_rs, protected=t.protected,
                               sla_p95_ms=t.sla_p95_ms) for t in tenants}
    reg = Registry(pool=pool, projects={p.id: p for p in projects}, tenants=recs)
    unknown = sorted({r.project for r in recs.values()} - set(reg.projects))
    if unknown:
        raise Invalid(f"tenants reference unknown projects {unknown}")
    return _checked(reg, cfg)


def snap_tenants(reg: Registry, quotas: Mapping[str, Quota]) -> List[SnapTenant]:
    """Tenants as the gateway needs them: key hash, project, tier and that tier's quota."""
    out = []
    for t in sorted(reg.tenants.values(), key=lambda r: r.id):
        tier = reg.projects[t.project].tier
        out.append(SnapTenant(id=t.id, name=t.name, project=t.project, tier=tier, key_sha256=t.key_sha256,
                              quota=quotas[tier], monthly_revenue_rs=t.monthly_revenue_rs,
                              protected=t.protected, sla_p95_ms=t.sla_p95_ms))
    return out
