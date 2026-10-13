"""Inside-a-project detection (pure, no I/O). docs/v2/03_ALGORITHMS.md §7.

attribute():  per replica, the cgroup CPU is split into tenants' metered CPU (handler + bg), the black box by
              documents handed over, and framework overhead by request share up to a ceiling per request.
              What is left is *unattributed*: work no meter explains. A split-test culprit is charged with it.
split_step(): when a project is hurt and its CPU stays unexplained, find the tenant behind it by group testing:
              pin half of the suspects (ordered by a per-round hash) to each of two replicas, settle, sample,
              keep the half on the replica with more unexplained CPU, repeat (ceil(log2 k) rounds).
contention(): every project slow while none uses more than its allocation = something outside the projects
              (host, disk, noisy machine): reported, nobody blamed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from .settings import DetectCfg


@dataclass
class ReplicaUsage:
    """One replica over the detection window. All rates are CPU ms per second (or events per second)."""
    container_ms: Optional[float]                              # cgroup rate; None = unknown
    metered: Dict[str, float] = field(default_factory=dict)    # handler + bg per tenant
    requests: Dict[str, float] = field(default_factory=dict)
    docs: Dict[str, float] = field(default_factory=dict)
    blackbox_ms: float = 0.0


@dataclass
class Attribution:
    tenant_ms: Dict[str, float]
    unattributed_ms: float
    container_ms: float


def _split(amount: float, weights: Mapping[str, float]) -> Dict[str, float]:
    total = sum(weights.values())
    return {t: amount * w / total for t, w in weights.items()} if total > 0 else {}


def attribute(u: ReplicaUsage, cfg: DetectCfg, blamed: Optional[str] = None) -> Attribution:
    """Every CPU ms of a replica, assigned to a tenant where a meter explains it; the rest is unattributed."""
    metered = sum(u.metered.values())
    container = u.container_ms if u.container_ms is not None else metered + u.blackbox_ms
    out = dict(u.metered)
    explained = metered
    for t, ms in _split(u.blackbox_ms, u.docs).items():
        out[t] = out.get(t, 0.0) + ms
        explained += ms
    budget = cfg.overhead_ms_per_request * sum(u.requests.values())
    for t, ms in _split(min(max(0.0, container - explained), budget), u.requests).items():
        out[t] = out.get(t, 0.0) + ms
        explained += ms
    rest = max(0.0, container - explained)
    if blamed is not None and rest > 0:
        out[blamed] = out.get(blamed, 0.0) + rest
        rest = 0.0
    return Attribution(out, rest, container)


def split_hash(tenant: str, rnd: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"{tenant}|split|{rnd}".encode(), digest_size=8).digest(), "big")


def halves(suspects: List[str], rnd: int) -> Tuple[List[str], List[str]]:
    """Suspects ordered by this round's hash, cut in two (always balanced, different every round)."""
    order = sorted(suspects, key=lambda t: split_hash(t, rnd))
    cut = len(order) // 2
    return order[:cut], order[cut:]


@dataclass
class Split:
    project: str
    suspects: List[str]
    replicas: Tuple[str, str]
    rnd: int = 0
    started_at: float = 0.0            # start of this round's settle period
    samples: Dict[str, List[float]] = field(default_factory=dict)   # replica -> unattributed ms/s samples
    log: List[dict] = field(default_factory=list)                  # one entry per finished round (evidence)

    def pins(self, others: List[str]) -> Dict[str, str]:
        """Tenant -> replica for this round: suspects split in halves, everyone else on the first replica."""
        a, b = halves(self.suspects, self.rnd)
        out = {t: self.replicas[0] for t in others}
        out.update({t: self.replicas[0] for t in a})
        out.update({t: self.replicas[1] for t in b})
        return out


@dataclass
class SplitOutcome:
    split: Optional[Split]             # None = finished
    culprit: Optional[str] = None
    round_done: Optional[dict] = None  # evidence of a round that just finished


def split_step(s: Split, unattributed: Mapping[str, float], now: float, cfg: DetectCfg) -> SplitOutcome:
    """Advance one tick: settle, sample, then keep the half on the replica with more unexplained CPU."""
    if now - s.started_at < cfg.split_settle_s:
        return SplitOutcome(s)
    for r in s.replicas:
        s.samples.setdefault(r, []).append(unattributed.get(r, 0.0))
    if now - s.started_at < cfg.split_settle_s + cfg.split_sample_s:
        return SplitOutcome(s)
    mean = {r: sum(v) / len(v) for r, v in s.samples.items() if v}
    a, b = halves(s.suspects, s.rnd)
    heavy = max(s.replicas, key=lambda r: mean.get(r, 0.0))
    keep = a if heavy == s.replicas[0] else b
    done = {"round": s.rnd + 1, "unattributed_ms_per_s": {r: round(v, 2) for r, v in mean.items()},
            "halves": [a, b], "kept": keep}
    s.log.append(done)
    if len(keep) <= 1:
        return SplitOutcome(None, keep[0] if keep else None, done)
    s.suspects, s.rnd, s.started_at, s.samples = keep, s.rnd + 1, now, {}
    return SplitOutcome(s, None, done)


def needs_split(project_unattributed: float, project_container: float, hurt: bool, active: int,
                cfg: DetectCfg) -> bool:
    """Unexplained CPU above the ratio (and the floor) while a tenant of the project is hurt, with someone to split."""
    return (hurt and active >= 2 and project_container > 0 and project_unattributed > cfg.unattributed_min_ms
            and project_unattributed > cfg.unattributed_ratio * project_container)


def contention(slow: Mapping[str, bool], over_alloc: Mapping[str, bool]) -> bool:
    """At least two projects with data, all slow, none above its allocation."""
    return len(slow) >= 2 and all(slow.values()) and not any(over_alloc.values())
