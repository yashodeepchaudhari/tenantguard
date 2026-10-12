"""Client-side measurement: percentile math and a per-tenant sample recorder. No I/O."""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

# Outcome codes. HTTP status for real responses; these for client-side failures.
TIMEOUT = -1      # request timed out
CONN_ERROR = -2   # connect / transport error


def percentile(values: Sequence[float], q: float) -> float:
    """q-th percentile (0..100) with linear interpolation between closest ranks
    (same as numpy's default 'linear' method). Empty input -> 0.0."""
    if not values:
        return 0.0
    if not 0 <= q <= 100:
        raise ValueError("q must be within 0..100")
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(xs[lo])
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


@dataclass(frozen=True)
class Sample:
    ts: float          # wall-clock completion time, epoch s
    latency_ms: float
    status: int        # HTTP status, or TIMEOUT / CONN_ERROR
    src: str           # "calm" or "attack"
    kind: str = ""     # request kind (light, read, search, write, or the attack name)


@dataclass
class Summary:
    count: int = 0
    ok: int = 0
    r429: int = 0
    r503: int = 0
    other_err: int = 0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0

    def as_dict(self, window_s: Optional[float] = None) -> dict:
        d = dict(self.__dict__)
        if window_s:
            for k in ("count", "ok", "r429", "r503", "other_err"):
                d[f"{k}_per_s"] = round(getattr(self, k) / window_s, 3)
        for k in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
            d[k] = round(d[k], 3)
        return d


def summarize(samples: Iterable[Sample]) -> Summary:
    s = Summary()
    lat: List[float] = []
    for x in samples:
        s.count += 1
        lat.append(x.latency_ms)
        if 200 <= x.status < 400:
            s.ok += 1
        elif x.status == 429:
            s.r429 += 1
        elif x.status == 503:
            s.r503 += 1
        else:
            s.other_err += 1
    if lat:
        lat.sort()
        s.p50_ms = percentile(lat, 50)
        s.p95_ms = percentile(lat, 95)
        s.p99_ms = percentile(lat, 99)
        s.max_ms = lat[-1]
    return s


class Recorder:
    """Thread-safe store of recent samples per tenant plus cumulative counters.

    Samples older than `retention_s` (relative to the newest) are pruned, and each tenant keeps
    at most `max_per_tenant` samples, so a long flood cannot exhaust memory."""

    def __init__(self, retention_s: float = 900.0, max_per_tenant: int = 400_000):
        self.retention_s = retention_s
        self._samples: Dict[str, Deque[Sample]] = {}
        self._max = max_per_tenant
        self._lock = threading.Lock()
        self.totals: Dict[str, Dict[str, int]] = {}
        self.dropped: Dict[str, int] = {}

    def add(self, tenant: str, sample: Sample) -> None:
        with self._lock:
            dq = self._samples.get(tenant)
            if dq is None:
                dq = self._samples[tenant] = deque(maxlen=self._max)
            dq.append(sample)
            cutoff = sample.ts - self.retention_s
            while dq and dq[0].ts < cutoff:
                dq.popleft()
            t = self.totals.setdefault(tenant, {"count": 0, "ok": 0, "r429": 0, "r503": 0, "other_err": 0})
            t["count"] += 1
            if 200 <= sample.status < 400:
                t["ok"] += 1
            elif sample.status == 429:
                t["r429"] += 1
            elif sample.status == 503:
                t["r503"] += 1
            else:
                t["other_err"] += 1

    def drop(self, tenant: str) -> None:
        """A client-side drop: the arrival happened but the in-flight cap was reached."""
        with self._lock:
            self.dropped[tenant] = self.dropped.get(tenant, 0) + 1

    def tenants(self) -> List[str]:
        with self._lock:
            return sorted(self._samples)

    def window(self, tenant: str, t0: float, t1: float, src: Optional[str] = None,
               kinds: Optional[Iterable[str]] = None) -> List[Sample]:
        """Samples of `tenant` completed in [t0, t1), optionally one source and some request kinds."""
        with self._lock:
            dq = list(self._samples.get(tenant, ()))
        ks = set(kinds) if kinds else None
        return [s for s in dq if t0 <= s.ts < t1 and (src is None or s.src == src) and (ks is None or s.kind in ks)]

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self.totals.clear()
            self.dropped.clear()


def bucketize(samples: Iterable[Sample], t0: float, t1: float, bucket_s: float) -> List[List[float]]:
    """Latencies grouped into consecutive buckets of bucket_s covering [t0, t1)."""
    n = max(0, int(math.ceil((t1 - t0) / bucket_s - 1e-9)))
    out: List[List[float]] = [[] for _ in range(n)]
    for s in samples:
        if t0 <= s.ts < t1:
            i = int((s.ts - t0) / bucket_s)
            if 0 <= i < n:
                out[i].append(s.latency_ms)
    return out


def time_to_recover(buckets: List[List[float]], baseline_p99: float, ratio: float, hold: int,
                    bucket_s: float = 1.0) -> Tuple[Optional[float], bool]:
    """Seconds from the first bucket (attack start) until victims are recovered: the start of the first
    run of `hold` consecutive non-empty buckets with p99 <= ratio x baseline that begins after the first
    degraded bucket. Returns (seconds or None if never recovered, ever_degraded).

    Empty buckets neither break nor extend a run. If victims were never degraded the answer is 0."""
    limit = ratio * baseline_p99
    ok = [None if not b else percentile(b, 99) <= limit for b in buckets]
    first_bad = next((i for i, v in enumerate(ok) if v is False), None)
    if first_bad is None:
        return 0.0, False
    streak = 0
    start = 0
    for i in range(first_bad + 1, len(ok)):
        v = ok[i]
        if v is None:
            continue
        if v:
            if streak == 0:
                start = i
            streak += 1
            if streak >= hold:
                return start * bucket_s, True
        else:
            streak = 0
    return None, True
