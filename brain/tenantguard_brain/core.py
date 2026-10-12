"""The brain's ladder engine: rolling state -> scores -> ladder -> per-tenant limits + incident events.

No I/O here. A tenant's neighbours are the other tenants of its project (they share its replicas); its CPU
comes from the control loop's attribution (metered + black box by docs + overhead, plus a split-test culprit's
unexplained CPU). The control loop owns allocation and replicas; this object owns ladders and incidents.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Mapping, Optional, Tuple

from tenantguard_common.contracts import (IncidentEvent, LimitsCmd, OperatorCmd, OverridesView, PoolView, ServerView,
                                          SnapTenant, StateSnapshot, TelemetryFrame, TenantView, WorkerStats)

from . import ladder as lad
from .detector import ReplicaUsage
from .fairness import jain, normalized_throughput, peer_ratio
from .incidents import Incident, IncidentBook
from .score import BaselineTracker, ScoreDetail, compute_scores, shares, slowdown
from .settings import BrainSettings

ACTING_MODE = "tenantguard"


@dataclass
class StatSample:
    t: float
    server: str                    # replica id
    dt: float                      # seconds covered (worker clock)
    tenant_cpu_ms: Dict[str, float]  # handler + bg CPU ms in dt
    total_cpu_ms: float
    limit_cores: float
    tenant_req: Dict[str, int] = field(default_factory=dict)
    tenant_docs: Dict[str, int] = field(default_factory=dict)
    blackbox_ms: float = 0.0


@dataclass
class OperatorHold:
    decision: str                  # release | extend
    until: float
    by: str
    case_id: Optional[str] = None
    suppressed: bool = False       # a 'would escalate' note was already emitted


@dataclass
class TickResult:
    events: List[IncidentEvent] = field(default_factory=list)
    changed: bool = False          # a tenant's factors changed: publish a snapshot now


@dataclass
class TenantAgg:
    completed: int = 0
    p95_weighted: float = 0.0
    rejected: int = 0
    occupancy: float = 0.0         # queued + in flight
    depth: float = 0.0             # queued only
    ticks: int = 0
    queued_ms: float = 0.0         # latest queued reference cost
    refused_ms: float = 0.0        # refused / expired reference cost over the window

    @property
    def p95(self) -> Optional[float]:
        return self.p95_weighted / self.completed if self.completed > 0 else None


def _replica_status(state: str, ratio: float, slow_ratio: float) -> str:
    """UI status of a replica: not taking traffic (starting / draining) = drained; slower than its project peers = slow."""
    if state != "ready":
        return "drained"
    return "slow" if ratio >= slow_ratio else "healthy"


def _diff(now: Mapping[str, float], before: Mapping[str, float]) -> Dict[str, float]:
    return {k: max(0.0, v - before.get(k, 0.0)) for k, v in now.items()}


class Brain:
    def __init__(self, cfg: BrainSettings, tenants: List[SnapTenant], replicas_of: Dict[str, List[str]]):
        self.cfg = cfg
        self.p = cfg.ladder
        self.tenants: Dict[str, SnapTenant] = {}
        self.ladders: Dict[str, lad.LadderState] = {}
        self.set_topology(tenants, replicas_of)
        self.frames: Deque[Tuple[float, TelemetryFrame]] = deque()
        self.mode: Optional[str] = None
        self.last_frame_at: Optional[float] = None
        self.prev_stats: Dict[str, WorkerStats] = {}
        self.stat_samples: Deque[StatSample] = deque()
        self.baseline = BaselineTracker(cfg.baseline_half_life_s)
        self.slowdowns: Dict[str, Optional[float]] = {}
        self.scores: Dict[str, ScoreDetail] = {}
        self.attributed: Dict[str, Dict[str, float]] = {}       # replica -> tenant -> ms/s (from the control loop)
        self.caps: Dict[str, float] = {}                         # replica -> cores (NanoCpus)
        self.book = IncidentBook()
        self.calm = True
        self.hurt_off = cfg.min_victim_impact / 2.0
        self.holds: Dict[str, OperatorHold] = {}

    # ------------------------------------------------------------------ topology
    def set_topology(self, tenants: List[SnapTenant], replicas_of: Dict[str, List[str]]) -> None:
        """Tenants (from the registry) and each project's replicas: a tenant's 'servers' are its project's
        replicas and its neighbours are the project's other tenants."""
        self.tenants = {t.id: t for t in tenants}
        self.weights = {t.id: float(t.weight) for t in tenants}
        self.servers_of = {t.id: list(replicas_of.get(t.project, [])) for t in tenants}
        self.neighbours = {t.id: [o.id for o in tenants if o.id != t.id and o.project == t.project] for t in tenants}
        for t in self.tenants:
            self.ladders.setdefault(t, lad.LadderState())
        for t in [t for t in self.ladders if t not in self.tenants]:
            self.ladders.pop(t)
            self.holds.pop(t, None)

    def set_attribution(self, by_replica: Dict[str, Dict[str, float]], caps: Dict[str, float]) -> None:
        self.attributed, self.caps = by_replica, caps

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
            if self._baseline_frozen(q.tenant):
                continue
            base = self.baseline.get(q.tenant)
            if base is not None and self.baseline.ready(q.tenant) and q.p95_ms > 2.0 * max(base, self.cfg.slow_abs_ms):
                continue        # an outlier in a calm period is not "normal"
            self.baseline.update(q.tenant, q.p95_ms, now)

    def adopt_overrides(self, ov: OverridesView, now: float) -> List[IncidentEvent]:
        """On start-up: take over limits a previous brain run left on the gateway, so they keep being managed
        (published, relaxed by AIMD, cleared) instead of lingering unmanaged until their TTL."""
        events: List[IncidentEvent] = []
        for t, cmd in ov.limits.items():
            st = self.ladders.get(t)
            if st is None or (cmd.factor >= 1 - lad.EPS and cmd.weight_factor >= 1 - lad.EPS):
                continue
            if st.level != "normal" or st.acting:
                continue                    # this run already manages the tenant: its state wins
            st.factor, st.weight_factor = cmd.factor, cmd.weight_factor
            st.level = st.peak = self.p.level_for(cmd.factor, cmd.weight_factor)
            st.changed_at = now
            inc = self.book.open_for(t, now)
            events.append(self.book.emit(
                inc, now, "detected", level=st.level, prev_level="normal",
                action=self._action_text(cmd.factor, cmd.weight_factor, st.level),
                detail=f"Adopted an override left by a previous brain run (factor {cmd.factor:.2f}, weight factor "
                       f"{cmd.weight_factor:.2f}, incident {cmd.incident_id or '?'}). It will be published while "
                       f"needed and relaxed when the project's other tenants are calm."))
        return events

    def _baseline_frozen(self, t: str) -> bool:
        """Don't learn a tenant's 'normal' while it is hurt, or while a project peer over-uses its share."""
        sd = self.slowdowns.get(t)
        if sd is not None and sd >= self.cfg.min_victim_impact:
            return True
        return any(self.scores[n].usage_ratio > 1.0 for n in self.neighbours.get(t, []) if n in self.scores)

    def ingest_stats(self, st: WorkerStats, now: float) -> None:
        prev = self.prev_stats.get(st.server)
        self.prev_stats[st.server] = st
        if prev is None or prev.started_at != st.started_at or st.ts <= prev.ts or st.cpu_total_ms < prev.cpu_total_ms:
            return              # first sample or the replica restarted: counters reset, no diff
        cpu = _diff({u.tenant: u.handler_cpu_ms + u.bg_cpu_ms for u in st.tenants},
                    {u.tenant: u.handler_cpu_ms + u.bg_cpu_ms for u in prev.tenants})
        req = _diff({u.tenant: u.requests for u in st.tenants}, {u.tenant: u.requests for u in prev.tenants})
        docs = _diff(st.docs_by_tenant, prev.docs_by_tenant)
        self.stat_samples.append(StatSample(now, st.server, st.ts - prev.ts, cpu, st.cpu_total_ms - prev.cpu_total_ms,
                                            st.cpu_limit_cores, req, docs,
                                            max(0.0, st.blackbox_cpu_ms - prev.blackbox_cpu_ms)))
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
                a.depth += q.depth
                a.queued_ms = q.queued_cost_ms
                a.refused_ms += q.refused_cost_ms
                if q.completed > 0 and q.p95_ms > 0:
                    a.completed += q.completed
                    a.p95_weighted += q.p95_ms * q.completed
        return agg

    def replica_usage(self, now: float, window: float) -> Dict[str, ReplicaUsage]:
        """Per replica meters over the window as rates (ms/s, req/s, docs/s); the control loop adds cgroup CPU."""
        acc: Dict[str, list] = {}
        for s in self.stat_samples:
            if s.t <= now - window:
                continue
            a = acc.setdefault(s.server, [0.0, defaultdict(float), defaultdict(float), defaultdict(float), 0.0])
            a[0] += s.dt
            for d, src in ((a[1], s.tenant_cpu_ms), (a[2], s.tenant_req), (a[3], s.tenant_docs)):
                for t, v in src.items():
                    d[t] += v
            a[4] += s.blackbox_ms
        return {r: ReplicaUsage(None, {t: v / dt for t, v in m.items()}, {t: v / dt for t, v in q.items()},
                                {t: v / dt for t, v in d.items()}, bb / dt)
                for r, (dt, m, q, d, bb) in acc.items() if dt > 0}

    def demand_signals(self, now: float, window: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        """(queued reference cost now, refused reference cost per second) per tenant, from gateway telemetry."""
        frames = self._frames(now, window)
        span = self._span(frames) or 1.0
        agg = self._tenant_agg(frames)
        return {t: a.queued_ms for t, a in agg.items()}, {t: a.refused_ms / span for t, a in agg.items()}

    def _replica_p95(self, frames: List[TelemetryFrame]) -> Dict[str, Optional[float]]:
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

    def _tenant_cpu(self) -> Dict[str, float]:
        out: Dict[str, float] = defaultdict(float)
        for per in self.attributed.values():
            for t, ms in per.items():
                out[t] += ms
        return dict(out)

    # ------------------------------------------------------------------ tick
    def _score(self, now: float) -> None:
        det = self._frames(now, self.cfg.detect_window_s)
        agg = self._tenant_agg(det)
        view_agg = self._tenant_agg(self._frames(now, self.cfg.view_window_s))
        self.slowdowns = {t: slowdown(agg[t].p95 if t in agg else None,
                                      self.baseline.get(t) if self.baseline.ready(t) else None, self.cfg.slow_abs_ms)
                          for t in self.tenants}
        # A hurt tenant holds slots longer because it is slowed down: its in-flight requests are a symptom, not a
        # cause, so only its queued requests count toward its occupancy.
        hurt = {t for t, s in self.slowdowns.items() if s is not None and s >= self.cfg.min_victim_impact}
        queue = {t: (a.depth if t in hurt else a.occupancy) / max(1, a.ticks) for t, a in agg.items()}
        replicas = {r for rs in self.servers_of.values() for r in rs}
        cpu_cap = {r: 1000.0 * (self.caps.get(r) or self.cfg.default_cores) for r in replicas}
        slots = {r: float(self.cfg.default_window) for r in replicas}
        for ls in (det[-1].limiter if det else []):
            if ls.server in slots and ls.window > 0:
                slots[ls.server] = float(ls.window)
        cpu = self._tenant_cpu()
        active = [t for t in self.tenants
                  if (t in view_agg and (view_agg[t].completed > 0 or view_agg[t].occupancy > 0)) or cpu.get(t, 0) > 0]
        self.scores = compute_scores(
            weights=self.weights, servers_of=self.servers_of, active=active, cpu_by_server=self.attributed,
            cpu_cap=cpu_cap, occupancy=queue, slots=slots, slowdowns=self.slowdowns,
            neighbours=self.neighbours, min_victim_impact=self.cfg.min_victim_impact,
            quarantine_threshold=self.p.quarantine)
        self.calm = not self.book.open and all(not (s.flagged and s.score >= self.p.warn) for s in self.scores.values())

    def tick(self, now: float) -> TickResult:
        self._prune(now)
        res = TickResult()
        self._score(now)
        before = {t: (s.factor, s.weight_factor) for t, s in self.ladders.items()}
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
        # stale telemetry: hold every ladder where it is (no evidence either way); limits keep being published
        res.changed = before != {t: (s.factor, s.weight_factor) for t, s in self.ladders.items()}
        return res

    def limits(self) -> List[LimitsCmd]:
        """The override of every acting tenant, for the next snapshot (the snapshot renews the TTL)."""
        out = []
        for t, st in sorted(self.ladders.items()):
            if st.acting:
                inc = self.book.open.get(t)
                out.append(LimitsCmd(tenant=t, factor=round(st.factor, 4), weight_factor=round(st.weight_factor, 4),
                                     ttl_s=self.p.ttl_for(st.level), incident_id=inc.incident_id if inc else None))
        return out

    # ------------------------------------------------------------------ findings from the control loop
    def on_finding(self, kind: str, project: Optional[str], tenant: Optional[str], detail: str, evidence: dict,
                   now: float) -> List[IncidentEvent]:
        """Turn a control-loop finding into incident events. The ladder still decides any throttling: a
        split-test culprit is charged the unexplained CPU, which raises its score."""
        if kind == "external_contention":
            inc = self.book.open_server(now)
            return [self.book.emit(inc, now, "detected", detail=detail, report={"verdict": "host_fault", **evidence})]
        if tenant is None or tenant not in self.tenants:
            inc = self.book.open_server(now)
            verdict = "mitigated_no_blame" if kind == "split_aborted" else None
            stage = "verdict" if verdict else "proving"
            return [self.book.emit(inc, now, stage, detail=detail, report={"project": project, "verdict": verdict,
                                                                            **evidence})]
        inc = self.book.open_for(tenant, now)
        return [self.book.emit(inc, now, "verdict", detail=detail, confidence=1.0,
                               action=f"unexplained CPU of project {project} charged to {tenant}",
                               report={"verdict": "costly_pattern", "project": project, **evidence})]

    # ------------------------------------------------------------------ operator decisions
    def operator(self, cmd: OperatorCmd, now: float) -> List[IncidentEvent]:
        """Apply a human decision (validated by the caller). The ladder obeys it, so humans and the ladder
        never fight: release = back to normal and no re-escalation until the hold ends; extend = keep the
        current level/factors until the hold ends; resume = cancel any hold."""
        t, who = cmd.tenant, cmd.by
        st = self.ladders[t]
        until = now + cmd.until_s
        case = f" (case {cmd.case_id})" if cmd.case_id else ""
        out: List[IncidentEvent] = []
        if cmd.decision == "resume":
            had = self.holds.pop(t, None)
            if had:
                self._restart_holds(st)
            inc = self.book.open.get(t)
            if inc is not None:
                out.append(self.book.emit(inc, now, "level_change", level=st.level, prev_level=st.level,
                                          action=f"operator resume by {who}",
                                          detail=f"Operator resume by {who}{case}: "
                                                 + ("the automatic ladder is back in charge."
                                                    if had else "there was no operator hold.")))
            return out
        self.holds[t] = OperatorHold(cmd.decision, until, who, cmd.case_id)
        inc = self.book.open_for(t, now)
        if cmd.decision == "release":
            prev = st.level
            tr = lad.reset(st, now, f"operator release by {who}")
            action = (self._action_text(1.0, 1.0, "normal") if tr else None)
            out.append(self.book.emit(inc, now, "level_change", level="normal", prev_level=prev,
                                      action=f"operator release by {who}" + (f"; {action}" if action else ""),
                                      detail=f"Operator hold by {who}{case}: released {self.tenants[t].name} ({t}) "
                                             f"from {prev} to normal; limits cleared and no automatic escalation "
                                             f"for {cmd.until_s:g} s."))
        else:  # extend
            out.append(self.book.emit(inc, now, "level_change", level=st.level, prev_level=st.level,
                                      action=f"operator extend by {who}",
                                      detail=f"Operator hold by {who}{case}: keeping {self.tenants[t].name} ({t}) at "
                                             f"{st.level} (factor {st.factor:.2f}, weight factor "
                                             f"{st.weight_factor:.2f}) for {cmd.until_s:g} s; the override is "
                                             f"published until then, then the automatic ladder resumes."))
        return out

    @staticmethod
    def _restart_holds(st: lad.LadderState) -> None:
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
            self._restart_holds(st)
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
                f"of project {self.tenants[t].project} against a fair share of {sd.fair_share:.0%} "
                f"({sd.usage_ratio:.1f}x, by {sd.dominant}). {len(hurt)} of {n_nb} other tenants of the project are "
                f"slower than their calm baseline (mean impact +{sd.victim_impact:.0%}"
                f"{': ' + worst if worst else ''}). Noisy score {sd.score:.2f}.")

    def _action_text(self, factor: float, wf: float, level: str) -> str:
        if factor >= 1 - lad.EPS and wf >= 1 - lad.EPS:
            return "LimitsCmd factor=1.00 weight_factor=1.00 (override cleared)"
        return f"LimitsCmd factor={factor:.2f} weight_factor={wf:.2f} ttl={self.p.ttl_for(level):g}s"

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
                                      confidence=conf, detail=f"Possible noisy neighbour: {ev_text}"))
        inc.peak = tr.level if lad.RANK[tr.level] > lad.RANK[inc.peak] else inc.peak
        acted = tr.factor != tr.prev_factor or tr.weight_factor != tr.prev_weight_factor
        action = self._action_text(tr.factor, tr.weight_factor, tr.level) if acted else None
        verb = {"escalate": "Escalated", "step": "Tightened", "recover": "Relaxed", "clear": "Cleared",
                "resolve": "Back to normal", "reset": "Reset"}[tr.kind]
        what = ""
        if acted:
            what = (f" Throttle factor {tr.prev_factor:.2f} -> {tr.factor:.2f} (rate, burst, concurrency), "
                    f"scheduler weight factor {tr.prev_weight_factor:.2f} -> {tr.weight_factor:.2f}.")
        detail = f"{verb} {tr.prev_level} -> {tr.level} because {tr.rule}.{what} {ev_text}".strip()
        out.append(self.book.emit(inc, now, "level_change", level=tr.level, prev_level=tr.prev_level,
                                  action=action, detail=detail, confidence=conf))
        if tr.kind == "clear":
            out.append(self.book.emit(inc, now, "resolved", level=tr.level, action=action,
                                      detail=f"{self.tenants[t].name} ({t}) is back to its full plan; the project's "
                                             f"other tenants have stayed healthy. Watching for {self.p.hold['warn']:g} s "
                                             f"before closing."))
        if tr.level == "normal" and t not in self.holds:
            dur = now - inc.opened_at
            out.append(self.book.emit(inc, now, "closed", level="normal",
                                      detail=f"Incident closed after {dur:.0f} s (worst level: {inc.peak}). "
                                             f"{tr.rule[0].upper() + tr.rule[1:]}."))
            self.book.close(inc, now)
        return out

    # ------------------------------------------------------------------ views
    def snapshot(self, now: float, pool: Optional[PoolView] = None) -> StateSnapshot:
        frames = self._frames(now, self.cfg.view_window_s)
        span = self._span(frames) or 1.0
        agg = self._tenant_agg(frames)
        cpu = shares({t: v for t, v in self._tenant_cpu().items() if t in self.tenants} or {t: 0.0 for t in self.tenants})
        rps = {t: (agg[t].completed if t in agg else 0) / span for t in self.tenants}
        tenants = []
        for t, cfg in self.tenants.items():
            a = agg.get(t, TenantAgg())
            sd = self.scores.get(t)
            st = self.ladders[t]
            tenants.append(TenantView(
                tenant=t, name=cfg.name, tier=cfg.tier, p95_ms=round(a.p95 or 0.0, 2), rps=round(rps[t], 2),
                rejected_rps=round(a.rejected / span, 2), cpu_share=round(cpu.get(t, 0.0), 4),
                noisy_score=round(sd.score, 3) if sd else 0.0, noisy_norm=round(sd.norm, 3) if sd else 0.0,
                level=st.level, factor=round(st.factor, 3), project=cfg.project))
        servers, util = self._replica_views(now, pool)
        fair = jain(normalized_throughput(rps, self.weights).values())
        return StateSnapshot(ts=now, mode=self.mode or self.cfg.default_mode, tenants=tenants, servers=servers,
                             utilization=round(util, 4), fairness=round(fair, 4),
                             status_sentence=self.status_sentence(now, util, fair), brain_ok=self.telemetry_fresh(now),
                             noisy_thresholds={"warn": self.p.warn, "throttle": self.p.throttle,
                                               "quarantine": self.p.quarantine, "recover_below": self.p.recover_below},
                             pool=pool)

    def _replica_views(self, now: float, pool: Optional[PoolView]) -> Tuple[List[ServerView], float]:
        """One ServerView per replica, and pool utilization (cgroup CPU used / pool CPU)."""
        p95 = self._replica_p95(self._frames(now, self.cfg.server_window_s))
        out, used = [], 0.0
        for pv in (pool.projects if pool else []):
            mine = {r.id: p95.get(r.id) for r in pv.replicas}
            used += pv.used_cpu
            for r in pv.replicas:
                cores = r.nano_cpus / 1e9 or self.cfg.default_cores
                ms = sum(self.attributed.get(r.id, {}).values())
                out.append(ServerView(server=r.id, project=pv.id, p95_ms=round(mine[r.id] or 0.0, 2),
                                      peer_ratio=round(peer_ratio(r.id, mine), 3),
                                      cpu_pct=round(min(100.0, ms / (cores * 10.0)), 1),
                                      status=_replica_status(r.state, peer_ratio(r.id, mine),
                                                             self.cfg.detect.contention_ratio)))
        util = min(1.0, used / pool.pool.cpu_total) if pool and pool.pool.cpu_total > 0 else 0.0
        return out, util

    def status_sentence(self, now: float, util: float, fair: float) -> str:
        if self.last_frame_at is None:
            return "Waiting for gateway telemetry."
        if not self.telemetry_fresh(now):
            return f"Gateway telemetry lost {now - self.last_frame_at:.0f} s ago; holding the last decisions."
        tail = f"Fairness {fair:.2f}, pool CPU {util:.0%}."
        if not self.ladders:
            return f"No tenants yet. {tail}"
        t, st = max(self.ladders.items(), key=lambda kv: (lad.RANK[kv[1].level],
                                                          self.scores[kv[0]].score if kv[0] in self.scores else 0))
        name = f"{self.tenants[t].name} ({t})"
        sd = self.scores.get(t)
        if self.mode != ACTING_MODE:
            flagged = [s for s in self.scores.values() if s.flagged and s.score >= self.p.warn]
            if flagged:
                f = max(flagged, key=lambda s: s.score)
                return (f"Mode '{self.mode}': {self.tenants[f.tenant].name} ({f.tenant}) is using "
                        f"{f.usage_ratio:.1f}x its fair share of its project and hurting the project's other tenants; "
                        f"brain actions are off in this mode. {tail}")
            return f"Mode '{self.mode}': brain is observing only. {tail}"
        if st.level == "normal":
            return f"All {len(self.tenants)} tenants healthy. {tail}"
        usage = f"{sd.usage_ratio:.1f}x its fair share" if sd else "over its fair share"
        if st.level == "warn" and not st.acting:
            hurt = len([v for v, s in sd.victims if s >= self.hurt_off]) if sd else 0
            return f"Watching {name}: using {usage}, {hurt} project peers slower than usual. {tail}"
        if st.level == "throttle":
            return f"{name} throttled to {st.factor:.0%} of plan: using {usage}. {tail}"
        if st.level == "deprioritize":
            return f"{name} deprioritized (weight x{st.weight_factor:.2f}, limits {st.factor:.0%}): using {usage}. {tail}"
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
            "open_incidents": {t: i.incident_id for t, i in self.book.open.items()},
            "attributed_ms_per_s": {r: {t: round(v, 2) for t, v in m.items()} for r, m in self.attributed.items()},
        }
