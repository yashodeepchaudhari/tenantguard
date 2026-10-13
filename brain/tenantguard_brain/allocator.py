"""Who gets how much CPU (pure, no I/O). docs/v2/03_ALGORITHMS.md §2-3.

    demand_p = clip(alpha * raw_p + (1 - alpha) * demand_prev_p, 0, cap_p)
    base_p   = min(demand_p, floor_p)                       floors first, always honoured
    surplus  = usable - sum(base)                           includes idle projects' unused floors
    loan     = water_fill(surplus, want = min(demand, cap) - base, weight = tier weight)
    alloc_p  = base_p + loan_p

Modes: tenantguard = the above; industry = static floor (no lending); off = cap (no isolation).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping

from tenantguard_common.contracts import ProjectSpec

EPS = 1e-9


@dataclass
class Alloc:
    base: float
    loan: float

    @property
    def total(self) -> float:
        return self.base + self.loan


def estimate_demand(prev: float, raw: float, alpha: float, cap: float) -> float:
    """EWMA of raw demand (cores: used + queued + refused work), clipped to [0, cap]."""
    return min(cap, max(0.0, alpha * raw + (1.0 - alpha) * prev))


def water_fill(surplus: float, wants: Mapping[str, float], weights: Mapping[str, float]) -> Dict[str, float]:
    """Split surplus by weight; a project never gets more than it wants, its excess is re-split."""
    out = {p: 0.0 for p in wants}
    active = {p for p, w in wants.items() if w > EPS}
    while surplus > EPS and active:
        total_w = sum(weights[p] for p in active)
        share = {p: surplus * weights[p] / total_w for p in active}
        capped = {p for p in active if wants[p] - out[p] <= share[p] + EPS}
        if not capped:
            for p in active:
                out[p] += share[p]
            return out
        for p in capped:
            surplus -= wants[p] - out[p]
            out[p] = wants[p]
        active -= capped
    return out


def allocate(usable_cpu: float, projects: Mapping[str, ProjectSpec], weights: Mapping[str, float],
             demand: Mapping[str, float], mode: str = "tenantguard") -> Dict[str, Alloc]:
    """Floors first, then the surplus by tier weight (tenantguard); static floors (industry); caps (off)."""
    if mode == "industry":
        return {p: Alloc(s.floor_cpu, 0.0) for p, s in projects.items()}
    if mode == "off":
        return {p: Alloc(s.cap_cpu, 0.0) for p, s in projects.items()}
    base = {p: min(demand.get(p, 0.0), s.floor_cpu) for p, s in projects.items()}
    wants = {p: max(0.0, min(demand.get(p, 0.0), s.cap_cpu) - base[p]) for p, s in projects.items()}
    loan = water_fill(max(0.0, usable_cpu - sum(base.values())), wants, weights)
    return {p: Alloc(base[p], loan[p]) for p in projects}
