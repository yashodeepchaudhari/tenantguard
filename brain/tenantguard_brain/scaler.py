"""How many replicas each project runs and how much CPU each gets (pure, no I/O).

- replica memory   = round_up(baseline + cache + per_tenant * tenants)            (fixed at start, never shrunk)
- own replicas     = max(warm_min, ceil(floor / (per_replica_max * target_util))) (memory reserved for the project)
- replicas needed  = ceil(min(demand, max(alloc, floor)) / (per_replica_max * target_util)), >= warm_min,
                     >= busy_min while busy, <= ceil(cap / per_replica_max); up after scale_up_ticks,
                     down by one after scale_down_ticks, to zero after zero_after_ticks idle (warm_min 0 only)
- CPU per replica  = NanoCpus min(per_replica_max, max(alloc, floor) / n); CpuShares 10240 per core of floor / n
- plan()           = ordered actions: drain -> stop -> lower CPU -> start -> raise CPU, never above usable memory
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tenantguard_common.contracts import ProjectSpec, ReplicaView

SHARES_PER_CORE = 10240
SHARES_RANGE = (2, 262144)          # Docker's CpuShares range
IDLE_CPU = 0.01                     # demand below this (cores) counts as idle


@dataclass
class ScaleCfg:
    per_replica_max_cpu: float = 1.0
    target_util: float = 0.8
    busy_cpu: float = 0.5
    busy_min_replicas: int = 2
    scale_up_ticks: int = 2
    scale_down_ticks: int = 30
    zero_after_ticks: int = 300
    drain_timeout_s: float = 2.0
    mem_baseline_mb: int = 80
    mem_cache_mb: int = 8
    mem_per_tenant_mb: int = 32
    mem_round_mb: int = 32

    @classmethod
    def from_yaml(cls, c: dict) -> "ScaleCfg":
        m = c.get("replica_mem", {})
        d = cls()
        return cls(per_replica_max_cpu=float(c.get("per_replica_max_cpu", d.per_replica_max_cpu)),
                   target_util=float(c.get("target_util", d.target_util)),
                   busy_cpu=float(c.get("busy_cpu", d.busy_cpu)),
                   busy_min_replicas=int(c.get("busy_min_replicas", d.busy_min_replicas)),
                   scale_up_ticks=int(c.get("scale_up_ticks", d.scale_up_ticks)),
                   scale_down_ticks=int(c.get("scale_down_ticks", d.scale_down_ticks)),
                   zero_after_ticks=int(c.get("zero_after_ticks", d.zero_after_ticks)),
                   drain_timeout_s=float(c.get("drain_timeout_s", d.drain_timeout_s)),
                   mem_baseline_mb=int(m.get("baseline_mb", d.mem_baseline_mb)),
                   mem_cache_mb=int(m.get("cache_mb", d.mem_cache_mb)),
                   mem_per_tenant_mb=int(m.get("per_tenant_mb", d.mem_per_tenant_mb)),
                   mem_round_mb=int(m.get("round_mb", d.mem_round_mb)))


def replica_memory_mb(n_tenants: int, cfg: ScaleCfg) -> int:
    """Memory limit of one replica of a project with n_tenants tenants."""
    raw = cfg.mem_baseline_mb + cfg.mem_cache_mb + cfg.mem_per_tenant_mb * n_tenants
    return int(math.ceil(raw / cfg.mem_round_mb) * cfg.mem_round_mb)


def own_replicas(p: ProjectSpec, cfg: ScaleCfg) -> int:
    """Replicas a project may always run (its memory is reserved); more than this are borrowed."""
    return max(p.warm_min, math.ceil(p.floor_cpu / (cfg.per_replica_max_cpu * cfg.target_util) - 1e-9))


def max_replicas(p: ProjectSpec, cfg: ScaleCfg) -> int:
    return max(1, math.ceil(p.cap_cpu / cfg.per_replica_max_cpu - 1e-9))


@dataclass
class Hyst:
    up: int = 0
    down: int = 0
    idle: int = 0


def _raw_need(p: ProjectSpec, demand: float, alloc: float, idle_ticks: int, cfg: ScaleCfg) -> int:
    usable = min(demand, max(alloc, p.floor_cpu))
    need = math.ceil(usable / (cfg.per_replica_max_cpu * cfg.target_util) - 1e-9) if usable > IDLE_CPU else 0
    if demand > cfg.busy_cpu:
        need = max(need, cfg.busy_min_replicas)
    asleep = p.warm_min == 0 and idle_ticks >= cfg.zero_after_ticks
    floor = 0 if asleep else max(p.warm_min, 1)       # warm_min 0 keeps one replica until it has slept
    return min(max(need, floor), max_replicas(p, cfg))


def replicas_needed(p: ProjectSpec, current: int, demand: float, alloc: float, h: Hyst,
                    cfg: ScaleCfg) -> Tuple[int, Hyst]:
    """Target replica count for this tick, with hysteresis (a sleeping project with demand starts at once)."""
    idle = h.idle + 1 if demand <= IDLE_CPU else 0
    want = _raw_need(p, demand, alloc, idle, cfg)
    if current == 0 and want > 0:
        return want, Hyst(idle=idle)
    if want > current:
        up = h.up + 1
        return (want, Hyst(idle=idle)) if up >= cfg.scale_up_ticks else (current, Hyst(up=up, idle=idle))
    if want < current:
        down = h.down + 1
        if want == 0 or down >= cfg.scale_down_ticks:
            return (want if want == 0 else current - 1), Hyst(idle=idle)
        return current, Hyst(down=down, idle=idle)
    return current, Hyst(idle=idle)


def cpu_settings(p: ProjectSpec, alloc: float, n: int, cfg: ScaleCfg, mode: str) -> Tuple[int, int]:
    """(NanoCpus, CpuShares) per replica. off: unlimited, equal shares; industry: static floor; tenantguard:
    cap = max(alloc, floor) so an idle project is never capped below its floor when it wakes."""
    if n <= 0:
        return 0, 0
    shares = min(SHARES_RANGE[1], max(SHARES_RANGE[0], round(SHARES_PER_CORE * p.floor_cpu / n)))
    if mode == "off":
        return 0, 1024
    cores = p.floor_cpu if mode == "industry" else max(alloc, p.floor_cpu)
    return int(round(min(cfg.per_replica_max_cpu, cores / n) * 1e9)), shares


@dataclass
class Action:
    kind: str                         # drain | stop | update | start
    project: str
    replica_id: str
    nano_cpus: int = 0
    cpu_shares: int = 0
    mem_mb: int = 0
    borrowed: bool = False
    tenants: List[str] = field(default_factory=list)


@dataclass
class Target:
    """What the control loop wants for one project this tick."""
    spec: ProjectSpec
    tenants: List[str]
    replicas: int
    nano_cpus: int
    cpu_shares: int


@dataclass
class Fleet:
    """Current replicas, when each drain started, and in-flight requests per replica (from gateway telemetry)."""
    replicas: Dict[str, ReplicaView]
    drain_since: Dict[str, float] = field(default_factory=dict)
    inflight: Dict[str, int] = field(default_factory=dict)


def _live(fleet: Fleet, project: str) -> List[ReplicaView]:
    return [r for r in fleet.replicas.values() if r.project == project and r.state != "draining"]


def _drain_order(rs: List[ReplicaView]) -> List[ReplicaView]:
    """Borrowed first, then youngest."""
    return sorted(rs, key=lambda r: (not r.borrowed, -r.started_at))


def _next_id(fleet: Fleet, project: str, taken: set) -> str:
    n = 1
    while f"tg-{project}-{n}" in fleet.replicas or f"tg-{project}-{n}" in taken:
        n += 1
    taken.add(f"tg-{project}-{n}")
    return f"tg-{project}-{n}"


def _drains(fleet: Fleet, targets: Dict[str, Target]) -> List[Action]:
    """Replicas of removed projects, with a stale tenant set, or above the target count."""
    out = []
    for r in fleet.replicas.values():
        t = targets.get(r.project)
        if r.state != "draining" and (t is None or sorted(r.tenants) != sorted(t.tenants)):
            out.append(Action("drain", r.project, r.id))
    gone = {a.replica_id for a in out}
    for pid, t in targets.items():
        keep = [r for r in _live(fleet, pid) if r.id not in gone]
        for r in _drain_order(keep)[:max(0, len(keep) - t.replicas)]:
            out.append(Action("drain", pid, r.id))
    return out


def _stops(fleet: Fleet, now: float, cfg: ScaleCfg) -> List[Action]:
    out = []
    for r in fleet.replicas.values():
        since = fleet.drain_since.get(r.id)
        if r.state == "draining" and since is not None and (
                fleet.inflight.get(r.id, 0) == 0 or now - since >= cfg.drain_timeout_s):
            out.append(Action("stop", r.project, r.id))
    return out


def _updates(fleet: Fleet, targets: Dict[str, Target], skip: set) -> Tuple[List[Action], List[Action]]:
    """(lowering updates, raising updates) for live replicas whose CPU settings differ from the target."""
    lower, raise_ = [], []
    for pid, t in targets.items():
        for r in _live(fleet, pid):
            if r.id in skip or (r.nano_cpus, r.cpu_shares) == (t.nano_cpus, t.cpu_shares):
                continue
            a = Action("update", pid, r.id, t.nano_cpus, t.cpu_shares, r.mem_mb, r.borrowed, list(r.tenants))
            tighter = t.nano_cpus > 0 and (r.nano_cpus == 0 or t.nano_cpus < r.nano_cpus)
            (lower if tighter else raise_).append(a)
    return lower, raise_


def _reclaim(fleet: Fleet, owner: str, skip: set) -> Optional[Action]:
    """Drain one borrowed replica of another project so an owner below its own allowance can start."""
    lent = [r for r in fleet.replicas.values()
            if r.borrowed and r.project != owner and r.state != "draining" and r.id not in skip]
    if not lent:
        return None
    r = _drain_order(lent)[0]
    return Action("drain", r.project, r.id)


def _starts(fleet: Fleet, targets: Dict[str, Target], skip: set, mem_free: int,
            cfg: ScaleCfg) -> Tuple[List[Action], List[Action]]:
    """(starts, reclaim drains). Owners below their own allowance go first, then heavier tiers' borrowers."""
    wants = []
    for pid, t in targets.items():
        live = [r for r in _live(fleet, pid) if r.id not in skip]
        own = own_replicas(t.spec, cfg)
        for i in range(len(live), t.replicas):
            wants.append((i >= own, pid, t))
    starts, reclaims, taken = [], [], set()
    for borrowed, pid, t in sorted(wants, key=lambda w: w[0]):
        mem = replica_memory_mb(len(t.tenants), cfg)
        if mem <= mem_free:
            mem_free -= mem
            starts.append(Action("start", pid, _next_id(fleet, pid, taken), t.nano_cpus, t.cpu_shares, mem,
                                 borrowed, list(t.tenants)))
        elif not borrowed:
            rc = _reclaim(fleet, pid, skip | {a.replica_id for a in reclaims})
            if rc:
                reclaims.append(rc)
    return starts, reclaims


def plan(fleet: Fleet, targets: Dict[str, Target], usable_mem_mb: int, now: float,
         cfg: ScaleCfg) -> List[Action]:
    """Ordered actions that move the fleet toward the targets without ever exceeding usable memory.
    Memory of draining replicas still counts until they are stopped."""
    drains = _drains(fleet, targets)
    stops = _stops(fleet, now, cfg)
    skip = {a.replica_id for a in drains}
    lower, raise_ = _updates(fleet, targets, skip)
    used = sum(r.mem_mb for r in fleet.replicas.values() if r.id not in {a.replica_id for a in stops})
    starts, reclaims = _starts(fleet, targets, skip, usable_mem_mb - used, cfg)
    return drains + reclaims + stops + lower + starts + raise_
