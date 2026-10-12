"""Toy fleet simulator (no I/O) used by tools/fake_gateway.py and the scenario tests.

It produces the same TelemetryFrame / WorkerStats the real gateway and workers emit, and
honours LimitsCmd (factor scales the tenant's rate/cost/concurrency quota, weight_factor lowers
its priority on shared servers). Latency on a server grows with its CPU utilisation, so a whale
on servers w1/w3/w8 hurts exactly its barcode neighbours.

The numbers are illustrative, not measurements: real numbers come from lab runs.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tenantguard_common.contracts import (Cell, LimiterStat, QueueStat, TelemetryFrame, TenantConfig,
                                          TenantUsage, WorkerStats)

CALM_RPS = {"enterprise": 30.0, "pro": 22.0, "free": 15.0}
CALM_COST_MS = 4.5           # mean handler CPU per calm request
BASE_LATENCY_MS = 6.0        # end-to-end p95 of a calm request on an idle server


@dataclass
class Phase:
    name: str
    duration_s: float
    attacker: Optional[str] = None
    attack_rps: float = 0.0
    attack_cost_ms: float = 0.0
    burst: Optional[str] = None          # a legit burst: high usage on its own servers, nobody hurt
    burst_mult: float = 1.0


def whale_scenario(attacker: str = "K", calm_s: float = 20, attack_s: float = 40, after_s: float = 60,
                   rps: float = 10.0, cost_ms: float = 90.0) -> List[Phase]:
    return [Phase("calm", calm_s), Phase("whale", attack_s, attacker, rps, cost_ms), Phase("after", after_s)]


@dataclass
class Override:
    factor: float = 1.0
    weight_factor: float = 1.0
    expires_at: float = 0.0


@dataclass
class SimWorker:
    started_at: float
    cpu_total_ms: float = 0.0
    requests: Dict[str, int] = field(default_factory=dict)
    cpu_ms: Dict[str, float] = field(default_factory=dict)


class Simulator:
    def __init__(self, tenants: List[TenantConfig], phases: List[Phase], *, tick_ms: int = 250,
                 server_cores: float = 0.5, quota_mult: Optional[Dict[str, float]] = None,
                 seed: int = 7, start: float = 0.0, mode: str = "tenantguard"):
        self.tenants = {t.id: t for t in tenants}
        self.phases = phases
        self.tick_ms = tick_ms
        self.cap_ms = server_cores * 1000.0           # CPU ms/s per server
        self.server_cores = server_cores
        self.quota_mult = quota_mult or {}
        self.rng = random.Random(seed)
        self.t0 = start
        self.now = start
        self.mode = mode
        self.overrides: Dict[str, Override] = {}
        servers = sorted({s for t in tenants for s in t.servers})
        self.workers = {s: SimWorker(started_at=start) for s in servers}
        self.route_weights: Dict[str, Dict[str, float]] = {}
        self.route_expires: Dict[str, float] = {}
        self.faults: Dict[str, float] = {}             # server -> extra latency ms (faulty host)
        self.route_log: List = []                      # (now, RouteWeightsCmd) for tests

    def apply_route_weights(self, weights: Dict[str, Dict[str, float]], ttl_s: float) -> None:
        for t, w in weights.items():
            tot = sum(w.values())
            if tot > 0:
                self.route_weights[t] = {s: v / tot for s, v in w.items()}
                self.route_expires[t] = self.now + ttl_s

    def set_fault(self, server: str, latency_ms: float) -> None:
        if latency_ms > 0:
            self.faults[server] = latency_ms
        else:
            self.faults.pop(server, None)

    def share(self, t: str) -> Dict[str, float]:
        """Traffic share per server: route weights (tenantguard mode, unexpired) or equal."""
        servers = self.tenants[t].servers
        w = self.route_weights.get(t)
        if w and self.mode == "tenantguard" and self.route_expires.get(t, 0) > self.now:
            return {s: w.get(s, 0.0) for s in servers}
        return {s: 1.0 / len(servers) for s in servers}

    def canary_ms(self, server: str) -> float:
        """Round trip of an unattributed probe: base latency + the server's fault."""
        return BASE_LATENCY_MS * 0.5 * self.rng.lognormvariate(0, 0.05) + self.faults.get(server, 0.0)

    # ---------------------------------------------------------------- control
    def apply_limits(self, tenant: str, factor: float, weight_factor: float, ttl_s: float) -> None:
        if factor >= 1.0 and weight_factor >= 1.0:
            self.overrides.pop(tenant, None)
        else:
            self.overrides[tenant] = Override(factor, weight_factor, self.now + ttl_s)

    def _override(self, tenant: str) -> Override:
        o = self.overrides.get(tenant)
        if o and o.expires_at <= self.now:
            self.overrides.pop(tenant, None)
            o = None
        return o or Override()

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
    def step(self) -> TelemetryFrame:
        dt = self.tick_ms / 1000.0
        self.now += dt
        phase = self.phase_at(self.now) or Phase("done", 0)
        limits_on = self.mode != "off"
        # demand per tenant: (rps, cost per request)
        demand = {}
        for t, cfg in self.tenants.items():
            rps = CALM_RPS[cfg.tier] * self.rng.uniform(0.9, 1.1)
            cost = CALM_COST_MS
            if phase.burst == t:
                rps *= phase.burst_mult
            demand[t] = [(rps, cost)]
            if phase.attacker == t:
                demand[t].append((phase.attack_rps, phase.attack_cost_ms))
        # admission: cost quota (ms/s) x factor, in all modes but off
        admitted: Dict[str, float] = {}       # CPU ms/s admitted
        admitted_rps: Dict[str, float] = {}
        rejected_rps: Dict[str, float] = {}
        for t, parts in demand.items():
            cfg = self.tenants[t]
            want_ms = sum(r * c for r, c in parts)
            want_rps = sum(r for r, _ in parts)
            o = self._override(t) if self.mode == "tenantguard" else Override()
            cap = cfg.quota.cost_ms_per_s * self.quota_mult.get(t, 1.0) * o.factor if limits_on else math.inf
            frac = min(1.0, cap / want_ms) if want_ms > 0 else 1.0
            admitted[t] = want_ms * frac
            admitted_rps[t] = want_rps * frac
            rejected_rps[t] = want_rps - admitted_rps[t]
        # server load (each tenant spreads evenly over its 3 servers); deprioritized load counts less
        # for others on the same server because the scheduler serves them first
        load_seen: Dict[str, float] = {s: 0.0 for s in self.workers}
        load_real: Dict[str, float] = {s: 0.0 for s in self.workers}
        for t, ms in admitted.items():
            servers = self.tenants[t].servers
            wf = self._override(t).weight_factor if self.mode == "tenantguard" else 1.0
            sh = self.share(t)
            for s in servers:
                load_real[s] += ms * sh[s]
                load_seen[s] += ms * sh[s] * wf
        # a container cannot burn more than its CPU limit: scale every tenant's CPU on a saturated server
        scale = {s: min(1.0, self.cap_ms / load_real[s]) if load_real[s] > 0 else 1.0 for s in self.workers}

        def lat(u: float) -> float:
            u = min(0.97, u)
            return BASE_LATENCY_MS * (1.0 + 0.6 * u / (1.0 - u))
        cells, queues = [], []
        for t, cfg in self.tenants.items():
            n_total = 0
            worst = 0.0
            sh = self.share(t)
            for s in cfg.servers:
                n = int(round(admitted_rps[t] * dt * sh[s] + self.rng.uniform(-0.5, 0.5))) if sh[s] > 0 else 0
                n = max(0, n)
                u = load_seen[s] / self.cap_ms
                p95 = lat(u) * self.rng.lognormvariate(0, 0.06) + self.faults.get(s, 0.0)
                if phase.attacker == t:
                    p95 += phase.attack_cost_ms * 0.5
                cells.append(Cell(tenant=t, server=s, count=n, p50_ms=round(p95 * 0.45, 2), p95_ms=round(p95, 2)))
                n_total += n
                if n:
                    worst = max(worst, p95)
                w = self.workers[s]
                w.requests[t] = w.requests.get(t, 0) + n
                w.cpu_ms[t] = w.cpu_ms.get(t, 0.0) + admitted[t] * sh[s] * dt * scale[s]
            rej = int(round(rejected_rps[t] * dt))
            depth = int(min(50, rejected_rps[t] * 0.2)) + (2 if phase.attacker == t else 0)
            queues.append(QueueStat(tenant=t, depth=depth, wait_p95_ms=round(worst * 0.1, 2),
                                    p95_ms=round(worst, 2) if n_total else 0.0, completed=n_total,
                                    admitted=n_total, rejected_429=rej, expired_503=0,
                                    inflight=int(admitted_rps[t] * worst / 1000.0)))
        for s, w in self.workers.items():
            w.cpu_total_ms += (load_real[s] * scale[s] + 8.0) * dt     # +8 ms/s runtime overhead
        return TelemetryFrame(ts=self.now, mode=self.mode, tick_ms=self.tick_ms, cells=cells, queues=queues,
                              limiter=[LimiterStat(server=s, window=8, inflight=0) for s in self.workers])

    def worker_stats(self, server: str) -> WorkerStats:
        w = self.workers[server]
        return WorkerStats(
            server=server, ts=self.now, started_at=w.started_at, cpu_total_ms=round(w.cpu_total_ms, 3),
            cpu_limit_cores=self.server_cores, mem_mb=80.0,
            tenants=[TenantUsage(tenant=t, requests=w.requests.get(t, 0), handler_cpu_ms=round(ms, 3),
                                 bg_cpu_ms=0.0, db_ms=0.0) for t, ms in w.cpu_ms.items()])
