"""Cumulative per-tenant and per-(tenant, class) meters (thread-safe)."""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple

from tenantguard_common.contracts import ClassCost, TenantUsage


@dataclass
class Usage:
    requests: int = 0
    handler_cpu_ms: float = 0.0
    bg_cpu_ms: float = 0.0
    db_ms: float = 0.0
    bytes_in: int = 0
    bg_jobs: int = 0


class Meters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage: Dict[str, Usage] = defaultdict(Usage)
        self._classes: Dict[Tuple[str, str], List[float]] = {}

    def record_request(self, tenant: str, cls: str, cpu_ms: float, bytes_in: int) -> None:
        with self._lock:
            u = self._usage[tenant]
            u.requests += 1
            u.handler_cpu_ms += cpu_ms
            u.bytes_in += bytes_in
            c = self._classes.setdefault((tenant, cls), [0, 0.0])
            c[0] += 1
            c[1] += cpu_ms

    def add_db(self, tenant: str, ms: float) -> None:
        with self._lock:
            self._usage[tenant].db_ms += ms

    def add_bg(self, tenant: str, cpu_ms: float) -> None:
        with self._lock:
            u = self._usage[tenant]
            u.bg_cpu_ms += cpu_ms
            u.bg_jobs += 1

    def tenants(self) -> List[str]:
        with self._lock:
            return list(self._usage)

    def snapshot(self) -> Tuple[Dict[str, Usage], List[ClassCost]]:
        with self._lock:
            usage = {t: Usage(**vars(u)) for t, u in self._usage.items()}
            classes = [ClassCost(tenant=t, cls=c, count=int(v[0]), cpu_ms_total=round(v[1], 3))
                       for (t, c), v in self._classes.items()]
        return usage, classes

    @staticmethod
    def to_contract(tenant: str, u: Usage, **extra) -> TenantUsage:
        return TenantUsage(tenant=tenant, requests=u.requests, handler_cpu_ms=round(u.handler_cpu_ms, 3),
                           bg_cpu_ms=round(u.bg_cpu_ms, 3), db_ms=round(u.db_ms, 3), bytes_in=u.bytes_in, **extra)
