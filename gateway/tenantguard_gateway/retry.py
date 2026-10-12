"""Retry detection and per-tenant retry budget. Pure: `now` is passed in (perf_counter seconds).

A request is a retry if
  - it repeats an Idempotency-Key the tenant sent within `window_s`, or
  - it repeats the fingerprint (method + path + full query + body hash) of a request that FAILED
    (429 / 503 / 5xx) within `window_s`.
Plain repeat traffic (the same GET /api/light every second) is not a retry: nothing failed.

Budget: retries in the last `window_s` <= max(min_allowed, ratio * first attempts in the last `window_s`).
"""
from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

MAX_KEYS = 4096            # per tenant memory bound for remembered keys / failures


def fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"{method.upper()} {path}?{query}\n".encode("utf-8"))
    h.update(body)
    return h.hexdigest()


@dataclass
class RetryVerdict:
    is_retry: bool
    allowed: bool = True
    retry_after_s: float = 0.0


class _Expiring:
    """key -> (expires_at, value), bounded, oldest evicted first."""

    def __init__(self) -> None:
        self.d: Dict[str, Tuple[float, float]] = {}

    def put(self, key: str, exp: float, value: float = 0.0) -> None:
        self.d.pop(key, None)
        self.d[key] = (exp, value)
        while len(self.d) > MAX_KEYS:
            self.d.pop(next(iter(self.d)))

    def get(self, key: str, now: float) -> Optional[Tuple[float, float]]:
        v = self.d.get(key)
        if v is None:
            return None
        if v[0] <= now:
            del self.d[key]
            return None
        return v


class TenantRetries:
    def __init__(self) -> None:
        self.firsts: Deque[float] = deque()
        self.retries: Deque[float] = deque()
        self.keys = _Expiring()           # idempotency keys seen
        self.failed = _Expiring()         # fingerprint -> (expiry, not_before)

    def _trim(self, now: float, window_s: float) -> None:
        for q in (self.firsts, self.retries):
            while q and q[0] <= now - window_s:
                q.popleft()


class RetryBudget:
    def __init__(self, ratio: float = 0.1, window_s: float = 10.0, min_allowed: int = 3):
        self.ratio, self.window_s, self.min_allowed = ratio, window_s, min_allowed
        self.tenants: Dict[str, TenantRetries] = {}

    def _t(self, tenant: str) -> TenantRetries:
        t = self.tenants.get(tenant)
        if t is None:
            t = self.tenants[tenant] = TenantRetries()
        return t

    def check(self, tenant: str, fp: str, idem_key: Optional[str], now: float) -> RetryVerdict:
        """Classify one arriving request and charge it to the budget."""
        t = self._t(tenant)
        t._trim(now, self.window_s)
        is_retry, not_before = False, 0.0
        if idem_key:
            if t.keys.get(idem_key, now) is not None:
                is_retry = True
            t.keys.put(idem_key, now + self.window_s)
        f = t.failed.get(fp, now)
        if f is not None:
            is_retry, not_before = True, max(not_before, f[1])
        if not is_retry:
            t.firsts.append(now)
            return RetryVerdict(False)
        budget = max(self.min_allowed, self.ratio * len(t.firsts))
        if len(t.retries) >= budget:
            # Over budget: wait for the original Retry-After, or until the oldest retry leaves the window.
            free_at = t.retries[0] + self.window_s if t.retries else now + 1.0
            wait = max(not_before - now, min(free_at - now, self.window_s))
            return RetryVerdict(True, False, max(0.0, wait))
        t.retries.append(now)
        return RetryVerdict(True, True)

    def failed(self, tenant: str, fp: str, now: float, retry_after_s: float = 0.0) -> None:
        """Remember a failed attempt so a repeat within window_s counts as a retry."""
        self._t(tenant).failed.put(fp, now + self.window_s, now + retry_after_s)

    def succeeded(self, tenant: str, fp: str) -> None:
        t = self.tenants.get(tenant)
        if t:
            t.failed.d.pop(fp, None)
