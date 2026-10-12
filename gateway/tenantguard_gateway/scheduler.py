"""Per-tenant queues served by weighted deficit round robin (DRR), or one global FIFO in `off` mode.

Pure data structure: no clocks, no I/O. Costs are CPU-ms estimates (the class's average cost);
each turn a tenant earns `quantum_ms * weight` of deficit and is served while its head fits.
Expired / cancelled items are removed lazily.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional


@dataclass(eq=False)
class Item:
    tenant: str
    cost: float
    cls: str = ""
    payload: Any = None
    seq: int = 0
    state: str = "queued"            # queued | dispatched | expired
    server: Optional[str] = None     # chosen by the eligibility check at dispatch
    meta: Dict[str, Any] = field(default_factory=dict)


Eligible = Callable[[Item], bool]


def _always(_: Item) -> bool:
    return True


class Scheduler:
    def __init__(self, weight_fn: Callable[[str], float], quantum_ms: float = 5.0, fifo: bool = False):
        self.weight_fn = weight_fn
        self.quantum_ms = quantum_ms
        self.fifo = fifo
        self.queues: Dict[str, Deque[Item]] = {}
        self.counts: Dict[str, int] = {}
        self.deficit: Dict[str, float] = {}
        self.active: List[str] = []      # round order of tenants with queued work
        self.cursor = 0
        self.granted = False             # has active[cursor] received its quantum this turn?
        self._seq = 0

    # ----- queue ops -----
    def push(self, item: Item) -> None:
        self._seq += 1
        item.seq = self._seq
        item.state = "queued"
        t = item.tenant
        q = self.queues.setdefault(t, deque())
        q.append(item)
        self.counts[t] = self.counts.get(t, 0) + 1
        if t not in self.active:
            self.active.append(t)
            self.deficit[t] = 0.0

    def cancel(self, item: Item) -> bool:
        """Mark a queued item expired. Returns False if it was already dispatched."""
        if item.state != "queued":
            return False
        item.state = "expired"
        self.counts[item.tenant] -= 1
        return True

    def depth(self, tenant: str) -> int:
        return self.counts.get(tenant, 0)

    def total(self) -> int:
        return sum(self.counts.values())

    def _head(self, t: str) -> Optional[Item]:
        q = self.queues.get(t)
        while q and q[0].state != "queued":
            q.popleft()
        return q[0] if q else None

    def _weight(self, t: str) -> float:
        return max(1e-6, float(self.weight_fn(t)))

    def _deactivate(self, i: int) -> None:
        t = self.active.pop(i)
        self.deficit[t] = 0.0
        if i < self.cursor:
            self.cursor -= 1
        elif i == self.cursor:
            self.granted = False
        if self.cursor >= len(self.active):
            self.cursor = 0

    def _advance(self) -> None:
        self.granted = False
        self.cursor = (self.cursor + 1) % len(self.active) if self.active else 0

    def _take(self, t: str) -> Item:
        item = self.queues[t].popleft()
        item.state = "dispatched"
        self.counts[t] -= 1
        return item

    # ----- dispatch -----
    def pop(self, eligible: Eligible = _always) -> Optional[Item]:
        """Next item to dispatch, or None if nothing queued is eligible (e.g. its servers are full)."""
        heads: Dict[str, Item] = {}
        i = 0
        while i < len(self.active):
            h = self._head(self.active[i])
            if h is None:
                self._deactivate(i)
                continue
            heads[self.active[i]] = h
            i += 1
        ok = {t for t, h in heads.items() if eligible(h)}
        if not ok:
            return None
        if self.fifo:
            t = min(ok, key=lambda x: heads[x].seq)
            item = self._take(t)
            if self._head(t) is None:
                self._deactivate(self.active.index(t))
            return item
        return self._pop_drr(heads, ok)

    def _pop_drr(self, heads: Dict[str, Item], ok: set) -> Item:
        stalled = 0
        while True:
            t = self.active[self.cursor]
            if t not in ok:
                self._advance()
                continue
            if not self.granted:
                self.deficit[t] += self.quantum_ms * self._weight(t)
                self.granted = True
            head = heads[t]
            if head.cost <= self.deficit[t]:
                self.deficit[t] -= head.cost
                item = self._take(t)
                if self._head(t) is None:
                    self._deactivate(self.cursor)
                return item
            self._advance()
            stalled += 1
            if stalled >= len(ok):
                # Every eligible tenant had a turn and none could afford its head: skip the empty rounds.
                rounds = min(math.ceil((heads[x].cost - self.deficit[x]) / (self.quantum_ms * self._weight(x)))
                             for x in ok)
                if rounds > 1:
                    for x in ok:
                        self.deficit[x] += (rounds - 1) * self.quantum_ms * self._weight(x)
                stalled = 0
