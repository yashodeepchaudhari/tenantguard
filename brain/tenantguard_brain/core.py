"""The brain's state machine: rolling state -> scores -> ladder -> commands + incident events.

No I/O here. The runtime feeds frames / worker stats in and sends the returned commands;
tests drive the same object with a simulated clock.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from tenantguard_common.contracts import (ChargeCmd, IncidentEvent, LimitsCmd, OperatorCmd, OverridesView, RouteWeightsCmd,
                                          ServerView, StateSnapshot, TelemetryFrame, TenantView, WorkerStats)

from . import ladder as lad
from .fairness import jain, normalized_throughput
from .incidents import Incident, IncidentBook
from .score import BaselineTracker, ScoreDetail, compute_scores, shares, slowdown
from .settings import BrainSettings
from .stage3 import Stage3

ACTING_MODE = "tenantguard"


@dataclass
class StatSample:
    t: float
    server: str
    dt: float                      # seconds covered (worker clock)
    tenant_cpu_ms: Dict[str, float]  # handler + bg CPU ms in dt
    total_cpu_ms: float
    limit_cores: float
    tenant_bg_ms: Dict[str, float] = field(default_factory=dict)        # bg CPU ms in dt
    tenant_evictions: Dict[str, int] = field(default_factory=dict)      # evictions caused in dt
    blackbox_ms: float = 0.0                                             # unattributed CPU ms in dt
    class_cpu_ms: Dict[Tuple[str, str], float] = field(default_factory=dict)   # (tenant, class) CPU ms in dt
    class_count: Dict[Tuple[str, str], int] = field(default_factory=dict)      # (tenant, class) requests in dt


@dataclass
class Sent:
    factor: float
    weight_factor: float
    at: float
    ttl: float
    ok: Optional[bool] = None      # None = in flight


@dataclass
class OperatorHold:
    decision: str                  # release | extend
    until: float
    by: str
    case_id: Optional[str] = None
    suppressed: bool = False       # a 'would escalate' note was already emitted


@dataclass
class TickResult:
    commands: List[LimitsCmd] = field(default_factory=list)
    route_cmds: List[RouteWeightsCmd] = field(default_factory=list)
    # other control commands: (sender key, gateway path, command). Charges, relocations, drains.
    control: List[Tuple[str, str, object]] = field(default_factory=list)
    jobs: List[object] = field(default_factory=list)         # async tests for the runtime (prover.py)
    events: List[IncidentEvent] = field(default_factory=list)


@dataclass
class TenantAgg:
    completed: int = 0
    p95_weighted: float = 0.0
    rejected: int = 0
    admitted: int = 0
    occupancy: float = 0.0
    ticks: int = 0
    served: int = 0               # requests answered by a worker (cells), ...
    failed: int = 0               # ... of which 5xx / unreachable

    @property
    def p95(self) -> Optional[float]:
        return self.p95_weighted / self.completed if self.completed > 0 else None

    @property
    def ok_fraction(self) -> float:
        """Share of answered requests that succeeded (1.0 when nothing was answered yet)."""
        return 1.0 - self.failed / self.served if self.served > 0 else 1.0


class Brain:
    def __init__(self, cfg: BrainSettings):
        self.cfg = cfg
        self.p = cfg.ladder
        self.tenants = {t.id: t for t in cfg.tenants}
        self.weights = {t.id: float(t.weight) for t in cfg.tenants}
        self.servers_of = {t.id: list(t.servers) for t in cfg.tenants}
        self.neighbours = {
            t.id: [o.id for o in cfg.tenants if o.id != t.id and set(o.servers) & set(t.servers)]
            for t in cfg.tenants}
        self.frames: Deque[Tuple[float, TelemetryFrame]] = deque()
        self.mode: Optional[str] = None
        self.last_frame_at: Optional[float] = None
        self.prev_stats: Dict[str, WorkerStats] = {}
        self.stat_samples: Deque[StatSample] = deque()
        self.worker_seen_at: Dict[str, float] = {}
        self.baseline = BaselineTracker(cfg.baseline_half_life_s)
        self.slowdowns: Dict[str, Optional[float]] = {}
        self.ladders: Dict[str, lad.LadderState] = {t: lad.LadderState() for t in self.tenants}
        self.scores: Dict[str, ScoreDetail] = {}
        self.book = IncidentBook()
        self.sent: Dict[str, Sent] = {}
        self.calm = True
        self.hurt_off = cfg.min_victim_impact / 2.0
        self.s3 = Stage3(self)
        self.holds: Dict[str, OperatorHold] = {}
        self.cls_base_n: Dict[Tuple[str, str], float] = {}        # calm request rate per (tenant, class)
        self.cls_base_cpu: Dict[Tuple[str, str], float] = {}      # calm CPU rate per (tenant, class)
        self.bg_pending: Dict[str, float] = defaultdict(float)   # bg CPU ms not yet charged to the gateway
        self.last_charge_at: Optional[float] = None

    # ------------------------------------------------------------------ ingest
    def ingest_frame(self, frame: TelemetryFrame, now: float) -> None:
        self.frames.append((now, frame))
        self.mode = frame.mode
        self.last_frame_at = now
        self._prune(now)
        self.s3.on_frame(frame, now)
        if not self.calm:
            return
        for q in frame.queues:
            if q.tenant not in self.tenants or q.completed <= 0 or q.p95_ms <= 0:
                continue        # contracts: skip ticks with completed == 0
            if self._baseline_frozen(q.tenant):
                continue
            base = self.baseline.get(q.tenant)
            if base is not None and self.baseline.ready(q.tenant) and q.p95_ms > 2.0 * max(base, self.cfg.slow_abs_ms):
                continue        # an outlier in a calm period is not "normal"
            self.baseline.update(q.tenant, q.p95_ms, now)

    def ingest_canary(self, server: str, rtt_ms: float, now: float) -> None:
        self.s3.on_canary(server, rtt_ms, now)

    def adopt_overrides(self, ov: OverridesView, now: float) -> Tuple[List[IncidentEvent], List[LimitsCmd]]:
        """On start-up: take over limits a previous brain run left on the gateway, so they keep being
        managed (renewed, relaxed by AIMD, cleared) instead of lingering unmanaged until their TTL.
        Limits for unknown tenants are cleared. Route weights are left to expire: the server loop
        recomputes them every tick if a server is still slow."""
        events: List[IncidentEvent] = []
        clears: List[LimitsCmd] = []
        for t, cmd in ov.limits.items():
            if t not in self.ladders:
                clears.append(LimitsCmd(tenant=t, factor=1.0, weight_factor=1.0, ttl_s=self.p.ttl_throttle))
                continue
            if cmd.factor >= 1 - lad.EPS and cmd.weight_factor >= 1 - lad.EPS:
                continue
            st = self.ladders[t]
            if st.level != "normal" or st.acting:
                continue                    # this run already manages the tenant: its state wins
            st.factor, st.weight_factor = cmd.factor, cmd.weight_factor
            st.level = st.peak = self.p.level_for(cmd.factor, cmd.weight_factor)
            st.changed_at = now
            inc = self.book.open_for(t, now)
            ttl = self.p.ttl_for(st.level)
            self.sent[t] = Sent(cmd.factor, cmd.weight_factor, now, ttl, ok=True)
            events.append(self.book.emit(
                inc, now, "detected", level=st.level, prev_level="normal",
                action=self._action_text(cmd.factor, cmd.weight_factor, st.level),
                detail=f"Adopted an override left by a previous brain run (factor {cmd.factor:.2f}, weight factor "
                       f"{cmd.weight_factor:.2f}, incident {cmd.incident_id or '?'}). It will be renewed while "
                       f"needed and relaxed when neighbours are calm."))
        return events, clears

    def cpu_by_server(self, now: float) -> Dict[str, Dict[str, float]]:
        return self._cpu_by_server(now, max(self.cfg.probe_window_s, 3 * self.cfg.stats_poll_s))

    def server_capacity(self, now: float) -> Dict[str, float]:
        lim = {s: l for s, (_, l) in self._server_cpu(now, 3 * self.cfg.stats_poll_s).items()}
        servers = set(self.cfg.fleet_servers) | set(self.cfg.worker_urls)
        return {s: 1000.0 * (lim.get(s) or self.cfg.default_cores) for s in servers}

    def _baseline_frozen(self, t: str) -> bool:
        """Don't learn a tenant's 'normal' while it is hurt, or while a neighbour over-uses its share:
        a moderate attack that never crosses a threshold would otherwise become the new baseline."""
        sd = self.slowdowns.get(t)
        if sd is not None and sd >= self.cfg.min_victim_impact:
            return True
        return any(self.scores[n].usage_ratio > 1.0 for n in self.neighbours.get(t, []) if n in self.scores)

    def ingest_stats(self, st: WorkerStats, now: float) -> None:
        self.worker_seen_at[st.server] = now
        prev = self.prev_stats.get(st.server)
        self.prev_stats[st.server] = st
        if prev is None or prev.started_at != st.started_at or st.ts <= prev.ts or st.cpu_total_ms < prev.cpu_total_ms:
            return              # first sample or the worker restarted: counters reset, no diff
        before = {u.tenant: u for u in prev.tenants}
        cpu, bg, ev = {}, {}, {}
        for u in st.tenants:
            b = before.get(u.tenant)
            cpu[u.tenant] = max(0.0, u.handler_cpu_ms + u.bg_cpu_ms - ((b.handler_cpu_ms + b.bg_cpu_ms) if b else 0.0))
            bg[u.tenant] = max(0.0, u.bg_cpu_ms - (b.bg_cpu_ms if b else 0.0))
            ev[u.tenant] = max(0, u.evictions_caused - (b.evictions_caused if b else 0))
        for t, ms in bg.items():
            if ms > 0 and t in self.tenants:
                self.bg_pending[t] += ms
        before_cls = {(c.tenant, c.cls): c for c in prev.classes}
        cls_cpu, cls_n = {}, {}
        for c in st.classes:
            b = before_cls.get((c.tenant, c.cls))
            d = c.cpu_ms_total - (b.cpu_ms_total if b else 0.0)
            n = c.count - (b.count if b else 0)
            if d > 0:
                cls_cpu[(c.tenant, c.cls)] = d
            if n > 0:
                cls_n[(c.tenant, c.cls)] = n
        self.stat_samples.append(StatSample(now, st.server, st.ts - prev.ts, cpu,
                                            st.cpu_total_ms - prev.cpu_total_ms, st.cpu_limit_cores,
                                            bg, ev, max(0.0, st.blackbox_cpu_ms - prev.blackbox_cpu_ms),
                                            cls_cpu, cls_n))
        self._prune(now)

    def _prune(self, now: float) -> None:
        cut = now - self.cfg.history_s
        while self.frames and self.frames[0][0] < cut:
            self.frames.popleft()
        while self.stat_samples and self.stat_samples[0].t < cut:
            self.stat_samples.popleft()

    # ------------------------------------------------------------------ windows
    def _frames(self, now: float, window: float) -> List[TelemetryFrame]:
        out = []
        for t, f in reversed(self.frames):
            if t <= now - window:
                break
            out.append(f)
        out.reverse()
        return out

    @staticmethod
    def _span(frames: List[TelemetryFrame]) -> float:
        return sum(f.tick_ms for f in frames) / 1000.0

    def _tenant_agg(self, frames: List[TelemetryFrame]) -> Dict[str, TenantAgg]:
        agg: Dict[str, TenantAgg] = defaultdict(TenantAgg)
        for f in frames:
            for q in f.queues:
                a = agg[q.tenant]
                a.ticks += 1
                a.rejected += q.rejected_429 + q.expired_503
                a.admitted += q.admitted
                a.occupancy += q.depth + q.inflight
                if q.completed > 0 and q.p95_ms > 0:
                    a.completed += q.completed
                    a.p95_weighted += q.p95_ms * q.completed
            for c in f.cells:
                a = agg[c.tenant]
                a.served += c.count
                a.failed += min(c.errors, c.count)
        return agg

    def _cpu_by_server(self, now: float, window: float) -> Dict[str, Dict[str, float]]:
        """server -> tenant -> CPU ms/s (handler + bg) over the window."""
        acc: Dict[str, Tuple[float, Dict[str, float]]] = {}
        for s in self.stat_samples:
            if s.t <= now - window:
                continue
            dt, per = acc.get(s.server, (0.0, defaultdict(float)))
            for t, ms in s.tenant_cpu_ms.items():
                per[t] += ms
            acc[s.server] = (dt + s.dt, per)
        return {srv: {t: ms / dt for t, ms in per.items()} for srv, (dt, per) in acc.items() if dt > 0}

    def _tenant_cpu(self, now: float, window: float) -> Dict[str, float]:
        """CPU ms/s per tenant (handler + bg), summed over servers."""
        out: Dict[str, float] = defaultdict(float)
        for per in self._cpu_by_server(now, window).values():
            for t, ms in per.items():
                out[t] += ms
        return dict(out)

    def _server_cpu(self, now: float, window: float) -> Dict[str, Tuple[float, float]]:
        """server -> (CPU ms/s, limit cores)."""
        acc: Dict[str, List[float]] = {}
        for s in self.stat_samples:
            if s.t <= now - window:
                continue
            a = acc.setdefault(s.server, [0.0, 0.0, s.limit_cores])
            a[0] += s.total_cpu_ms
            a[1] += s.dt
            a[2] = s.limit_cores
        return {srv: (ms / dt if dt > 0 else 0.0, lim) for srv, (ms, dt, lim) in acc.items()}

    def _server_p95(self, frames: List[TelemetryFrame]) -> Dict[str, Optional[float]]:
        w: Dict[str, List[float]] = {}
        for f in frames:
            for c in f.cells:
                if c.count > 0:
                    a = w.setdefault(c.server, [0.0, 0])
                    a[0] += c.p95_ms * c.count
                    a[1] += c.count
        return {s: (v / n if n else None) for s, (v, n) in w.items()}

    def telemetry_fresh(self, now: float) -> bool:
        return self.last_frame_at is not None and now - self.last_frame_at <= max(2.0, 8 * self.cfg.tick_s)

    # ------------------------------------------------------------------ tick
    def tick(self, now: float) -> TickResult:
        self._prune(now)
        res = TickResult()
        det = self._frames(now, self.cfg.probe_window_s)
        agg = self._tenant_agg(det)
        view_agg = self._tenant_agg(self._frames(now, self.cfg.view_window_s))
        cpu_window = max(self.cfg.probe_window_s, 3 * self.cfg.stats_poll_s)
        by_server = self._cpu_by_server(now, cpu_window)
        cpu = defaultdict(float)
        for per in by_server.values():
            for t, ms in per.items():
                cpu[t] += ms
        slow = {}
        for t in self.tenants:
            base = self.baseline.get(t) if self.baseline.ready(t) else None
            slow[t] = slowdown(agg[t].p95 if t in agg else None, base, self.cfg.slow_abs_ms)
        self.slowdowns = slow
        # failed / 5xx requests (e.g. all of a tenant's servers down: fast 502s) are not slot usage
        queue = {t: a.occupancy / max(1, a.ticks) * a.ok_fraction for t, a in agg.items()}
        servers = {s for t in self.tenants.values() for s in t.servers}
        limits = {s: lim for s, (_, lim) in self._server_cpu(now, cpu_window).items()}
        cpu_cap = {s: 1000.0 * (limits.get(s) or self.cfg.default_cores) for s in servers}
        slots = {s: float(self.cfg.default_window) for s in servers}
        if det:
            for ls in det[-1].limiter:
                if ls.server in slots and ls.window > 0:
                    slots[ls.server] = float(ls.window)
        active = [t for t in self.tenants
                  if (t in view_agg and (view_agg[t].completed > 0 or view_agg[t].occupancy > 0)) or cpu.get(t, 0) > 0]
        self.scores = compute_scores(
            weights=self.weights, servers_of=self.servers_of, active=active, cpu_by_server=by_server,
            cpu_cap=cpu_cap, occupancy=queue, slots=slots, slowdowns=slow,
            neighbours=self.neighbours, min_victim_impact=self.cfg.min_victim_impact,
            quarantine_threshold=self.p.quarantine)
        self.calm = not self.book.open and all(
            not (s.flagged and s.score >= self.p.warn) for s in self.scores.values())

        fresh = self.telemetry_fresh(now)
        res.events.extend(self._expire_holds(now))
        if fresh and self.mode == ACTING_MODE:
            for t, st in self.ladders.items():
                sd = self.scores[t]
                if t in self.holds:
                    res.events.extend(self._held_tick(t, sd, now))
                    continue                # a human decision owns this tenant until the hold ends
                sig = lad.Signal(score=sd.score, flagged=sd.flagged, unhurt=sd.victim_impact < self.hurt_off)
                tr = lad.step(st, sig, now, self.p)
                if tr:
                    res.events.extend(self._on_transition(t, tr, now))
        elif fresh and self.mode is not None:
            for t, st in self.ladders.items():
                tr = lad.reset(st, now, f"mode is '{self.mode}': brain actions are off outside '{ACTING_MODE}' mode")
                if tr:
                    res.events.extend(self._on_transition(t, tr, now))
        # stale telemetry: hold every ladder where it is (no evidence either way), keep renewing

        if self.s3.inc is None and self.calm and int(now) != int(now - self.cfg.tick_s):
            self._learn_classes(now)                     # once a second, in calm only
        ev, routes = self.s3.tick(now, acting=fresh and self.mode == ACTING_MODE)
        res.events.extend(ev)
        res.route_cmds = routes
        res.control.extend(self.s3.control)
        res.jobs.extend(self.s3.jobs)
        self.s3.control, self.s3.jobs = [], []
        res.commands = self._commands(now)
        res.control.extend(self._charges(now, fresh and self.mode == ACTING_MODE))
        return res

    # ------------------------------------------------------------------ stage 3 support
    def on_proof(self, result, now: float) -> TickResult:
        """A relocation test finished (runtime): verdict + actions."""
        res = TickResult(events=self.s3.on_proof(result, now))
        res.control, res.jobs = self.s3.control, self.s3.jobs
        self.s3.control, self.s3.jobs = [], []
        return res

    def on_host(self, result, now: float) -> TickResult:
        res = TickResult(events=self.s3.on_host(result, now))
        res.control, res.jobs = self.s3.control, self.s3.jobs
        self.s3.control, self.s3.jobs = [], []
        return res

    def control_done(self, cmd, ok: bool, now: float) -> None:
        """Relocate / drain delivery result. A refused action is logged by the runtime; its TTL bounds it."""
        return None

    def readings(self, now: float):
        from .prover import Readings
        cpu = {srv: ms for srv, (ms, _) in self._server_cpu(now, max(2.0, 2 * self.cfg.stats_poll_s)).items()}
        hs = self.s3.health.servers
        return Readings(slowdown={s: max(0.0, h.peer_ratio - 1.0) for s, h in hs.items()},
                        peer_ratio={s: h.peer_ratio for s, h in hs.items()}, cpu_ms=cpu,
                        source={s: h.source for s, h in hs.items()})

    def footprint(self, t: str, now: float) -> Dict[str, float]:
        """The suspect's CPU on its servers: paid (metered handler + bg) and unexplained (black-box)."""
        window = max(self.cfg.probe_window_s, 3 * self.cfg.stats_poll_s)
        by = self.cpu_by_server(now)
        servers = self.servers_of[t]
        paid = sum(by.get(s, {}).get(t, 0.0) for s in servers)
        bb: Dict[str, List[float]] = {}
        for x in self.stat_samples:
            if x.t > now - window and x.server in servers:
                a = bb.setdefault(x.server, [0.0, 0.0])
                a[0] += x.blackbox_ms
                a[1] += x.dt
        unexplained = sum(ms / dt for ms, dt in bb.values() if dt > 0)
        return {"paid": paid, "unexplained": unexplained, "footprint": paid + unexplained}

    def verdict_inputs(self, t: str, now: float) -> Dict[str, float]:
        fp = self.footprint(t, now)
        agg = self._tenant_agg(self._frames(now, self.cfg.view_window_s)).get(t, TenantAgg())
        asked = agg.admitted + agg.rejected
        led = self.s3.ledger
        return {"paid_share": fp["paid"] / fp["footprint"] if fp["footprint"] > 0 else 1.0,
                "blackbox_hint": led.blackbox_all.get(t, 0.0), "evict_hint": led.evict_share.get(t, 0.0),
                "rejected_share": agg.rejected / asked if asked > 0 else 0.0}

    def class_rates(self, now: float, window: float) -> Dict[Tuple[str, str], Tuple[float, float]]:
        """(tenant, class) -> (requests/s, CPU ms/s) over the window, summed over servers."""
        span: Dict[str, float] = defaultdict(float)
        n: Dict[Tuple[str, str], float] = defaultdict(float)
        ms: Dict[Tuple[str, str], float] = defaultdict(float)
        for x in self.stat_samples:
            if x.t > now - window:
                span[x.server] += x.dt
                for k, v in x.class_count.items():
                    n[k] += v
                for k, v in x.class_cpu_ms.items():
                    ms[k] += v
        per = max(span.values()) if span else 0.0
        if per <= 0:
            return {}
        return {k: (n.get(k, 0.0) / per, ms.get(k, 0.0) / per) for k in set(n) | set(ms)}

    def _learn_classes(self, now: float) -> None:
        """Calm-period baseline of every class's request and CPU rate (for 'which class is new / grew')."""
        for (t, cls), (rn, rc) in self.class_rates(now, 5 * self.cfg.stats_poll_s).items():
            for base, v in ((self.cls_base_n, rn), (self.cls_base_cpu, rc)):
                prev = base.get((t, cls))
                base[(t, cls)] = v if prev is None else prev + 0.05 * (v - prev)

    def top_class(self, t: str, now: float) -> Optional[str]:
        """The tenant's guilty request class: the one that grew most against its calm baseline (request rate or
        metered CPU; a class never seen in calm grows the most). Black-box CPU is billed to no class, so the
        request-rate growth is what points at it then."""
        window = max(self.cfg.probe_window_s, 5 * self.cfg.stats_poll_s)
        best, best_g = None, -1.0
        for (tt, cls), (rn, rc) in self.class_rates(now, window).items():
            if tt != t:
                continue
            gn = (rn - self.cls_base_n.get((tt, cls), 0.0)) / max(self.cls_base_n.get((tt, cls), 0.0), 1.0)
            gc = (rc - self.cls_base_cpu.get((tt, cls), 0.0)) / max(self.cls_base_cpu.get((tt, cls), 0.0), 20.0)
            g = max(gn, gc)
            if g > best_g:
                best, best_g = cls, g
        return best

    def server_utilisation(self, now: float) -> Dict[str, float]:
        return {srv: (ms / (lim * 1000.0) if lim > 0 else 0.0)
                for srv, (ms, lim) in self._server_cpu(now, max(2.0, 2 * self.cfg.stats_poll_s)).items()}

    def force_throttle(self, t: str, now: float, reason: str) -> List[IncidentEvent]:
        """over_plan verdict: put the tenant's ladder at throttle (factor = throttle_factor) so the normal
        AIMD path renews and later relaxes it."""
        st = self.ladders[t]
        f = min(st.factor, self.p.throttle_factor)
        if st.acting and st.factor <= f:
            return []
        tr = lad.Transition("escalate", st.level, self.p.level_for(f, st.weight_factor), st.factor, f,
                            st.weight_factor, st.weight_factor, reason)
        st.level, st.factor, st.changed_at = tr.level, f, now
        st.peak = tr.level if lad.RANK[tr.level] > lad.RANK[st.peak] else st.peak
        self._restart_holds(st, now)
        return self._on_transition(t, tr, now)

    def _charges(self, now: float, acting: bool) -> List[Tuple[str, str, ChargeCmd]]:
        """Once a second: debit each tenant's background-job CPU (not in X-TenantGuard-Cpu-Ms) from its cost
        bucket. tenantguard mode only; outside it the CPU is simply not charged."""
        if self.last_charge_at is not None and now - self.last_charge_at < self.cfg.charge_s:
            return []
        period = now - self.last_charge_at if self.last_charge_at is not None else self.cfg.charge_s
        self.last_charge_at = now
        pending, self.bg_pending = self.bg_pending, defaultdict(float)
        if not acting:
            return []
        return [(f"charge:{t}:{int(now)}", "/control/charge",
                 ChargeCmd(tenant=t, cpu_ms=round(ms, 3), period_s=round(period, 3), charge_id=f"{t}-{int(now)}"))
                for t, ms in sorted(pending.items()) if ms > 0]

    def charge_failed(self, cmd: ChargeCmd) -> None:
        """Not delivered: fold it into the next charge (a new charge_id; the gateway never saw this one)."""
        self.bg_pending[cmd.tenant] += cmd.cpu_ms

    # ------------------------------------------------------------------ operator decisions
    def operator(self, cmd: OperatorCmd, now: float) -> List[IncidentEvent]:
        """Apply a human decision (validated by the caller). The ladder obeys it, so humans and the ladder
        never fight: release = back to normal and no re-escalation until the hold ends; extend = keep the
        current level/factors (TTLs keep being renewed) until the hold ends; resume = cancel any hold."""
        t, who = cmd.tenant, cmd.by
        st = self.ladders[t]
        until = now + cmd.until_s
        case = f" (case {cmd.case_id})" if cmd.case_id else ""
        out: List[IncidentEvent] = []
        if cmd.decision == "resume":
            had = self.holds.pop(t, None)
            if had:
                self._restart_holds(st, now)
            inc = self.book.open.get(t)
            if inc is not None:
                out.append(self.book.emit(inc, now, "level_change", level=st.level, prev_level=st.level,
                                          action=f"operator resume by {who}",
                                          detail=f"Operator resume by {who}{case}: "
                                                 + ("the automatic ladder is back in charge."
                                                    if had else "there was no operator hold.")))
            return out
        self.holds[t] = OperatorHold(cmd.decision, until, who, cmd.case_id)
        if cmd.decision == "release":
            inc = self.book.open_for(t, now)
            prev = st.level
            tr = lad.reset(st, now, f"operator release by {who}")
            action = (self._action_text(1.0, 1.0, "normal") if tr else None)
            out.append(self.book.emit(inc, now, "level_change", level="normal", prev_level=prev,
                                      action=f"operator release by {who}" + (f"; {action}" if action else ""),
                                      detail=f"Operator hold by {who}{case}: released {self.tenants[t].name} ({t}) "
                                             f"from {prev} to normal; limits cleared and no automatic escalation "
                                             f"for {cmd.until_s:g} s."))
        else:  # extend
            inc = self.book.open_for(t, now)
            out.append(self.book.emit(inc, now, "level_change", level=st.level, prev_level=st.level,
                                      action=f"operator extend by {who}",
                                      detail=f"Operator hold by {who}{case}: keeping {self.tenants[t].name} ({t}) at "
                                             f"{st.level} (factor {st.factor:.2f}, weight factor "
                                             f"{st.weight_factor:.2f}) for {cmd.until_s:g} s; overrides are renewed "
                                             f"until then, then the automatic ladder resumes."))
        return out

    @staticmethod
    def _restart_holds(st: lad.LadderState, now: float) -> None:
        """The ladder saw no ticks during an operator hold: every rung needs a fresh, full hold."""
        for hd in (st.warn, st.throttle, st.quarantine, st.calm):
            hd.samples.clear()
            hd.start = None
        st.last_recover_at = None

    def _held_tick(self, t: str, sd: ScoreDetail, now: float) -> List[IncidentEvent]:
        h = self.holds[t]
        if h.decision != "release" or h.suppressed or not (sd.flagged and sd.score >= self.p.warn):
            return []
        h.suppressed = True
        inc = self.book.open_for(t, now)
        return [self.book.emit(inc, now, "level_change", level="normal", prev_level="normal",
                               action=f"operator hold by {h.by}",
                               detail=f"Operator hold by {h.by}: escalation suppressed until the hold ends in "
                                      f"{h.until - now:.0f} s. {self._evidence(t)}")]

    def _expire_holds(self, now: float) -> List[IncidentEvent]:
        out = []
        for t, h in list(self.holds.items()):
            if now < h.until:
                continue
            del self.holds[t]
            st = self.ladders[t]
            self._restart_holds(st, now)
            inc = self.book.open.get(t)
            if inc is None:
                continue
            out.append(self.book.emit(inc, now, "level_change", level=st.level, prev_level=st.level,
                                      action=f"operator hold by {h.by} ended",
                                      detail=f"Operator hold by {h.by} ({h.decision}) ended; the automatic ladder "
                                             f"resumes from {st.level}."))
            if st.level == "normal" and not st.acting:
                out.append(self.book.emit(inc, now, "closed", level="normal",
                                          detail=f"Incident closed after the operator hold by {h.by}."))
                self.book.close(inc, now)
        return out

    # ------------------------------------------------------------------ incidents
    def _evidence(self, t: str) -> str:
        sd = self.scores.get(t)
        if sd is None:
            return ""
        name = self.tenants[t].name
        hurt = [(v, s) for v, s in sd.victims if s >= self.hurt_off]
        worst = ", ".join(f"{v} {1 + s:.1f}x" for v, s in hurt[:3])
        n_nb = len(self.neighbours.get(t, []))
        return (f"{name} ({t}) uses {sd.cpu_share:.0%} of the CPU and {sd.queue_share:.0%} of the dispatch slots "
                f"of its servers against a fair share of {sd.fair_share:.0%} ({sd.usage_ratio:.1f}x, by {sd.dominant}). "
                f"{len(hurt)} of {n_nb} neighbours are slower than their calm baseline "
                f"(mean impact +{sd.victim_impact:.0%}{': ' + worst if worst else ''}). "
                f"Noisy score {sd.score:.2f}.")

    def _action_text(self, factor: float, wf: float, level: str) -> str:
        if factor >= 1 - lad.EPS and wf >= 1 - lad.EPS:
            return "LimitsCmd factor=1.00 weight_factor=1.00 (override cleared)"
        return (f"LimitsCmd factor={factor:.2f} weight_factor={wf:.2f} "
                f"ttl={self.p.ttl_for(level):g}s")

    def _on_transition(self, t: str, tr: lad.Transition, now: float) -> List[IncidentEvent]:
        out = []
        inc: Optional[Incident] = self.book.open.get(t)
        conf = self.scores[t].norm if t in self.scores else None
        ev_text = self._evidence(t)
        if inc is None:
            if tr.level == "normal":
                return out
            inc = self.book.open_for(t, now)
            out.append(self.book.emit(inc, now, "detected", level=tr.level, prev_level=tr.prev_level,
                                      confidence=conf,
                                      detail=f"Possible noisy neighbour: {ev_text}"))
        inc.peak = tr.level if lad.RANK[tr.level] > lad.RANK[inc.peak] else inc.peak
        acted = tr.factor != tr.prev_factor or tr.weight_factor != tr.prev_weight_factor
        action = self._action_text(tr.factor, tr.weight_factor, tr.level) if acted else None
        verb = {"escalate": "Escalated", "step": "Tightened", "recover": "Relaxed", "clear": "Cleared",
                "resolve": "Back to normal", "reset": "Reset"}[tr.kind]
        what = ""
        if acted:
            what = (f" Throttle factor {tr.prev_factor:.2f} -> {tr.factor:.2f} (rate, burst, cost, concurrency), "
                    f"scheduler weight factor {tr.prev_weight_factor:.2f} -> {tr.weight_factor:.2f}.")
        detail = f"{verb} {tr.prev_level} -> {tr.level} because {tr.rule}.{what} {ev_text}".strip()
        out.append(self.book.emit(inc, now, "level_change", level=tr.level, prev_level=tr.prev_level,
                                  action=action, detail=detail, confidence=conf))
        if tr.kind == "clear":
            out.append(self.book.emit(inc, now, "resolved", level=tr.level, action=action,
                                      detail=f"{self.tenants[t].name} ({t}) is back to its full plan; "
                                             f"neighbours have stayed healthy. Watching for {self.p.hold['warn']:g} s "
                                             f"before closing."))
        if tr.level == "normal" and t not in self.holds:
            dur = now - inc.opened_at
            out.append(self.book.emit(inc, now, "closed", level="normal",
                                      detail=f"Incident closed after {dur:.0f} s (worst level: {inc.peak}). "
                                             f"{tr.rule[0].upper() + tr.rule[1:]}."))
            self.book.close(inc, now)
        return out

    # ------------------------------------------------------------------ commands
    def _commands(self, now: float) -> List[LimitsCmd]:
        cmds = []
        for t, st in self.ladders.items():
            last = self.sent.get(t)
            if st.acting:
                ttl = self.p.ttl_for(st.level)
                due = (last is None or abs(last.factor - st.factor) > lad.EPS
                       or abs(last.weight_factor - st.weight_factor) > lad.EPS
                       or (last.ok is False and now - last.at >= 1.0)
                       or (last.ok is None and now - last.at >= 3.0)
                       or (last.ok and now - last.at >= ttl / 4.0))        # renew well before the TTL
                if due:
                    cmds.append(self._cmd(t, st.factor, st.weight_factor, ttl, now))
            elif last is not None:
                if abs(last.factor - 1) > lad.EPS or abs(last.weight_factor - 1) > lad.EPS:
                    cmds.append(self._cmd(t, 1.0, 1.0, self.p.ttl_throttle, now))     # explicit clear
                elif last.ok is False and now - last.at >= 1.0:
                    cmds.append(self._cmd(t, 1.0, 1.0, self.p.ttl_throttle, now))     # retry the clear
        return cmds

    def _cmd(self, t: str, f: float, wf: float, ttl: float, now: float) -> LimitsCmd:
        inc = self.book.open.get(t)
        self.sent[t] = Sent(f, wf, now, ttl)
        return LimitsCmd(tenant=t, factor=round(f, 4), weight_factor=round(wf, 4), ttl_s=ttl,
                         incident_id=inc.incident_id if inc else None)

    def mark_sent(self, cmd: LimitsCmd, ok: bool, now: float) -> None:
        last = self.sent.get(cmd.tenant)
        if last is None or abs(last.factor - cmd.factor) > 1e-3 or abs(last.weight_factor - cmd.weight_factor) > 1e-3:
            return              # superseded by a newer command
        last.ok = ok
        last.at = now
        if ok and cmd.factor >= 1 - lad.EPS and cmd.weight_factor >= 1 - lad.EPS:
            self.sent.pop(cmd.tenant, None)

    # ------------------------------------------------------------------ views
    def snapshot(self, now: float) -> StateSnapshot:
        frames = self._frames(now, self.cfg.view_window_s)
        span = self._span(frames) or 1.0
        agg = self._tenant_agg(frames)
        cpu = shares({t: v for t, v in self._tenant_cpu(now, self.cfg.view_window_s).items() if t in self.tenants}
                     or {t: 0.0 for t in self.tenants})
        rps = {t: (agg[t].completed if t in agg else 0) / span for t in self.tenants}
        tenants = []
        for t, cfg in self.tenants.items():
            a = agg.get(t, TenantAgg())
            sd = self.scores.get(t)
            st = self.ladders[t]
            tenants.append(TenantView(
                tenant=t, name=cfg.name, tier=cfg.tier, p95_ms=round(a.p95 or 0.0, 2),
                rps=round(rps[t], 2), rejected_rps=round(a.rejected / span, 2),
                cpu_share=round(cpu.get(t, 0.0), 4),
                noisy_score=round(sd.score, 3) if sd else 0.0, noisy_norm=round(sd.norm, 3) if sd else 0.0,
                level=st.level, factor=round(st.factor, 3), placement="normal"))

        sp95 = self._server_p95(self._frames(now, self.cfg.server_window_s))
        scpu = self._server_cpu(now, max(self.cfg.server_window_s, 2 * self.cfg.stats_poll_s))
        servers = []
        names = list(self.cfg.worker_urls) or sorted(set(sp95) | set(scpu))
        for s in names:
            p95 = sp95.get(s)
            h = self.s3.health.servers.get(s)
            ms, lim = scpu.get(s, (0.0, 0.0))
            servers.append(ServerView(server=s, p95_ms=round(p95 or 0.0, 2),
                                      peer_ratio=round(h.peer_ratio, 3) if h else 1.0,
                                      cpu_pct=round(100.0 * ms / (lim * 1000.0), 1) if lim > 0 else 0.0,
                                      status="slow" if h and h.slow else "healthy"))
        fleet = [scpu[s] for s in self.cfg.fleet_servers if s in scpu]
        cap = sum(lim * 1000.0 for _, lim in fleet)
        util = min(1.0, sum(ms for ms, _ in fleet) / cap) if cap > 0 else 0.0
        fair = jain(normalized_throughput(rps, self.weights).values())
        return StateSnapshot(ts=now, mode=self.mode or self.cfg.default_mode, tenants=tenants, servers=servers,
                             utilization=round(util, 4), fairness=round(fair, 4),
                             status_sentence=self.status_sentence(now, util, fair),
                             brain_ok=self.telemetry_fresh(now),
                             noisy_thresholds={"warn": self.p.warn, "throttle": self.p.throttle,
                                               "quarantine": self.p.quarantine,
                                               "recover_below": self.p.recover_below})

    def status_sentence(self, now: float, util: float, fair: float) -> str:
        if self.last_frame_at is None:
            return "Waiting for gateway telemetry."
        if not self.telemetry_fresh(now):
            return f"Gateway telemetry lost {now - self.last_frame_at:.0f} s ago; holding the last decisions."
        tail = f"Fairness {fair:.2f}, fleet CPU {util:.0%}."
        worst = max(self.ladders.items(), key=lambda kv: (lad.RANK[kv[1].level], self.scores.get(kv[0]).score
                                                           if kv[0] in self.scores else 0))
        t, st = worst
        name = f"{self.tenants[t].name} ({t})"
        sd = self.scores.get(t)
        if self.mode != ACTING_MODE:
            flagged = [s for s in self.scores.values() if s.flagged and s.score >= self.p.warn]
            if flagged:
                f = max(flagged, key=lambda s: s.score)
                return (f"Mode '{self.mode}': {self.tenants[f.tenant].name} ({f.tenant}) is using "
                        f"{f.usage_ratio:.1f}x its fair share and hurting neighbours; brain actions are off "
                        f"in this mode. {tail}")
            return f"Mode '{self.mode}': brain is observing only. {tail}"
        srv = self.s3.status()            # server incident, if any, leads the sentence
        if st.level == "normal":
            return f"{srv} {tail}" if srv else f"All {len(self.tenants)} tenants healthy. {tail}"
        if srv:
            tail = f"{srv} {tail}"
        usage = f"{sd.usage_ratio:.1f}x its fair share" if sd else "over its fair share"
        hurt = len([v for v, s in sd.victims if s >= self.hurt_off]) if sd else 0
        if st.level == "warn" and not st.acting:
            return f"Watching {name}: using {usage}, {hurt} neighbours slower than usual. {tail}"
        if st.level == "throttle":
            return f"{name} throttled to {st.factor:.0%} of plan: using {usage}. {tail}"
        if st.level == "deprioritize":
            return (f"{name} deprioritized (weight x{st.weight_factor:.2f}, limits {st.factor:.0%}): "
                    f"using {usage}. {tail}")
        if st.level == "quarantine":
            return f"{name} quarantined to {st.factor:.0%} of plan: using {usage}. {tail}"
        return f"{name} recovered; override cleared, watching before closing. {tail}"

    def debug(self) -> dict:
        return {
            "mode": self.mode, "calm": self.calm,
            "scores": {t: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in vars(s).items()
                           if k != "victims"} for t, s in self.scores.items()},
            "ladders": {t: {"level": s.level, "factor": s.factor, "weight_factor": s.weight_factor,
                            "changed_at": s.changed_at, "peak": s.peak} for t, s in self.ladders.items()},
            "baseline_p95_ms": {t: round(v, 2) for t, v in self.baseline.value.items()},
            "sent": {t: vars(s) for t, s in self.sent.items()},
            "open_incidents": {t: i.incident_id for t, i in self.book.open.items()},
            "servers": {s: {"source": h.source, "level_ms": h.level_ms, "peer_ms": h.peer_ms,
                            "peer_ratio": round(h.peer_ratio, 3), "slow_now": h.slow_now, "slow": h.slow}
                        for s, h in self.s3.health.servers.items()},
            "decoding": ({"culprits": [vars(c) for c in self.s3.decoding.culprits],
                          "host_suspects": self.s3.decoding.host_suspects,
                          "confidence": self.s3.decoding.top_confidence} if self.s3.decoding else None),
            "rescue": (vars(self.s3.plan) if self.s3.plan else None),
        }
