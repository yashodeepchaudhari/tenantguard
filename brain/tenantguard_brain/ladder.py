"""Graduated, self-recovering throttle ladder (pure, no I/O).

    normal -> warn -> throttle (factor x0.5 per step, down to throttle_floor)
           -> deprioritize (weight_factor 0.25) -> quarantine (factor 0.1, weight_factor 0.1)

Escalation (multiplicative decrease) needs the tenant to be *flagged* (over-uses AND neighbours
hurt) with its score above the rung's threshold, continuously for that rung's hold time:

    normal -> warn          score >= warn        held hold_s.warn
    warn -> throttle        score >= throttle    held hold_s.throttle
    throttle step (x0.5)    score >= throttle    held hold_s.throttle   (until factor hits the floor)
    -> deprioritize         score >= throttle    held hold_s.deprioritize (factor already at floor)
    -> quarantine           score >= quarantine  held hold_s.quarantine (already deprioritized)

Every change restarts the hold clocks, so each further step needs a fresh hold.

Recovery (additive increase) needs the tenant to be *calm* (score < recover_below, or neighbours
clearly unhurt) continuously for hold_s.recover; then factor and weight_factor each rise by
recover_step once per recover_tick_s while calm lasts. Calm and flagged use different impact
thresholds (hurt_on / hurt_off), so between them the ladder holds still (hysteresis).
When both factors are back at 1.0 the tenant drops to warn (override cleared), and after
hold_s.warn more calm seconds to normal.

TODO(Wave 2): quarantine = relocate the tenant to the quarantine worker instead of a 0.1 limit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

LEVELS = ("normal", "warn", "throttle", "deprioritize", "quarantine")
RANK = {lvl: i for i, lvl in enumerate(LEVELS)}
EPS = 1e-6


@dataclass
class LadderParams:
    warn: float = 1.5
    throttle: float = 2.0
    quarantine: float = 4.0
    recover_below: float = 1.0
    hold: Dict[str, float] = field(default_factory=lambda: {
        "warn": 2, "throttle": 2, "deprioritize": 4, "quarantine": 6, "recover": 10})
    throttle_factor: float = 0.5
    recover_step: float = 0.1
    recover_tick_s: float = 1.0
    throttle_floor: float = 0.25
    deprioritize_weight: float = 0.25
    quarantine_factor: float = 0.1
    quarantine_weight: float = 0.1
    ttl_throttle: float = 60.0
    ttl_quarantine: float = 120.0

    @classmethod
    def from_yaml(cls, noisy: dict, ttl: dict | None = None) -> "LadderParams":
        ttl = ttl or {}
        d = cls()
        return cls(
            warn=float(noisy["warn"]), throttle=float(noisy["throttle"]),
            quarantine=float(noisy["quarantine"]), recover_below=float(noisy["recover_below"]),
            hold={**d.hold, **{k: float(v) for k, v in noisy.get("hold_s", {}).items()}},
            throttle_factor=float(noisy.get("throttle_factor", d.throttle_factor)),
            recover_step=float(noisy.get("recover_step", d.recover_step)),
            recover_tick_s=float(noisy.get("recover_tick_s", d.recover_tick_s)),
            throttle_floor=float(noisy.get("throttle_floor", d.throttle_floor)),
            deprioritize_weight=float(noisy.get("deprioritize_weight", d.deprioritize_weight)),
            quarantine_factor=float(noisy.get("quarantine_factor", d.quarantine_factor)),
            quarantine_weight=float(noisy.get("quarantine_weight", d.quarantine_weight)),
            ttl_throttle=float(ttl.get("throttle", d.ttl_throttle)),
            ttl_quarantine=float(ttl.get("quarantine", d.ttl_quarantine)),
        )

    def level_for(self, factor: float, weight_factor: float) -> str:
        """Level implied by the applied factors (used on the way down)."""
        if factor <= self.quarantine_factor + EPS and weight_factor <= self.quarantine_weight + EPS:
            return "quarantine"
        if weight_factor < 1 - EPS:
            return "deprioritize"
        if factor < 1 - EPS:
            return "throttle"
        return "warn"

    def ttl_for(self, level: str) -> float:
        return self.ttl_quarantine if level == "quarantine" else self.ttl_throttle


@dataclass
class Signal:
    score: float
    flagged: bool       # over-uses AND neighbours hurt (impact >= hurt_on)
    unhurt: bool        # neighbours clearly fine (impact < hurt_off)


@dataclass
class LadderState:
    level: str = "normal"
    factor: float = 1.0
    weight_factor: float = 1.0
    changed_at: float = 0.0
    peak: str = "normal"                  # worst level since the last normal
    warn_since: Optional[float] = None
    throttle_since: Optional[float] = None
    quarantine_since: Optional[float] = None
    calm_since: Optional[float] = None
    last_recover_at: Optional[float] = None

    @property
    def acting(self) -> bool:
        """True while an override (factor or weight_factor < 1) must be in force."""
        return self.factor < 1 - EPS or self.weight_factor < 1 - EPS


@dataclass
class Transition:
    kind: str           # escalate | step | recover | clear | resolve | reset
    prev_level: str
    level: str
    prev_factor: float
    factor: float
    prev_weight_factor: float
    weight_factor: float
    rule: str           # which rule fired, in plain English


def _since(ts: Optional[float], cond: bool, now: float) -> Optional[float]:
    if not cond:
        return None
    return now if ts is None else ts


def _held(ts: Optional[float], hold: float, now: float) -> bool:
    return ts is not None and now - ts >= hold - EPS


def _apply(s: LadderState, now: float, kind: str, level: str, factor: float, wf: float,
           rule: str) -> Transition:
    t = Transition(kind, s.level, level, s.factor, factor, s.weight_factor, wf, rule)
    s.level, s.factor, s.weight_factor, s.changed_at = level, factor, wf, now
    if RANK[level] > RANK[s.peak]:
        s.peak = level
    if kind in ("escalate", "step"):
        # each further step needs a fresh, full hold
        s.warn_since = now if s.warn_since is not None else None
        s.throttle_since = now if s.throttle_since is not None else None
        s.quarantine_since = now if s.quarantine_since is not None else None
        s.calm_since = None
        s.last_recover_at = None
    if level == "normal":
        s.peak = "normal"
    return t


def step(s: LadderState, sig: Signal, now: float, p: LadderParams) -> Optional[Transition]:
    """Advance one tenant's ladder by one tick. Mutates `s`; returns the change, if any."""
    s.warn_since = _since(s.warn_since, sig.flagged and sig.score >= p.warn, now)
    s.throttle_since = _since(s.throttle_since, sig.flagged and sig.score >= p.throttle, now)
    s.quarantine_since = _since(s.quarantine_since, sig.flagged and sig.score >= p.quarantine, now)
    calm = sig.score < p.recover_below or sig.unhurt
    s.calm_since = _since(s.calm_since, calm, now)
    h = p.hold

    if s.level == "normal":
        if _held(s.warn_since, h["warn"], now):
            return _apply(s, now, "escalate", "warn", 1.0, 1.0,
                          f"score >= warn {p.warn:g} with neighbours hurt for {h['warn']:g} s")
        return None

    if s.level == "warn" and not s.acting:
        if _held(s.throttle_since, h["throttle"], now):
            f = p.throttle_factor
            return _apply(s, now, "escalate", "throttle", f, 1.0,
                          f"score >= throttle {p.throttle:g} with neighbours hurt for {h['throttle']:g} s")
        # coming down from an acted level only needs hold_s.warn; a plain warning needs the full recover hold
        down = h["recover"] if s.peak == "warn" else h["warn"]
        if _held(s.calm_since, down, now) and now - s.changed_at >= down - EPS:
            return _apply(s, now, "resolve", "normal", 1.0, 1.0, f"calm for {down:g} s")
        return None

    # --- acting levels: throttle / deprioritize / quarantine (or recovering through them) ---
    at_floor = s.factor <= p.throttle_floor + EPS
    deprioritized = s.weight_factor <= p.deprioritize_weight + EPS
    if not at_floor and _held(s.throttle_since, h["throttle"], now):
        f = max(p.throttle_floor, s.factor * p.throttle_factor)
        lvl = p.level_for(f, s.weight_factor)
        kind = "step" if lvl == s.level else "escalate"
        return _apply(s, now, kind, lvl, f, s.weight_factor,
                      f"still hurting neighbours at score >= {p.throttle:g} for {h['throttle']:g} s: "
                      f"factor x{p.throttle_factor:g}")
    if at_floor and not deprioritized and _held(s.throttle_since, h["deprioritize"], now):
        kind = "step" if s.level == "deprioritize" else "escalate"
        return _apply(s, now, kind, "deprioritize", s.factor, p.deprioritize_weight,
                      f"throttled to the floor ({p.throttle_floor:g}) and still score >= {p.throttle:g} "
                      f"for {h['deprioritize']:g} s: scheduler weight x{p.deprioritize_weight:g}")
    if at_floor and deprioritized and s.level != "quarantine" and _held(s.quarantine_since, h["quarantine"], now):
        return _apply(s, now, "escalate", "quarantine", p.quarantine_factor, p.quarantine_weight,
                      f"deprioritized and still score >= quarantine {p.quarantine:g} for {h['quarantine']:g} s")

    if _held(s.calm_since, h["recover"], now) and (
            s.last_recover_at is None or now - s.last_recover_at >= p.recover_tick_s - EPS):
        f = min(1.0, round(s.factor + p.recover_step, 6))
        wf = min(1.0, round(s.weight_factor + p.recover_step, 6))
        lvl = p.level_for(f, wf)
        kind = "clear" if lvl == "warn" else "recover"
        t = _apply(s, now, kind, lvl, f, wf,
                   f"calm (score < {p.recover_below:g} or neighbours unhurt) for >= {h['recover']:g} s: "
                   f"+{p.recover_step:g} per {p.recover_tick_s:g} s")
        s.last_recover_at = now
        return t
    return None


def reset(s: LadderState, now: float, rule: str) -> Optional[Transition]:
    """Force the tenant back to normal (e.g. mode left tenantguard). None if already normal."""
    if s.level == "normal" and not s.acting:
        return None
    t = _apply(s, now, "reset", "normal", 1.0, 1.0, rule)
    s.warn_since = s.throttle_since = s.quarantine_since = s.calm_since = s.last_recover_at = None
    return t
