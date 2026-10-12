"""Toy fleet simulator (no I/O) used by tools/fake_gateway.py and the scenario tests.

It produces the same TelemetryFrame / WorkerStats the real gateway and workers emit, and honours the
brain's controls in `tenantguard` mode:
- LimitsCmd: factor scales the tenant's cost quota; weight_factor lowers its priority on shared servers;
- RouteWeightsCmd: a tenant's traffic share over its servers;
- RelocateCmd: a tenant (or one of its request classes) runs on a target server (spare / quarantine / wN);
- DrainCmd: a server gets no traffic (unless that would leave a tenant nowhere to go);
- docker resizing of the spare (`set_cpus`).

Latency on a server grows with its CPU utilisation, so a whale on w1/w3/w8 hurts exactly its barcode
neighbours. A `blackbox` attack burns CPU nobody is billed for (WorkerStats.blackbox_cpu_ms), the way a
vendor indexer would. Each tenant has two request classes: "c-light" (calm work) and "c-heavy" (the attack).

The numbers are illustrative, not measurements: real numbers come from lab runs.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from tenantguard_common.contracts import (Cell, ClassCost, LimiterStat, QueueStat, TelemetryFrame, TenantConfig,
                                          TenantUsage, WorkerStats)

CALM_RPS = {"enterprise": 30.0, "pro": 22.0, "free": 15.0}
CALM_COST_MS = 4.5           # mean handler CPU per calm request
BASE_LATENCY_MS = 12.0       # end-to-end p95 of a calm request on an idle server (host calm: ~10-15 ms)
LIGHT, HEAVY = "c-light", "c-heavy"
POOL = {"spare": 1.0, "quarantine": 0.25}      # extra servers and their CPU (cores)


@dataclass
class Phase:
    name: str
    duration_s: float
    attacker: Optional[str] = None
    attack_rps: float = 0.0
    attack_cost_ms: float = 0.0
    burst: Optional[str] = None          # a legit burst: high usage on its own servers, nobody hurt
    burst_mult: float = 1.0
    blackbox: bool = False               # the attack's CPU is unattributed (black-box)


def whale_scenario(attacker: str = "K", calm_s: float = 20, attack_s: float = 40, after_s: float = 60,
                   rps: float = 10.0, cost_ms: float = 90.0, blackbox: bool = False) -> List[Phase]:
    return [Phase("calm", calm_s), Phase("whale", attack_s, attacker, rps, cost_ms, blackbox=blackbox),
            Phase("after", after_s)]


@dataclass
class Override:
    factor: float = 1.0
    weight_factor: float = 1.0
    expires_at: float = 0.0


@dataclass
class SimWorker:
    started_at: float
    cpu_total_ms: float = 0.0
    blackbox_ms: float = 0.0
    requests: Dict[str, int] = field(default_factory=dict)
    cpu_ms: Dict[str, float] = field(default_factory=dict)
    cls_count: Dict[Tuple[str, str], int] = field(default_factory=dict)
    cls_cpu: Dict[Tuple[str, str], float] = field(default_factory=dict)


class Simulator:
    def __init__(self, tenants: List[TenantConfig], phases: List[Phase], *, tick_ms: int = 250,
                 server_cores: float = 0.5, quota_mult: Optional[Dict[str, float]] = None,
                 seed: int = 7, start: float = 0.0, mode: str = "tenantguard", noise: Optional[str] = None,
                 calm_scale: float = 1.0):
        self.tenants = {t.id: t for t in tenants}
        self.phases = phases
        self.tick_ms = tick_ms
        self.server_cores = server_cores
        self.quota_mult = quota_mult or {}
        self.rng = random.Random(seed)
        self.t0 = start
        self.now = start
        self.mode = mode
        self.overrides: Dict[str, Override] = {}
        servers = sorted({s for t in tenants for s in t.servers})
        self.cores: Dict[str, float] = {s: server_cores for s in servers}
        self.cores.update(POOL)
        self.workers = {s: SimWorker(started_at=start) for s in self.cores}
        self.route_weights: Dict[str, Dict[str, float]] = {}
        self.route_expires: Dict[str, float] = {}
        self.relocations: Dict[Tuple[str, Optional[str]], Tuple[str, float]] = {}   # (tenant, cls) -> (target, until)
        self.drained: Dict[str, float] = {}            # server -> until
        self.faults: Dict[str, float] = {}             # server -> extra latency ms (faulty host)
        self.route_log: List = []                      # (now, RouteWeightsCmd) for tests
        self.control_log: List = []                    # (now, path, cmd) for tests
        self.util: Dict[str, float] = {}               # last tick's CPU utilisation per server
        # noise="host": what 0.5-CPU containers look like at calm on the Docker host. Each server stalls now
        # and then (CPU-quota throttling: 0.5-3 s, ~every 30 s, latency x3-8 for everyone on it, canary
        # included), and 5% of cell-ticks have a 3-8x tail spike.
        self.noise = noise
        self.calm_scale = calm_scale                   # >1: a busier calm (host calm runs near fair share)
        self.stall: Dict[str, Tuple[float, float]] = {}   # server -> (until, multiplier)

    @property
    def cap_ms(self) -> float:                         # a barcode server's CPU ms/s (back-compat)
        return self.server_cores * 1000.0

    # ---------------------------------------------------------------- control
    def apply_limits(self, tenant: str, factor: float, weight_factor: float, ttl_s: float) -> None:
        if factor >= 1.0 and weight_factor >= 1.0:
            self.overrides.pop(tenant, None)
        else:
            self.overrides[tenant] = Override(factor, weight_factor, self.now + ttl_s)

    def apply_route_weights(self, weights: Dict[str, Dict[str, float]], ttl_s: float) -> None:
        for t, w in weights.items():
            tot = sum(w.values())
            if tot > 0:
                self.route_weights[t] = {s: v / tot for s, v in w.items()}
                self.route_expires[t] = self.now + ttl_s

    def apply_relocate(self, tenant: str, cls: Optional[str], target: Optional[str], ttl_s: float) -> None:
        if target is None:
            self.relocations.pop((tenant, cls), None)
        else:
            self.relocations[(tenant, cls)] = (target, self.now + ttl_s)

    def apply_drain(self, server: str, drained: bool, ttl_s: float) -> None:
        if drained:
            self.drained[server] = self.now + ttl_s
        else:
            self.drained.pop(server, None)

    def apply(self, path: str, cmd) -> None:
        """Apply a control command as the gateway would (tests)."""
        self.control_log.append((self.now, path, cmd))
        if path.endswith("/relocate"):
            self.apply_relocate(cmd.tenant, cmd.cls, cmd.target, cmd.ttl_s)
        elif path.endswith("/drain"):
            self.apply_drain(cmd.server, cmd.drained, cmd.ttl_s)
        elif path.endswith("/route_weights"):
            self.apply_route_weights(cmd.weights, cmd.ttl_s)
        elif path.endswith("/limits"):
            self.apply_limits(cmd.tenant, cmd.factor, cmd.weight_factor, cmd.ttl_s)

    def set_cpus(self, server: str, cores: float) -> None:
        self.cores[server] = cores

    def set_fault(self, server: str, latency_ms: float) -> None:
        if latency_ms > 0:
            self.faults[server] = latency_ms
        else:
            self.faults.pop(server, None)

    def _override(self, tenant: str) -> Override:
        o = self.overrides.get(tenant)
        if o and o.expires_at <= self.now:
            self.overrides.pop(tenant, None)
            o = None
        return o or Override()

    def _live(self) -> bool:
        return self.mode == "tenantguard"

    def share(self, t: str) -> Dict[str, float]:
        """Traffic share per barcode server: route weights (tenantguard mode, unexpired) or equal, minus drained."""
        servers = self.tenants[t].servers
        w = self.route_weights.get(t)
        if w and self._live() and self.route_expires.get(t, 0) > self.now:
            sh = {s: w.get(s, 0.0) for s in servers}
        else:
            sh = {s: 1.0 / len(servers) for s in servers}
        drained = {s for s, until in self.drained.items() if until > self.now} if self._live() else set()
        kept = {s: v for s, v in sh.items() if s not in drained}
        tot = sum(kept.values())
        if tot <= 0:
            return sh                                # drains never black-hole a tenant
        return {s: kept.get(s, 0.0) / tot for s in servers}

    def route(self, t: str, cls: str) -> Dict[str, float]:
        if self._live():
            for key in ((t, cls), (t, None)):
                r = self.relocations.get(key)
                if r and r[1] > self.now:
                    return {r[0]: 1.0}
        return self.share(t)

    def _stall_mult(self, server: str) -> float:
        st = self.stall.get(server)
        return st[1] if st and st[0] > self.now else 1.0

    def canary_ms(self, server: str) -> float:
        """Round trip of an unattributed probe: it queues for CPU like everyone else, plus the server's fault."""
        u = min(0.97, self.util.get(server, 0.0))
        return (BASE_LATENCY_MS * 0.5 * (1.0 + 0.6 * u / (1.0 - u)) * self.rng.lognormvariate(0, 0.05)
                * self._stall_mult(server) + self.faults.get(server, 0.0))

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
        if self.noise == "host":
            for s in self.cores:
                if self._stall_mult(s) == 1.0 and self.rng.random() < dt / 30.0:
                    self.stall[s] = (self.now + self.rng.uniform(0.5, 3.0), self.rng.uniform(3.0, 8.0))
        limits_on = self.mode != "off"
        # demand per tenant: [(rps, cost per request, class, blackbox)]
        demand: Dict[str, List[Tuple[float, float, str, bool]]] = {}
        for t, cfg in self.tenants.items():
            rps = CALM_RPS[cfg.tier] * self.calm_scale * self.rng.uniform(0.9, 1.1)
            if phase.burst == t:
                rps *= phase.burst_mult
            demand[t] = [(rps, CALM_COST_MS, LIGHT, False)]
            if phase.attacker == t:
                demand[t].append((phase.attack_rps, phase.attack_cost_ms, HEAVY, phase.blackbox))
        # admission: cost quota (ms/s) x factor, in all modes but off. Black-box CPU isn't billed: it gets through.
        frac: Dict[str, float] = {}
        rejected_rps: Dict[str, float] = {}
        for t, parts in demand.items():
            cfg = self.tenants[t]
            billed = sum(r * c for r, c, _, bb in parts if not bb)
            o = self._override(t) if self._live() else Override()
            cap = cfg.quota.cost_ms_per_s * self.quota_mult.get(t, 1.0) * o.factor if limits_on else math.inf
            frac[t] = min(1.0, cap / billed) if billed > 0 else 1.0
            rejected_rps[t] = sum(r for r, _, _, _ in parts) * (1.0 - frac[t])
        # per (tenant, server): requests/s, paid CPU ms/s, black-box CPU ms/s, per class
        rps_ts: Dict[Tuple[str, str], float] = defaultdict(float)
        cpu_ts: Dict[Tuple[str, str], float] = defaultdict(float)
        bb_s: Dict[str, float] = defaultdict(float)
        cls_ts: Dict[Tuple[str, str, str], Tuple[float, float]] = {}
        heavy_on: Dict[Tuple[str, str], bool] = {}
        load_seen: Dict[str, float] = defaultdict(float)
        load_real: Dict[str, float] = defaultdict(float)
        for t, parts in demand.items():
            wf = self._override(t).weight_factor if self._live() else 1.0
            for r, c, cls, bb in parts:
                r_adm = r * frac[t]
                for s, f in self.route(t, cls).items():
                    if f <= 0:
                        continue
                    rps_ts[(t, s)] += r_adm * f
                    ms = r_adm * c * f
                    if bb:
                        bb_s[s] += ms
                    else:
                        cpu_ts[(t, s)] += ms
                    n0, c0 = cls_ts.get((t, s, cls), (0.0, 0.0))
                    cls_ts[(t, s, cls)] = (n0 + r_adm * f, c0 + (0.0 if bb else ms))
                    if cls == HEAVY:
                        heavy_on[(t, s)] = True
                    load_real[s] += ms
                    load_seen[s] += ms * wf
        cap = {s: self.cores[s] * 1000.0 for s in self.workers}
        self.util = {s: load_real[s] / cap[s] for s in self.workers}
        # a container cannot burn more than its CPU limit: scale every tenant's CPU on a saturated server
        scale = {s: min(1.0, cap[s] / load_real[s]) if load_real[s] > 0 else 1.0 for s in self.workers}

        def lat(u: float) -> float:
            u = min(0.97, u)
            return BASE_LATENCY_MS * (1.0 + 0.6 * u / (1.0 - u))
        cells, queues = [], []
        for t, cfg in self.tenants.items():
            n_total, worst = 0, 0.0
            servers = list(cfg.servers) + sorted({s for (tt, s) in rps_ts if tt == t and s not in cfg.servers})
            for s in servers:
                r = rps_ts.get((t, s), 0.0)
                n = max(0, int(round(r * dt + self.rng.uniform(-0.5, 0.5)))) if r > 0 else 0
                p95 = lat(load_seen[s] / cap[s]) * self.rng.lognormvariate(0, 0.06) * self._stall_mult(s)
                if self.noise == "host" and self.rng.random() < 0.05:
                    p95 *= self.rng.uniform(3.0, 8.0)
                p95 += self.faults.get(s, 0.0)
                if heavy_on.get((t, s)):
                    p95 += phase.attack_cost_ms * 0.5
                if n or s in cfg.servers:
                    cells.append(Cell(tenant=t, server=s, count=n, p50_ms=round(p95 * 0.45, 2),
                                      p95_ms=round(p95, 2)))
                n_total += n
                if n:
                    worst = max(worst, p95)
                w = self.workers[s]
                w.requests[t] = w.requests.get(t, 0) + n
                w.cpu_ms[t] = w.cpu_ms.get(t, 0.0) + cpu_ts.get((t, s), 0.0) * dt * scale[s]
            rej = int(round(rejected_rps[t] * dt))
            depth = int(min(50, rejected_rps[t] * 0.2)) + (2 if phase.attacker == t else 0)
            queues.append(QueueStat(tenant=t, depth=depth, wait_p95_ms=round(worst * 0.1, 2),
                                    p95_ms=round(worst, 2) if n_total else 0.0, completed=n_total,
                                    admitted=n_total, rejected_429=rej, expired_503=0,
                                    inflight=int(sum(rps_ts.get((t, s), 0.0) for s in servers) * worst / 1000.0)))
        for (t, s, cls), (r, c) in cls_ts.items():
            w = self.workers[s]
            w.cls_count[(t, cls)] = w.cls_count.get((t, cls), 0) + int(round(r * dt))
            w.cls_cpu[(t, cls)] = w.cls_cpu.get((t, cls), 0.0) + c * dt * scale[s]
        for s, w in self.workers.items():
            w.blackbox_ms += bb_s.get(s, 0.0) * dt * scale[s]
            w.cpu_total_ms += (load_real[s] * scale[s] + 8.0) * dt     # +8 ms/s runtime overhead
        return TelemetryFrame(ts=self.now, mode=self.mode, tick_ms=self.tick_ms, cells=cells, queues=queues,
                              limiter=[LimiterStat(server=s, window=8, inflight=0) for s in self.workers])

    def worker_stats(self, server: str) -> WorkerStats:
        w = self.workers[server]
        return WorkerStats(
            server=server, ts=self.now, started_at=w.started_at, cpu_total_ms=round(w.cpu_total_ms, 3),
            blackbox_cpu_ms=round(w.blackbox_ms, 3), cpu_limit_cores=self.cores[server], mem_mb=80.0,
            tenants=[TenantUsage(tenant=t, requests=w.requests.get(t, 0), handler_cpu_ms=round(ms, 3),
                                 bg_cpu_ms=0.0, db_ms=0.0) for t, ms in w.cpu_ms.items()],
            classes=[ClassCost(tenant=t, cls=c, count=n, cpu_ms_total=round(w.cls_cpu.get((t, c), 0.0), 3))
                     for (t, c), n in w.cls_count.items()])


class SimDocker:
    """Fake Docker for the prover: resizing the spare container resizes the simulator's spare."""

    def __init__(self, sim: Simulator, server: str = "spare"):
        self.sim, self.server = sim, server
        self.calls: List[Tuple[str, float]] = []

    async def get_cpus(self, container: str) -> float:
        return self.sim.cores[self.server]

    async def set_cpus(self, container: str, cores: float) -> None:
        self.calls.append((container, cores))
        self.sim.set_cpus(self.server, cores)
