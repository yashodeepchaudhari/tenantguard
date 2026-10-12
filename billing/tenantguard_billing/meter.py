"""Turn cumulative WorkerStats into per-bucket deltas (pure, no I/O).

Restart rules, per server:
- first sight of a server: baseline only, unless the worker started after billing did
  (then everything it reports is new usage since its start)
- started_at changed: the worker restarted, so the new counters ARE the delta
- a counter that went down without a started_at change: treated as a reset of that counter
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from tenantguard_common.contracts import WorkerStats


@dataclass
class Usage:
    requests: int = 0
    handler_cpu_ms: float = 0.0
    bg_cpu_ms: float = 0.0
    db_ms: float = 0.0
    classes: Dict[str, List[float]] = field(default_factory=dict)  # cls -> [count, cpu_ms]

    def add(self, other: "Usage") -> None:
        self.requests += other.requests
        self.handler_cpu_ms += other.handler_cpu_ms
        self.bg_cpu_ms += other.bg_cpu_ms
        self.db_ms += other.db_ms
        for c, (n, ms) in other.classes.items():
            cur = self.classes.setdefault(c, [0, 0.0])
            cur[0] += n
            cur[1] += ms


def _d(cur: float, prev: float) -> float:
    return cur - prev if cur >= prev else cur  # counter reset: count from zero


@dataclass
class BlackboxSample:
    server: str
    t0: float
    t1: float
    cpu_ms: float


class Meter:
    def __init__(self, bucket_s: float, started_at: float, retention_s: float = 86_400) -> None:
        self.bucket_s = bucket_s
        self.started_at = started_at          # billing service start
        self.retention_s = retention_s
        self._last: Dict[str, WorkerStats] = {}
        self.buckets: Dict[Tuple[str, float], Usage] = defaultdict(Usage)   # (tenant, bucket_start)
        self.blackbox: List[BlackboxSample] = []
        self.restarts: Dict[str, int] = defaultdict(int)

    def bucket_of(self, ts: float) -> float:
        return ts - ts % self.bucket_s

    def ingest(self, s: WorkerStats) -> None:
        prev = self._last.get(s.server)
        if prev is not None and s.ts <= prev.ts:
            return  # duplicate / out of order
        self._last[s.server] = s
        if prev is None:
            if s.started_at < self.started_at:
                return  # history before billing started is not ours to bill
            base = WorkerStats(server=s.server, ts=s.started_at, started_at=s.started_at, cpu_total_ms=0, mem_mb=0)
        elif s.started_at != prev.started_at:
            self.restarts[s.server] += 1
            base = WorkerStats(server=s.server, ts=s.started_at, started_at=s.started_at, cpu_total_ms=0, mem_mb=0)
        else:
            base = prev
        self._apply(base, s)

    def _apply(self, prev: WorkerStats, cur: WorkerStats) -> None:
        b = self.bucket_of(cur.ts)
        before = {t.tenant: t for t in prev.tenants}
        for t in cur.tenants:
            p = before.get(t.tenant)
            u = Usage(
                requests=int(_d(t.requests, p.requests if p else 0)),
                handler_cpu_ms=_d(t.handler_cpu_ms, p.handler_cpu_ms if p else 0.0),
                bg_cpu_ms=_d(t.bg_cpu_ms, p.bg_cpu_ms if p else 0.0),
                db_ms=_d(t.db_ms, p.db_ms if p else 0.0),
            )
            if u.requests or u.handler_cpu_ms or u.bg_cpu_ms or u.db_ms:
                self.buckets[(t.tenant, b)].add(u)
        prev_cls = {(c.tenant, c.cls): c for c in prev.classes}
        for c in cur.classes:
            p = prev_cls.get((c.tenant, c.cls))
            n = int(_d(c.count, p.count if p else 0))
            if n:
                ms = _d(c.cpu_ms_total, p.cpu_ms_total if p else 0.0)
                self.buckets[(c.tenant, b)].add(Usage(classes={c.cls: [n, ms]}))
        bb = _d(cur.blackbox_cpu_ms, prev.blackbox_cpu_ms)
        self.blackbox.append(BlackboxSample(cur.server, prev.ts, cur.ts, bb))

    def prune(self, now: float) -> None:
        cutoff = now - self.retention_s
        for k in [k for k in self.buckets if k[1] < cutoff]:
            del self.buckets[k]
        self.blackbox = [s for s in self.blackbox if s.t1 >= cutoff]

    def tenant_buckets(self, tenant: str, start: float, end: float) -> List[Tuple[float, Usage]]:
        return sorted((b, u) for (t, b), u in self.buckets.items() if t == tenant and start <= b < end)

    def all_buckets(self) -> List[Tuple[str, float, Usage]]:
        return sorted(((t, b, u) for (t, b), u in self.buckets.items()), key=lambda x: (x[1], x[0]))

    def blackbox_ms(self, servers: List[str], start: float, end: Optional[float]) -> Tuple[float, float]:
        """(cpu_ms, seconds covered) of black-box CPU on `servers` within [start, end], pro-rated per sample."""
        end = end if end is not None else float("inf")
        cpu, secs = 0.0, 0.0
        for s in self.blackbox:
            if s.server not in servers or s.t1 <= start or s.t0 >= end:
                continue
            span = s.t1 - s.t0
            if span <= 0:
                continue
            overlap = min(s.t1, end) - max(s.t0, start)
            cpu += s.cpu_ms * overlap / span
            secs += overlap
        return cpu, secs
