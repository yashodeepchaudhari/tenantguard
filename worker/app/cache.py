"""Shared LRU cache, namespaced per tenant, with a guaranteed share and an eviction ledger.

Keys are stored as (tenant, key) with the tenant taken from context, so a tenant can
only ever see its own entries. When the cache is full, the victim is the least recently
used entry among tenants using more than their guaranteed share (capacity * share);
a tenant within its share is never evicted by someone else. Every eviction is recorded
as evictor -> victim (counts and bytes).
"""
from __future__ import annotations

import threading
from collections import OrderedDict, defaultdict
from typing import Dict, Optional, Tuple

from .tenancy import current_tenant


class TenantCache:
    def __init__(self, capacity_bytes: int, guaranteed_share: float) -> None:
        self.capacity = int(capacity_bytes)
        self.guaranteed = int(capacity_bytes * guaranteed_share)
        self._lock = threading.Lock()
        self._lru: Dict[str, "OrderedDict[str, Tuple[str, int, int]]"] = defaultdict(OrderedDict)  # key -> (value, size, tick)
        self._bytes: Dict[str, int] = defaultdict(int)
        self._total = 0
        self._tick = 0
        self.hits: Dict[str, int] = defaultdict(int)
        self.misses: Dict[str, int] = defaultdict(int)
        self.caused: Dict[str, int] = defaultdict(int)
        self.suffered: Dict[str, int] = defaultdict(int)
        self.ledger: Dict[Tuple[str, str], int] = defaultdict(int)  # (evictor, victim) -> bytes

    @staticmethod
    def _size(key: str, value: str) -> int:
        return len(key) + len(value) + 64

    def get(self, key: str) -> Optional[str]:
        tenant = current_tenant()
        with self._lock:
            entries = self._lru.get(tenant)
            item = entries.get(key) if entries else None
            if item is None:
                self.misses[tenant] += 1
                return None
            self._tick += 1
            entries[key] = (item[0], item[1], self._tick)
            entries.move_to_end(key)
            self.hits[tenant] += 1
            return item[0]

    def put(self, key: str, value: str) -> None:
        tenant = current_tenant()
        size = self._size(key, value)
        if size > self.capacity:
            return
        with self._lock:
            entries = self._lru[tenant]
            old = entries.pop(key, None)
            if old is not None:
                self._bytes[tenant] -= old[1]
                self._total -= old[1]
            self._tick += 1
            entries[key] = (value, size, self._tick)
            self._bytes[tenant] += size
            self._total += size
            while self._total > self.capacity:
                self._evict_one(tenant)

    def _evict_one(self, evictor: str) -> None:
        over = [t for t, b in self._bytes.items() if b > self.guaranteed and self._lru[t]]
        pool = over or [t for t in self._lru if self._lru[t]]
        victim = min(pool, key=lambda t: next(iter(self._lru[t].values()))[2])
        _, (_, size, _) = self._lru[victim].popitem(last=False)
        self._bytes[victim] -= size
        self._total -= size
        if victim != evictor:
            self.caused[evictor] += 1
            self.suffered[victim] += 1
            self.ledger[(evictor, victim)] += size

    def tenant_bytes(self, tenant: str) -> int:
        with self._lock:
            return self._bytes.get(tenant, 0)

    def counters(self, tenant: str) -> Dict[str, int]:
        with self._lock:
            return {"cache_hits": self.hits.get(tenant, 0), "cache_misses": self.misses.get(tenant, 0),
                    "evictions_caused": self.caused.get(tenant, 0), "evictions_suffered": self.suffered.get(tenant, 0)}
