"""Calm reference pricing (pure, no I/O).

- A bucket is calm if no brain incident overlaps it. Calm buckets bill measured CPU and update the
  calm reference cost per (tenant, class) and per class fleet-wide (EWMA, weight ref_alpha).
- A bucket overlapping an incident never updates a reference (prices are frozen).
- In such a bucket a victim pays, per class, min(measured, count x calm reference), so contention
  never inflates its bill. A tenant the incident is about (ladder tenant or named culprit) pays measured.
- Per (tenant, class), because the gateway class ignores query values: search?n=1k and n=500k share a
  class, so a fleet-wide price would let a whale pay a light user's price.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .meter import Meter, Usage
from .timeline import Timeline


@dataclass
class Priced:
    requests: int
    measured_handler_ms: float
    billed_handler_ms: float
    bg_cpu_ms: float
    db_ms: float
    calm: bool
    blamed: bool


class Pricer:
    def __init__(self, alpha: float, bucket_s: float, settle_s: float) -> None:
        self.alpha = alpha
        self.bucket_s = bucket_s
        self.settle_s = settle_s                   # wait this long after a bucket ends (incident reports lag)
        self.ref: Dict[Tuple[str, str], float] = {}  # (tenant, cls) -> calm CPU ms per request
        self.gref: Dict[str, float] = {}              # cls -> calm CPU ms per request, fleet-wide
        self.closed: Dict[Tuple[str, float], Priced] = {}

    def _ewma(self, table: Dict, key, value: float) -> None:
        old = table.get(key)
        table[key] = value if old is None else old + self.alpha * (value - old)

    def ref_for(self, tenant: str, cls: str) -> Optional[float]:
        r = self.ref.get((tenant, cls))
        return r if r is not None else self.gref.get(cls)

    def price(self, tenant: str, b: float, u: Usage, tl: Timeline) -> Priced:
        eps = tl.overlapping(b, b + self.bucket_s)
        calm = not eps
        blamed = any(tenant in e.blamed for e in eps)
        billed = u.handler_cpu_ms
        if not calm and not blamed:
            billed = 0.0
            classified = 0.0
            for cls, (n, ms) in u.classes.items():
                classified += ms
                ref = self.ref_for(tenant, cls)
                billed += ms if ref is None else min(ms, n * ref)
            billed += max(0.0, u.handler_cpu_ms - classified)  # anything not in a class: measured
        return Priced(u.requests, u.handler_cpu_ms, billed, u.bg_cpu_ms, u.db_ms, calm, blamed)

    def close(self, meter: Meter, tl: Timeline, now: float) -> int:
        """Price every finished, settled bucket in time order; calm ones update the references."""
        n = 0
        by_bucket: Dict[float, list] = {}
        for t, b, u in meter.all_buckets():
            if (t, b) not in self.closed and b + self.bucket_s + self.settle_s <= now:
                by_bucket.setdefault(b, []).append((t, u))
        for b in sorted(by_bucket):
            calm_cls: Dict[str, list] = {}
            for t, u in by_bucket[b]:
                p = self.price(t, b, u, tl)
                self.closed[(t, b)] = p
                n += 1
                if p.calm:
                    for cls, (cnt, ms) in u.classes.items():
                        if cnt:
                            self._ewma(self.ref, (t, cls), ms / cnt)
                            agg = calm_cls.setdefault(cls, [0, 0.0])
                            agg[0] += cnt
                            agg[1] += ms
            for cls, (cnt, ms) in calm_cls.items():
                self._ewma(self.gref, cls, ms / cnt)
        return n

    def priced(self, tenant: str, b: float, u: Usage, tl: Timeline) -> Priced:
        """Closed price if settled, else a provisional one at the current references."""
        return self.closed.get((tenant, b)) or self.price(tenant, b, u, tl)

    def prune(self, cutoff: float) -> None:
        for k in [k for k in self.closed if k[1] < cutoff]:
            del self.closed[k]
