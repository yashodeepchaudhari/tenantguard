"""Barcode decoder: name the culprit(s) from *which* servers are slow (pure, no I/O).

Every tenant runs on 3 servers (its barcode). In AG(2,3) any two barcodes share at most one server,
so the set of slow servers points at the tenant(s) whose barcodes it covers, even when the culprit's
work is invisible to every meter (black-box CPU).

Per server: p_s = logistic(k * (peer_ratio_s - slow_peer_ratio)), the probability the server is slow.

Per candidate tenant t (servers S_t):
    evidence(t) = w_min * min_{s in S_t} p_s          (all of its servers slow, on the ORIGINAL signal)
                + w_mean * mean_{s in S_t} p'_s       (how much of the still-unexplained slowness it covers)
                + w_stuck * stuck(t)                  (every server it can be routed to is slow)
                + w_ledger * ledger(t) * mean p_s     (bg CPU growth, evictions caused, black-box growth
                                                       on all its servers, paid usage over fair share;
                                                       weighted by how slow its servers are)
Two extra options compete with the tenants:
    host_fault = w_host * max_s p'_s * (1 - best cover of s)   a slow server no full barcode explains
    none       = w_none * (1 - max_s p'_s)                     nothing is (still) slow

confidence = softmax(evidence / temperature) over tenants + host_fault + none.

Explain-away: accept the top option if it is a tenant and the tenants together hold >= min_confidence
(blaming some tenant beats host_fault / none), then remove
the slowness it explains (p'_s = 0 on its servers) and decode again, up to `max_culprits` times. This
names two culprits exactly: after removing the first one, only the second covers what is left.
Slow servers still unexplained at the end are *host suspects*.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence

HOST = "host_fault"
NONE = "none"


@dataclass
class DecoderParams:
    slow_peer_ratio: float = 1.6
    k: float = 8.0                 # logistic steepness around slow_peer_ratio
    w_min: float = 4.0
    w_mean: float = 2.0
    w_stuck: float = 1.0
    w_ledger: float = 2.0
    w_host: float = 4.0
    w_none: float = 3.0
    temperature: float = 1.0
    min_confidence: float = 0.5
    max_culprits: int = 2
    host_p: float = 0.5            # residual p_s above this = host suspect

    @classmethod
    def from_yaml(cls, health: dict, decoder: Optional[dict] = None) -> "DecoderParams":
        d = cls()
        dec = decoder or {}
        kw = {k: type(getattr(d, k))(v) for k, v in dec.items() if hasattr(d, k)}
        kw.setdefault("slow_peer_ratio", float(health.get("slow_peer_ratio", d.slow_peer_ratio)))
        return cls(**kw)


@dataclass
class Named:
    tenant: str
    confidence: float
    evidence: float
    covers: List[str]              # its servers that were slow


@dataclass
class Decoding:
    culprits: List[Named] = field(default_factory=list)
    host_suspects: List[str] = field(default_factory=list)
    p: Dict[str, float] = field(default_factory=dict)                 # server -> p_s
    rounds: List[Dict[str, float]] = field(default_factory=list)      # confidence per option per round

    @property
    def top_confidence(self) -> Dict[str, float]:
        return self.rounds[0] if self.rounds else {}


def logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def p_slow(ratios: Mapping[str, float], p: DecoderParams) -> Dict[str, float]:
    return {s: logistic(p.k * (r - p.slow_peer_ratio)) for s, r in ratios.items()}


def softmax(scores: Mapping[str, float], temperature: float) -> Dict[str, float]:
    m = max(scores.values())
    ex = {k: math.exp((v - m) / temperature) for k, v in scores.items()}
    tot = sum(ex.values())
    return {k: v / tot for k, v in ex.items()}


def _round(barcodes: Mapping[str, Sequence[str]], p0: Mapping[str, float], pr: Mapping[str, float],
           stuck: Mapping[str, bool], ledger: Mapping[str, float], exclude: Sequence[str],
           prm: DecoderParams) -> Dict[str, float]:
    scores: Dict[str, float] = {}
    cover: Dict[str, float] = {s: 0.0 for s in p0}       # best full-barcode cover of each server
    for t, servers in barcodes.items():
        if t in exclude:
            continue
        mn = min(p0.get(s, 0.0) for s in servers)
        mean_r = sum(pr.get(s, 0.0) for s in servers) / len(servers)
        mean_0 = sum(p0.get(s, 0.0) for s in servers) / len(servers)
        # ledger hints only count where the tenant's servers are actually slow
        scores[t] = (prm.w_min * mn + prm.w_mean * mean_r + prm.w_stuck * float(stuck.get(t, False))
                     + prm.w_ledger * max(0.0, min(1.0, ledger.get(t, 0.0))) * mean_0)
        for s in servers:
            cover[s] = max(cover.get(s, 0.0), mn)
    unexplained = max((pr.get(s, 0.0) * (1.0 - cover.get(s, 0.0)) for s in pr), default=0.0)
    scores[HOST] = prm.w_host * unexplained
    scores[NONE] = prm.w_none * (1.0 - max(pr.values(), default=0.0))
    return scores


def decode(barcodes: Mapping[str, Sequence[str]], ratios: Mapping[str, float], *,
           stuck: Optional[Mapping[str, bool]] = None, ledger: Optional[Mapping[str, float]] = None,
           params: Optional[DecoderParams] = None) -> Decoding:
    """barcodes: tenant -> servers; ratios: server -> peer ratio (1.0 = like its peers)."""
    prm = params or DecoderParams()
    stuck = stuck or {}
    ledger = ledger or {}
    p0 = p_slow(ratios, prm)
    pr = dict(p0)
    out = Decoding(p={s: round(v, 4) for s, v in p0.items()})
    named: List[str] = []
    for _ in range(prm.max_culprits):
        scores = _round(barcodes, p0, pr, stuck, ledger, named, prm)
        conf = softmax(scores, prm.temperature)
        out.rounds.append({k: round(v, 4) for k, v in conf.items()})
        top = max(conf, key=conf.get)
        blame = sum(v for k, v in conf.items() if k not in (HOST, NONE))
        if top in (HOST, NONE) or blame < prm.min_confidence:
            break
        covers = [s for s in barcodes[top] if p0.get(s, 0.0) >= prm.host_p]
        out.culprits.append(Named(top, round(conf[top], 4), round(scores[top], 4), covers))
        named.append(top)
        for s in barcodes[top]:
            pr[s] = 0.0
    out.host_suspects = sorted(s for s, v in pr.items() if v >= prm.host_p)
    if len(out.culprits) > 1:
        # report each culprit's confidence with the *other* culprits explained away (ties in round 1
        # would otherwise split the confidence between two equally guilty tenants)
        for c in out.culprits:
            others = [o.tenant for o in out.culprits if o.tenant != c.tenant]
            p_loo = dict(p0)
            for o in others:
                for srv in barcodes[o]:
                    p_loo[srv] = 0.0
            scores = _round(barcodes, p0, p_loo, stuck, ledger, others, prm)
            c.confidence = round(softmax(scores, prm.temperature)[c.tenant], 4)
            c.evidence = round(scores[c.tenant], 4)
    return out
