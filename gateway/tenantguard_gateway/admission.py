"""Multi-dimensional admission: block, token bucket, cost bucket, concurrency. Pure."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tenantguard_common.contracts import Quota

from .buckets import CostBucket, TokenBucket


@dataclass
class Decision:
    ok: bool
    reasons: List[str] = field(default_factory=list)   # every dimension that refused
    retry_after_s: float = 0.0                          # time until ALL refusing dimensions pass

    @property
    def reason(self) -> str:
        return self.reasons[0] if self.reasons else ""

    @property
    def retry_after_header(self) -> str:
        """HTTP delay-seconds: integer, rounded up, at least 1 for a refusal."""
        return str(max(1, math.ceil(self.retry_after_s - 1e-9)))


# Concurrency frees up when one in-flight request finishes; we cannot know when, so we advertise this.
CONCURRENCY_RETRY_S = 1.0


def admit(tokens: TokenBucket, cost: CostBucket, pending: int, quota: Quota, now: float,
          block_remaining_s: Optional[float] = None) -> Decision:
    """Check every dimension; consume a token only if the request is admitted."""
    reasons: List[str] = []
    wait = 0.0
    if block_remaining_s is not None:
        reasons.append("blocked")
        wait = max(wait, block_remaining_s)
    if not tokens.available(now):
        reasons.append("rate")
        wait = max(wait, tokens.retry_after(now))
    if not cost.available(now):
        reasons.append("cost")
        wait = max(wait, cost.retry_after(now))
    if pending >= quota.concurrency:
        reasons.append("concurrency")
        wait = max(wait, CONCURRENCY_RETRY_S)
    if reasons:
        return Decision(False, reasons, wait)
    tokens.take(now)
    return Decision(True)


def ratelimit_headers(tokens: TokenBucket, cost: CostBucket, pending: int, quota: Quota,
                      cost_bucket_s: float, now: float) -> Dict[str, str]:
    """IETF draft-ietf-httpapi-ratelimit-headers (structured-field form), one named policy per dimension."""
    window = quota.burst / quota.rate_rps if quota.rate_rps > 0 else 1.0
    policy = (f'"rate";q={quota.burst};w={max(1, math.ceil(window))}, '
              f'"cost";q={int(quota.cost_ms_per_s * cost_bucket_s)};w={max(1, math.ceil(cost_bucket_s))};qu="cpu-ms", '
              f'"concurrency";q={quota.concurrency}')
    tok = tokens.level(now)
    lvl = cost.level(now)
    rate_r = max(0, math.floor(tok))
    rate_t = 0 if rate_r > 0 else math.ceil(tokens.retry_after(now))
    cost_r = max(0, math.floor(lvl))
    cost_t = 0 if lvl >= 0 else math.ceil(cost.retry_after(now))
    conc_r = max(0, quota.concurrency - pending)
    state = (f'"rate";r={rate_r};t={rate_t}, "cost";r={cost_r};t={cost_t}, '
             f'"concurrency";r={conc_r}')
    return {"RateLimit-Policy": policy, "RateLimit": state}
