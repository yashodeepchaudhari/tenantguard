"""Business impact since the brain started (pure, no I/O) -> contracts.ImpactView.

Each tick in tenantguard mode, the brain passes the *threatened* tenants: tenants with a server in an open
server incident and neighbours of tenants the ladder is warning or acting on; never a named culprit, a
tenant the ladder acts on, or one using more than its fair share (over-users are not victims).
For each one, that tick counts as
- **suffered** if its end-to-end p95 is above its plan's `sla_p95_ms` (layout), or
- **avoided** if it stayed within SLO although it was threatened; revenue protected grows by
  monthly_revenue_rs x avoided seconds / 2_592_000 (seconds in 30 days).
`customers_shielded` = threatened tenants with more avoided than suffered seconds.
`innocents_penalised` = tenants the brain tightened while their own noisy score was below `warn` (a
proven verdict is not an innocent). It must stay empty.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, Mapping, Optional, Set

from tenantguard_common.contracts import ImpactView

MONTH_S = 2_592_000.0


class ImpactMeter:
    def __init__(self, since: float, sla_ms: Mapping[str, float], revenue_rs: Mapping[str, float]):
        self.since = since
        self.sla = dict(sla_ms)
        self.revenue = dict(revenue_rs)
        self.avoided: Dict[str, float] = defaultdict(float)
        self.suffered: Dict[str, float] = defaultdict(float)
        self.revenue_protected = 0.0
        self.innocents: Set[str] = set()

    def tick(self, dt: float, threatened: Iterable[str], p95: Mapping[str, Optional[float]]) -> None:
        for t in threatened:
            v = p95.get(t)
            if v is None:
                continue                  # no completions this window: no evidence either way
            if v > self.sla.get(t, 100.0):
                self.suffered[t] += dt
            else:
                self.avoided[t] += dt
                self.revenue_protected += self.revenue.get(t, 0.0) * dt / MONTH_S

    def note_action(self, tenant: str, score: float, warn: float) -> None:
        if score < warn:
            self.innocents.add(tenant)

    def view(self) -> ImpactView:
        shielded = sorted(t for t, a in self.avoided.items() if a > self.suffered.get(t, 0.0))
        return ImpactView(since=self.since, revenue_protected_rs=round(self.revenue_protected, 2),
                          customers_shielded=shielded,
                          slo_violation_avoided_s=round(sum(self.avoided.values()), 2),
                          slo_violation_suffered_s=round(sum(self.suffered.values()), 2),
                          innocents_penalised=sorted(self.innocents))
