"""Token bucket (requests) and cost bucket (CPU-ms). Pure: every call takes `now` (perf_counter seconds)."""
from __future__ import annotations

import math


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


class CostBucket:
    """CPU-ms budget: refills `rate` ms/s up to `capacity`, debited after the response with measured CPU.

    A request is admitted while the level is >= 0 and refused while it is negative, so one expensive
    request can overdraw the bucket and the debt is paid back at `rate` before the next one is admitted.
    """

    def __init__(self, rate: float, capacity: float, now: float):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.level_ms = float(capacity)
        self.last = now

    def _refill(self, now: float) -> None:
        if now > self.last:
            self.level_ms = min(self.capacity, self.level_ms + (now - self.last) * self.rate)
            self.last = now

    def configure(self, rate: float, capacity: float, now: float) -> None:
        self._refill(now)
        self.rate, self.capacity = float(rate), float(capacity)
        self.level_ms = min(self.level_ms, self.capacity)

    def level(self, now: float) -> float:
        self._refill(now)
        return self.level_ms

    def available(self, now: float) -> bool:
        return self.level(now) >= 0.0

    def retry_after(self, now: float) -> float:
        lvl = self.level(now)
        return 0.0 if lvl >= 0 else -lvl / self.rate

    def debit(self, ms: float, now: float) -> None:
        self._refill(now)
        if ms > 0 and math.isfinite(ms):
            self.level_ms -= ms
