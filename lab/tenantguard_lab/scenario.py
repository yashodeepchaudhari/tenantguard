"""Scripted scenario run: set mode -> calm -> attack -> calm, then compute a RunResult.

The timeline driver is abstract (ScenarioDriver) so tests can run it on a fake clock; the result
math (compute_result) is pure over a Recorder."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from tenantguard_common.contracts import AttackReq, PhaseP99, RunReq, RunResult

from .stats import Recorder, bucketize, percentile, time_to_recover


@dataclass(frozen=True)
class Timeline:
    """Planned offsets (seconds from run start). Measurement windows use the actual marks."""
    warmup_s: float
    calm_s: float
    attack_s: float
    cold_start_s: float = 0.0   # extra warmup when calm traffic was not already running (pools, connections)

    @property
    def baseline_start(self) -> float:
        return self.warmup_s

    @property
    def attack_start(self) -> float:
        return self.warmup_s + self.calm_s

    @property
    def attack_end(self) -> float:
        return self.attack_start + self.attack_s

    @property
    def end(self) -> float:
        return self.attack_end + self.calm_s

    def phases(self) -> List[tuple]:
        return [("warmup", 0.0, self.baseline_start), ("baseline", self.baseline_start, self.attack_start),
                ("attack", self.attack_start, self.attack_end), ("after", self.attack_end, self.end)]


@dataclass
class Marks:
    """Actual epoch timestamps of each transition."""
    t0: float = 0.0
    baseline_start: float = 0.0
    attack_start: float = 0.0
    attack_end: float = 0.0
    end: float = 0.0


class ScenarioDriver:
    """What the runner needs from the lab. core.Lab implements it against the real engine."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, s: float) -> None:
        raise NotImplementedError

    async def set_mode(self, mode: str) -> None:
        raise NotImplementedError

    async def ensure_calm(self) -> bool:
        """Start calm traffic if needed; return whether it was already running."""
        raise NotImplementedError

    async def restore_calm(self, was_running: bool) -> None:
        raise NotImplementedError

    async def start_attack(self, req: AttackReq) -> str:
        raise NotImplementedError

    async def stop_attack(self, attack_id: str) -> None:
        raise NotImplementedError

    def on_phase(self, name: str) -> None:
        """Hook for status reporting."""


async def run_timeline(driver: ScenarioDriver, req: RunReq, attacker: Optional[str], tl: Timeline) -> Marks:
    """Drive one scenario. Always stops the attack and restores calm state, even on cancellation."""
    m = Marks()
    await driver.set_mode(req.mode)
    was_running = await driver.ensure_calm()
    attack_id: Optional[str] = None
    try:
        m.t0 = driver.now()
        driver.on_phase("warmup")
        await driver.sleep(tl.warmup_s + (0.0 if was_running else tl.cold_start_s))
        m.baseline_start = driver.now()
        driver.on_phase("baseline")
        await driver.sleep(tl.calm_s)
        driver.on_phase("attack")
        # Duration is a safety net; the runner stops it explicitly at the planned time.
        attack_id = await driver.start_attack(AttackReq(
            attack=req.scenario, tenant=attacker, server=req.server, intensity=req.intensity,
            duration_s=min(600.0, req.attack_s + 30.0)))
        # Marked once the attack traffic is really flowing (spawning its process takes seconds on Windows).
        m.attack_start = driver.now()
        await driver.sleep(tl.attack_s)
        await driver.stop_attack(attack_id)
        attack_id = None
        m.attack_end = driver.now()
        driver.on_phase("after")
        await driver.sleep(tl.calm_s)
        m.end = driver.now()
        driver.on_phase("done")
    finally:
        if attack_id is not None:
            await driver.stop_attack(attack_id)
        await driver.restore_calm(was_running)
    return m


def _p99(lat: List[float]) -> float:
    return round(percentile(lat, 99), 3)


def compute_result(rec: Recorder, tenants: Iterable[str], req: RunReq, attacker: Optional[str], m: Marks,
                   run_id: str, recover_ratio: float = 1.5, recover_hold_s: float = 5.0,
                   bucket_s: float = 1.0, time_to_detect_s: Optional[float] = None,
                   victim_kinds: Optional[Iterable[str]] = None) -> RunResult:
    """Victims = every tenant except the attacker; only their calm traffic of `victim_kinds` counts
    (None / empty = every calm request)."""
    victims = [t for t in sorted(tenants) if t != attacker]
    pooled: Dict[str, List[float]] = {"baseline": [], "attack": [], "after": []}
    by_tenant: Dict[str, PhaseP99] = {}
    windows = {"baseline": (m.baseline_start, m.attack_start), "attack": (m.attack_start, m.attack_end),
               "after": (m.attack_end, m.end)}
    recovery_samples = []
    for v in victims:
        per: Dict[str, List[float]] = {}
        for phase, (a, b) in windows.items():
            lat = [s.latency_ms for s in rec.window(v, a, b, src="calm", kinds=victim_kinds)]
            per[phase] = lat
            pooled[phase].extend(lat)
        by_tenant[v] = PhaseP99(baseline=_p99(per["baseline"]), attack=_p99(per["attack"]), after=_p99(per["after"]))
        recovery_samples.extend(rec.window(v, m.attack_start, m.end, src="calm", kinds=victim_kinds))

    baseline = _p99(pooled["baseline"])
    buckets = bucketize(recovery_samples, m.attack_start, m.end, bucket_s)
    ttr, _ = time_to_recover(buckets, baseline, recover_ratio, max(1, int(round(recover_hold_s / bucket_s))), bucket_s)

    a429 = a503 = 0
    if attacker:
        for s in rec.window(attacker, m.attack_start, m.end):
            a429 += s.status == 429
            a503 += s.status == 503

    return RunResult(
        run_id=run_id, scenario=req.scenario, mode=req.mode, attacker=attacker, started_at=m.t0,
        victim_p99_ms=PhaseP99(baseline=baseline, attack=_p99(pooled["attack"]), after=_p99(pooled["after"])),
        victim_p99_by_tenant=by_tenant,
        time_to_detect_s=time_to_detect_s,
        time_to_recover_s=None if ttr is None else round(ttr, 3),
        attacker_429=a429, attacker_503=a503,
    )
