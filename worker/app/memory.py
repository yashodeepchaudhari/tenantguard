"""Per-tenant in-worker memory budget: held allocations + the tenant's cache bytes."""
from __future__ import annotations

import mmap
import threading
import time
from collections import defaultdict
from typing import Callable, Dict

from .tenancy import current_tenant

MB = 1024 * 1024
PAGE = 4096


class OverBudget(Exception):
    def __init__(self, kind: str, who: str, used_mb: float, asked_mb: float, budget_mb: float) -> None:
        super().__init__(f"{who} uses {used_mb:.1f} MB, asked {asked_mb:.1f} MB, budget {budget_mb:.0f} MB")
        self.kind = kind  # tenant_memory_budget | worker_memory


class MemoryBudget:
    def __init__(self, budget_mb: float, cache_bytes: Callable[[str], int],
                 cache_total: Callable[[], int] = lambda: 0,
                 worker_budget_mb: Callable[[], float] = lambda: float("inf")) -> None:
        self.budget_mb = budget_mb
        self._cache_bytes = cache_bytes
        self._cache_total = cache_total
        self._worker_budget_mb = worker_budget_mb  # mem limit - baseline: all tenants together
        self._held: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def used_mb(self, tenant: str) -> float:
        with self._lock:
            held = self._held.get(tenant, 0)
        return (held + self._cache_bytes(tenant)) / MB

    def hold(self, mb: float, hold_s: float) -> dict:
        """Allocate `mb` (pages touched so RSS really grows), hold it, release. Raises OverBudget."""
        tenant = current_tenant()
        n = int(mb * MB)
        with self._lock:
            used = self._held.get(tenant, 0) + self._cache_bytes(tenant)
            if used + n > self.budget_mb * MB:
                raise OverBudget("tenant_memory_budget", f"tenant {tenant}", used / MB, mb, self.budget_mb)
            total = sum(self._held.values()) + self._cache_total()
            worker_budget = self._worker_budget_mb()
            if total + n > worker_budget * MB:
                raise OverBudget("worker_memory", "worker (all tenants)", total / MB, mb, worker_budget)
            self._held[tenant] += n
        buf = None
        try:
            # An anonymous mmap goes back to the OS on close, whatever the allocator does with heap memory.
            buf = mmap.mmap(-1, n) if n else None
            for off in range(0, n, PAGE):           # touch every page so RSS really grows
                buf[off] = 1
            time.sleep(hold_s)
            checksum = sum(buf[off] for off in range(0, n, PAGE * 64)) if buf is not None else 0
        finally:
            if buf is not None:
                buf.close()
            with self._lock:
                self._held[tenant] -= n
        return {"mb": mb, "held_ms": hold_s * 1000, "checksum": checksum}
