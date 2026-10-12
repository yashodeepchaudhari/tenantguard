"""Verdicts, proof evaluation, action budget and repeat-offender memory (pure, no I/O).

Five verdicts:
- **mitigated_no_blame**: the decoder's confidence is below `confidence_to_prove`, or the relocation test did
  not confirm (and the tenant is not a repeat offender). Rescue already protects the victims; nobody is
  penalised.
- **over_plan**: confirmed, and admission is already refusing a large share of its requests
  (>= `over_plan_rejected_share`): it simply wants more than its plan. -> tighten its limits via the ladder.
- **costly_pattern**: confirmed, and its *paid* (metered) CPU explains little of its footprint
  (paid share < `paid_share_costly`), or black-box / eviction evidence points at it. -> its most expensive
  request class goes to the quarantine worker (heavy lane); the rest of its traffic goes home.
- **honest_growth**: confirmed, and paid work explains the load. -> it stays on the spare as a burst lane
  until the relocation TTL ends. No penalty.
- **host_fault**: a host suspect that stays slow while drained and idle (canary >= `host_idle_ratio` x peers).
  -> the server stays drained (TTL) until it is healthy again.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import mean
from typing import Deque, Dict, List, Optional, Sequence, Tuple

COSTLY, OVER, HONEST, HOST, NO_BLAME = ("costly_pattern", "over_plan", "honest_growth", "host_fault",
                                       "mitigated_no_blame")


@dataclass
class VerdictParams:
    confidence_to_prove: float = 0.6
    paid_share_costly: float = 0.35
    recovery_R: float = 0.5
    host_idle_ratio: float = 1.5
    over_plan_rejected_share: float = 0.2
    harm_follow_share: float = 0.3      # spare CPU must rise by >= this share of the suspect's footprint ...
    harm_follow_ratio: float = 1.6      # ... or the spare's latency reach this peer ratio
    evidence_hint: float = 0.3          # a ledger hint above this counts as black-box / eviction evidence
    repeat_count: int = 3
    repeat_window_s: float = 600.0

    @classmethod
    def from_yaml(cls, brain: dict) -> "VerdictParams":
        d = cls()
        v = brain.get("verdicts", {}) or {}
        health = brain.get("health", {}) or {}
        return cls(
            confidence_to_prove=float(brain.get("confidence_to_prove", d.confidence_to_prove)),
            paid_share_costly=float(brain.get("paid_share_costly", d.paid_share_costly)),
            recovery_R=float(brain.get("recovery_R", d.recovery_R)),
            host_idle_ratio=float(brain.get("host_idle_ratio", d.host_idle_ratio)),
            over_plan_rejected_share=float(v.get("over_plan_rejected_share", d.over_plan_rejected_share)),
            harm_follow_share=float(v.get("harm_follow_share", d.harm_follow_share)),
            harm_follow_ratio=float(v.get("harm_follow_ratio", health.get("slow_peer_ratio", d.harm_follow_ratio))),
            evidence_hint=float(v.get("evidence_hint", d.evidence_hint)),
            repeat_count=int(v.get("repeat_count", d.repeat_count)),
            repeat_window_s=float(v.get("repeat_window_s", d.repeat_window_s)),
        )


# ------------------------------------------------------------------ proof evaluation
@dataclass
class ProofMeasure:
    pre_slowdown: float                  # mean over the suspect's servers of max(0, peer_ratio - 1), before
    post_slowdown: List[float]           # same, sampled during the measurement window
    spare_cpu_pre: float                 # spare CPU ms/s before
    spare_cpu: List[float]               # sampled during the window
    spare_ratio: List[float]             # spare peer ratio during the window
    footprint_ms: float                  # the suspect's CPU ms/s on its servers (paid + unexplained)


def evaluate_proof(m: ProofMeasure, p: VerdictParams) -> Tuple[float, bool, bool, str]:
    """(R, harm_followed, confirmed, explanation). R = 1 - post/pre slowdown on the original servers."""
    if m.pre_slowdown <= 0 or not m.post_slowdown:
        return 0.0, False, False, "no slowdown to remove (or no samples), nothing to prove"
    post = mean(m.post_slowdown)
    R = max(-1.0, min(1.0, 1.0 - post / m.pre_slowdown))
    cpu_rise = (mean(m.spare_cpu) - m.spare_cpu_pre) if m.spare_cpu else 0.0
    ratio = max(m.spare_ratio) if m.spare_ratio else 1.0
    by_cpu = m.footprint_ms > 0 and cpu_rise >= p.harm_follow_share * m.footprint_ms
    by_lat = ratio >= p.harm_follow_ratio
    harm = by_cpu or by_lat
    confirmed = R >= p.recovery_R and harm
    why = (f"original servers recovered R = {R:.2f} (slowdown {m.pre_slowdown:.2f} -> {post:.2f}; need "
           f">= {p.recovery_R:g}); spare CPU {'+' if cpu_rise >= 0 else ''}{cpu_rise:.0f} ms/s vs footprint "
           f"{m.footprint_ms:.0f} ms/s, spare latency {ratio:.1f}x peers: harm "
           f"{'followed' if harm else 'did NOT follow'}")
    if not harm and R >= p.recovery_R:
        why += " (the suspect may have paused during the test)"
    return round(R, 3), harm, confirmed, why


# ------------------------------------------------------------------ classification
@dataclass
class Evidence:
    tenant: str
    confidence: float
    confirmed: bool
    R: float = 0.0
    harm_followed: bool = False
    paid_share: float = 1.0              # paid / (paid + unexplained) CPU on its servers
    blackbox_hint: float = 0.0           # 0..1 black-box CPU growth on all its servers
    evict_hint: float = 0.0              # 0..1 share of cache evictions it caused
    rejected_share: float = 0.0          # admission refusals / (admitted + refusals), recent window
    repeat: bool = False
    last_verdict: Optional[str] = None


def classify(e: Evidence, p: VerdictParams) -> Tuple[str, List[str]]:
    """Pick the verdict and the reasons, in plain English."""
    why: List[str] = []
    if e.confidence < p.confidence_to_prove and not e.repeat:
        return NO_BLAME, [f"decoder confidence {e.confidence:.0%} is below the {p.confidence_to_prove:.0%} bar "
                          f"to prove; the rescue protects the victims and nobody is penalised"]
    if not e.confirmed:
        if not e.repeat:
            return NO_BLAME, [f"the relocation test did not confirm it (R = {e.R:.2f}, harm "
                              f"{'followed' if e.harm_followed else 'did not follow'}); nobody is penalised"]
        why.append(f"not confirmed this time, but a repeat offender (named {p.repeat_count}+ times in "
                   f"{p.repeat_window_s / 60:.0f} min): judged on its pre-test evidence")
    else:
        why.append(f"proved by relocation (R = {e.R:.2f}, the harm followed it to the spare)")
    if e.rejected_share >= p.over_plan_rejected_share:
        why.append(f"admission already refuses {e.rejected_share:.0%} of its requests (>= "
                   f"{p.over_plan_rejected_share:.0%}): it wants more than its plan")
        return OVER, why
    costly = []
    if e.paid_share < p.paid_share_costly:
        costly.append(f"its metered (paid) CPU explains only {e.paid_share:.0%} of its footprint "
                      f"(< {p.paid_share_costly:.0%})")
    if e.blackbox_hint >= p.evidence_hint:
        costly.append("unattributed black-box CPU grew on all of its servers")
    if e.evict_hint >= p.evidence_hint:
        costly.append(f"it caused {e.evict_hint:.0%} of cache evictions")
    if costly:
        return COSTLY, why + costly
    why.append(f"its paid work explains the load (paid share {e.paid_share:.0%}) and it is within its plan")
    return HONEST, why


def host_verdict(idle_ratio: float, p: VerdictParams) -> Tuple[str, str]:
    if idle_ratio >= p.host_idle_ratio:
        return HOST, (f"still {idle_ratio:.1f}x slower than its peers while drained and idle "
                      f"(>= {p.host_idle_ratio:g}x): the host itself is at fault")
    return NO_BLAME, (f"only {idle_ratio:.1f}x its peers once idle (< {p.host_idle_ratio:g}x): it was load, not "
                      f"the host; undrained")


# ------------------------------------------------------------------ budget + memory
@dataclass
class ActionBudget:
    relocations_per_min: int = 6
    quarantine_every_s: float = 30.0
    max_drained: int = 1
    drain_peer_busy: float = 0.75       # drain only while the other servers are below this utilisation
    relocations: Deque[float] = field(default_factory=deque)
    last_quarantine: Optional[float] = None
    drained: Dict[str, float] = field(default_factory=dict)     # server -> since

    @classmethod
    def from_yaml(cls, brain: dict) -> "ActionBudget":
        b = brain.get("action_budget", {}) or {}
        d = cls()
        return cls(relocations_per_min=int(b.get("relocations_per_min", d.relocations_per_min)),
                   quarantine_every_s=float(b.get("quarantine_every_s", d.quarantine_every_s)),
                   max_drained=int(b.get("max_drained", d.max_drained)),
                   drain_peer_busy=float(b.get("drain_peer_busy", d.drain_peer_busy)))

    def can_relocate(self, now: float) -> bool:
        while self.relocations and self.relocations[0] <= now - 60.0:
            self.relocations.popleft()
        return len(self.relocations) < self.relocations_per_min

    def note_relocation(self, now: float) -> None:
        self.relocations.append(now)

    def can_quarantine(self, now: float) -> bool:
        return self.last_quarantine is None or now - self.last_quarantine >= self.quarantine_every_s

    def note_quarantine(self, now: float) -> None:
        self.last_quarantine = now

    def can_drain(self, server: str, peer_util: Sequence[float]) -> Tuple[bool, str]:
        if server in self.drained:
            return False, f"{server} is already drained"
        if len(self.drained) >= self.max_drained:
            return False, f"at most {self.max_drained} drained server(s) ({', '.join(self.drained)} is drained)"
        busy = max(peer_util) if peer_util else 0.0
        if busy >= self.drain_peer_busy:
            return False, f"peers are too busy to absorb it ({busy:.0%} >= {self.drain_peer_busy:.0%})"
        return True, ""


@dataclass
class OffenderMemory:
    count: int = 3
    window_s: float = 600.0
    named: Dict[str, Deque[float]] = field(default_factory=dict)
    last_verdict: Dict[str, str] = field(default_factory=dict)

    def note_named(self, tenant: str, now: float) -> None:
        q = self.named.setdefault(tenant, deque())
        q.append(now)
        while q and q[0] <= now - self.window_s:
            q.popleft()

    def is_repeat(self, tenant: str, now: float) -> bool:
        q = self.named.get(tenant, ())
        return sum(1 for t in q if t > now - self.window_s) >= self.count

    def note_verdict(self, tenant: str, verdict: str) -> None:
        if verdict in (COSTLY, OVER, HONEST):
            self.last_verdict[tenant] = verdict
