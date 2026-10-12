"""Tenant identity (sha256 of X-API-Key), request classes and per-tenant admission state."""
from __future__ import annotations

import hashlib
import math
from typing import Dict, Iterable, List, Optional

from tenantguard_common.contracts import Quota, TenantConfig

from .buckets import CostBucket, TokenBucket


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def body_bucket(n: int) -> str:
    if n == 0:
        return "0"
    for limit, label in ((1024, "1k"), (16 * 1024, "16k"), (256 * 1024, "256k")):
        if n <= limit:
            return label
    return "big"


def request_class(method: str, path: str, query_keys: Iterable[str], body_len: int) -> str:
    """sha1(method + path + sorted query keys + body-size bucket)[:8]."""
    raw = "|".join([method.upper(), path, ",".join(sorted(set(query_keys))), body_bucket(body_len)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def scale_quota(q: Quota, factor: float) -> Quota:
    """LimitsCmd semantics: ints = max(1, round(base * factor)); floats = base * factor."""
    if factor == 1.0:
        return q
    return Quota(rate_rps=q.rate_rps * factor, burst=max(1, round(q.burst * factor)),
                 cost_ms_per_s=q.cost_ms_per_s * factor, concurrency=max(1, round(q.concurrency * factor)))


def quota_problems(q: Quota, maxima: Optional[Dict[str, float]] = None) -> List[str]:
    """Every field finite, > 0 and <= its maximum (gateway.quota_max)."""
    errs = []
    for name in ("rate_rps", "burst", "cost_ms_per_s", "concurrency"):
        v = getattr(q, name)
        hi = (maxima or {}).get(name)
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            errs.append(f"{name} must be a finite number > 0")
        elif hi is not None and v > hi:
            errs.append(f"{name} must be <= {hi:g}")
    return errs


class TenantState:
    def __init__(self, cfg: TenantConfig, cost_bucket_s: float, now: float):
        self.key_hash = hash_key(cfg.api_key)
        self.cfg = cfg.model_copy(update={"api_key": ""})   # never keep the plaintext key
        q = cfg.quota
        self.tokens = TokenBucket(q.rate_rps, q.burst, now)
        self.cost = CostBucket(q.cost_ms_per_s, q.cost_ms_per_s * cost_bucket_s, now)
        self.pending = 0      # admitted and not finished (queued + upstream): the concurrency count
        self.inflight = 0     # forwarded upstream right now

    @property
    def id(self) -> str:
        return self.cfg.id

    def configure(self, eff: Quota, cost_bucket_s: float, now: float) -> None:
        if self.tokens.rate != eff.rate_rps or self.tokens.burst != eff.burst:
            self.tokens.configure(eff.rate_rps, eff.burst, now)
        cap = eff.cost_ms_per_s * cost_bucket_s
        if self.cost.rate != eff.cost_ms_per_s or self.cost.capacity != cap:
            self.cost.configure(eff.cost_ms_per_s, cap, now)


class Registry:
    def __init__(self, cost_bucket_s: float):
        self.cost_bucket_s = cost_bucket_s
        self.by_id: Dict[str, TenantState] = {}
        self.by_hash: Dict[str, TenantState] = {}

    def add(self, cfg: TenantConfig, now: float) -> TenantState:
        if cfg.id in self.by_id:
            raise KeyError(f"tenant {cfg.id} already exists")
        st = TenantState(cfg, self.cost_bucket_s, now)
        if st.key_hash in self.by_hash:
            raise KeyError("api_key already in use")
        self.by_id[cfg.id] = st
        self.by_hash[st.key_hash] = st
        return st

    def remove(self, tenant: str) -> Optional[TenantState]:
        st = self.by_id.pop(tenant, None)
        if st:
            self.by_hash.pop(st.key_hash, None)
        return st

    def lookup(self, api_key: Optional[str]) -> Optional[TenantState]:
        return self.by_hash.get(hash_key(api_key)) if api_key else None

    def get(self, tenant: str) -> Optional[TenantState]:
        return self.by_id.get(tenant)

    def ids(self) -> List[str]:
        return list(self.by_id)
