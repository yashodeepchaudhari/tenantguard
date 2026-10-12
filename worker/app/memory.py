"""Per-tenant in-worker memory budget: held allocations + the tenant's cache bytes."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Callable, Dict

from .tenancy import current_tenant

MB = 1024 * 1024
PAGE = 4096


class OverBudget(Exception):
    def __init__(self, tenant: str, used_mb: float, asked_mb: float, budget_mb: float) -> None:
        super().__init__(f"tenant {tenant} uses {used_mb:.1f} MB, asked {asked_mb:.1f} MB, budget {budget_mb:.0f} MB")
        self.tenant = tenant


class MemoryBudget:
    def __init__(self, budget_mb: float, cache_bytes: Callable[[str], int]) -> None:
        self.budget_mb = budget_mb
        self._cache_bytes = cache_bytes
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
                raise OverBudget(tenant, used / MB, mb, self.budget_mb)
            self._held[tenant] += n
        try:
            buf = bytearray(n)
            buf[::PAGE] = b"\x01" * len(range(0, n, PAGE))
            time.sleep(hold_s)
            checksum = sum(buf[::PAGE * 64])
            del buf
        finally:
            with self._lock:
                self._held[tenant] -= n
        return {"mb": mb, "held_ms": hold_s * 1000, "checksum": checksum}
