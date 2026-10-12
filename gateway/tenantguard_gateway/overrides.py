"""Brain control overrides with TTL auto-revert, plus heartbeat tracking for fail-static.

All times are epoch seconds (time.time()), per the contracts units header. `now` is injectable for tests.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from tenantguard_common.contracts import (BlockCmd, DrainCmd, LimitsCmd, OverridesView, RelocateCmd,
                                     RouteWeightsCmd)

from .router import normalize


class Overrides:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.limits: Dict[str, LimitsCmd] = {}
        self.blocks: Dict[str, BlockCmd] = {}
        self.route_weights: Dict[str, Dict[str, float]] = {}
        self.relocations: Dict[str, RelocateCmd] = {}      # "<tenant>" or "<tenant>/<cls>"
        self.drains: Dict[str, DrainCmd] = {}
        self.expires: Dict[str, float] = {}                 # "<kind>:<key>" -> epoch s
        self.last_heartbeat: Optional[float] = None         # OUR receive time
        self.reverted: List[str] = []                       # history of auto-reverted keys (bounded)

    # ----- setters (validation is done by the caller) -----
    def _arm(self, key: str, ttl_s: float) -> None:
        self.expires[key] = self.clock() + ttl_s

    def set_limits(self, cmd: LimitsCmd) -> None:
        key = f"limits:{cmd.tenant}"
        if cmd.factor == 1.0 and cmd.weight_factor == 1.0:
            self.limits.pop(cmd.tenant, None)
            self.expires.pop(key, None)
            return
        self.limits[cmd.tenant] = cmd
        self._arm(key, cmd.ttl_s)

    def set_block(self, cmd: BlockCmd) -> None:
        key = f"block:{cmd.tenant}"
        if not cmd.blocked:
            self.blocks.pop(cmd.tenant, None)
            self.expires.pop(key, None)
            return
        self.blocks[cmd.tenant] = cmd
        self._arm(key, cmd.ttl_s)

    def set_route_weights(self, cmd: RouteWeightsCmd) -> None:
        for tenant, w in cmd.weights.items():
            self.route_weights[tenant] = normalize(w)
            self._arm(f"route_weights:{tenant}", cmd.ttl_s)

    def set_relocate(self, cmd: RelocateCmd) -> None:
        rk = cmd.tenant if cmd.cls is None else f"{cmd.tenant}/{cmd.cls}"
        key = f"relocate:{rk}"
        if cmd.target is None:
            self.relocations.pop(rk, None)
            self.expires.pop(key, None)
            return
        self.relocations[rk] = cmd
        self._arm(key, cmd.ttl_s)

    def set_drain(self, cmd: DrainCmd) -> None:
        key = f"drain:{cmd.server}"
        if not cmd.drained:
            self.drains.pop(cmd.server, None)
            self.expires.pop(key, None)
            return
        self.drains[cmd.server] = cmd
        self._arm(key, cmd.ttl_s)

    def heartbeat(self) -> None:
        self.last_heartbeat = self.clock()

    # ----- expiry -----
    def _live(self, key: str, now: float) -> bool:
        exp = self.expires.get(key)
        return exp is not None and exp > now

    def sweep(self, now: Optional[float] = None) -> List[str]:
        """Drop every expired override; returns the reverted keys."""
        now = self.clock() if now is None else now
        gone = [k for k, exp in self.expires.items() if exp <= now]
        for key in gone:
            kind, _, k = key.partition(":")
            self.expires.pop(key, None)
            {"limits": self.limits, "block": self.blocks, "route_weights": self.route_weights,
             "relocate": self.relocations, "drain": self.drains}[kind].pop(k, None)
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

    def weights_for(self, tenant: str, now: Optional[float] = None) -> Optional[Dict[str, float]]:
        now = self.clock() if now is None else now
        return self.route_weights.get(tenant) if self._live(f"route_weights:{tenant}", now) else None

    def relocation_for(self, tenant: str, cls: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
        now = self.clock() if now is None else now
        if cls is not None:
            k = f"{tenant}/{cls}"
            if k in self.relocations and self._live(f"relocate:{k}", now):
                return self.relocations[k].target
        if tenant in self.relocations and self._live(f"relocate:{tenant}", now):
            return self.relocations[tenant].target
        return None

    def drained(self, now: Optional[float] = None) -> set:
        now = self.clock() if now is None else now
        return {s for s in self.drains if self._live(f"drain:{s}", now)}

    def tenant_expiry(self, tenant: str) -> Optional[float]:
        """Latest expiry among this tenant's overrides: when it is fully back to normal."""
        keys = [k for k in self.expires
                if k.split(":", 1)[1] == tenant or k.split(":", 1)[1].startswith(tenant + "/")]
        keys = [k for k in keys if not k.startswith("drain:")]
        return max((self.expires[k] for k in keys), default=None)

    # ----- view -----
    def heartbeat_age(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_heartbeat is None:
            return None
        now = self.clock() if now is None else now
        return max(0.0, now - self.last_heartbeat)

    def fail_static(self, after_s: float, now: Optional[float] = None) -> bool:
        """True when the control plane is silent: no heartbeat yet, or none for `after_s`.

        While fail-static the gateway keeps serving with its last known-good config; overrides still
        expire at their TTL (PLAN §2.9), so a dead brain can never leave a tenant throttled forever.
        """
        age = self.heartbeat_age(now)
        return age is None or age > after_s

    def view(self, fail_static_after_s: float) -> OverridesView:
        now = self.clock()
        self.sweep(now)
        return OverridesView(
            limits=dict(self.limits),
            blocks=dict(self.blocks),
            relocations=list(self.relocations.values()),
            route_weights={t: dict(w) for t, w in self.route_weights.items()},
            drains=dict(self.drains),
            expires_at=dict(self.expires),
            last_heartbeat_age_s=self.heartbeat_age(now),
            fail_static=self.fail_static(fail_static_after_s, now),
        )
