"""Noisy-neighbor score (pure, no I/O).

score(t) = max(cpu_share / fair_share, queue_share / fair_share) * (1 + victim_impact(t))

- cpu_share(t)     = t's CPU ms/s on its servers / CPU capacity of those servers
- queue_share(t)   = t's (queue depth + inflight) / dispatch slots of its servers
- fair_share(t)    = per server weight(t) / sum of active weights on that server, capacity-weighted
                     over t's servers (= weight / sum of active weights, scoped to where t runs)
- victim_impact(t) = mean over t's *neighbours* (other tenants sharing at least one server with t)
                     of max(0, p95 / baseline_p95 - 1), clipped to [0, 3]
- baseline_p95     = per-tenant EWMA, updated only in calm periods (see BaselineTracker)

A tenant is *flagged* only if it over-uses (ratio > 1) AND its neighbours are hurt
(victim_impact >= min_victim_impact). A heavy tenant whose neighbours are fine is never flagged.

Neighbours instead of "all other tenants": with shuffle-sharded barcodes (AG(2,3)) each tenant
shares servers with 6 of the 11 others and is disjoint from 5. Averaging over neighbours keeps an
innocent heavy tenant on disjoint servers from being blamed for someone else's victims.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

IMPACT_CLIP = 3.0


def shares(values: Mapping[str, float]) -> Dict[str, float]:
    """Each value's fraction of the total (all 0 when the total is 0)."""
    total = sum(max(0.0, v) for v in values.values())
    if total <= 0:
        return {k: 0.0 for k in values}
    return {k: max(0.0, v) / total for k, v in values.items()}


def fair_shares(weights: Mapping[str, float], active: Iterable[str]) -> Dict[str, float]:
    """weight / sum of active weights, for every tenant in `weights` (inactive ones still get a
    fair share computed as if they had just become active, so a first burst is not divided by 0)."""
    active = set(active)
    base = sum(w for t, w in weights.items() if t in active)
    out = {}
    for t, w in weights.items():
        denom = base if t in active else base + w
        out[t] = w / denom if denom > 0 else 0.0
    return out


def slowdown(p95: Optional[float], baseline: Optional[float], floor_ms: float = 0.0) -> Optional[float]:
    """max(0, p95 / baseline - 1). The baseline is floored at `floor_ms` so sub-millisecond
    jitter on a ~1 ms endpoint never reads as a 3x slowdown. None when either side is unknown."""
    if p95 is None or baseline is None or baseline <= 0:
        return None
    return max(0.0, p95 / max(baseline, floor_ms) - 1.0)


def victim_impact(culprit: str, slowdowns: Mapping[str, Optional[float]],
                  neighbours: Optional[Iterable[str]] = None) -> Tuple[float, List[Tuple[str, float]]]:
    """Mean slowdown over the culprit's neighbours with data, clipped to [0, 3].
    Returns (impact, [(victim, slowdown) sorted worst first])."""
    pool = set(neighbours) if neighbours is not None else set(slowdowns)
    pool.discard(culprit)
    vals = [(t, s) for t, s in slowdowns.items() if t in pool and s is not None]
    if not vals:
        return 0.0, []
    mean = sum(s for _, s in vals) / len(vals)
    return min(IMPACT_CLIP, max(0.0, mean)), sorted(vals, key=lambda x: -x[1])


@dataclass
class ScoreDetail:
    tenant: str
    cpu_share: float
    queue_share: float
    fair_share: float
    usage_ratio: float            # max(cpu_share, queue_share) / fair_share
    victim_impact: float
    score: float
    norm: float                   # min(1, score / quarantine threshold)
    flagged: bool                 # over-uses AND neighbours hurt
    victims: List[Tuple[str, float]] = field(default_factory=list)   # (tenant, slowdown) worst first

    @property
    def dominant(self) -> str:
        return "CPU" if self.cpu_share >= self.queue_share else "queue"


