"""Tenant identity (sha256 of X-API-Key, from the brain's snapshot), request classes and per-tenant admission state."""
from __future__ import annotations

import hashlib
import math
from typing import Dict, Iterable, List, Mapping, Optional

from tenantguard_common.contracts import Quota, SnapTenant

from .buckets import TokenBucket


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def body_bucket(n: int) -> str:
    if n == 0:
        return "0"
    for limit, label in ((1024, "1k"), (16 * 1024, "16k"), (256 * 1024, "256k")):
        if n <= limit:
            return label
    return "big"


def size_bucket(query: Mapping[str, str], size_params: Iterable[str]) -> str:
    """log2 bucket of every numeric size parameter, e.g. 'n=2^16': a big search is a different class."""
    out = []
    for k in sorted(size_params):
        try:
            x = float(query[k]) if k in query else None
        except ValueError:
            x = None
        if x is not None and math.isfinite(x) and x >= 1:
            out.append(f"{k}=2^{int(math.log2(x))}")
    return ",".join(out)


def request_class(method: str, path: str, query_keys: Iterable[str], body_len: int, size: str = "") -> str:
    """sha1(method + path + sorted query keys + body-size bucket + size bucket)[:8]."""
    raw = "|".join([method.upper(), path, ",".join(sorted(set(query_keys))), body_bucket(body_len), size])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def scale_quota(q: Quota, factor: float) -> Quota:
    """LimitsCmd semantics: ints = max(1, round(base * factor)); floats = base * factor."""
    if factor == 1.0:
        return q
    return Quota(rate_rps=q.rate_rps * factor, burst=max(1, round(q.burst * factor)),
                 concurrency=max(1, round(q.concurrency * factor)))


class TenantState:
    def __init__(self, cfg: SnapTenant, now: float):
        self.cfg = cfg
        self.tokens = TokenBucket(cfg.quota.rate_rps, cfg.quota.burst, now)
        self.pending = 0      # admitted and not finished (queued + upstream): the concurrency count
        self.inflight = 0     # forwarded upstream right now

    @property
    def id(self) -> str:
        return self.cfg.id

    @property
    def project(self) -> str:
        return self.cfg.project

    def configure(self, eff: Quota, now: float) -> None:
        if self.tokens.rate != eff.rate_rps or self.tokens.burst != eff.burst:
            self.tokens.configure(eff.rate_rps, eff.burst, now)


class Registry:
    """Tenants from the latest snapshot. State (buckets, counters) survives a snapshot for tenants that stay."""

    def __init__(self) -> None:
        self.by_id: Dict[str, TenantState] = {}
        self.by_hash: Dict[str, TenantState] = {}

    def sync(self, tenants: List[SnapTenant], now: float) -> List[str]:
        """Adopt the snapshot's tenants; returns the ids that were removed."""
        keep: Dict[str, TenantState] = {}
        for t in tenants:
            st = self.by_id.get(t.id)
            if st is None:
                st = TenantState(t, now)
            else:
                st.cfg = t
            keep[t.id] = st
        gone = [t for t in self.by_id if t not in keep]
        self.by_id = keep
        self.by_hash = {st.cfg.key_sha256: st for st in keep.values()}
        return gone

    def lookup(self, api_key: Optional[str]) -> Optional[TenantState]:
        return self.by_hash.get(hash_key(api_key)) if api_key else None

    def get(self, tenant: str) -> Optional[TenantState]:
        return self.by_id.get(tenant)

    def ids(self) -> List[str]:
        return list(self.by_id)
