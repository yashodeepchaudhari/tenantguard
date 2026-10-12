"""Rescue: route victims around slow servers (pure, no I/O).

For every tenant with at least one slow and one healthy server in its barcode, move its traffic share
off the slow servers onto its healthy ones. The shift is capped so that no healthy server's projected
CPU utilisation goes above `rescue_capacity_guard` (default 0.7 of its limit): a rescue must not create
the next slow server. Tenants whose servers are *all* slow are "stuck" (nothing to route to) and are
left alone; being stuck is itself decoder evidence.

Load model: a tenant's CPU on a server moves with its traffic share, so moving a fraction f of its
slow-server share moves f of its CPU there onto its healthy servers, in proportion to their headroom.
Tenants are processed lightest first, so the guard's headroom rescues as many tenants as possible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

MIN_WEIGHT = 0.0


@dataclass
class RescuePlan:
    weights: Dict[str, Dict[str, float]] = field(default_factory=dict)   # tenant -> server -> weight (sum 1)
    moved: Dict[str, float] = field(default_factory=dict)                # tenant -> fraction of its slow share moved
    stuck: List[str] = field(default_factory=list)                        # all servers slow
    capped: List[str] = field(default_factory=list)                       # moved < 1 because of the guard
    projected_util: Dict[str, float] = field(default_factory=dict)        # server -> utilisation after the plan


def plan_rescue(*, barcodes: Mapping[str, Sequence[str]], slow: Sequence[str],
                load: Mapping[str, Mapping[str, float]], capacity: Mapping[str, float],
                server_load: Optional[Mapping[str, float]] = None, guard: float = 0.7,
                current: Optional[Mapping[str, Mapping[str, float]]] = None,
                targets: Optional[Iterable[str]] = None) -> RescuePlan:
    """
    barcodes:    tenant -> its servers
    slow:        servers judged slow
    load:        server -> tenant -> CPU ms/s now
    capacity:    server -> CPU ms/s limit
    server_load: server -> total CPU ms/s now (defaults to the sum of `load`)
    current:     tenant -> server -> weight in force (defaults to equal over the barcode)
    targets:     servers known to be up and healthy (default: every server not slow). A rescue never
                 routes to a server that is down or unknown, and only names servers the gateway knows.
    """
    slow_set = set(slow)
    allowed = set(targets) if targets is not None else None
    plan = RescuePlan()
    proj = {s: float(server_load[s]) if server_load and s in server_load else sum(load.get(s, {}).values())
            for s in capacity}
    affected = []
    for t, servers in barcodes.items():
        bad = [s for s in servers if s in slow_set]
        if not bad:
            continue
        good = [s for s in servers if s not in slow_set and (allowed is None or s in allowed)]
        if not good:
            if all(s in slow_set for s in servers):
                plan.stuck.append(t)
            continue                            # nowhere healthy (and up) to go
        on_bad = sum(load.get(s, {}).get(t, 0.0) for s in bad)
        affected.append((on_bad, t, bad, good))

    for on_bad, t, bad, good in sorted(affected):
        headroom = {s: max(0.0, guard * capacity.get(s, 0.0) - proj.get(s, 0.0)) for s in good}
        room = sum(headroom.values())
        f = 1.0 if on_bad <= 0 else min(1.0, room / on_bad)
        if f < 1.0:
            plan.capped.append(t)
        if f <= 0.0:
            plan.moved[t] = 0.0
            continue                            # no safe room at all: leave it where it is
        if on_bad > 0 and room > 0:
            for s in good:
                proj[s] = proj.get(s, 0.0) + on_bad * f * headroom[s] / room
            for s in bad:
                proj[s] = proj.get(s, 0.0) - load.get(s, {}).get(t, 0.0) * f
        w0 = dict(current.get(t, {})) if current and t in current else {}
        servers = bad + good                    # only servers that are up: the gateway rejects unknown ones
        if not w0:
            w0 = {s: 1.0 / len(servers) for s in servers}
        tot = sum(w0.get(s, 0.0) for s in servers) or 1.0
        w0 = {s: w0.get(s, 0.0) / tot for s in servers}
        bad_share = sum(w0[s] for s in bad)
        moved = bad_share * f
        new = {s: w0[s] * (1.0 - f) for s in bad}
        if room > 0:
            for s in good:
                new[s] = w0[s] + moved * headroom[s] / room
        else:                                   # no measured load anywhere: split evenly
            for s in good:
                new[s] = w0[s] + moved / len(good)
        tot = sum(new.values())
        plan.weights[t] = {s: round(v / tot, 4) for s, v in new.items()} if tot > 0 else w0
        plan.moved[t] = round(f, 4)
    plan.projected_util = {s: round(proj[s] / capacity[s], 4) for s in capacity if capacity.get(s)}
    return plan
