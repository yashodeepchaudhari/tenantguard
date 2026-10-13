"""Incidents and latency history, from the brain's structured data only (pure, no I/O).

Never copies the brain's `detail` text: it names tenants. Everything a victim later reads is
built from structured fields (stage timestamps, report.culprits, ServerView.status, barcodes).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from tenantguard_common.contracts import IncidentEvent, StateSnapshot

THROTTLE_LEVELS = {"throttle", "deprioritize", "quarantine", "blocked"}
NOMINAL_TICK_S = 0.5  # the brain's StateSnapshot period (2 Hz)
MAX_TICK_S = 1.0      # a longer gap (stream down) counts as one nominal tick, never as the whole gap


@dataclass
class Episode:
    incident_id: str
    kind: str                                  # "server" (srv-*) | "tenant" (usage ladder, inc-*)
    start: float
    end: Optional[float] = None                # None = still open
    ladder_tenant: Optional[str] = None        # tenant incident: the over-user
    culprits: Dict[str, float] = field(default_factory=dict)   # named culprit -> confidence
    named_at: Optional[float] = None
    host_suspects: List[str] = field(default_factory=list)
    rescued_at: Optional[float] = None
    throttled_at: Optional[float] = None

    def overlaps(self, a: float, b: float) -> bool:
        return self.start < b and (self.end is None or self.end > a)

    def open_at(self, ts: float) -> bool:
        return self.start <= ts and (self.end is None or ts < self.end)

    @property
    def blamed(self) -> Set[str]:
        """Tenants this episode was about (they pay measured cost and get no credit for it)."""
        out = set(self.culprits)
        if self.ladder_tenant:
            out.add(self.ladder_tenant)
        return out


class Timeline:
    def __init__(self) -> None:
        self.episodes: Dict[str, Episode] = {}
        self._seen: Set[Tuple[str, str, float]] = set()

    def ingest(self, events: Iterable[IncidentEvent]) -> None:
        for ev in sorted(events, key=lambda e: e.ts):
            key = (ev.incident_id, ev.stage, ev.ts)
            if key in self._seen:
                continue
            self._seen.add(key)
            ep = self.episodes.get(ev.incident_id)
            if ep is None:
                kind = "server" if ev.incident_id.startswith("srv-") else "tenant"
                ep = self.episodes[ev.incident_id] = Episode(ev.incident_id, kind, ev.ts)
                if kind == "tenant":
                    ep.ladder_tenant = ev.tenant
            ep.start = min(ep.start, ev.ts)
            if ev.stage == "named":
                report = ev.report or {}
                culprits = report.get("culprits")
                if culprits is not None:
                    ep.culprits = {c["tenant"]: float(c.get("confidence", 0.0)) for c in culprits}
                else:
                    ep.culprits = {s.tenant: s.confidence for s in ev.suspects if s.tenant}
                ep.host_suspects = list(report.get("host_suspects") or [])
                ep.named_at = ep.named_at or ev.ts
            elif ev.stage == "rescued":
                ep.rescued_at = ep.rescued_at or ev.ts
            elif ev.stage in ("level_change", "detected") and ev.level in THROTTLE_LEVELS:
                ep.throttled_at = ep.throttled_at or ev.ts
            elif ev.stage == "closed":
                ep.end = ev.ts

    def overlapping(self, a: float, b: float) -> List[Episode]:
        return sorted((e for e in self.episodes.values() if e.overlaps(a, b)), key=lambda e: e.start)

    def open_at(self, ts: float) -> List[Episode]:
        return [e for e in self.episodes.values() if e.open_at(ts)]

    def prune(self, cutoff: float) -> None:
        for iid in [i for i, e in self.episodes.items() if e.end is not None and e.end < cutoff]:
            del self.episodes[iid]
        self._seen = {k for k in self._seen if k[0] in self.episodes}


@dataclass
class Tick:
    ts: float
    dt: float
    p95: Dict[str, float]
    slow_servers: Set[str]


class LatencyLog:
    """StateSnapshot history (2 Hz): per-tenant p95 and slow servers."""

    def __init__(self) -> None:
        self.ticks: List[Tick] = []
        self._ts: List[float] = []

    def ingest(self, snap: StateSnapshot) -> None:
        if self._ts and snap.ts <= self._ts[-1]:
            return
        gap = snap.ts - self._ts[-1] if self._ts else NOMINAL_TICK_S
        dt = gap if gap <= MAX_TICK_S else NOMINAL_TICK_S
        self.ticks.append(Tick(snap.ts, dt, {t.tenant: t.p95_ms for t in snap.tenants},
                               {s.server for s in snap.servers if s.status == "slow"}))
        self._ts.append(snap.ts)

    def between(self, a: float, b: float) -> List[Tick]:
        i, j = bisect.bisect_left(self._ts, a), bisect.bisect_left(self._ts, b)
        return self.ticks[i:j]

    def prune(self, cutoff: float) -> None:
        i = bisect.bisect_left(self._ts, cutoff)
        del self.ticks[:i]
        del self._ts[:i]


def violations(tenant: str, sla_ms: float, log: LatencyLog, tl: Timeline, a: float, b: float
               ) -> List[Tuple[Tick, Episode]]:
    """Ticks in [a, b) where the tenant missed its SLA while an incident it was not blamed for was open."""
    out = []
    for tick in log.between(a, b):
        p95 = tick.p95.get(tenant, 0.0)
        if p95 <= sla_ms:  # 0 = no completed requests: skip, never "fast"
            continue
        eps = [e for e in tl.open_at(tick.ts) if tenant not in e.blamed]
        if eps:
            out.append((tick, eps[0]))
    return out
