"""Per-server health (pure, no I/O).

**Traffic source (primary): tenant-relative.** For every (tenant, server) cell the level over the last
`window_s` seconds is the count-weighted median of its per-tick p95, used only with at least
`min_cell_requests` requests. Each tenant's reference is the median of its levels across its own servers,
and its ratio on a server is level / reference. A server's score is the median of that ratio over its
tenants (at least `min_tenants` of them):

    peer_ratio(s) = median over tenants t on s of  level(t, s) / median(level(t, s') for s' in t's servers)

Comparing a tenant with itself cancels workload differences (a search-heavy tenant is slow everywhere,
a light one fast everywhere), which a raw cross-server p95 comparison does not. It also needs real data:
a cell with 1-2 requests in the window is not an opinion. A fleet-wide slowdown keeps every ratio near 1,
so nobody is accused.

**Canary source (fallback):** round trip of the brain's own `GET /canary`, median of the recent probes,
compared with the other (fleet) servers' canaries. Used when too few tenants have data on a server, e.g.
after a rescue moved everyone away from it, so a drained-by-weights server can still be judged healed or
broken. The canary can only keep an already-slow server open, never open one: an incident needs slow real
traffic first, so an idle or lightly loaded host (probe jitter of a few ms) opens nothing.

A server is *slow now* when peer_ratio >= slow_peer_ratio AND its median excess latency >= slow_abs_ms
(canary: canary_abs_ms; ratio alone would flag 2 ms vs 1 ms). It becomes *slow* (open) after `open_after_ticks` consecutive
slow-now ticks, and healthy again after as many consecutive not-slow ticks (hysteresis).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median
from typing import Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


@dataclass
class HealthParams:
    window_ticks: int = 8
    tick_s: float = 0.25
    window_s: float = 3.0              # traffic window per (tenant, server)
    slow_peer_ratio: float = 1.6
    slow_abs_ms: float = 8.0
    open_after_ticks: int = 4
    min_cell_requests: int = 5         # requests of one tenant on one server in the window to trust its level
    min_tenants: int = 2               # tenants with data on a server to use the traffic source
    min_canaries: int = 3
    canary_window_s: float = 6.0
    canary_abs_ms: float = 8.0         # absolute floor on the canary path too (idle hosts jitter by ms)

    @classmethod
    def from_yaml(cls, health: dict, tick_s: float) -> "HealthParams":
        d = cls()
        ticks = int(health.get("probe_window_ticks", d.window_ticks))
        return cls(window_ticks=ticks, tick_s=tick_s,
                   window_s=float(health.get("window_s", max(d.window_s, ticks * tick_s))),
                   slow_peer_ratio=float(health.get("slow_peer_ratio", d.slow_peer_ratio)),
                   slow_abs_ms=float(health.get("slow_abs_ms", d.slow_abs_ms)),
                   open_after_ticks=int(health.get("open_after_ticks", d.open_after_ticks)),
                   min_cell_requests=int(health.get("min_cell_requests", health.get("min_requests",
                                                                                    d.min_cell_requests))),
                   min_tenants=int(health.get("min_tenants", d.min_tenants)),
                   min_canaries=int(health.get("min_canaries", d.min_canaries)),
                   canary_window_s=float(health.get("canary_window_s", d.canary_window_s)),
                   canary_abs_ms=float(health.get("canary_abs_ms", d.canary_abs_ms)))


@dataclass
class ServerHealth:
    server: str
    source: str = "none"                  # traffic | canary | none
    level_ms: Optional[float] = None      # median level of its tenants here (traffic) or canary median
    peer_ms: Optional[float] = None       # median of those tenants' references (traffic) or peers' canaries
    peer_ratio: float = 1.0
    excess_ms: float = 0.0
    tenants_used: int = 0
    slow_now: bool = False
    slow: bool = False                    # open (debounced)
    streak: int = 0
    slow_since: Optional[float] = None
    canary: Deque[Tuple[float, float]] = field(default_factory=deque)          # (t, rtt ms)


def weighted_median(samples: Sequence[Tuple[float, int]]) -> float:
    """Median of values weighted by counts."""
    xs = sorted(samples)
    total = sum(n for _, n in xs)
    acc = 0
    for v, n in xs:
        acc += n
        if acc * 2 >= total:
            return v
    return xs[-1][0]


class HealthTracker:
    def __init__(self, servers: Iterable[str], params: HealthParams,
                 barcodes: Optional[Mapping[str, Sequence[str]]] = None):
        self.p = params
        self.servers: Dict[str, ServerHealth] = {s: ServerHealth(s) for s in servers}
        # servers that are peers of each other (the barcode fleet). Pool servers (spare, quarantine) are judged
        # against the fleet but never count as anyone's peer: they hold whoever we moved there.
        self.fleet = set(s for v in (barcodes or {}).values() for s in v) or None
        self.cells: Dict[Tuple[str, str], Deque[Tuple[float, float, int]]] = {}   # (tenant, server) -> (t, p95, n)

    def _h(self, s: str) -> ServerHealth:
        h = self.servers.get(s)
        if h is None:
            h = self.servers[s] = ServerHealth(s)
        return h

    # ------------------------------------------------------------ inputs
    def observe_canary(self, server: str, rtt_ms: float, now: float) -> None:
        self._h(server).canary.append((now, rtt_ms))

    def observe(self, cells: Iterable[Tuple[str, str, float, int]], now: float) -> List[Tuple[str, bool]]:
        """Feed one tick of cells: (tenant, server, p95_ms, count). Returns open-state flips [(server, slow)]."""
        for t, s, p95, n in cells:
            if n > 0 and p95 > 0:
                self.cells.setdefault((t, s), deque()).append((now, p95, n))
                self._h(s)
        return self._evaluate(now)

    # ------------------------------------------------------------ evaluation
    def _cell_levels(self, now: float) -> Dict[Tuple[str, str], float]:
        cut = now - self.p.window_s
        out = {}
        for key, dq in list(self.cells.items()):
            while dq and dq[0][0] <= cut:
                dq.popleft()
            if not dq:
                del self.cells[key]
                continue
            if sum(n for _, _, n in dq) >= self.p.min_cell_requests:
                out[key] = weighted_median([(p, n) for _, p, n in dq])
        return out

    def _canary_levels(self, now: float) -> Dict[str, float]:
        cut = now - self.p.canary_window_s
        out = {}
        for s, h in self.servers.items():
            while h.canary and h.canary[0][0] <= cut:
                h.canary.popleft()
            if len(h.canary) >= self.p.min_canaries:
                out[s] = median(r for _, r in h.canary)
        return out

    def _evaluate(self, now: float) -> List[Tuple[str, bool]]:
        levels = self._cell_levels(now)
        by_tenant: Dict[str, Dict[str, float]] = {}
        for (t, s), v in levels.items():
            by_tenant.setdefault(t, {})[s] = v
        per_server: Dict[str, List[Tuple[float, float, float, float]]] = {}   # (ratio, excess, level, ref)
        for t, lv in by_tenant.items():
            if len(lv) < 2:
                continue                      # a tenant seen on one server can't compare itself
            ref = median(lv.values())
            if ref <= 0:
                continue
            for s, v in lv.items():
                per_server.setdefault(s, []).append((v / ref, v - ref, v, ref))
        canary = self._canary_levels(now)
        flips = []
        for s, h in self.servers.items():
            rows = per_server.get(s, [])
            if len(rows) >= self.p.min_tenants:
                h.source = "traffic"
                h.tenants_used = len(rows)
                h.peer_ratio = median(r for r, _, _, _ in rows)
                h.excess_ms = median(e for _, e, _, _ in rows)
                h.level_ms = median(v for _, _, v, _ in rows)
                h.peer_ms = median(rf for _, _, _, rf in rows)
            elif s in canary and len([k for k in canary if k != s and self._peer(k)]) >= 1:
                others = [v for k, v in canary.items() if k != s and self._peer(k)]
                h.source, h.tenants_used = "canary", 0
                h.level_ms, h.peer_ms = canary[s], median(others)
                h.peer_ratio = h.level_ms / h.peer_ms if h.peer_ms > 0 else 1.0
                h.excess_ms = h.level_ms - h.peer_ms
            else:
                h.source, h.tenants_used, h.level_ms, h.peer_ms = "none", 0, None, None
                h.peer_ratio, h.excess_ms = 1.0, 0.0
            floor = self.p.canary_abs_ms if h.source == "canary" else self.p.slow_abs_ms
            h.slow_now = (h.source != "none" and h.peer_ratio >= self.p.slow_peer_ratio and h.excess_ms >= floor)
            if h.source == "canary" and not h.slow:
                h.slow_now = False     # the canary can keep a slow server open, never open one: real traffic must
                #                        be slow first (an idle host's probe jitter is nobody's problem)
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

    def _peer(self, s: str) -> bool:
        return self.fleet is None or s in self.fleet

    def slow_servers(self) -> List[str]:
        """Slow fleet servers. (A pool server's slowness is read through `servers[...]`, e.g. by the prover.)"""
        return sorted(s for s, h in self.servers.items() if h.slow and self._peer(s))

    def ratios(self) -> Dict[str, float]:
        return {s: h.peer_ratio for s, h in self.servers.items()}
