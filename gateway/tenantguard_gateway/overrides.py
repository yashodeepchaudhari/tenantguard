"""Per-tenant overrides with TTL auto-revert: limits and split-test pins (from the brain's snapshot) and operator
blocks (/control/block), plus the snapshot clock for fail-static.

All times are epoch seconds (time.time()), per the contracts units header. `now` is injectable for tests.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from tenantguard_common.contracts import BlockCmd, LimitsCmd, OverridesView, Snapshot


class Overrides:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.limits: Dict[str, LimitsCmd] = {}
        self.blocks: Dict[str, BlockCmd] = {}
        self.pins: Dict[str, str] = {}
        self.expires: Dict[str, float] = {}                 # "<kind>:<tenant>" -> epoch s
        self.last_snapshot: Optional[float] = None          # OUR receive time
        self.snapshot_version: Optional[int] = None
        self.reverted: List[str] = []                       # history of auto-reverted keys (bounded)

    # ----- setters (validation is done by the caller) -----
    def apply_snapshot(self, snap: Snapshot) -> None:
        """The snapshot's limits and pins replace the previous ones (absent = cleared); each restarts its TTL."""
        now = self.clock()
        for kind in ("limits", "pin"):
            for key in [k for k in self.expires if k.startswith(kind + ":")]:
                self.expires.pop(key)
        self.limits = {c.tenant: c for c in snap.limits if c.factor < 1.0 or c.weight_factor < 1.0}
        for t, c in self.limits.items():
            self.expires[f"limits:{t}"] = now + c.ttl_s
        self.pins = dict(snap.pins)
        for t in self.pins:
            self.expires[f"pin:{t}"] = now + snap.pin_ttl_s
        self.last_snapshot, self.snapshot_version = now, snap.version

    def set_block(self, cmd: BlockCmd) -> None:
        key = f"block:{cmd.tenant}"
        if not cmd.blocked:
            self.blocks.pop(cmd.tenant, None)
            self.expires.pop(key, None)
            return
        self.blocks[cmd.tenant] = cmd
        self.expires[key] = self.clock() + cmd.ttl_s

    # ----- expiry -----
    def _live(self, key: str, now: float) -> bool:
        exp = self.expires.get(key)
        return exp is not None and exp > now

    def sweep(self, now: Optional[float] = None) -> List[str]:
        """Drop every expired override; returns the reverted keys."""
        now = self.clock() if now is None else now
        gone = [k for k, exp in self.expires.items() if exp <= now]
        for key in gone:
            kind, _, t = key.partition(":")
            self.expires.pop(key, None)
            {"limits": self.limits, "block": self.blocks, "pin": self.pins}[kind].pop(t, None)
        if gone:
            self.reverted = (self.reverted + gone)[-100:]
        return gone

    # ----- getters (expiry-checked, so a lagging sweep never applies a stale override) -----
    def limits_for(self, tenant: str, now: Optional[float] = None) -> Optional[LimitsCmd]:
        now = self.clock() if now is None else now
        return self.limits.get(tenant) if self._live(f"limits:{tenant}", now) else None

    def block_remaining(self, tenant: str, now: Optional[float] = None) -> Optional[float]:
        now = self.clock() if now is None else now
        key = f"block:{tenant}"
        return self.expires[key] - now if tenant in self.blocks and self._live(key, now) else None

    def pin_for(self, tenant: str, now: Optional[float] = None) -> Optional[str]:
        now = self.clock() if now is None else now
        return self.pins.get(tenant) if self._live(f"pin:{tenant}", now) else None

    def tenant_expiry(self, tenant: str) -> Optional[float]:
        """Latest expiry among this tenant's overrides: when it is fully back to normal."""
        return max((exp for k, exp in self.expires.items() if k.split(":", 1)[1] == tenant), default=None)

    # ----- fail-static -----
    def snapshot_age(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_snapshot is None:
            return None
        now = self.clock() if now is None else now
        return max(0.0, now - self.last_snapshot)

    def fail_static(self, after_s: float, now: Optional[float] = None) -> bool:
        """True when the brain is silent: no snapshot yet, or none for `after_s`. The gateway keeps serving with
        the last snapshot; overrides still expire at their TTL, so a dead brain never leaves a tenant throttled."""
        age = self.snapshot_age(now)
        return age is None or age > after_s

    def view(self, fail_static_after_s: float) -> OverridesView:
        now = self.clock()
        self.sweep(now)
        return OverridesView(limits=dict(self.limits), blocks=dict(self.blocks), pins=dict(self.pins),
                             expires_at=dict(self.expires), snapshot_version=self.snapshot_version,
                             last_snapshot_age_s=self.snapshot_age(now),
                             fail_static=self.fail_static(fail_static_after_s, now))
