"""The brain's state machine: rolling state -> scores -> ladder -> commands + incident events.

No I/O here. The runtime feeds frames / worker stats in and sends the returned commands;
tests drive the same object with a simulated clock.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from tenantguard_common.contracts import (IncidentEvent, LimitsCmd, ServerView, StateSnapshot,
                                          TelemetryFrame, TenantView, WorkerStats)

from . import ladder as lad
from .fairness import jain, normalized_throughput, peer_ratio
from .incidents import Incident, IncidentBook
from .score import BaselineTracker, ScoreDetail, compute_scores, shares, slowdown
from .settings import BrainSettings

ACTING_MODE = "tenantguard"


@dataclass
class StatSample:
    t: float
    server: str
    dt: float                      # seconds covered (worker clock)
    tenant_cpu_ms: Dict[str, float]  # handler + bg CPU ms in dt
    total_cpu_ms: float
    limit_cores: float


@dataclass
class Sent:
    factor: float
    weight_factor: float
    at: float
    ttl: float
    ok: Optional[bool] = None      # None = in flight


@dataclass
class TickResult:
    commands: List[LimitsCmd] = field(default_factory=list)
    events: List[IncidentEvent] = field(default_factory=list)


@dataclass
class TenantAgg:
    completed: int = 0
    p95_weighted: float = 0.0
    rejected: int = 0
    occupancy: float = 0.0
    ticks: int = 0

    @property
    def p95(self) -> Optional[float]:
        return self.p95_weighted / self.completed if self.completed > 0 else None


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
        self.baseline = BaselineTracker(cfg.baseline_alpha)
        self.ladders: Dict[str, lad.LadderState] = {t: lad.LadderState() for t in self.tenants}
        self.scores: Dict[str, ScoreDetail] = {}
        self.book = IncidentBook()
        self.sent: Dict[str, Sent] = {}
        self.calm = True
        self.hurt_off = cfg.min_victim_impact / 2.0

    # ------------------------------------------------------------------ ingest
    def ingest_frame(self, frame: TelemetryFrame, now: float) -> None:
        self.frames.append((now, frame))
        self.mode = frame.mode
        self.last_frame_at = now
        self._prune(now)
        if not self.calm:
            return
        for q in frame.queues:
            if q.tenant not in self.tenants or q.completed <= 0 or q.p95_ms <= 0:
                continue        # contracts: skip ticks with completed == 0
            base = self.baseline.get(q.tenant)
            if base is not None and self.baseline.ready(q.tenant) and q.p95_ms > 2.0 * max(base, self.cfg.slow_abs_ms):
                continue        # an outlier in a calm period is not "normal"
            self.baseline.update(q.tenant, q.p95_ms)

    def ingest_stats(self, st: WorkerStats, now: float) -> None:
        self.worker_seen_at[st.server] = now
        prev = self.prev_stats.get(st.server)
        self.prev_stats[st.server] = st
        if prev is None or prev.started_at != st.started_at or st.ts <= prev.ts or st.cpu_total_ms < prev.cpu_total_ms:
            return              # first sample or the worker restarted: counters reset, no diff
        before = {u.tenant: u.handler_cpu_ms + u.bg_cpu_ms for u in prev.tenants}
        cpu = {}
        for u in st.tenants:
            d = u.handler_cpu_ms + u.bg_cpu_ms - before.get(u.tenant, 0.0)
            cpu[u.tenant] = max(0.0, d)
        self.stat_samples.append(StatSample(now, st.server, st.ts - prev.ts, cpu,
                                            st.cpu_total_ms - prev.cpu_total_ms, st.cpu_limit_cores))
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
                a.occupancy += q.depth + q.inflight
                if q.completed > 0 and q.p95_ms > 0:
                    a.completed += q.completed
                    a.p95_weighted += q.p95_ms * q.completed
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
        queue = {t: a.occupancy / max(1, a.ticks) for t, a in agg.items()}
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
        if fresh and self.mode == ACTING_MODE:
            for t, st in self.ladders.items():
                sd = self.scores[t]
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

        res.commands = self._commands(now)
        return res

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
        if tr.level == "normal":
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
            pr = peer_ratio(s, sp95)
            others = [v for k, v in sp95.items() if k != s and v]
            med = sorted(others)[len(others) // 2] if others else 0.0
            slow = p95 is not None and pr >= self.cfg.slow_peer_ratio and p95 - med >= self.cfg.slow_abs_ms
            ms, lim = scpu.get(s, (0.0, 0.0))
            servers.append(ServerView(server=s, p95_ms=round(p95 or 0.0, 2), peer_ratio=round(pr, 3),
                                      cpu_pct=round(100.0 * ms / (lim * 1000.0), 1) if lim > 0 else 0.0,
                                      status="slow" if slow else "healthy"))
        fleet = [scpu[s] for s in self.cfg.fleet_servers if s in scpu]
        cap = sum(lim * 1000.0 for _, lim in fleet)
        util = min(1.0, sum(ms for ms, _ in fleet) / cap) if cap > 0 else 0.0
        fair = jain(normalized_throughput(rps, self.weights).values())
        return StateSnapshot(ts=now, mode=self.mode or "off", tenants=tenants, servers=servers,
                             utilization=round(util, 4), fairness=round(fair, 4),
                             status_sentence=self.status_sentence(now, util, fair),
                             brain_ok=self.telemetry_fresh(now))

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
        if st.level == "normal":
            return f"All {len(self.tenants)} tenants healthy. {tail}"
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
            "ladders": {t: vars(s) for t, s in self.ladders.items()},
            "baseline_p95_ms": {t: round(v, 2) for t, v in self.baseline.value.items()},
            "sent": {t: vars(s) for t, s in self.sent.items()},
            "open_incidents": {t: i.incident_id for t, i in self.book.open.items()},
        }
