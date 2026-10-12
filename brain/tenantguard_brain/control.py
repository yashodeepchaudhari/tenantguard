"""The control loop (pure, no I/O): the only writer of allocations, replicas, split tests and the snapshot.

Every control tick (docs/v2/05_DATA_AND_CONFIG.md §4):
  container CPU -> attribution per replica -> demand per project -> allocate (floors, weighted surplus)
  -> replica targets -> ordered actions (the runtime applies them through a backend)
  -> detection inside projects (hash split, external contention) -> findings for the brain's incident book.
The runtime builds an Observation, calls tick(), applies the actions, reports the results with applied(), marks
replicas ready, and publishes snapshot().
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tenantguard_common.contracts import LimitsCmd, PoolView, ProjectView, ReplicaView, Snapshot

from . import detector as det
from . import registry as reg_mod
from .allocator import Alloc, allocate, estimate_demand
from .backends import ActionResult
from .registry import Registry
from .scaler import IDLE_CPU, Action, Fleet, Hyst, Target, cpu_settings, own_replicas, plan, replicas_needed
from .settings import BrainSettings

WAKE_DEMAND = 0.1      # cores a woken project is assumed to need until its own demand shows up
SATURATED = 0.9        # estimate: a project using this share of its CPU caps may want more than it can show
PROBE = 1.25           # estimate: ...so its demand is taken as at least this multiple of its use


@dataclass
class Observation:
    now: float
    mode: Optional[str]
    fresh: bool                                                    # gateway telemetry is current
    queued_ms: Dict[str, float] = field(default_factory=dict)      # tenant -> reference cost queued now
    refused_ms_s: Dict[str, float] = field(default_factory=dict)   # tenant -> refused cost per second
    usage: Dict[str, det.ReplicaUsage] = field(default_factory=dict)   # replica -> meters (container filled here)
    container_ms: Dict[str, float] = field(default_factory=dict)  # replica -> cumulative cgroup CPU ms
    inflight: Dict[str, int] = field(default_factory=dict)        # replica -> requests in flight
    slowdown: Dict[str, Optional[float]] = field(default_factory=dict)   # tenant -> p95 / baseline - 1


@dataclass
class Finding:
    kind: str                  # split_round | unattributed_work | split_aborted | external_contention
    project: Optional[str]
    tenant: Optional[str]
    detail: str
    evidence: dict = field(default_factory=dict)


class ControlLoop:
    def __init__(self, cfg: BrainSettings, registry: Registry):
        self.cfg = cfg
        self.scale = cfg.control.scale
        self.registry = registry
        self.fleet = Fleet({})
        self.starting_since: Dict[str, float] = {}
        self.demand: Dict[str, float] = {}
        self.hyst: Dict[str, Hyst] = {}
        self.allocs: Dict[str, Alloc] = {}
        self.used: Dict[str, float] = {}                  # project -> cores (cgroup)
        self.woken: Dict[str, float] = {}
        self.cpu_prev: Dict[str, tuple] = {}              # replica -> (cumulative ms, at)
        self.cpu_rate: Dict[str, float] = {}              # replica -> ms/s
        self.attributed: Dict[str, Dict[str, float]] = {}  # replica -> tenant -> ms/s
        self.unattributed: Dict[str, float] = {}          # replica -> ms/s
        self.unexplained_since: Dict[str, float] = {}
        self.contention_since: Optional[float] = None
        self.contention_open = False
        self.split: Optional[det.Split] = None
        self.split_waiting_since: Optional[float] = None
        self.blame: Dict[str, tuple] = {}                 # project -> (tenant, until)
        self.version = 0
        self.mode = cfg.default_mode

    # ------------------------------------------------------------------ registry / fleet bookkeeping
    def set_registry(self, registry: Registry) -> None:
        self.registry = registry
        for p in [p for p in self.demand if p not in registry.projects]:
            for d in (self.demand, self.hyst, self.allocs, self.used, self.woken, self.blame):
                d.pop(p, None)

    def adopt(self, replicas: List[ReplicaView], now: float) -> None:
        """Replicas found running at start (labels): they must pass a health check before taking traffic."""
        for r in replicas:
            self.fleet.replicas[r.id] = r.model_copy(update={"state": "starting"})
            self.starting_since[r.id] = now

    def tenants_of(self, project: str) -> List[str]:
        return reg_mod.tenants_of(self.registry, project)

    def wake(self, project: str, now: float) -> bool:
        if project not in self.registry.projects:
            return False
        self.woken[project] = now
        return True

    def mark_ready(self, rid: str) -> None:
        r = self.fleet.replicas.get(rid)
        if r is not None and r.state == "starting":
            self.fleet.replicas[rid] = r.model_copy(update={"state": "ready"})
            self.starting_since.pop(rid, None)

    def applied(self, results: List[ActionResult], now: float) -> None:
        for res in results:
            a = res.action
            if not res.ok:
                continue
            if a.kind == "start" and res.replica is not None:
                self.fleet.replicas[a.replica_id] = res.replica
                self.starting_since[a.replica_id] = now
            elif a.kind == "drain" and a.replica_id in self.fleet.replicas:
                self.fleet.replicas[a.replica_id] = self.fleet.replicas[a.replica_id].model_copy(update={"state": "draining"})
                self.fleet.drain_since[a.replica_id] = now
            elif a.kind == "update" and a.replica_id in self.fleet.replicas:
                self.fleet.replicas[a.replica_id] = self.fleet.replicas[a.replica_id].model_copy(
                    update={"nano_cpus": a.nano_cpus, "cpu_shares": a.cpu_shares})
            elif a.kind == "stop":
                for d in (self.fleet.replicas, self.fleet.drain_since, self.starting_since, self.cpu_prev,
                          self.cpu_rate, self.attributed, self.unattributed):
                    d.pop(a.replica_id, None)

    def replicas_of(self, project: str, states=("starting", "ready", "draining")) -> List[ReplicaView]:
        return sorted((r for r in self.fleet.replicas.values() if r.project == project and r.state in states),
                      key=lambda r: r.id)

    # ------------------------------------------------------------------ the tick
    def tick(self, obs: Observation) -> tuple:
        """(actions, findings) for this tick."""
        self.mode = obs.mode or self.mode
        self.fleet.inflight = dict(obs.inflight)
        self._container_rates(obs)
        self._attribute(obs)
        if not obs.fresh:
            return [], []                       # no evidence either way: hold allocations and replicas
        self._demand(obs)
        cpu, mem = reg_mod.usable(self.registry.pool)
        weights = {p: self.cfg.tier_weights.get(s.tier, 1.0) for p, s in self.registry.projects.items()}
        self.allocs = allocate(cpu, self.registry.projects, weights, self.demand, self.mode)
        actions = plan(self.fleet, self._targets(), mem, obs.now, self.scale) + self._start_timeouts(obs.now)
        findings = self._detect(obs) if self.mode == "tenantguard" else []
        return actions, findings

    def _container_rates(self, obs: Observation) -> None:
        for rid, ms in obs.container_ms.items():
            prev = self.cpu_prev.get(rid)
            self.cpu_prev[rid] = (ms, obs.now)
            if prev and obs.now > prev[1] and ms >= prev[0]:
                rate = (ms - prev[0]) / (obs.now - prev[1])
                old = self.cpu_rate.get(rid)
                self.cpu_rate[rid] = rate if old is None else 0.5 * rate + 0.5 * old

    def _blamed(self, project: str, now: float) -> Optional[str]:
        b = self.blame.get(project)
        if b and b[1] > now and b[0] in self.registry.tenants:
            return b[0]
        self.blame.pop(project, None)
        return None

    def _attribute(self, obs: Observation) -> None:
        self.used = {p: 0.0 for p in self.registry.projects}
        for r in self.fleet.replicas.values():
            u = obs.usage.get(r.id) or det.ReplicaUsage(container_ms=None)
            u = det.ReplicaUsage(self.cpu_rate.get(r.id, u.container_ms), u.metered, u.requests, u.docs, u.blackbox_ms)
            a = det.attribute(u, self.cfg.detect, self._blamed(r.project, obs.now))
            self.attributed[r.id], self.unattributed[r.id] = a.tenant_ms, a.unattributed_ms
            if r.project in self.used:
                self.used[r.project] += a.container_ms / 1000.0

    def tenant_cpu_by_replica(self) -> Dict[str, Dict[str, float]]:
        """The one source of per-tenant CPU (ms/s) for scores and views."""
        return {rid: dict(v) for rid, v in self.attributed.items()}

    def _caps(self, project: str) -> float:
        """Cores the project's live replicas may use now (NanoCpus; 0 = unlimited counts as one core each)."""
        return sum(r.nano_cpus / 1e9 or 1.0 for r in self.replicas_of(project, ("ready",)))

    def _demand(self, obs: Observation) -> None:
        """Used + queued + refused work. A project held at its caps cannot show more than it gets, so then its
        demand is probed upward (x PROBE) and grows into idle capacity while there is any."""
        for p, spec in self.registry.projects.items():
            ts = self.tenants_of(p)
            used = self.used.get(p, 0.0)
            raw = used + sum(obs.queued_ms.get(t, 0.0) + obs.refused_ms_s.get(t, 0.0) for t in ts) / 1000.0
            caps = self._caps(p)
            if caps > 0 and used >= SATURATED * caps:
                raw = max(raw, PROBE * used)
            if obs.now - self.woken.get(p, -1e9) <= self.cfg.control.wake_hold_s:
                raw = max(raw, WAKE_DEMAND)
            self.demand[p] = estimate_demand(self.demand.get(p, 0.0), raw, self.cfg.control.demand_alpha, spec.cap_cpu)

    def _targets(self) -> Dict[str, Target]:
        out = {}
        for p, spec in self.registry.projects.items():
            live = self.replicas_of(p, ("starting", "ready"))
            if self.mode == "tenantguard":
                n, self.hyst[p] = replicas_needed(spec, len(live), self.demand.get(p, 0.0), self.allocs[p].total,
                                                  self.hyst.get(p, Hyst()), self.scale)
                if self.split is not None and self.split.project == p:
                    n = max(n, 2)
            else:
                n = max(1, own_replicas(spec, self.scale))      # off / industry: a static fleet
            nano, shares = cpu_settings(spec, self.allocs[p].total, n, self.scale, self.mode)
            out[p] = Target(spec, self.tenants_of(p), n, nano, shares)
        return out

    def _start_timeouts(self, now: float) -> List[Action]:
        out = []
        for rid, since in list(self.starting_since.items()):
            r = self.fleet.replicas.get(rid)
            if r is not None and now - since > self.cfg.control.start_timeout_s:
                out.append(Action("stop", r.project, rid))
        return out

    # ------------------------------------------------------------------ detection (tenantguard mode)
    def _project_slowdowns(self, obs: Observation, p: str) -> List[float]:
        return [s for t in self.tenants_of(p) if (s := obs.slowdown.get(t)) is not None]

    def _detect(self, obs: Observation) -> List[Finding]:
        out = self._contention(obs)
        if self.split is not None:
            return out + self._advance_split(obs)
        for p in self.registry.projects:
            reps = [r.id for r in self.replicas_of(p, ("ready",))]
            un = sum(self.unattributed.get(r, 0.0) for r in reps)
            cont = sum(self.cpu_rate.get(r, 0.0) for r in reps)
            hurt = any(s >= self.cfg.min_victim_impact for s in self._project_slowdowns(obs, p))
            active = [t for t in self.tenants_of(p) if any(t in self.attributed.get(r, {}) for r in reps)]
            if not det.needs_split(un, cont, hurt, len(active), self.cfg.detect):
                self.unexplained_since.pop(p, None)
                continue
            since = self.unexplained_since.setdefault(p, obs.now)
            if obs.now - since >= self.cfg.detect.unattributed_hold_s and self._blamed(p, obs.now) is None:
                self.split = det.Split(p, sorted(active), ("", ""), started_at=obs.now)
                self.split_waiting_since = obs.now
                self.unexplained_since.pop(p, None)
                return out
        return out

    def _advance_split(self, obs: Observation) -> List[Finding]:
        s = self.split
        if s.replicas == ("", ""):
            ready = [r.id for r in self.replicas_of(s.project, ("ready",))]
            if len(ready) >= 2:
                s.replicas, s.started_at = (ready[0], ready[1]), obs.now
                return []
            if obs.now - self.split_waiting_since > self.cfg.control.start_timeout_s:
                self.split = None
                return [Finding("split_aborted", s.project, None, "No capacity for a second replica: split test "
                                "abandoned, nobody blamed.")]
            return []
        out = det.split_step(s, self.unattributed, obs.now, self.cfg.detect)
        found: List[Finding] = []
        if out.round_done:
            found.append(Finding("split_round", s.project, None, f"Split round {out.round_done['round']}: "
                                 f"kept {', '.join(out.round_done['kept'])}", out.round_done))
        if out.split is None:
            self.split = None
            if out.culprit:
                self.blame[s.project] = (out.culprit, obs.now + self.cfg.detect.blame_ttl_s)
                found.append(Finding("unattributed_work", s.project, out.culprit,
                                     f"Hash split found {out.culprit}: the unexplained CPU of project {s.project} "
                                     f"followed it in every round; that CPU is now charged to it.",
                                     {"rounds": s.log}))
        return found

    def _contention(self, obs: Observation) -> List[Finding]:
        slow, over = {}, {}
        for p in self.registry.projects:
            sd = self._project_slowdowns(obs, p)
            if sd:
                slow[p] = sum(sd) / len(sd) >= self.cfg.detect.contention_ratio - 1.0
                over[p] = self.used.get(p, 0.0) > 1.05 * self.allocs[p].total + 0.05
        if not det.contention(slow, over):
            self.contention_since, self.contention_open = None, False
            return []
        self.contention_since = self.contention_since or obs.now
        if self.contention_open or obs.now - self.contention_since < self.cfg.detect.contention_open_s:
            return []
        self.contention_open = True
        return [Finding("external_contention", None, None,
                        f"All {len(slow)} active projects are slow while each stays within its CPU allocation: "
                        "the cause is outside the projects (host, disk or another machine). Nobody is blamed.",
                        {"projects": sorted(slow)})]

    # ------------------------------------------------------------------ outputs
    def pins(self) -> Dict[str, str]:
        s = self.split
        if s is None or s.replicas == ("", ""):
            return {}
        others = [t for t in self.tenants_of(s.project) if t not in s.suspects]
        return s.pins(others)

    def snapshot(self, limits: List[LimitsCmd], now: Optional[float] = None) -> Snapshot:
        self.version += 1
        replicas: Dict[str, List[ReplicaView]] = {p: self.replicas_of(p) for p in self.registry.projects}
        return Snapshot(version=self.version, issued_at=now or time.time(),
                        tenants=reg_mod.snap_tenants(self.registry, self.cfg.tier_quotas), replicas=replicas,
                        limits=limits, pins=self.pins(), pin_ttl_s=self.cfg.detect.split_settle_s
                        + self.cfg.detect.split_sample_s + 5 * self.cfg.control.tick_s)

    def project_state(self, p: str) -> str:
        n = len(self.replicas_of(p, ("starting", "ready")))
        if n == 0:
            return "waking" if p in self.woken and self.demand.get(p, 0.0) > IDLE_CPU else "asleep"
        return "busy" if self.demand.get(p, 0.0) > self.scale.busy_cpu else "warm"

    def pool_view(self) -> PoolView:
        cpu, mem = reg_mod.usable(self.registry.pool)
        projects = []
        for p, s in sorted(self.registry.projects.items()):
            a = self.allocs.get(p, Alloc(0.0, 0.0))
            projects.append(ProjectView(
                id=p, name=s.name, tier=s.tier, weight=self.cfg.tier_weights.get(s.tier, 1.0), floor_cpu=s.floor_cpu,
                cap_cpu=s.cap_cpu, warm_min=s.warm_min, tenants=self.tenants_of(p),
                demand_cpu=round(self.demand.get(p, 0.0), 3), used_cpu=round(self.used.get(p, 0.0), 3),
                base_cpu=round(a.base, 3), loan_cpu=round(a.loan, 3), alloc_cpu=round(a.total, 3),
                state=self.project_state(p), replicas=self.replicas_of(p)))
        return PoolView(pool=self.registry.pool, usable_cpu=round(cpu, 3), usable_mem_mb=mem,
                        floors_cpu=round(sum(s.floor_cpu for s in self.registry.projects.values()), 3),
                        reserved_mem_mb=sum(r.mem_mb for r in self.fleet.replicas.values()), projects=projects)
