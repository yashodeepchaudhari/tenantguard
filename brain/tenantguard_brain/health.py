"""Per-server health from peer comparison (pure, no I/O).

Two latency sources per server, each compared only with the same source on the other servers:

1. **traffic**: each tick, the median over the server's tenants of their cell p95 (server side), so
   one tenant's own heavy requests don't make the server look slow. Primary source.
2. **canary**: round-trip ms of the brain's own `GET /canary` probe. Used when a server has too few
   tenant requests in the window, e.g. after a rescue moved everyone away from it: without it a
   drained-by-weights server could never be judged healed (or still broken).

Over the last `probe_window_ticks` ticks the server's level is the *median* of its samples (robust to
one bad tick), and

    peer_ratio(s) = level(s) / median(level of the other servers)

A server is *slow now* when peer_ratio >= slow_peer_ratio AND level - peer median >= slow_abs_ms
(ratio alone would flag 2 ms vs 1 ms). It becomes *slow* (open) after `open_after_ticks` consecutive
slow-now ticks, and healthy again after as many consecutive not-slow ticks (hysteresis).
Comparing with peers (not an absolute threshold) keeps a fleet-wide slowdown from accusing anyone.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median
from typing import Deque, Dict, Iterable, List, Optional, Tuple


@dataclass
class HealthParams:
    window_ticks: int = 8
    tick_s: float = 0.25
    slow_peer_ratio: float = 1.6
    slow_abs_ms: float = 8.0
    open_after_ticks: int = 4
    min_requests: int = 4          # tenant requests in the window needed to trust the traffic source
    min_canaries: int = 2

    @classmethod
    def from_yaml(cls, health: dict, tick_s: float) -> "HealthParams":
        d = cls()
        return cls(window_ticks=int(health.get("probe_window_ticks", d.window_ticks)), tick_s=tick_s,
                   slow_peer_ratio=float(health.get("slow_peer_ratio", d.slow_peer_ratio)),
                   slow_abs_ms=float(health.get("slow_abs_ms", d.slow_abs_ms)),
                   open_after_ticks=int(health.get("open_after_ticks", d.open_after_ticks)),
                   min_requests=int(health.get("min_requests", d.min_requests)),
                   min_canaries=int(health.get("min_canaries", d.min_canaries)))

    @property
    def window_s(self) -> float:
        return self.window_ticks * self.tick_s


@dataclass
class ServerHealth:
    server: str
    source: str = "none"                  # traffic | canary | none
    level_ms: Optional[float] = None      # median latency over the window (from `source`)
    peer_ms: Optional[float] = None       # median level of the other servers (same source)
    peer_ratio: float = 1.0
    slow_now: bool = False
    slow: bool = False                    # open (debounced)
    streak: int = 0
    slow_since: Optional[float] = None
    traffic: Deque[Tuple[float, float, int]] = field(default_factory=deque)   # (t, p95, count)
    canary: Deque[Tuple[float, float]] = field(default_factory=deque)          # (t, rtt ms)


class HealthTracker:
    def __init__(self, servers: Iterable[str], params: HealthParams):
        self.p = params
        self.servers: Dict[str, ServerHealth] = {s: ServerHealth(s) for s in servers}

    def _h(self, s: str) -> ServerHealth:
        h = self.servers.get(s)
        if h is None:
            h = self.servers[s] = ServerHealth(s)
        return h

    def observe_canary(self, server: str, rtt_ms: float, now: float) -> None:
        self._h(server).canary.append((now, rtt_ms))

    def _levels(self, now: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        cut = now - self.p.window_s
        canary_cut = now - max(self.p.window_s, 4.0)      # canaries are ~1 Hz: keep a few
        traffic, canary = {}, {}
        for s, h in self.servers.items():
            while h.traffic and h.traffic[0][0] <= cut:
                h.traffic.popleft()
            while h.canary and h.canary[0][0] <= canary_cut:
                h.canary.popleft()
            if h.traffic and sum(n for _, _, n in h.traffic) >= self.p.min_requests:
                traffic[s] = median(p for _, p, _ in h.traffic)
            if len(h.canary) >= self.p.min_canaries:
                canary[s] = median(r for _, r in h.canary)
        return traffic, canary

    def observe(self, per_server: Dict[str, Tuple[float, int]], now: float) -> List[Tuple[str, bool]]:
        """Feed one tick: server -> (count-weighted p95, request count). Returns the servers whose
        open state flipped this tick: [(server, now_slow)]."""
        for s, (p95, n) in per_server.items():
            if n > 0 and p95 > 0:
                self._h(s).traffic.append((now, p95, n))
        traffic, canary = self._levels(now)
        flips = []
        for s, h in self.servers.items():
            src, levels = ("traffic", traffic) if s in traffic else (("canary", canary) if s in canary else ("none", {}))
            h.source = src
            others = [v for k, v in levels.items() if k != s]
            if src == "none" or not others:
                h.level_ms, h.peer_ms, h.peer_ratio, h.slow_now = levels.get(s), None, 1.0, False
            else:
                h.level_ms = levels[s]
                h.peer_ms = median(others)
                h.peer_ratio = h.level_ms / h.peer_ms if h.peer_ms > 0 else 1.0
                h.slow_now = (h.peer_ratio >= self.p.slow_peer_ratio
                              and h.level_ms - h.peer_ms >= self.p.slow_abs_ms)
            if h.slow_now != h.slow:
                h.streak += 1
                if h.streak >= self.p.open_after_ticks:
                    h.slow = h.slow_now
                    h.slow_since = now if h.slow else None
                    h.streak = 0
                    flips.append((s, h.slow))
            else:
                h.streak = 0
        return flips

    def slow_servers(self) -> List[str]:
        return sorted(s for s, h in self.servers.items() if h.slow)

    def ratios(self) -> Dict[str, float]:
        return {s: h.peer_ratio for s, h in self.servers.items()}
