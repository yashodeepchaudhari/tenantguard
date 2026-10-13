"""Token bucket (requests). Pure: every call takes `now` (perf_counter seconds).

There is no per-tenant CPU bucket in v2: a project's CPU is bounded by its replicas' cgroups, and inside a
project DRR charges every request its class cost, so an expensive tenant only spends its own turns.
"""
from __future__ import annotations


class TokenBucket:
    """Refills `rate` tokens/s up to `burst`. One request takes one token."""

    def __init__(self, rate: float, burst: float, now: float):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.last = now

    def _refill(self, now: float) -> None:
        if now > self.last:
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now

    def configure(self, rate: float, burst: float, now: float) -> None:
        """Change rate/burst, keeping the current level (clamped to the new burst)."""
        self._refill(now)
        self.rate, self.burst = float(rate), float(burst)
        self.tokens = min(self.tokens, self.burst)

    def level(self, now: float) -> float:
        self._refill(now)
        return self.tokens

    def available(self, now: float) -> bool:
        return self.level(now) >= 1.0

    def retry_after(self, now: float) -> float:
        """Seconds until one token is available (0 if available now)."""
        missing = 1.0 - self.level(now)
        return 0.0 if missing <= 0 else missing / self.rate

    def take(self, now: float, n: float = 1.0) -> None:
        self._refill(now)
        self.tokens -= n
