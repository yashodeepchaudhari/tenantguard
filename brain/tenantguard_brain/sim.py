"""Toy project / replica simulator (no I/O) for the scenario tests (tests/conftest.py Harness).

It emits what the real gateway and replicas emit (TelemetryFrame, WorkerStats, cgroup CPU) and obeys what the
brain controls: the snapshot (ready replicas, limits, pins) and each replica's CPU cap and shares. Tenants go to
their project's replicas by the gateway's rendezvous rule. When the replicas want more CPU than the pool has,
the pool is shared by CpuShares, as the kernel does. Latency on a replica grows with its utilisation.

The numbers are illustrative, not measurements: real numbers come from lab runs.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from tenantguard_common.contracts import (Cell, LimiterStat, QueueStat, Snapshot, SnapTenant, TelemetryFrame,
                                          TenantUsage, WorkerStats)

from .allocator import water_fill

CALM_RPS = {"enterprise": 30.0, "pro": 22.0, "free": 15.0}
CALM_COST_MS = 4.5           # mean handler CPU per calm request
OVERHEAD_MS = 1.3            # framework CPU per request outside the handler (median in worker/calibration.json)
BASE_LATENCY_MS = 6.0        # end-to-end p95 of a calm request on an idle replica


@dataclass
class Phase:
    name: str
    duration_s: float
    attacker: Optional[str] = None       # extra (rps, cost) traffic from one tenant
    attack_rps: float = 0.0
    attack_cost_ms: float = 0.0
    burst: Set[str] = field(default_factory=set)   # a legit burst: these tenants' normal traffic x burst_mult
    burst_mult: float = 1.0
    hidden: Optional[str] = None         # CPU burned on the tenant's replica that no meter sees
    hidden_ms: float = 0.0
    fault_ms: float = 0.0                # extra latency on every replica (a slow host)
    idle: Set[str] = field(default_factory=set)


def whale_scenario(attacker: str = "K", calm_s: float = 20, attack_s: float = 40, after_s: float = 60,
                   rps: float = 10.0, cost_ms: float = 90.0) -> List[Phase]:
    return [Phase("calm", calm_s), Phase("whale", attack_s, attacker, rps, cost_ms), Phase("after", after_s)]


@dataclass
class SimReplica:
    project: str
    nano_cpus: int
    cpu_shares: int
    tenants: List[str]
    started_at: float
    cpu_total_ms: float = 0.0            # cgroup CPU, cumulative
    requests: Dict[str, int] = field(default_factory=dict)
    cpu_ms: Dict[str, float] = field(default_factory=dict)

    @property
    def cap_ms(self) -> float:
        return self.nano_cpus / 1e6 if self.nano_cpus else 1000.0


def _rendezvous(tenant: str, replicas: List[str]) -> Optional[str]:
    h = lambda r: hashlib.blake2b(f"{tenant}|{r}".encode(), digest_size=8).digest()   # noqa: E731
    return max(replicas, key=h) if replicas else None


class Simulator:
    def __init__(self, tenants: List[SnapTenant], phases: List[Phase], *, pool_cpu: float = 5.75, tick_ms: int = 250,
                 seed: int = 7, start: float = 0.0, mode: str = "tenantguard"):
        self.tenants = {t.id: t for t in tenants}
        self.phases = phases
        self.pool_ms = pool_cpu * 1000.0
        self.tick_ms = tick_ms
        self.rng = random.Random(seed)
        self.t0 = self.now = start
        self.mode = mode
        self.replicas: Dict[str, SimReplica] = {}
        self.ready: Dict[str, List[str]] = {}          # project -> replicas the gateway routes to
        self.limits: Dict[str, tuple] = {}             # tenant -> (factor, weight_factor, expires_at)
        self.pins: Dict[str, str] = {}

    # ---------------------------------------------------------------- what the brain controls
    def start_replica(self, rid: str, project: str, nano: int, shares: int, tenants: List[str]) -> None:
        self.replicas[rid] = SimReplica(project, nano, shares, list(tenants), self.now)

    def update_replica(self, rid: str, nano: int, shares: int) -> None:
        if rid in self.replicas:
            self.replicas[rid].nano_cpus, self.replicas[rid].cpu_shares = nano, shares

    def stop_replica(self, rid: str) -> None:
        self.replicas.pop(rid, None)

    def apply_snapshot(self, snap: Snapshot) -> None:
        self.ready = {p: [r.id for r in rs if r.state == "ready" and r.id in self.replicas]
                      for p, rs in snap.replicas.items()}
        self.limits = {c.tenant: (c.factor, c.weight_factor, self.now + c.ttl_s) for c in snap.limits}
        self.pins = dict(snap.pins)

    def _factors(self, t: str) -> tuple:
        f = self.limits.get(t)
        if self.mode != "tenantguard" or f is None or f[2] <= self.now:
            return 1.0, 1.0
        return f[0], f[1]

    def phase_at(self, t: float) -> Optional[Phase]:
        el = t - self.t0
        for p in self.phases:
            if el < p.duration_s:
                return p
            el -= p.duration_s
        return None

    @property
    def total_s(self) -> float:
        return sum(p.duration_s for p in self.phases)

    # ---------------------------------------------------------------- one tick
    def _demand(self, phase: Phase) -> Dict[str, list]:
        out = {}
        for t, cfg in self.tenants.items():
            rps = 0.0 if t in phase.idle else CALM_RPS[cfg.tier] * self.rng.uniform(0.9, 1.1)
            if t in phase.burst:
                rps *= phase.burst_mult
            out[t] = [(rps, CALM_COST_MS)]
            if phase.attacker == t:
                out[t].append((phase.attack_rps, phase.attack_cost_ms))
        return out

    def _route(self, t: str) -> Optional[str]:
        ready = self.ready.get(self.tenants[t].project, [])
        pin = self.pins.get(t) if self.mode == "tenantguard" else None
        return pin if pin in ready else _rendezvous(t, ready)

    def _host_share(self, want: Dict[str, float]) -> Dict[str, float]:
        """CPU each replica gets: its cap, unless the pool is short; then the pool is split by CpuShares."""
        capped = {r: min(w, self.replicas[r].cap_ms) for r, w in want.items()}
        if sum(capped.values()) <= self.pool_ms:
            return capped
        return water_fill(self.pool_ms, capped, {r: max(2, self.replicas[r].cpu_shares) for r in capped})

    def step(self) -> TelemetryFrame:
        dt = self.tick_ms / 1000.0
        self.now += dt
        phase = self.phase_at(self.now) or Phase("done", 0)
        limits_on = self.mode != "off"
        want = {r: 0.0 for r in self.replicas}
        seen = {r: 0.0 for r in self.replicas}
        plan = {}
        for t, parts in self._demand(phase).items():
            factor, wf = self._factors(t)
            rps = sum(r for r, _ in parts)
            ms = sum(r * (c + OVERHEAD_MS) for r, c in parts)
            cap_rps = self.tenants[t].quota.rate_rps * factor if limits_on else float("inf")
            frac = min(1.0, cap_rps / rps) if rps > 0 else 1.0
            rid = self._route(t)
            plan[t] = (rid, rps * frac, ms * frac, rps * (1 - frac), parts)
            if rid is not None:
                want[rid] += ms * frac
                seen[rid] += ms * frac * wf
        if phase.hidden and (rid := self._route(phase.hidden)) is not None:
            want[rid] += phase.hidden_ms
            seen[rid] += phase.hidden_ms
        got = self._host_share(want)
        for rid, rep in self.replicas.items():
            rep.cpu_total_ms += (got[rid] + 5.0) * dt                    # +5 ms/s idle runtime
        return self._frame(phase, plan, want, seen, got, dt)

    def _frame(self, phase: Phase, plan: dict, want: dict, seen: dict, got: dict, dt: float) -> TelemetryFrame:
        def lat(u: float) -> float:
            u = min(0.97, u)
            return BASE_LATENCY_MS * (1.0 + 0.6 * u / (1.0 - u))
        cells, queues = [], []
        for t, (rid, rps, ms, rej_rps, parts) in plan.items():
            if rid is None:
                queues.append(QueueStat(tenant=t, depth=int(rps * 0.5), wait_p95_ms=0.0, p95_ms=0.0, completed=0,
                                        admitted=0, rejected_429=0, expired_503=0, inflight=0,
                                        queued_cost_ms=round(ms * 0.5, 2)))
                continue
            rep = self.replicas[rid]
            u = seen[rid] / max(1.0, got[rid] if want[rid] > got[rid] else rep.cap_ms)
            scale = got[rid] / want[rid] if want[rid] > 0 else 1.0
            p95 = lat(u) * self.rng.lognormvariate(0, 0.06) + phase.fault_ms
            if want[rid] > got[rid]:
                p95 *= want[rid] / max(1.0, got[rid])                     # saturated: queueing inside the replica
            if phase.attacker == t:
                p95 += phase.attack_cost_ms * 0.5
            n = max(0, int(round(rps * dt + self.rng.uniform(-0.5, 0.5))))
            cells.append(Cell(tenant=t, server=rid, count=n, p50_ms=round(p95 * 0.45, 2), p95_ms=round(p95, 2)))
            rep.requests[t] = rep.requests.get(t, 0) + n
            handler = sum(r * c for r, c in parts) * (rps / max(1e-9, sum(r for r, _ in parts)))
            rep.cpu_ms[t] = rep.cpu_ms.get(t, 0.0) + handler * scale * dt
            backlog = max(0.0, want[rid] - got[rid]) * (ms / want[rid] if want[rid] else 0.0)
            queues.append(QueueStat(tenant=t, depth=int(min(50, rej_rps * 0.2)), wait_p95_ms=round(p95 * 0.1, 2),
                                    p95_ms=round(p95, 2) if n else 0.0, completed=n, admitted=n,
                                    rejected_429=int(round(rej_rps * dt)), expired_503=0,
                                    inflight=int(rps * p95 / 1000.0), queued_cost_ms=round(backlog * 0.25, 2),
                                    refused_cost_ms=round(rej_rps * dt * CALM_COST_MS, 2)))
        return TelemetryFrame(ts=self.now, mode=self.mode, tick_ms=self.tick_ms, cells=cells, queues=queues,
                              limiter=[LimiterStat(server=r, window=8, inflight=0) for r in self.replicas])

    def worker_stats(self, rid: str) -> WorkerStats:
        r = self.replicas[rid]
        return WorkerStats(
            server=rid, ts=self.now, started_at=r.started_at, cpu_total_ms=round(r.cpu_total_ms, 3),
            cpu_limit_cores=r.cap_ms / 1000.0, mem_mb=80.0,
            tenants=[TenantUsage(tenant=t, requests=r.requests.get(t, 0), handler_cpu_ms=round(ms, 3), bg_cpu_ms=0.0,
                                 db_ms=0.0) for t, ms in r.cpu_ms.items()])

    def container_ms(self, rid: str) -> float:
        return self.replicas[rid].cpu_total_ms