def noisy_score(cpu_share: float, queue_share: float, fair_share: float, impact: float) -> Tuple[float, float]:
    """(score, usage_ratio)."""
    if fair_share <= 0:
        return 0.0, 0.0
    ratio = max(cpu_share, queue_share) / fair_share
    return ratio * (1.0 + impact), ratio


def compute_scores(*, weights: Mapping[str, float], servers_of: Mapping[str, Sequence[str]],
                   active: Iterable[str],
                   cpu_by_server: Mapping[str, Mapping[str, float]], cpu_cap: Mapping[str, float],
                   occupancy: Mapping[str, float], slots: Mapping[str, float],
                   slowdowns: Mapping[str, Optional[float]],
                   neighbours: Mapping[str, Sequence[str]],
                   min_victim_impact: float, quarantine_threshold: float) -> Dict[str, ScoreDetail]:
    """Score every tenant in `weights`.

    Shares are measured against the capacity of the tenant's *own* servers, and the fair share is
    its weighted entitlement of that capacity (per server: weight / sum of active weights on that
    server). So a free-tier tenant doing its normal 5% of the work on a 30%-busy fleet is at a ratio
    well below 1, and only a tenant crowding its co-located neighbours goes above 1.

    - cpu_by_server: server -> tenant -> CPU ms/s (handler + bg); cpu_cap: server -> CPU ms/s limit
    - occupancy: tenant -> mean (queue depth + inflight); slots: server -> dispatch window
    """
    active = set(active)
    out = {}
    for t, w in weights.items():
        servers = list(servers_of.get(t, []))
        cap = ent = used = slot_cap = slot_ent = 0.0
        for s in servers:
            on_s = {o: weights[o] for o in weights if s in servers_of.get(o, ()) and (o in active or o == t)}
            f = fair_shares(on_s, active | {t}).get(t, 0.0)
            c = cpu_cap.get(s, 0.0)
            cap += c
            ent += f * c
            used += cpu_by_server.get(s, {}).get(t, 0.0)
            slot_cap += slots.get(s, 0.0)
            slot_ent += f * slots.get(s, 0.0)
        cpu_share = used / cap if cap > 0 else 0.0
        queue_share = occupancy.get(t, 0.0) / slot_cap if slot_cap > 0 else 0.0
        fair = ent / cap if cap > 0 else (slot_ent / slot_cap if slot_cap > 0 else 0.0)
        impact, victims = victim_impact(t, slowdowns, neighbours.get(t))
        score, ratio = noisy_score(cpu_share, queue_share, fair, impact)
        out[t] = ScoreDetail(
            tenant=t, cpu_share=cpu_share, queue_share=queue_share, fair_share=fair,
            usage_ratio=ratio, victim_impact=impact, score=score,
            norm=min(1.0, score / quarantine_threshold) if quarantine_threshold > 0 else 0.0,
            flagged=ratio > 1.0 and impact >= min_victim_impact,
            victims=victims,
        )
    return out


class BaselineTracker:
    """Calm-period EWMA of each tenant's end-to-end p95.

    The first samples use a running mean (alpha = max(alpha, 1/n)) so the baseline settles fast
    after start-up. The caller decides what "calm" means and only calls update() then.
    """

    def __init__(self, alpha: float = 0.05):
        self.alpha = alpha
        self.value: Dict[str, float] = {}
        self.n: Dict[str, int] = {}

    def update(self, tenant: str, p95: float) -> None:
        if p95 <= 0:
            return
        n = self.n.get(tenant, 0) + 1
        self.n[tenant] = n
        a = max(self.alpha, 1.0 / n)
        prev = self.value.get(tenant)
        self.value[tenant] = p95 if prev is None else prev + a * (p95 - prev)

    def get(self, tenant: str) -> Optional[float]:
        return self.value.get(tenant)

    def ready(self, tenant: str, min_samples: int = 4) -> bool:
        return self.n.get(tenant, 0) >= min_samples
