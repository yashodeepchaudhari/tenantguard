"""Per-tick telemetry (TelemetryFrame) and cumulative Prometheus counters."""
from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from tenantguard_common.contracts import Cell, LimiterStat, QueueStat, TelemetryFrame

# Prometheus latency histogram bounds (ms).
LAT_BUCKETS_MS = (1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


def percentile(xs: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in 0..100); 0 for an empty sample."""
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return float(s[k])


class _TenantTick:
    __slots__ = ("waits", "e2e", "admitted", "rejected", "expired", "retries", "refused_cost")

    def __init__(self) -> None:
        self.waits: List[float] = []
        self.e2e: List[float] = []
        self.admitted = 0
        self.rejected = 0
        self.expired = 0
        self.retries = 0
        self.refused_cost = 0.0     # reference cost of refused / expired requests (unserved demand)


class _CellTick:
    __slots__ = ("lat", "errors")

    def __init__(self) -> None:
        self.lat: List[float] = []
        self.errors = 0


class Telemetry:
    def __init__(self) -> None:
        self._tenants: Dict[str, _TenantTick] = defaultdict(_TenantTick)
        self._cells: Dict[Tuple[str, str], _CellTick] = defaultdict(_CellTick)
        # cumulative, for /metrics
        self.c_requests: Dict[Tuple[str, str, str], int] = defaultdict(int)    # (tenant, server, code class)
        self.c_rejected: Dict[Tuple[str, str], int] = defaultdict(int)         # (tenant, reason)
        self.c_expired: Dict[str, int] = defaultdict(int)
        self.c_admitted: Dict[str, int] = defaultdict(int)
        self.c_cpu_ms: Dict[str, float] = defaultdict(float)
        self.c_retries: Dict[str, int] = defaultdict(int)
        self.c_cpu_missing: Dict[Tuple[str, str], int] = defaultdict(int)   # (tenant, server)
        self.h_e2e: Dict[str, List[int]] = defaultdict(lambda: [0] * (len(LAT_BUCKETS_MS) + 1))
        self.h_e2e_sum: Dict[str, float] = defaultdict(float)
        self.frames = 0

    # ----- events -----
    def admitted(self, tenant: str) -> None:
        self._tenants[tenant].admitted += 1
        self.c_admitted[tenant] += 1

    def rejected(self, tenant: str, reasons: Sequence[str], cost_ms: float = 0.0) -> None:
        tt = self._tenants[tenant]
        tt.rejected += 1
        tt.refused_cost += cost_ms
        for r in reasons or ("unknown",):
            self.c_rejected[(tenant, r)] += 1

    def expired(self, tenant: str, wait_ms: float, cost_ms: float = 0.0) -> None:
        tt = self._tenants[tenant]
        tt.expired += 1
        tt.refused_cost += cost_ms
        tt.waits.append(wait_ms)
        self.c_expired[tenant] += 1

    def dispatched(self, tenant: str, wait_ms: float) -> None:
        self._tenants[tenant].waits.append(wait_ms)

    def retry(self, tenant: str) -> None:
        self._tenants[tenant].retries += 1
        self.c_retries[tenant] += 1

    def cpu_missing(self, tenant: str, server: str) -> None:
        """Worker answered without a usable X-TenantGuard-Cpu-Ms: debited 0, counted here."""
        self.c_cpu_missing[(tenant, server)] += 1

    def done(self, tenant: str, server: str, upstream_ms: float, e2e_ms: float, status: int,
             cpu_ms: float = 0.0) -> None:
        err = status >= 500
        cell = self._cells[(tenant, server)]
        cell.lat.append(upstream_ms)
        cell.errors += int(err)
        self._tenants[tenant].e2e.append(e2e_ms)
        self.c_requests[(tenant, server, f"{status // 100}xx")] += 1
        self.c_cpu_ms[tenant] += cpu_ms
        h = self.h_e2e[tenant]
        for i, b in enumerate(LAT_BUCKETS_MS):
            if e2e_ms <= b:
                h[i] += 1
                break
        else:
            h[-1] += 1
        self.h_e2e_sum[tenant] += e2e_ms

    # ----- tick -----
    def frame(self, mode: str, tick_ms: int, tenants: Sequence[str], depth: Callable[[str], int],
              inflight: Callable[[str], int], limiter: Sequence[LimiterStat], ts: Optional[float] = None,
              queued_cost: Callable[[str], float] = lambda t: 0.0) -> TelemetryFrame:
        """Build the frame for the tick that just ended and start a new tick."""
        cells = [Cell(tenant=t, server=s, count=len(c.lat), p50_ms=percentile(c.lat, 50),
                      p95_ms=percentile(c.lat, 95), errors=c.errors)
                 for (t, s), c in sorted(self._cells.items())]
        names = list(tenants) + sorted(set(self._tenants) - set(tenants))
        queues = []
        for t in names:
            tt = self._tenants.get(t) or _TenantTick()
            queues.append(QueueStat(
                tenant=t, depth=depth(t), wait_p95_ms=percentile(tt.waits, 95),
                p95_ms=percentile(tt.e2e, 95), completed=len(tt.e2e), admitted=tt.admitted,
                rejected_429=tt.rejected, expired_503=tt.expired, retries=tt.retries, inflight=inflight(t),
                queued_cost_ms=round(queued_cost(t), 3), refused_cost_ms=round(tt.refused_cost, 3)))
        self._tenants = defaultdict(_TenantTick)
        self._cells = defaultdict(_CellTick)
        self.frames += 1
        return TelemetryFrame(ts=time.time() if ts is None else ts, mode=mode, tick_ms=tick_ms,
                              cells=cells, queues=queues, limiter=list(limiter))
