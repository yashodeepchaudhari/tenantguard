"""Pick a replica of the tenant's project: split-test pin, else rendezvous (highest random weight) hashing. Pure.

Rendezvous: every (tenant, replica) pair gets a stable 64-bit hash; the tenant goes to the replica with the
highest one. Adding a replica moves only the tenants that now hash highest to it (about 1/(n+1) of them), a
removed replica moves only its own tenants, so caches stay warm while the project scales. If the first choice
has no free slot the request spills to the next replica in the same order.
"""
from __future__ import annotations

import hashlib
from typing import Callable, List, Optional, Sequence


def hash64(tenant: str, replica: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"{tenant}|{replica}".encode("utf-8"), digest_size=8).digest(), "big")


def rank(tenant: str, replicas: Sequence[str]) -> List[str]:
    """The tenant's replicas, most preferred first."""
    return sorted(replicas, key=lambda r: hash64(tenant, r), reverse=True)


def choose(tenant: str, ready: Sequence[str], has_capacity: Callable[[str], bool],
           pin: Optional[str] = None) -> Optional[str]:
    """Pinned replica if it is ready (if full, the request waits for it), else the first replica in rendezvous
    order with a free slot. None = nothing ready or everything full."""
    if pin is not None and pin in ready:
        return pin if has_capacity(pin) else None
    for r in rank(tenant, ready):
        if has_capacity(r):
            return r
    return None
